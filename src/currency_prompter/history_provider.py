"""Bounded, opt-in historical reference-rate reads from Frankfurter v2."""

import json
import time
from datetime import timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .domain import SystemClock, currency, utc
from .provider import RateUnavailable, parse_response

MAX_HISTORY_BYTES = 128_000


class FrankfurterHistory:
    def __init__(self, *, timeout=10, retries=2, transport=urlopen, sleep=time.sleep):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be 0-3")
        self.timeout, self.retries = timeout, retries
        self.transport, self.sleep = transport, sleep

    def fetch(self, base, counter, *, days=90, clock=None):
        currency(base)
        currency(counter)
        if base == counter:
            raise ValueError("currencies must differ")
        if type(days) is not int or not 1 <= days <= 90:
            raise ValueError("history range must be 1-90 days")
        clock = clock or SystemClock()
        end = utc(clock.now()).date()
        start = end - timedelta(days=days - 1)
        query = urlencode(
            dict(
                base=base.lower(),
                quotes=counter.lower(),
                **{"from": start.isoformat(), "to": end.isoformat()},
            )
        )
        request = Request(
            "https://api.frankfurter.dev/v2/rates?" + query,
            headers={"Accept": "application/json", "User-Agent": "CurrencyRatePrompter/1.2"},
        )
        for attempt in range(self.retries + 1):
            try:
                with self.transport(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_HISTORY_BYTES + 1)
                    received_at = utc(clock.now())
                if len(raw) > MAX_HISTORY_BYTES:
                    raise ValueError("history response exceeds 128 KB")
                rows = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
                if rows == []:
                    raise RateUnavailable(
                        f"No reference history is available for {base}/{counter} in this date range. "
                        "Try another pair; your saved data is unchanged."
                    )
                if not isinstance(rows, list) or not 1 <= len(rows) <= days:
                    raise ValueError(
                        "provider must return 1-90 daily observations within the requested range"
                    )
                quotes = [parse_response(row, base, counter, received_at) for row in rows]
                if any(not start <= quote.observed_at.date() <= end for quote in quotes):
                    raise ValueError("provider history is outside the requested dates")
                if len({quote.key for quote in quotes}) != len(quotes):
                    raise ValueError("provider history contains duplicate observation dates")
                return sorted(quotes, key=lambda quote: quote.observed_at)
            except HTTPError as exc:
                if exc.code in (404, 422):
                    raise RateUnavailable(
                        f"No reference history is available for {base}/{counter} in this date range. "
                        "Try another pair; your saved data is unchanged."
                    ) from exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ValueError(f"history provider returned HTTP {exc.code}") from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    raise ValueError("history provider is unreachable or timed out") from exc
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
                raise ValueError("history provider returned invalid JSON") from exc
            self.sleep(0.25 * 2**attempt)
        raise AssertionError("unreachable")
