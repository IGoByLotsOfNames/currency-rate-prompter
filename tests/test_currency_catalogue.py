"""Catalogue refresh, offline coverage, and persistent data boundary regressions."""

import csv
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from currency_prompter.app_service import DEMO_UNITS, AppError, AppService, demo_points
from currency_prompter.currency_catalogue import (
    CATALOGUE_URL,
    MAX_CURRENCIES,
    MAX_RESPONSE,
    FrankfurterCatalogue,
    decode_json,
    load_bundled,
    make_snapshot,
    validate_rows,
    validate_snapshot,
)
from currency_prompter.domain import FakeClock, Quote, timestamp, utc
from currency_prompter.provider import RateUnavailable
from currency_prompter.storage import Store

NOW = utc("2026-10-03T12:00:00Z")


def extra_row():
    return {
        "iso_code": "ZZZ",
        "iso_numeric": None,
        "name": "New test currency",
        "symbol": None,
        "start_date": "2026-01-01",
        "end_date": "2026-10-01",
    }


class Response(io.BytesIO):
    def __init__(self, body, clock=None):
        super().__init__(body)
        self.clock, self.read_sizes = clock, []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if self.clock is not None:
            self.clock.current += timedelta(seconds=1)
        return super().read(size)


class PairProvider:
    def fetch(self, base, counter, *, clock, days=None):
        quote = Quote(
            base, counter, "1.25", clock.now().replace(hour=0), clock.now(), "frankfurter"
        )
        return [quote] if days is not None else quote


