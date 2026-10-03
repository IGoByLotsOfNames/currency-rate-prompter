"""Pure validation and value objects. Rates never pass through binary floats."""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

UTC = timezone.utc


def utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be ISO 8601 with a UTC offset") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    try:
        return value.astimezone(UTC)
    except (OverflowError, ValueError) as exc:
        raise ValueError("timestamp is outside the supported UTC range") from exc


def timestamp(value: datetime) -> str:
    return utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def rate(value: str | Decimal) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("rate must be a decimal string or Decimal, never a float")
    if len(str(value)) > 64:
        raise ValueError("rate representation is too long")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal rate") from exc
    if not number.is_finite() or not Decimal("1e-18") <= number <= Decimal("1e18"):
        raise ValueError("rate must be finite and between 1e-18 and 1e18")
    if len(number.as_tuple().digits) > 28:
        raise ValueError("rate must have at most 28 significant digits")
    return number


def decimal_text(value: Decimal) -> str:
    # Decimal.normalize() obeys the ambient precision and can silently round.
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def currency(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("currency must be a three-letter uppercase ASCII code")
    return value


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError(
            "identifier must contain 1-64 letters, numbers, dots, underscores or hyphens"
        )
    return value


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Quote:
    base: str
    counter: str
    value: Decimal
    observed_at: datetime
    received_at: datetime
    source: str

    def __post_init__(self):
        currency(self.base)
        currency(self.counter)
        if self.base == self.counter:
            raise ValueError("base and counter currency must differ")
        object.__setattr__(self, "value", rate(self.value))
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        object.__setattr__(self, "received_at", utc(self.received_at))
        identifier(self.source)
        if self.observed_at > self.received_at:
            raise ValueError("quote observation cannot be later than receipt")

    @property
    def key(self) -> str:
        return digest({k: v for k, v in self.to_dict().items() if k not in ("rate", "received_at")})

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "counter": self.counter,
            "rate": decimal_text(self.value),
            "observed_at": timestamp(self.observed_at),
            "received_at": timestamp(self.received_at),
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, value: dict):
        if not isinstance(value, dict) or set(value) != {
            "base",
            "counter",
            "rate",
            "observed_at",
            "received_at",
            "source",
        }:
            raise ValueError(
                "quote requires exactly base, counter, rate, observed_at, received_at and source"
            )
        return cls(
            value["base"],
            value["counter"],
            rate(value["rate"]),
            utc(value["observed_at"]),
            utc(value["received_at"]),
            value["source"],
        )


@dataclass(frozen=True)
class Rule:
    name: str
    base: str
    counter: str
    source: str
    direction: str
    threshold: Decimal
    cooldown_seconds: int = 86400
    max_age_seconds: int = 345600

    def __post_init__(self):
        identifier(self.name)
        currency(self.base)
        currency(self.counter)
        identifier(self.source)
        if self.base == self.counter:
            raise ValueError("rule currencies must differ")
        if self.direction not in ("at_or_above", "at_or_below"):
            raise ValueError("direction must be at_or_above or at_or_below")
        object.__setattr__(self, "threshold", rate(self.threshold))
        for label in ("cooldown_seconds", "max_age_seconds"):
            value = getattr(self, label)
            if type(value) is not int or not 0 <= value <= 31536000:
                raise ValueError(f"{label} must be an integer from 0 to 31536000")

    @property
    def key(self):
        return digest(self.to_dict())

    def to_dict(self):
        return {
            "name": self.name,
            "base": self.base,
            "counter": self.counter,
            "source": self.source,
            "direction": self.direction,
            "threshold": decimal_text(self.threshold),
            "cooldown_seconds": self.cooldown_seconds,
            "max_age_seconds": self.max_age_seconds,
        }

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise ValueError("rule must be an object")
        try:
            return cls(**value)
        except TypeError as exc:
            raise ValueError("rule has missing or unexpected fields") from exc

    def matches(self, quote):
        return (self.base, self.counter, self.source) == (quote.base, quote.counter, quote.source)

    def qualifies(self, quote):
        return (
            quote.value >= self.threshold
            if self.direction == "at_or_above"
            else quote.value <= self.threshold
        )


@dataclass
class FakeClock:
    current: datetime

    def now(self):
        return utc(self.current)

    def advance(self, seconds):
        self.current = self.now() + timedelta(seconds=seconds)


class SystemClock:
    def now(self):
        return datetime.now(UTC)
