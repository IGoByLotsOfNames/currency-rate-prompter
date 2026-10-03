import unittest
from datetime import datetime, timedelta
from decimal import Decimal, localcontext

from currency_prompter.domain import Quote, Rule, currency, decimal_text, rate, timestamp, utc


class DomainTests(unittest.TestCase):
    def test_exact_decimal(self):
        self.assertEqual(rate("0.1") + rate("0.2"), Decimal("0.3"))

    def test_serialization_ignores_ambient_precision(self):
        value = rate("1.23456789012345678901")
        config = dict(
            name="test",
            base="SGD",
            counter="THB",
            source="demo",
            direction="at_or_above",
            threshold=value,
        )
        original = Rule(**config).key
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(decimal_text(value), "1.23456789012345678901")
            self.assertEqual(Rule(**config).key, original)

    def test_out_of_range_utc_offset_rejected(self):
        with self.assertRaises(ValueError):
            utc("0001-01-01T00:00:00+14:00")

    def test_bad_rates(self):
        for value in (
            0.1,
            1,
            True,
            None,
            "NaN",
            "Infinity",
            "-1",
            "0",
            "1e1000",
            "1e-999",
            "x",
            "1" * 100,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                rate(value)

    def test_overprecise_rate(self):
        with self.assertRaises(ValueError):
            rate("1.123456789012345678901234567890")

    def test_utc_conversion(self):
        self.assertEqual(timestamp(utc("2026-01-01T08:00:00+08:00")), "2026-01-01T00:00:00.000000Z")

    def test_naive_or_invalid_time(self):
        for value in ("2026-01-01", "wrong", datetime(2026, 1, 1), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                utc(value)

    def test_currency_validation(self):
        for value in ("sgd", "ABCD", "A_B", "１２３", "S'G", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                currency(value)

    def test_quote_round_trip(self):
        quote = Quote(
            "SGD",
            "THB",
            Decimal("25.1000"),
            utc("2026-01-01T00:00Z"),
            utc("2026-01-01T00:00Z"),
            "demo",
        )
        self.assertEqual(quote, Quote.from_dict(quote.to_dict()))
        other = Quote(
            "SGD",
            "THB",
            Decimal("25.1"),
            quote.observed_at,
            quote.received_at + timedelta(hours=1),
            "demo",
        )
        self.assertEqual(quote.key, other.key)

    def test_quote_future_observation(self):
        with self.assertRaises(ValueError):
            Quote(
                "SGD",
                "THB",
                Decimal(25),
                utc("2026-01-02T00:00Z"),
                utc("2026-01-01T00:00Z"),
                "demo",
            )

    def test_quote_schema(self):
        with self.assertRaises(ValueError):
            Quote.from_dict({"base": "SGD"})

    def test_rule_validation(self):
        for change in (
            {"cooldown_seconds": -1},
            {"max_age_seconds": True},
            {"direction": "buy"},
            {"source": "<script>"},
            {"counter": "SGD"},
        ):
            config = dict(
                name="test",
                base="SGD",
                counter="THB",
                source="demo",
                direction="at_or_above",
                threshold="25",
            )
            config.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                Rule(**config)

    def test_rule_key_changes_with_semantics(self):
        config = dict(
            name="test",
            base="SGD",
            counter="THB",
            source="demo",
            direction="at_or_above",
            threshold="25",
        )
        self.assertEqual(Rule(**config).key, Rule(**dict(config, threshold="25.0")).key)
        self.assertNotEqual(Rule(**config).key, Rule(**dict(config, threshold="26")).key)
