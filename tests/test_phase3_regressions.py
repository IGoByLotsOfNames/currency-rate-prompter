import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import URLError

from currency_prompter.cli import main
from currency_prompter.domain import FakeClock, Quote, Rule, timestamp, utc
from currency_prompter.monitor import dispatch
from currency_prompter.provider import Frankfurter
from currency_prompter.report import write_report
from currency_prompter.storage import Store

START = utc("2026-01-01T00:00:00Z")


def quote(hours=0, *, received_hours=None):
    observed = START + timedelta(hours=hours)
    received = observed if received_hours is None else START + timedelta(hours=received_hours)
    return Quote("SGD", "THB", Decimal("25"), observed, received, "demo")


def rule(**changes):
    values = dict(
        name="test",
        base="SGD",
        counter="THB",
        source="demo",
        direction="at_or_above",
        threshold="25",
        cooldown_seconds=3600,
        max_age_seconds=86400,
    )
    values.update(changes)
    return Rule(**values)


class StoreRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "history.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def process(self, item, *, now=None):
        return self.store.process(item, [rule()], item.received_at if now is None else now)

    def test_report_rejects_database_and_all_companion_paths(self):
        self.process(quote())
        for suffix in ("", "-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                target = Path(str(self.path) + suffix)
                before = target.read_bytes() if target.exists() else None
                with self.assertRaisesRegex(ValueError, "database"):
                    write_report(self.store, target)
                self.assertEqual(target.read_bytes() if target.exists() else None, before)
        self.assertEqual(self.store.counts()["quotes"], 1)

    def test_report_rejects_normalized_alias(self):
        self.process(quote())
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            write_report(self.store, self.directory / "unused" / ".." / self.path.name)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.directory / "unused").exists())

    def test_report_rejects_hardlink_alias(self):
        self.process(quote())
        alias = self.directory / "alias.html"
        try:
            os.link(self.path, alias)
        except OSError as exc:
            self.skipTest(f"OS cannot create hardlink: {type(exc).__name__}")
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            write_report(self.store, alias)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(alias.samefile(self.path))

    def test_report_rejects_symlink_alias(self):
        self.process(quote())
        alias = self.directory / "alias.html"
        try:
            alias.symlink_to(self.path)
        except OSError as exc:
            self.skipTest(f"OS cannot create symlink: {type(exc).__name__}")
        with self.assertRaises(ValueError):
            write_report(self.store, alias)
        self.assertTrue(alias.is_symlink())
        self.assertEqual(self.store.counts()["quotes"], 1)

    def test_report_failed_replacement_preserves_existing_output(self):
        target = self.directory / "history.html"
        target.write_text("existing report", encoding="utf-8")
        with patch("currency_prompter.report.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                write_report(self.store, target)
        self.assertEqual(target.read_text(encoding="utf-8"), "existing report")
        self.assertEqual(list(self.directory.glob(".report-*.tmp")), [])

    def test_cli_report_collision_returns_error_without_data_loss(self):
        self.process(quote())
        before = self.path.read_bytes()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            status = main(["report", "--db", str(self.path), "--output", str(self.path)])
        self.assertEqual(status, 2)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.counts()["quotes"], 1)

    def test_cli_dispatch_failure_status_and_successful_retry(self):
        self.process(quote())
        self.store.connection.execute(
            "CREATE TRIGGER reject_journal BEFORE INSERT ON notifications "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        self.store.connection.commit()
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            status = main(["dispatch", "--db", str(self.path)])
        self.assertEqual(status, 2)
        self.assertEqual(json.loads(output.getvalue())["failed"], 1)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.counts()["notifications"], 0)
        self.store.connection.execute("DROP TRIGGER reject_journal")
        self.store.connection.commit()
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(["dispatch", "--db", str(self.path)]), 0)
        self.assertEqual(len(self.store.pending()), 0)
        self.assertEqual(self.store.counts()["notifications"], 1)

    def test_stale_decision_advances_processing_guard(self):
        self.process(quote())
        self.assertEqual(
            self.process(quote(2), now=START + timedelta(hours=48))["outcomes"], ["stale"]
        )
        self.assertEqual(self.process(quote(3))["outcomes"], ["out_of_order"])
        state = self.store.connection.execute("SELECT * FROM rule_state").fetchone()
        self.assertEqual(state["latest_observed"], timestamp(START))
        self.assertEqual(state["last_queued"], timestamp(START))
        self.assertEqual(self.process(quote(49))["outcomes"], ["queued"])

    def test_first_stale_decision_also_protects_processing_clock(self):
        self.assertEqual(
            self.process(quote(), now=START + timedelta(hours=48))["outcomes"], ["stale"]
        )
        self.assertIsNone(self.store.connection.execute("SELECT * FROM rule_state").fetchone())
        self.assertEqual(self.process(quote(3))["outcomes"], ["out_of_order"])
        self.assertEqual(self.store.counts()["outbox"], 0)

    def test_late_observation_advances_clock_without_rewinding_valid_state(self):
        self.process(quote(4))
        self.assertEqual(self.process(quote(2, received_hours=6))["outcomes"], ["out_of_order"])
        self.assertEqual(self.process(quote(5))["outcomes"], ["out_of_order"])
        processing = self.store.connection.execute(
            "SELECT latest_processing FROM rule_processing"
        ).fetchone()[0]
        state = self.store.connection.execute("SELECT * FROM rule_state").fetchone()
        self.assertEqual(processing, timestamp(START + timedelta(hours=6)))
        self.assertEqual(state["latest_observed"], timestamp(START + timedelta(hours=4)))
        self.assertEqual(self.process(quote(7))["outcomes"], ["queued"])

    def test_legacy_migration_uses_all_recorded_decision_times(self):
        self.process(quote())
        self.process(quote(2), now=START + timedelta(hours=48))
        before = self.store.counts()
        self.store.connection.execute("DROP TABLE rule_processing")
        self.store.connection.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.counts(), before)
        processing = self.store.connection.execute(
            "SELECT latest_processing FROM rule_processing"
        ).fetchone()[0]
        self.assertEqual(processing, timestamp(START + timedelta(hours=48)))
        self.assertEqual(self.process(quote(3))["outcomes"], ["out_of_order"])
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.process(quote(4))["outcomes"], ["out_of_order"])

    def test_duplicate_decision_remains_a_no_op(self):
        self.process(quote())
        self.assertEqual(
            self.process(quote(), now=START + timedelta(hours=100))["outcomes"], ["duplicate"]
        )
        self.assertEqual(self.process(quote(1))["outcomes"], ["queued"])

    def test_watermark_failure_rolls_back_whole_processing_transaction(self):
        self.store.connection.execute(
            "CREATE TRIGGER reject_watermark BEFORE INSERT ON rule_processing "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.process(quote())
        for table in ("quotes", "rules", "decisions", "rule_state", "rule_processing", "outbox"):
            self.assertEqual(
                self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
            )

    def test_dispatch_before_enqueue_remains_pending_and_can_retry(self):
        self.process(quote())
        result = dispatch(self.store, clock=FakeClock(START - timedelta(seconds=1)))
        self.assertEqual(result, {"acknowledged": 0, "new_notifications": 0, "failed": 1})
        self.assertEqual(self.store.counts()["notifications"], 0)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(dispatch(self.store, clock=FakeClock(START))["acknowledged"], 1)

    def test_clock_rewind_after_journal_preserves_receipt_and_retry(self):
        self.process(quote())
        times = iter([START + timedelta(seconds=2), START + timedelta(seconds=1)])
        result = dispatch(self.store, clock=SimpleNamespace(now=lambda: next(times)))
        self.assertEqual(result, {"acknowledged": 0, "new_notifications": 1, "failed": 1})
        self.assertEqual(self.store.counts()["notifications"], 1)
        self.assertEqual(len(self.store.pending()), 1)
        # A later attempt still cannot claim to happen before the first journal receipt.
        self.assertEqual(
            dispatch(self.store, clock=FakeClock(START + timedelta(seconds=1)))["failed"], 1
        )
        self.assertEqual(
            dispatch(self.store, clock=FakeClock(START + timedelta(seconds=3))),
            {"acknowledged": 1, "new_notifications": 0, "failed": 0},
        )
        self.assertEqual(self.store.counts()["notifications"], 1)

    def test_direct_journal_and_ack_reject_noncausal_times(self):
        self.process(quote())
        event = json.loads(self.store.pending()[0]["payload"])
        with self.assertRaises(ValueError):
            self.store.record_notification(event, START - timedelta(seconds=1))
        with self.assertRaises(ValueError):
            self.store.acknowledge(event["id"], START - timedelta(seconds=1))
        self.store.record_notification(event, START + timedelta(seconds=2))
        with self.assertRaises(ValueError):
            self.store.acknowledge(event["id"], START + timedelta(seconds=1))
        self.assertEqual(len(self.store.pending()), 1)


class ReceiptRegressions(unittest.TestCase):
    def response(self, clock, seconds=5, day="2026-01-01"):
        body = json.dumps({"base": "SGD", "quote": "THB", "rate": "25", "date": day}).encode()

        class DelayedBody(BytesIO):
            def read(self, *args):
                clock.advance(seconds)
                return super().read(*args)

        return DelayedBody(body)

    def test_receipt_is_after_completed_body_read(self):
        clock = FakeClock(START)
        provider = Frankfurter(transport=lambda *a, **k: self.response(clock))
        result = provider.fetch("SGD", "THB", clock=clock)
        self.assertEqual(result.received_at, START + timedelta(seconds=5))

    def test_receipt_after_retry_and_backoff(self):
        clock = FakeClock(START)
        calls = []

        def transport(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                clock.advance(2)
                raise URLError("synthetic retry")
            return self.response(clock)

        provider = Frankfurter(transport=transport, sleep=clock.advance)
        result = provider.fetch("SGD", "THB", clock=clock)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.received_at, START + timedelta(seconds=7.25))

    def test_provider_date_rollover_does_not_use_request_start(self):
        clock = FakeClock(utc("2026-01-01T23:59:59Z"))
        provider = Frankfurter(transport=lambda *a, **k: self.response(clock, 2, "2026-01-02"))
        result = provider.fetch("SGD", "THB", clock=clock)
        self.assertEqual(result.observed_at, utc("2026-01-02T00:00:00Z"))
        self.assertEqual(result.received_at, utc("2026-01-02T00:00:01Z"))

    def test_fixed_receipt_is_backward_compatible_and_forms_are_exclusive(self):
        transport = Mock(return_value=self.response(FakeClock(START), 0))
        provider = Frankfurter(transport=transport)
        self.assertEqual(provider.fetch("SGD", "THB", START).received_at, START)
        transport.reset_mock()
        with self.assertRaises(ValueError):
            provider.fetch("SGD", "THB", START, clock=FakeClock(START))
        transport.assert_not_called()

    def test_cli_poll_uses_injected_receipt_clock(self):
        clock = FakeClock(START)
        provider = Frankfurter(transport=lambda *a, **k: self.response(clock))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rules = root / "rules.json"
            rules.write_text(json.dumps([rule(source="frankfurter").to_dict()]), encoding="utf-8")
            output = StringIO()
            with (
                patch("currency_prompter.cli.SystemClock", return_value=clock),
                patch("currency_prompter.cli.Frankfurter", return_value=provider),
                redirect_stdout(output),
                redirect_stderr(StringIO()),
            ):
                status = main(["poll", "--db", str(root / "history.sqlite"), "--rules", str(rules)])
            self.assertEqual(status, 0)
            self.assertEqual(
                json.loads(output.getvalue())["quote"]["received_at"],
                timestamp(START + timedelta(seconds=5)),
            )


if __name__ == "__main__":
    unittest.main()
