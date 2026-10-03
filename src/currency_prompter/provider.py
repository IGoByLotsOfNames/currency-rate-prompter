"""Opt-in Frankfurter daily reference rates; bounded reads and injectable transport."""

import json
import time
from datetime import date, datetime
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .domain import UTC, Quote, SystemClock, currency, rate, utc

MAX_RESPONSE = 64_000


def parse_response(data, base, counter, received_at):
    if not isinstance(data, dict):
        raise ValueError("provider response must be an object")
    if not isinstance(data.get("base"), str) or not isinstance(data.get("quote"), str):
        raise ValueError("provider currency codes must be strings")
    if data["base"].upper() != base or data["quote"].upper() != counter:
        raise ValueError("provider returned the wrong currency pair")
    date_text = data.get("date")
    if not isinstance(date_text, str) or len(date_text) != 10:
        raise ValueError("provider date must be YYYY-MM-DD")
    try:
        day = date.fromisoformat(date_text)
    except ValueError as exc:
        raise ValueError("invalid provider date") from exc
    # The provider supplies a calendar date, not an intraday timestamp.
    observed = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return Quote(base, counter, rate(data.get("rate")), observed, utc(received_at), "frankfurter")


class RateUnavailable(ValueError):
    """The provider has no rate for the requested pair or date range."""


class Frankfurter:
    def __init__(self, *, timeout=10, retries=2, transport=urlopen, sleep=time.sleep):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be 0-3")
        self.timeout, self.retries = timeout, retries
        self.transport, self.sleep = transport, sleep

    def fetch(self, base, counter, received_at=None, *, clock=None):
        """Stamp successful receipt after reading; fixed time supports historical callers."""
        if received_at is not None and clock is not None:
            raise ValueError("use a receipt clock or a fixed received_at, not both")
        fixed_receipt = utc(received_at) if received_at is not None else None
        clock = clock if clock is not None else SystemClock()
        currency(base)
        currency(counter)
        if base == counter:
            raise ValueError("currencies must differ")
        url = f"https://api.frankfurter.dev/v2/rate/{base.lower()}/{counter.lower()}"
        request = Request(
            url, headers={"Accept": "application/json", "User-Agent": "CurrencyRatePrompter/1.2"}
        )
        for attempt in range(self.retries + 1):
            try:
                with self.transport(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("provider response exceeds 64 KB")
                    receipt = fixed_receipt if fixed_receipt is not None else utc(clock.now())
                    body = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
                    return parse_response(body, base, counter, receipt)
            except HTTPError as exc:
                if exc.code in (404, 422):
                    raise RateUnavailable(
                        f"No reference rate is available for {base}/{counter}. "
                        "Try another pair; your saved data is unchanged."
                    ) from exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ValueError(f"rate provider returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise ValueError("rate provider is unreachable or timed out") from exc
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
                raise ValueError("provider returned invalid JSON") from exc
            self.sleep(0.25 * 2**attempt)
        raise AssertionError("unreachable")