class CatalogueTests(unittest.TestCase):
    def test_bundle_is_complete_and_end_date_does_not_determine_activity(self):
        snapshot = load_bundled()
        self.assertEqual(165, len(snapshot["currencies"]))
        self.assertEqual(165, len({row["iso_code"] for row in snapshot["currencies"]}))
        self.assertTrue(
            {"SGD", "THB", "XAU", "IRR", "ZWG"}
            <= {row["iso_code"] for row in snapshot["currencies"]}
        )
        row = snapshot["currencies"][0]
        row["end_date"] = row["start_date"]
        self.assertEqual([row], validate_rows([row]))

    def test_catalogue_rejects_bad_batch_without_salvaging_individual_rows(self):
        row = extra_row()
        invalid = [[], {}, [None], [row, row], [row] * (MAX_CURRENCIES + 1)]
        for key, value in (
            ("iso_code", "zzz"),
            ("iso_code", "AB"),
            ("name", ""),
            ("name", " leading"),
            ("name", "line\nbreak"),
            ("name", "hidden\u200b"),
            ("name", "x" * 161),
            ("iso_numeric", 1),
            ("iso_numeric", "１２３"),
            ("symbol", "bad\x00"),
            ("start_date", "2026-13-01"),
            ("end_date", "2025-12-31"),
            ("end_date", None),
        ):
            invalid.append([{**row, key: value}])
        invalid.append([{key: value for key, value in row.items() if key != "symbol"}])
        for rows in invalid:
            with self.subTest(rows=str(rows)[:120]), self.assertRaises(ValueError):
                validate_rows(rows)

    def test_json_and_metadata_are_bounded_and_strict(self):
        for raw in (
            b" " * (MAX_RESPONSE + 1),
            b'{"x":1,"x":2}',
            b"NaN",
            b"\xff",
            b"[" * 2000 + b"]" * 2000,
        ):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                validate_rows(decode_json(raw))
        good = make_snapshot([extra_row()], NOW)
        for key, value in (
            ("schema_version", True),
            ("schema_version", 2),
            ("scope", "all"),
            ("url", "https://elsewhere.invalid"),
            ("as_of", "2026-10-03"),
            ("currencies", []),
        ):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_snapshot({**good, key: value})

    def test_normalized_snapshot_size_is_checked_before_cache_commit(self):
        # A compact provider array may fit while its normalized snapshot does not.
        rows = []
        for index in range(512):
            code = "A" + chr(65 + index // 26) + chr(65 + index % 26)
            rows.append({**extra_row(), "iso_code": code, "name": "n" * 130})
        raw = json.dumps(rows, separators=(",", ":")).encode()
        self.assertLessEqual(len(raw), MAX_RESPONSE)
        validated = validate_rows(decode_json(raw))
        self.assertEqual(512, len(validated))
        with self.assertRaisesRegex(ValueError, "snapshot exceeds"):
            make_snapshot(validated, NOW)

    def test_transport_bounds_reads_retries_and_samples_receipt_after_body(self):
        clock = FakeClock(NOW)
        response = Response(json.dumps([extra_row()]).encode(), clock)
        transport = Mock(
            side_effect=[
                HTTPError(CATALOGUE_URL, 429, "limited", {}, None),
                URLError("offline"),
                response,
            ]
        )
        sleep = Mock()
        result = FrankfurterCatalogue(transport=transport, sleep=sleep).fetch(clock=clock)
        self.assertEqual(timestamp(NOW + timedelta(seconds=1)), result["as_of"])
        self.assertEqual([MAX_RESPONSE + 1], response.read_sizes)
        self.assertTrue(response.closed)
        self.assertEqual(3, transport.call_count)
        self.assertEqual([0.25, 0.5], [call.args[0] for call in sleep.call_args_list])
        self.assertEqual(CATALOGUE_URL, transport.call_args.args[0].full_url)
        self.assertEqual(10, transport.call_args.kwargs["timeout"])

    def test_transport_permanent_bad_payload_and_exhausted_failures_are_bounded(self):
        cases = [
            HTTPError(CATALOGUE_URL, 404, "missing", {}, None),
            Response(b"[]"),
            Response(b"x" * (MAX_RESPONSE + 1)),
        ]
        for outcome in cases:
            transport = (
                Mock(side_effect=outcome)
                if isinstance(outcome, Exception)
                else Mock(return_value=outcome)
            )
            with self.subTest(outcome=type(outcome).__name__), self.assertRaises(ValueError):
                FrankfurterCatalogue(transport=transport, sleep=Mock()).fetch(clock=FakeClock(NOW))
            self.assertEqual(1, transport.call_count)
        transport = Mock(side_effect=TimeoutError("offline"))
        with self.assertRaises(ValueError):
            FrankfurterCatalogue(transport=transport, sleep=Mock(), retries=1).fetch()
        self.assertEqual(2, transport.call_count)
        for options in (
            {"timeout": True},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"timeout": 0},
            {"timeout": 31},
            {"retries": True},
            {"retries": 4},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                FrankfurterCatalogue(**options)


class CatalogueServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "tracker.sqlite"
        self.clock = FakeClock(NOW)
        self.catalogue = Mock()
        self.catalogue.fetch.return_value = make_snapshot(
            load_bundled()["currencies"] + [extra_row()], NOW
        )
        self.service = AppService(
            self.db,
            clock=self.clock,
            catalogue_factory=lambda: self.catalogue,
            provider_factory=PairProvider,
            history_factory=PairProvider,
        )

    def rule(self, base, counter, mode="demo"):
        return self.service.create_rule(
            {
                "name": "Retained rule",
                "base": base,
                "counter": counter,
                "mode": mode,
                "direction": "at_or_above",
                "threshold": "1e-18",
                "cooldown_seconds": 0,
            }
        )["rule"]

    def test_every_bundled_code_works_through_offline_app_operations(self):
        currencies = self.service.bootstrap()["currencies"]
        for item in currencies:
            base, counter = ("SGD", item["code"]) if item["code"] != "SGD" else ("SGD", "USD")
            with self.subTest(code=item["code"]):
                selected = {"base": base, "counter": counter}
                self.service.set_watchlist(selected)
                self.assertEqual(
                    90, len(self.service.dashboard(base, counter, "demo", 90)["points"])
                )
                converted = self.service.convert(
                    {**selected, "mode": "demo", "amount": "100", "direction": "base_to_counter"}
                )
                self.assertGreater(Decimal(converted["result"]), 0)
                rows = list(csv.DictReader(io.StringIO(self.service.export(base, counter, "demo"))))
                self.assertEqual(90, len(rows))
                self.assertEqual({"synthetic-demo"}, {row["source"] for row in rows})
                for mode in ("demo", "reference"):
                    rule = self.rule(base, counter, mode)
                    self.assertTrue(self.service.refresh({**selected, "mode": mode})["ok"])
                    self.service.update_rule({"id": rule["id"]}, delete=True)
                self.assertTrue(self.service.import_history(selected)["ok"])
                self.assertEqual(
                    "1.25", self.service.dashboard(base, counter, "reference")["latest"]["rate"]
                )
                self.assertEqual(
                    "125",
                    self.service.convert(
                        {
                            **selected,
                            "mode": "reference",
                            "amount": "100",
                            "direction": "base_to_counter",
                        }
                    )["result"],
                )
                self.service.set_watchlist(selected, remove=True)
        self.catalogue.fetch.assert_not_called()

    def test_original_sixteen_demo_paths_keep_their_frozen_hashes(self):
        # Receipt generated from the preserved Phase 3 source, independent of new helper.
        import hashlib

        receipt = hashlib.sha256()
        for base in sorted(DEMO_UNITS):
            for counter in sorted(DEMO_UNITS):
                if base != counter:
                    receipt.update(f"{base}/{counter}\n".encode())
                    receipt.update(
                        json.dumps(
                            demo_points(base, counter), sort_keys=True, separators=(",", ":")
                        ).encode()
                    )
                    receipt.update(b"\n")
        self.assertEqual(
            "7c975c9bde60b09bd01738e04bbd563b568f436a7bdfafc6426a8f3a88c343a2", receipt.hexdigest()
        )

    def test_new_codes_persist_and_removed_codes_keep_saved_data_and_names(self):
        self.assertEqual("bundled", self.service.bootstrap()["catalogue"]["source"])
        self.catalogue.fetch.assert_not_called()
        refreshed = self.service.refresh_currencies({})
        self.assertEqual(166, refreshed["catalogue"]["count"])
        self.assertEqual("cached", refreshed["catalogue"]["source"])
        selected = {"base": "SGD", "counter": "ZZZ"}
        self.service.set_watchlist(selected)
        rule = self.rule("SGD", "ZZZ", "reference")
        self.service.refresh({**selected, "mode": "reference"})
        reopened = AppService(self.db, clock=self.clock, catalogue_factory=lambda: self.catalogue)
        self.assertEqual(166, reopened.bootstrap()["catalogue"]["count"])
        self.assertIn(selected, reopened.bootstrap()["watchlist"])
        with Store(self.db, readonly=True) as store:
            before = store.counts()
        self.catalogue.fetch.return_value = make_snapshot(load_bundled()["currencies"], NOW)
        refreshed = reopened.refresh_currencies({})
        self.assertEqual(
            [{"code": "ZZZ", "name": "New test currency"}], refreshed["saved_currencies"]
        )
        after = reopened.dashboard("SGD", "ZZZ", "reference")
        self.assertFalse(after["catalogue_available"])
        self.assertIn("absent", after["note"])
        self.assertEqual(rule["id"], after["rules"][0]["id"])
        self.assertEqual("1.25", after["latest"]["rate"])
        self.assertEqual(1, len(after["alerts"]))
        self.assertEqual(
            "2.5",
            reopened.convert(
                {**selected, "mode": "reference", "amount": "2", "direction": "base_to_counter"}
            )["result"],
        )
        self.assertIn("frankfurter", reopened.export("SGD", "ZZZ", "reference"))
        with Store(self.db, readonly=True) as store:
            self.assertEqual(before, store.counts())

    def test_invalid_failed_and_future_refresh_keep_last_good_snapshot(self):
        self.service.refresh_currencies({})
        before = self.service.bootstrap()
        oversized = {
            "schema_version": 1,
            "as_of": timestamp(NOW),
            "url": CATALOGUE_URL,
            "scope": "active",
            "currencies": [
                {
                    **extra_row(),
                    "iso_code": "A" + chr(65 + i // 26) + chr(65 + i % 26),
                    "name": "n" * 130,
                }
                for i in range(512)
            ],
        }
        for bad in ({}, make_snapshot([extra_row()], NOW + timedelta(seconds=1)), oversized):
            self.catalogue.fetch.return_value = bad
            with self.assertRaises(AppError) as error:
                self.service.refresh_currencies({})
            self.assertEqual(502, error.exception.status)
            self.assertEqual(before, self.service.bootstrap())
        self.catalogue.fetch.side_effect = OSError("offline")
        with self.assertRaises(AppError):
            self.service.refresh_currencies({})
        self.assertEqual(before, self.service.bootstrap())
        with self.assertRaises(AppError):
            self.service.refresh_currencies({"unexpected": True})

    def test_older_response_cannot_replace_newer_cached_catalogue(self):
        self.service.refresh_currencies({})
        before = self.service.bootstrap()
        self.catalogue.fetch.return_value = make_snapshot(
            load_bundled()["currencies"], NOW - timedelta(seconds=1)
        )
        with self.assertRaises(AppError) as error:
            self.service.refresh_currencies({})
        self.assertEqual(409, error.exception.status)
        self.assertEqual(before, self.service.bootstrap())

    def test_sql_failure_rolls_back_catalogue_and_names_together(self):
        with Store(self.db) as store:
            store.connection.execute(
                "CREATE TRIGGER reject_name BEFORE INSERT ON app_currency_names WHEN NEW.code='ZZZ' BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with self.assertRaises(sqlite3.Error):
            self.service.refresh_currencies({})
        self.assertEqual("bundled", self.service.bootstrap()["catalogue"]["source"])
        with Store(self.db, readonly=True) as store:
            self.assertIsNone(
                store.connection.execute(
                    "SELECT value FROM app_settings WHERE key='currency_catalogue'"
                ).fetchone()
            )
            self.assertIsNone(
                store.connection.execute(
                    "SELECT name FROM app_currency_names WHERE code='ZZZ'"
                ).fetchone()
            )

    def test_corrupt_local_cache_falls_back_offline_without_hiding_saved_codes(self):
        self.service.refresh_currencies({})
        self.service.set_watchlist({"base": "SGD", "counter": "ZZZ"})
        with Store(self.db) as store, store.connection:
            store.connection.execute(
                "UPDATE app_settings SET value='broken' WHERE key='currency_catalogue'"
            )
        before_calls = self.catalogue.fetch.call_count
        result = AppService(self.db, catalogue_factory=lambda: self.catalogue).bootstrap()
        self.assertEqual(165, result["catalogue"]["count"])
        self.assertEqual("bundled", result["catalogue"]["source"])
        self.assertEqual([{"code": "ZZZ", "name": "New test currency"}], result["saved_currencies"])
        self.assertEqual(before_calls, self.catalogue.fetch.call_count)

    def test_reference_unavailable_preserves_data_and_tiny_conversion_stays_nonzero(self):
        provider = Mock()
        provider.fetch.side_effect = RateUnavailable("no pair")
        self.service.provider_factory = lambda: provider
        self.service.history_factory = lambda: provider
        for method, body in (
            (self.service.refresh, {"base": "SGD", "counter": "THB", "mode": "reference"}),
            (self.service.import_history, {"base": "SGD", "counter": "THB"}),
        ):
            with self.assertRaises(AppError) as error:
                method(body)
            self.assertEqual(422, error.exception.status)
        with Store(self.db) as store:
            self.assertEqual(0, store.counts()["quotes"])
            store.process(Quote("SGD", "XAU", "1e-18", NOW, NOW, "frankfurter"), [], NOW)
            store.process(Quote("SGD", "IRR", "1e18", NOW, NOW, "frankfurter"), [], NOW)
        for counter, amount, direction, expected in (
            ("XAU", "0.000000000001", "base_to_counter", Decimal("1e-30")),
            ("IRR", "1000000000000", "base_to_counter", Decimal("1e30")),
            ("IRR", "0.000000000001", "counter_to_base", Decimal("1e-30")),
        ):
            result = self.service.convert(
                {
                    "base": "SGD",
                    "counter": counter,
                    "mode": "reference",
                    "amount": amount,
                    "direction": direction,
                }
            )
            self.assertEqual(expected, Decimal(result["result"]))
            self.assertIn("significant digits", result["note"])


if __name__ == "__main__":
    unittest.main()
