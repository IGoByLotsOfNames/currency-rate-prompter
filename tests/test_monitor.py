from datetime import timedelta
from decimal import Decimal
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest

from currency_prompter.domain import FakeClock, Quote, Rule, utc
from currency_prompter.monitor import JournalNotifier, dispatch, load_quotes, load_rules, replay
from currency_prompter.storage import Store

START = utc("2026-01-01T00:00:00Z")


def quote(hours=0, value="25", source="demo"):
    when = START + timedelta(hours=hours)
    return Quote("SGD", "THB", Decimal(value), when, when, source)


def rule(**kwargs):
    config = dict(name="test", base="SGD", counter="THB", source="demo", direction="at_or_above",
                  threshold="25", cooldown_seconds=3600, max_age_seconds=86400)
    config.update(kwargs)
    return Rule(**config)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "test.sqlite"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def process(self, q, rules=None, now=None):
        return self.store.process(q, rules if rules is not None else [rule()], now or q.received_at)

    def test_first_quote_equal_threshold(self):
        self.assertEqual(self.process(quote())["outcomes"], ["queued"])
        self.assertEqual(self.store.counts()["outbox"], 1)

    def test_below_threshold(self):
        self.assertEqual(self.process(quote(value="24.999999999999999999"))["outcomes"], ["below_condition"])

    def test_below_direction(self):
        self.assertEqual(self.process(quote(), [rule(direction="at_or_below")])["outcomes"], ["queued"])

    def test_duplicate_quote(self):
        self.process(quote())
        result = self.process(quote())
        self.assertEqual(result, {"inserted": False, "outcomes": ["duplicate"]})
        self.assertEqual(self.store.counts()["outbox"], 1)

    def test_semantic_duplicate(self):
        self.process(quote(value="25.00"))
        self.assertFalse(self.process(quote(value="25"))["inserted"])

    def test_conflicting_revision_rolls_back(self):
        self.process(quote())
        before = self.store.counts()
        with self.assertRaises(ValueError):
            self.process(quote(value="26"))
        self.assertEqual(before, self.store.counts())

    def test_failure_after_outbox_insert_rolls_back_whole_transaction(self):
        self.store.connection.execute("CREATE TRIGGER forced_failure BEFORE INSERT ON decisions BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.process(quote())
        for table in ("quotes", "rules", "rule_state", "outbox", "decisions"):
            self.assertEqual(self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        self.store.connection.execute("DROP TRIGGER forced_failure")
        self.assertEqual(self.process(quote())["outcomes"], ["queued"])

    def test_cooldown_boundary(self):
        self.process(quote())
        self.assertEqual(self.process(quote(hours=.999))["outcomes"], ["cooldown"])
        self.assertEqual(self.process(quote(hours=1))["outcomes"], ["queued"])

    def test_cooldown_persists_after_reopen(self):
        self.process(quote())
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.process(quote(hours=.5))["outcomes"], ["cooldown"])

    def test_out_of_order_is_stored_without_alert(self):
        self.process(quote(hours=3))
        self.assertEqual(self.process(quote(hours=2))["outcomes"], ["out_of_order"])
        self.assertEqual(self.store.counts()["quotes"], 2)

    def test_stale_quote(self):
        self.assertEqual(self.process(quote(), now=START + timedelta(days=2))["outcomes"], ["stale"])
        self.assertEqual(self.store.counts()["outbox"], 0)

    def test_stale_boundary_inclusive(self):
        self.assertEqual(self.process(quote(), now=START + timedelta(days=1))["outcomes"], ["queued"])

    def test_future_receipt_rejected_before_write(self):
        with self.assertRaises(ValueError):
            self.process(quote(hours=1), now=START)
        self.assertEqual(self.store.counts()["quotes"], 0)

    def test_clock_rewind_does_not_trigger(self):
        self.process(quote(), now=START + timedelta(hours=2))
        self.assertEqual(self.process(quote(hours=1))["outcomes"], ["out_of_order"])

    def test_other_source_does_not_trigger(self):
        self.assertEqual(self.process(quote(source="other"))["outcomes"], [])

    def test_rule_revision_is_distinct(self):
        self.process(quote())
        self.assertEqual(self.process(quote(), [rule(threshold="24")])["outcomes"], ["queued"])

    def test_history_can_be_evaluated_by_new_rule(self):
        self.process(quote(), [])
        self.assertEqual(self.process(quote())["outcomes"], ["queued"])

    def test_duplicate_rule_names_rejected(self):
        with self.assertRaises(ValueError):
            self.process(quote(), [rule(), rule(threshold="26")])
        self.assertEqual(self.store.counts()["quotes"], 0)

    def test_dispatch_twice(self):
        self.process(quote())
        self.assertEqual(dispatch(self.store, clock=FakeClock(START))["new_notifications"], 1)
        self.assertEqual(dispatch(self.store)["new_notifications"], 0)
        self.assertEqual(self.store.counts()["notifications"], 1)

    def test_failure_keeps_pending_then_retry(self):
        class Broken:
            def send(self, event, now):
                raise RuntimeError("private detail must not persist")
        self.process(quote())
        self.assertEqual(dispatch(self.store, Broken())["failed"], 1)
        self.assertEqual(len(self.store.pending()), 1)
        self.assertEqual(self.store.pending()[0]["last_error"], "RuntimeError")
        self.assertEqual(dispatch(self.store)["new_notifications"], 1)

    def test_delivery_crash_before_ack_is_idempotent(self):
        self.process(quote())
        row = self.store.pending()[0]
        JournalNotifier(self.store).send(json.loads(row["payload"]), START)
        self.store.close()
        self.store = Store(self.path)
        result = dispatch(self.store)
        self.assertEqual(result["new_notifications"], 0)
        self.assertEqual(result["acknowledged"], 1)
        self.assertEqual(self.store.counts()["notifications"], 1)

    def test_month_year_and_timezone_boundaries(self):
        q = Quote("SGD", "THB", Decimal(25), utc("2025-12-31T23:30Z"), utc("2026-01-01T07:30+08:00"), "demo")
        self.assertEqual(self.process(q)["outcomes"], ["queued"])
        self.assertEqual(self.process(quote(hours=.5))["outcomes"], ["queued"])

    def test_replay_twice(self):
        quotes = [quote(hours=i) for i in range(10)]
        self.assertEqual(replay(self.store, quotes, [rule()])["queued"], 10)
        self.assertEqual(replay(self.store, quotes, [rule()])["duplicates"], 10)
        self.assertEqual(self.store.counts()["outbox"], 10)

    def test_invalid_batch_validated_before_processing(self):
        path = Path(self.tmp.name) / "bad.json"
        path.write_text(json.dumps([quote().to_dict(), {"bad": 1}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_quotes(path)
        self.assertEqual(self.store.counts()["quotes"], 0)

    def test_rules_duplicate_and_empty_rejected(self):
        path = Path(self.tmp.name) / "bad.json"
        for data in ([], [rule().to_dict(), rule().to_dict()]):
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_rules(path)
