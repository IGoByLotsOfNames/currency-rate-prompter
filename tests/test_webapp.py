"""Offline local-app contracts; provider transports are always fakes."""

import csv
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from decimal import ROUND_UP, Inexact, localcontext
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch

from currency_prompter.app_service import DEMO_ANCHOR, AppError, AppService, demo_points
from currency_prompter.currency_catalogue import load_bundled, make_snapshot
from currency_prompter.domain import FakeClock, Quote, Rule, utc
from currency_prompter.provider import RateUnavailable
from currency_prompter.storage import Store
from currency_prompter.webapp import MAX_BODY, make_app_server

NOW = utc("2026-10-03T12:00:00Z")


def reference_quote(value="40", when=None):
    return Quote("SGD", "THB", value, when or NOW.replace(hour=0), NOW, "frankfurter")


class FakeProvider:
    def __init__(self):
        self.calls = []
        self.quotes = [reference_quote()]

    def fetch(self, base, counter, *, clock, days=None):
        self.calls.append((base, counter, days, clock.now()))
        return list(self.quotes) if days is not None else self.quotes[-1]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "tracker.sqlite"
        self.provider = FakeProvider()
        self.clock = FakeClock(NOW)
        self.service = AppService(
            self.db,
            clock=self.clock,
            provider_factory=lambda: self.provider,
            history_factory=lambda: self.provider,
        )

    def rule_body(self, mode="demo", **changes):
        body = {
            "name": "SGD above my threshold",
            "base": "SGD",
            "counter": "THB",
            "mode": mode,
            "direction": "at_or_above",
            "threshold": "1",
            "cooldown_seconds": 0,
        }
        return {**body, **changes}

    def refresh(self, mode="demo"):
        return self.service.refresh({"base": "SGD", "counter": "THB", "mode": mode})

    def test_bootstrap_and_read_paths_are_offline_without_quote_writes(self):
        self.assertEqual(165, len(self.service.bootstrap()["currencies"]))
        self.assertEqual(3, len(self.service.bootstrap()["watchlist"]))
        demo = self.service.dashboard(days=90)
        self.assertEqual(90, len(demo["points"]))
        self.assertEqual(DEMO_ANCHOR, utc(demo["latest"]["observed_at"]))
        self.assertEqual(demo, self.service.dashboard(days=90))
        self.assertEqual("synthetic-demo", demo["source"])
        self.assertFalse(demo["stale"])
        self.assertIsNone(self.service.dashboard(mode="reference")["latest"])
        self.assertEqual(3, len(self.service.watchlist("reference")["watchlist"]))
        self.assertEqual([], self.provider.calls)
        with Store(self.db, readonly=True) as store:
            self.assertEqual(
                {"quotes": 0, "decisions": 0, "outbox": 0, "notifications": 0}, store.counts()
            )

    def test_watchlist_changes_persist_even_when_every_default_is_removed(self):
        for item in self.service.bootstrap()["watchlist"]:
            self.service.set_watchlist(item, remove=True)
        self.assertEqual([], AppService(self.db).bootstrap()["watchlist"])
        self.service.set_watchlist({"base": "GBP", "counter": "JPY"})
        self.service.set_watchlist({"base": "GBP", "counter": "JPY"})
        self.assertEqual(
            [{"base": "GBP", "counter": "JPY"}], AppService(self.db).bootstrap()["watchlist"]
        )
        for body in ({"base": "ZZZ", "counter": "SGD"}, {"base": "SGD", "counter": "SGD"}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.service.set_watchlist(body)

    def test_rules_have_separate_ids_persist_disable_and_keep_deleted_names(self):
        created = self.service.create_rule(self.rule_body())["rule"]
        reference = self.service.create_rule(self.rule_body("reference"))["rule"]
        self.assertNotEqual(created["id"], reference["id"])
        self.assertEqual(1, len(AppService(self.db, clock=self.clock).dashboard()["rules"]))
        self.service.update_rule({"id": created["id"], "enabled": False})
        self.assertEqual([], self.refresh()["processing"]["outcomes"])
        self.service.update_rule({"id": created["id"], "enabled": True})
        self.assertEqual(["queued"], self.refresh()["processing"]["outcomes"])
        self.assertEqual(["duplicate"], self.refresh()["processing"]["outcomes"])
        self.assertEqual(1, len(self.service.dashboard()["alerts"]))
        self.assertEqual([], self.service.dashboard(mode="reference")["alerts"])
        self.service.update_rule({"id": created["id"]}, delete=True)
        self.assertEqual([], self.service.dashboard()["rules"])
        self.assertEqual(created["name"], self.service.dashboard()["alerts"][0]["rule"])
        with self.assertRaises(AppError):
            self.service.update_rule({"id": created["id"], "enabled": True})

    def test_reference_refresh_is_explicit_and_latest_data_are_not_invented(self):
        self.assertIsNone(self.service.dashboard(mode="reference")["latest"])
        self.refresh("reference")
        result = self.service.dashboard(mode="reference")
        self.assertEqual("40", result["latest"]["rate"])
        self.assertIsNone(result["previous_rate"])
        self.assertEqual(1, len(result["points"]))
        self.assertEqual(1, len(self.provider.calls))
        self.assertIsNotNone(result["last_refresh"])
        self.clock.advance(5 * 86400)
        self.assertTrue(self.service.dashboard(mode="reference")["stale"])
        self.assertFalse(self.service.dashboard()["stale"])

    def test_conversion_is_decimal_paired_and_independent_of_ambient_context(self):
        body = {
            "base": "SGD",
            "counter": "THB",
            "mode": "reference",
            "amount": "100",
            "direction": "base_to_counter",
            "fee_percent": "2",
        }
        with self.assertRaises(AppError):
            self.service.convert(body)
        self.refresh("reference")
        expected = self.service.convert(body)
        self.assertEqual("3920", expected["result"])
        self.assertEqual(
            "2.45", self.service.convert({**body, "direction": "counter_to_base"})["result"]
        )
        self.assertEqual("0", self.service.convert({**body, "fee_percent": "100"})["result"])
        with localcontext() as context:
            context.prec = 3
            context.rounding = ROUND_UP
            context.traps[Inexact] = True
            self.assertEqual(expected, self.service.convert(body))
            self.assertEqual(demo_points("USD", "THB"), demo_points("USD", "THB"))
        for change in (
            {"amount": 1.2},
            {"amount": "NaN"},
            {"amount": "1e13"},
            {"fee_percent": "101"},
            {"amount": "1e-10000"},
            {"direction": "unknown"},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.service.convert({**body, **change})

    def test_history_import_creates_no_historical_alerts_and_checks_conflicts_first(self):
        self.service.create_rule(self.rule_body("reference"))
        self.provider.quotes = [
            reference_quote("39", NOW.replace(hour=0) - timedelta(days=1)),
            reference_quote("40"),
        ]
        self.assertEqual(
            2, self.service.import_history({"base": "SGD", "counter": "THB"})["imported"]
        )
        self.assertEqual(
            0, self.service.import_history({"base": "SGD", "counter": "THB"})["imported"]
        )
        with Store(self.db, readonly=True) as store:
            self.assertEqual(0, store.counts()["decisions"])
            self.assertEqual(0, store.counts()["outbox"])
        self.provider.quotes = [
            reference_quote("38", NOW.replace(hour=0) - timedelta(days=2)),
            reference_quote("41"),
        ]
        with self.assertRaisesRegex(AppError, "nothing was imported"):
            self.service.import_history({"base": "SGD", "counter": "THB"})
        with Store(self.db, readonly=True) as store:
            self.assertEqual(2, store.counts()["quotes"])
        self.assertEqual("40", self.service.dashboard(mode="reference")["latest"]["rate"])

    def test_history_rejects_empty_outside_window_and_future_batches_without_writes(self):
        future = Quote(
            "SGD", "THB", "40", NOW + timedelta(days=1), NOW + timedelta(days=1), "frankfurter"
        )
        for quotes in (
            [],
            [future],
            [reference_quote(when=NOW - timedelta(days=90))],
            [reference_quote()] * 91,
        ):
            self.provider.quotes = quotes
            with self.subTest(count=len(quotes)), self.assertRaises(AppError):
                self.service.import_history({"base": "SGD", "counter": "THB"})
        with Store(self.db, readonly=True) as store:
            self.assertEqual(0, store.counts()["quotes"])

    def test_history_sql_failure_reports_partial_progress(self):
        self.provider.quotes = [
            reference_quote("39", NOW.replace(hour=0) - timedelta(days=1)),
            reference_quote("40"),
        ]
        with Store(self.db) as store:
            store.connection.execute(
                "CREATE TRIGGER injected BEFORE INSERT ON quotes WHEN NEW.rate='40' BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with self.assertRaisesRegex(AppError, "after 1 new observations"):
            self.service.import_history({"base": "SGD", "counter": "THB"})
        with Store(self.db, readonly=True) as store:
            self.assertEqual(1, store.counts()["quotes"])

    def test_demo_refresh_does_not_dispatch_pending_reference_events(self):
        with Store(self.db) as store:
            quote = reference_quote()
            rule = Rule("reference", "SGD", "THB", "frankfurter", "at_or_above", "1")
            store.process(quote, [rule], NOW)
        self.service.create_rule(self.rule_body())
        self.assertEqual(1, self.refresh()["delivery"]["new_notifications"])
        with Store(self.db, readonly=True) as store:
            pending = store.pending()
            self.assertEqual(1, len(pending))
            self.assertEqual("frankfurter", json.loads(pending[0]["payload"])["quote"]["source"])
            self.assertEqual(0, pending[0]["attempts"])

    def test_scoped_dispatch_reaches_past_ten_thousand_unrelated_events(self):
        payload = json.dumps({"quote": {"base": "USD", "counter": "SGD", "source": "frankfurter"}})
        with Store(self.db) as store, store.connection:
            store.connection.executemany(
                "INSERT INTO outbox(id,payload,created_at) VALUES (?,?,?)",
                (
                    (f"unrelated-{index:05}", payload, "2020-01-01T00:00:00Z")
                    for index in range(10000)
                ),
            )
        self.service.create_rule(self.rule_body())
        self.assertEqual(1, self.refresh()["delivery"]["new_notifications"])
        with Store(self.db, readonly=True) as store:
            self.assertEqual(
                10000,
                store.connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE delivered_at IS NULL"
                ).fetchone()[0],
            )

    def test_pair_alert_history_reaches_past_ten_thousand_unrelated_events(self):
        self.service.create_rule(self.rule_body())
        self.refresh()
        payload = json.dumps({"quote": {"base": "USD", "counter": "SGD", "source": "frankfurter"}})
        with Store(self.db) as store, store.connection:
            store.connection.executemany(
                "INSERT INTO outbox(id,payload,created_at) VALUES (?,?,?)",
                ((f"newer-{index:05}", payload, "2040-01-01T00:00:00Z") for index in range(10000)),
            )
        alerts = self.service.dashboard()["alerts"]
        self.assertEqual(1, len(alerts))
        self.assertEqual(self.rule_body()["name"], alerts[0]["rule"])

    def test_future_cached_reference_is_not_used_after_clock_rollback(self):
        old_time = NOW - timedelta(days=2)
        old = Quote("SGD", "THB", "39", old_time, old_time, "frankfurter")
        with Store(self.db) as store:
            store.process(old, [], old_time)
            store.process(reference_quote("40"), [], NOW)
        self.clock.current = NOW - timedelta(days=1)
        dashboard = self.service.dashboard(mode="reference")
        self.assertEqual("39", dashboard["latest"]["rate"])
        self.assertEqual(1, len(dashboard["points"]))
        converted = self.service.convert(
            {
                "base": "SGD",
                "counter": "THB",
                "mode": "reference",
                "amount": "1",
                "direction": "base_to_counter",
            }
        )
        self.assertEqual("39", converted["result"])

    def test_failed_journal_is_not_reported_as_success_and_can_retry(self):
        self.service.create_rule(self.rule_body())
        with Store(self.db) as store:
            store.connection.execute(
                "CREATE TRIGGER injected BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        with self.assertRaises(AppError) as failed:
            self.refresh()
        self.assertEqual(500, failed.exception.status)
        with Store(self.db) as store:
            self.assertEqual(1, len(store.pending()))
            store.connection.execute("DROP TRIGGER injected")
        self.assertEqual(1, self.refresh()["delivery"]["new_notifications"])


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "app.sqlite"
        self.provider = FakeProvider()
        self.service = AppService(
            self.db,
            clock=FakeClock(NOW),
            provider_factory=lambda: self.provider,
            history_factory=lambda: self.provider,
        )
        self.server = make_app_server(self.db, 0, service=self.service)
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        status, _, data = self.request("GET", "/api/app")
        self.assertEqual(200, status)
        self.token = data["csrf_token"]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def request(self, method, path, data=None, *, headers=None, authenticated=True, raw=None):
        actual = {}
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        if method != "GET":
            actual["Content-Type"] = "application/json"
            if authenticated:
                actual.update({"Origin": self.origin, "X-CSRF-Token": self.token})
        actual.update(headers or {})
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=actual)
            response = connection.getresponse()
            content = response.read()
            metadata = dict(response.getheaders())
            payload = (
                json.loads(content)
                if metadata.get("Content-Type", "").startswith("application/json")
                else content.decode("utf-8")
            )
            return response.status, metadata, payload
        finally:
            connection.close()

    def test_bootstrap_static_resources_and_initial_reads_are_offline(self):
        for route, kind in (
            ("/", "text/html"),
            ("/app.js", "application/javascript"),
            ("/app.css", "text/css"),
        ):
            status, headers, payload = self.request("GET", route)
            self.assertEqual(200, status, payload)
            self.assertTrue(headers["Content-Type"].startswith(kind))
            self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual({"status": "ok", "mode": "local-app"}, self.request("GET", "/health")[2])
        self.assertEqual(200, self.request("GET", "/api/watchlist?mode=reference")[0])
        self.assertIsNone(self.request("GET", "/api/dashboard?mode=reference")[2]["latest"])
        self.assertEqual([], self.provider.calls)

    def test_mutations_require_host_origin_and_csrf(self):
        body = {"base": "GBP", "counter": "JPY"}
        for headers, authenticated in (
            ({}, False),
            ({"Origin": "https://evil.example"}, True),
            ({"X-CSRF-Token": "wrong"}, True),
            ({"X-CSRF-Token": "é"}, True),
            ({"Host": "evil.example"}, True),
        ):
            with self.subTest(headers=headers):
                status, _, payload = self.request(
                    "POST", "/api/watchlist", body, headers=headers, authenticated=authenticated
                )
                self.assertEqual(403, status, payload)
        self.assertEqual(403, self.request("GET", "/api/app", headers={"Host": "evil.example"})[0])
        self.assertEqual(
            403, self.request("GET", "/api/app", headers={"Origin": "https://evil.example"})[0]
        )
        self.assertEqual(3, len(self.service.bootstrap()["watchlist"]))
        self.assertEqual(200, self.request("POST", "/api/watchlist", body)[0])
        self.assertEqual(4, len(AppService(self.db).bootstrap()["watchlist"]))
        self.assertEqual(200, self.request("DELETE", "/api/watchlist", body)[0])

    def test_invalid_inputs_are_bounded_json_errors(self):
        for raw, status in (
            (b"", 400),
            (b"[]", 400),
            (b'{"base":"SGD","base":"USD","counter":"THB"}', 400),
            (b'{"base":NaN}', 400),
            (b"[" * 2000 + b"]" * 2000, 400),
            (b" " * (MAX_BODY + 1), 413),
        ):
            with self.subTest(size=len(raw)):
                result = self.request("POST", "/api/watchlist", raw=raw)
                self.assertEqual(status, result[0], result[2])
                self.assertEqual({"error"}, set(result[2]))
        self.assertEqual(
            415,
            self.request("POST", "/api/watchlist", {}, headers={"Content-Type": "text/plain"})[0],
        )
        for route in (
            "/api/dashboard?mode=unknown",
            "/api/dashboard?days=x",
            "/api/dashboard?days=0",
            "/api/dashboard?base=SGD&base=USD",
            "/api/app?unexpected=1",
            "/api/dashboard?base=SGD&counter=SGD",
        ):
            with self.subTest(route=route):
                self.assertEqual(400, self.request("GET", route)[0])
        self.assertEqual(404, self.request("GET", "/../pyproject.toml")[0])
        self.assertEqual(405, self.request("PATCH", "/api/watchlist", {})[0])
        self.assertEqual(400, self.request("POST", "/api/rules", {"name": ""})[0])

    def test_api_rules_refresh_converter_and_csv_preserve_mode(self):
        rule = {
            "name": "Visible rule name",
            "base": "SGD",
            "counter": "THB",
            "mode": "demo",
            "direction": "at_or_above",
            "threshold": "1",
            "cooldown_seconds": 0,
        }
        status, _, created = self.request("POST", "/api/rules", rule)
        self.assertEqual(200, status)
        identity = created["rule"]["id"]
        self.assertEqual(
            200,
            self.request("POST", "/api/refresh", {"base": "SGD", "counter": "THB", "mode": "demo"})[
                0
            ],
        )
        dashboard = self.request("GET", "/api/dashboard?mode=demo")[2]
        self.assertEqual("Visible rule name", dashboard["alerts"][0]["rule"])
        self.assertEqual(
            200, self.request("PATCH", "/api/rules", {"id": identity, "enabled": False})[0]
        )
        self.assertEqual(200, self.request("DELETE", "/api/rules", {"id": identity})[0])
        status, _, conversion = self.request(
            "POST",
            "/api/convert",
            {
                "base": "SGD",
                "counter": "THB",
                "mode": "demo",
                "amount": "100",
                "direction": "base_to_counter",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("demo", conversion["mode"])
        status, headers, text = self.request("GET", "/api/export?base=SGD&counter=THB&mode=demo")
        self.assertEqual(200, status)
        self.assertIn("SGD-THB-demo.csv", headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(text)))
        self.assertEqual(90, len(rows))
        self.assertEqual({"synthetic-demo"}, {row["source"] for row in rows})
        self.assertEqual([], self.provider.calls)
        self.assertEqual(
            200, self.request("POST", "/api/history", {"base": "SGD", "counter": "THB"})[0]
        )
        self.assertEqual(1, len(self.provider.calls))
        reference = self.request("GET", "/api/dashboard?mode=reference")[2]
        self.assertEqual("40", reference["latest"]["rate"])
        self.assertEqual([], reference["alerts"])

    def test_catalogue_refresh_is_explicit_protected_and_preserves_last_good(self):
        provider = Mock()
        provider.fetch.return_value = make_snapshot(load_bundled()["currencies"], NOW)
        self.service.catalogue_factory = lambda: provider
        bootstrap = self.request("GET", "/api/app")[2]
        self.assertEqual(165, bootstrap["catalogue"]["count"])
        self.assertEqual("bundled", bootstrap["catalogue"]["source"])
        provider.fetch.assert_not_called()
        self.assertEqual(
            403, self.request("POST", "/api/currencies/refresh", {}, authenticated=False)[0]
        )
        self.assertEqual(
            400, self.request("POST", "/api/currencies/refresh", {"unexpected": True})[0]
        )
        provider.fetch.assert_not_called()
        status, _, refreshed = self.request("POST", "/api/currencies/refresh", {})
        self.assertEqual(200, status)
        self.assertEqual("cached", refreshed["catalogue"]["source"])
        self.assertEqual(1, provider.fetch.call_count)
        before = self.request("GET", "/api/app")[2]
        provider.fetch.side_effect = ValueError("bad response")
        self.assertEqual(502, self.request("POST", "/api/currencies/refresh", {})[0])
        self.assertEqual(before, self.request("GET", "/api/app")[2])

    def test_unavailable_reference_pair_is_422_without_writes(self):
        provider = Mock()
        provider.fetch.side_effect = RateUnavailable("unavailable")
        self.service.provider_factory = lambda: provider
        self.service.history_factory = lambda: provider
        for route, body in (
            ("/api/refresh", {"base": "SGD", "counter": "THB", "mode": "reference"}),
            ("/api/history", {"base": "SGD", "counter": "THB"}),
        ):
            status, _, payload = self.request("POST", route, body)
            self.assertEqual(422, status)
            self.assertIn("pair", payload["error"])
        with Store(self.db, readonly=True) as store:
            self.assertEqual(
                {"quotes": 0, "decisions": 0, "outbox": 0, "notifications": 0}, store.counts()
            )

    def test_failure_responses_are_safe_and_dispatch_failure_is_non_success(self):
        with patch.object(
            self.service, "dashboard", side_effect=sqlite3.OperationalError("private-path.sqlite")
        ):
            status, _, payload = self.request("GET", "/api/dashboard")
        self.assertEqual(503, status)
        self.assertNotIn("private-path", payload["error"])
        self.service.create_rule(
            {
                "name": "Failure test",
                "base": "SGD",
                "counter": "THB",
                "mode": "demo",
                "direction": "at_or_above",
                "threshold": "1",
                "cooldown_seconds": 0,
            }
        )
        with Store(self.db) as store:
            store.connection.execute(
                "CREATE TRIGGER injected BEFORE INSERT ON notifications BEGIN SELECT RAISE(ABORT,'private detail'); END"
            )
        status, _, payload = self.request(
            "POST", "/api/refresh", {"base": "SGD", "counter": "THB", "mode": "demo"}
        )
        self.assertEqual(500, status)
        self.assertIn("Observation recorded", payload["error"])
        self.assertNotIn("private detail", payload["error"])


if __name__ == "__main__":
    unittest.main()
