"""Bounded numeric and provider failure regressions for the expanded catalogue."""

import tempfile
import threading
import unittest
from decimal import Decimal, localcontext
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from currency_prompter.domain import FakeClock, Quote, Rule, decimal_text, rate, utc
from currency_prompter.history_provider import FrankfurterHistory
from currency_prompter.provider import Frankfurter, RateUnavailable
from currency_prompter.server import make_server
from currency_prompter.storage import Store


class ExpandedRateTests(unittest.TestCase):
    def test_extreme_cross_rates_round_trip_without_context_rounding(self):
        # Constructed extremes exercise the wider range; these are not live market quotes.
        for value in ("1e-18", "0.000000000184", "5427436842.105263157894736842", "1e18"):
            with self.subTest(value=value), localcontext() as context:
                context.prec = 6
                q = Quote(
                    "XAU",
                    "IRR",
                    rate(value),
                    utc("2026-10-02T00:00Z"),
                    utc("2026-10-03T00:00Z"),
                    "test",
                )
                self.assertEqual(Quote.from_dict(q.to_dict()), q)
                self.assertEqual(Decimal(decimal_text(q.value)), Decimal(value))
                rule = Rule("extreme", q.base, q.counter, q.source, "at_or_above", q.value)
                self.assertTrue(rule.qualifies(q))
                self.assertEqual(Rule.from_dict(rule.to_dict()).key, rule.key)

    def test_rate_bounds_are_inclusive_but_not_unbounded(self):
        for value in ("9.999e-19", "1.000000000000000001e18", "1e99999", "1e-99999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rate(value)


class ProviderFailureTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(utc("2026-10-03T12:00Z"))

    def test_unavailable_pair_has_typed_error_without_retry(self):
        for factory in (Frankfurter, FrankfurterHistory):
            for status in (404, 422):
                calls, sleeps = [], []

                def transport(request, **kwargs):
                    calls.append(request.full_url)
                    raise HTTPError(request.full_url, status, "unavailable", {}, None)

                provider = factory(transport=transport, sleep=sleeps.append)
                with (
                    self.subTest(factory=factory, status=status),
                    self.assertRaisesRegex(RateUnavailable, "XAU/IRR"),
                ):
                    provider.fetch("XAU", "IRR", clock=self.clock)
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, [])

    def test_empty_history_is_unavailable_not_malformed(self):
        provider = FrankfurterHistory(transport=lambda *args, **kwargs: BytesIO(b"[]"))
        with self.assertRaises(RateUnavailable):
            provider.fetch("XAU", "IRR", clock=self.clock)

    def test_transient_status_retry_and_success(self):
        for status in (429, 500, 502, 503, 504):
            calls, sleeps = [], []

            def transport(request, **kwargs):
                calls.append(request.full_url)
                if len(calls) < 3:
                    raise HTTPError(request.full_url, status, "temporary", {}, None)
                return BytesIO(
                    b'{"base":"XAU","quote":"IRR","rate":5427436842.105263157894736842,"date":"2026-10-02"}'
                )

            with self.subTest(status=status):
                quote = Frankfurter(transport=transport, sleep=sleeps.append).fetch(
                    "XAU", "IRR", clock=self.clock
                )
                self.assertEqual(quote.value, Decimal("5427436842.105263157894736842"))
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleeps, [0.25, 0.5])

    def test_nested_json_is_normalized_without_retry(self):
        for factory in (Frankfurter, FrankfurterHistory):
            calls = []

            def transport(*args, **kwargs):
                calls.append(1)
                return BytesIO(b"[" * 2000 + b"]" * 2000)

            with self.subTest(factory=factory), self.assertRaises(ValueError):
                factory(transport=transport).fetch("SGD", "THB", clock=self.clock)
            self.assertEqual(len(calls), 1)

    def test_boolean_nan_and_infinite_timeouts_rejected(self):
        for factory in (Frankfurter, FrankfurterHistory):
            for value in (True, False, float("nan"), float("inf"), -1, 0, 31, "10"):
                with self.subTest(factory=factory, timeout=value), self.assertRaises(ValueError):
                    factory(timeout=value)

    def test_parser_recursion_failure_is_normalized(self):
        for factory in (Frankfurter, FrankfurterHistory):
            with (
                self.subTest(factory=factory),
                patch("currency_prompter.provider.json.loads", side_effect=RecursionError),
                self.assertRaisesRegex(ValueError, "invalid JSON"),
            ):
                factory(transport=lambda *args, **kwargs: BytesIO(b"{}")).fetch(
                    "SGD", "THB", clock=self.clock
                )


class ReadonlyRejectionTests(unittest.TestCase):
    def test_body_bearing_mutations_are_rejected_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "readonly.sqlite"
            with Store(db) as store:
                before = store.counts()
            server = make_server(db, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for method in ("POST", "PUT", "PATCH", "DELETE"):
                    request = Request(
                        f"http://127.0.0.1:{server.server_port}/api/quotes",
                        data=b'{"rate":"25"}',
                        method=method,
                    )
                    with self.subTest(method=method), self.assertRaises(HTTPError) as error:
                        urlopen(request, timeout=3)
                    self.assertEqual(error.exception.code, 501)
                    self.assertIn(b"read-only", error.exception.read())
                    error.exception.close()
                with Store(db) as store:
                    self.assertEqual(store.counts(), before)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
