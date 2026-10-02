"""Opt-in Frankfurter daily reference rates; bounded reads and injectable transport."""

from datetime import date, datetime
from decimal import Decimal
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .domain import Quote, UTC, currency, rate, utc

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


class Frankfurter:
    def __init__(self, *, timeout=10, retries=2, transport=urlopen, sleep=time.sleep):
        if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be 0-3")
        self.timeout, self.retries = timeout, retries
        self.transport, self.sleep = transport, sleep

    def fetch(self, base, counter, received_at):
        currency(base)
        currency(counter)
        if base == counter:
            raise ValueError("currencies must differ")
        url = f"https://api.frankfurter.dev/v2/rate/{base.lower()}/{counter.lower()}"
        request = Request(url, headers={"Accept": "application/json", "User-Agent": "CurrencyRatePrompter/1.0"})
        for attempt in range(self.retries + 1):
            try:
                with self.transport(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                    if len(raw) > MAX_RESPONSE:
                        raise ValueError("provider response exceeds 64 KB")
                    body = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
                    return parse_response(body, base, counter, received_at)
            except HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ValueError(f"rate provider returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise ValueError("rate provider is unreachable or timed out") from exc
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ValueError("provider returned invalid JSON") from exc
            self.sleep(0.25 * 2**attempt)
        raise AssertionError("unreachable")
