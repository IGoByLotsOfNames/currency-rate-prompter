import json
import unittest
from decimal import Decimal
from io import BytesIO
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from currency_prompter.domain import FakeClock, utc
from currency_prompter.history_provider import MAX_HISTORY_BYTES, FrankfurterHistory


class HistoryProviderTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock(utc("2026-10-03T12:00:00Z"))

    def row(self, day="2026-10-02", **kwargs):
        return dict(dict(date=day, base="SGD", quote="THB", rate="25.123456789123456789"), **kwargs)

    def provider(self, rows):
        return FrankfurterHistory(transport=lambda *a, **k: BytesIO(json.dumps(rows).encode()))

    def test_exact_values_sorted_and_receipt_after_read(self):
        def transport(request, timeout):
            query = parse_qs(urlsplit(request.full_url).query)
            self.assertEqual(
                query,
                {"base": ["sgd"], "quotes": ["thb"], "from": ["2026-07-06"], "to": ["2026-10-03"]},
            )
            self.clock.advance(5)
            return BytesIO(
                b'[{"date":"2026-10-02","base":"SGD","quote":"THB","rate":25.123456789123456789},{"date":"2026-10-01","base":"SGD","quote":"THB","rate":25}]'
            )

        quotes = FrankfurterHistory(transport=transport).fetch("SGD", "THB", clock=self.clock)
        self.assertEqual(quotes[0].observed_at, utc("2026-10-01T00:00Z"))
        self.assertEqual(quotes[1].value, Decimal("25.123456789123456789"))
        self.assertEqual(quotes[1].received_at, utc("2026-10-03T12:00:05Z"))

    def test_range_pair_shape_and_duplicate_validation(self):
        invalid = [
            [],
            {},
            [self.row(), self.row()],
            [self.row("2026-01-01")],
            [self.row("2026-10-04")],
            [self.row(base="USD")],
            [self.row(rate="NaN")],
        ]
        for rows in invalid:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.provider(rows).fetch("SGD", "THB", clock=self.clock)

    def test_input_validation_before_transport(self):
        def transport(*args, **kwargs):
            raise AssertionError("transport must not be used")

        provider = FrankfurterHistory(transport=transport)
        for days in (0, 91, True, "30"):
            with self.subTest(days=days), self.assertRaises(ValueError):
                provider.fetch("SGD", "THB", days=days, clock=self.clock)
        with self.assertRaises(ValueError):
            provider.fetch("SGD", "SGD", clock=self.clock)

    def test_oversized_and_invalid_json_not_retried(self):
        for raw in (b"x" * (MAX_HISTORY_BYTES + 1), b"{", b"[" * 2000):
            calls = []

            def transport(*args, **kwargs):
                calls.append(1)
                return BytesIO(raw)

            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                FrankfurterHistory(transport=transport).fetch("SGD", "THB", clock=self.clock)
            self.assertEqual(len(calls), 1)

    def test_network_retry_is_bounded(self):
        calls, sleeps = [], []

        def transport(*args, **kwargs):
            calls.append(1)
            raise URLError("synthetic offline")

        with self.assertRaises(ValueError):
            FrankfurterHistory(transport=transport, sleep=sleeps.append).fetch(
                "SGD", "THB", clock=self.clock
            )
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.25, 0.5])
