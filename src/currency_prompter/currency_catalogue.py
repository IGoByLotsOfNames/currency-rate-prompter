"""Offline currency catalogue plus an explicitly invoked, bounded provider refresh.

The default endpoint supplies active codes. Its end_date is the latest available
observation, not a retirement flag or a guarantee of pair/date availability.
"""

import json
import time
import unicodedata
from datetime import date
from importlib.resources import files
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .domain import SystemClock, currency, timestamp

CATALOGUE_URL = "https://api.frankfurter.dev/v2/currencies"
MAX_RESPONSE = 128_000
MAX_CURRENCIES = 512
REQUIRED_FIELDS = {"iso_code", "iso_numeric", "name", "symbol", "start_date", "end_date"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("catalogue contains duplicate JSON fields")
        result[key] = value
    return result


def _finite_json(value):
    raise ValueError("catalogue JSON contains a non-finite number")


def decode_json(raw):
    if not isinstance(raw, (bytes, str)) or len(raw) > MAX_RESPONSE:
        raise ValueError("catalogue response exceeds its size bound")
    try:
        return json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_finite_json)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ValueError("catalogue must be valid bounded JSON with unique fields") from error


def _text(value, label, maximum, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or value != value.strip()
        or (not empty and not value)
        or any(unicodedata.category(char).startswith("C") for char in value)
    ):
        raise ValueError(f"catalogue {label} is invalid")
    return value


def validate_rows(rows):
    """Validate the whole batch before caching; never infer activity from end_date."""
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_CURRENCIES:
        raise ValueError("catalogue must contain 1-512 currencies")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict) or not REQUIRED_FIELDS <= row.keys():
            raise ValueError("catalogue row is missing required provider fields")
        code = currency(row["iso_code"])
        if code in seen:
            raise ValueError("catalogue contains a duplicate currency code")
        seen.add(code)
        name = _text(row["name"], "name", 160)
        numeric = row["iso_numeric"]
        if numeric not in (None, "") and (
            not isinstance(numeric, str)
            or len(numeric) != 3
            or not numeric.isascii()
            or not numeric.isdigit()
        ):
            raise ValueError("catalogue numeric code is invalid")
        symbol = row["symbol"]
        if symbol is not None:
            _text(symbol, "symbol", 32, empty=True)
        dates = []
        for key in ("start_date", "end_date"):
            value = row[key]
            if not isinstance(value, str) or len(value) != 10:
                raise ValueError("catalogue dates must be YYYY-MM-DD")
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError("catalogue dates must be YYYY-MM-DD")
            dates.append(parsed)
        if dates[0] > dates[1]:
            raise ValueError("catalogue date range is reversed")
        result.append(
            {
                "iso_code": code,
                "iso_numeric": numeric,
                "name": name,
                "symbol": symbol,
                "start_date": dates[0].isoformat(),
                "end_date": dates[1].isoformat(),
            }
        )
    return sorted(result, key=lambda row: row["iso_code"])


def make_snapshot(rows, as_of):
    snapshot = {
        "schema_version": 1,
        "as_of": timestamp(as_of),
        "url": CATALOGUE_URL,
        "scope": "active",
        "currencies": validate_rows(rows),
    }
    if len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("catalogue snapshot exceeds its size bound")
    return snapshot


def validate_snapshot(value):
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("scope") != "active"
        or value.get("url") != CATALOGUE_URL
        or not isinstance(value.get("as_of"), str)
    ):
        raise ValueError("catalogue snapshot metadata is invalid")
    return make_snapshot(value.get("currencies"), value["as_of"])


def load_bundled():
    return validate_snapshot(
        decode_json(files("currency_prompter").joinpath("data", "currencies.json").read_bytes())
    )


def public_catalogue(snapshot, source):
    rows = [{"code": row["iso_code"], "name": row["name"]} for row in snapshot["currencies"]]
    return {
        "currencies": rows,
        "catalogue": {
            "count": len(rows),
            "as_of": snapshot["as_of"],
            "source": source,
            "scope": "active",
            "url": CATALOGUE_URL,
            "note": "Active codes listed by the provider; availability of each pair and date is not guaranteed. end_date records the latest available observation.",
        },
    }


class FrankfurterCatalogue:
    def __init__(self, *, timeout=10, retries=2, transport=urlopen, sleep=time.sleep):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError("timeout must be between 0 and 30 seconds")
        if type(retries) is not int or not 0 <= retries <= 3:
            raise ValueError("retries must be 0-3")
        self.timeout, self.retries, self.transport, self.sleep = timeout, retries, transport, sleep

    def fetch(self, *, clock=None):
        clock = clock if clock is not None else SystemClock()
        request = Request(
            CATALOGUE_URL,
            headers={"Accept": "application/json", "User-Agent": "CurrencyRatePrompter/1.2"},
        )
        for attempt in range(self.retries + 1):
            try:
                with self.transport(request, timeout=self.timeout) as response:
                    raw = response.read(MAX_RESPONSE + 1)
                    received_at = clock.now()
                return make_snapshot(decode_json(raw), received_at)
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise ValueError(f"catalogue provider returned HTTP {error.code}") from error
            except (URLError, TimeoutError, OSError) as error:
                if attempt == self.retries:
                    raise ValueError("catalogue provider is unreachable or timed out") from error
            self.sleep(0.25 * 2**attempt)
        raise AssertionError("unreachable")
