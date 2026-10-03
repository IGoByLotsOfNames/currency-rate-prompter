"""Local tracker application services; provider access requires an explicit action."""

import csv
import hashlib
import io
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from pathlib import Path

from .currency_catalogue import (
    FrankfurterCatalogue,
    decode_json,
    load_bundled,
    public_catalogue,
    validate_snapshot,
)
from .domain import UTC, FakeClock, Quote, Rule, SystemClock, currency, decimal_text, timestamp, utc
from .monitor import dispatch
from .provider import Frankfurter, RateUnavailable
from .storage import Store

LEGACY_DEMO_CURRENCIES = [
    {"code": code, "name": name}
    for code, name in (
        ("SGD", "Singapore dollar"),
        ("THB", "Thai baht"),
        ("USD", "US dollar"),
        ("EUR", "Euro"),
        ("GBP", "Pound sterling"),
        ("JPY", "Japanese yen"),
        ("MYR", "Malaysian ringgit"),
        ("AUD", "Australian dollar"),
        ("CNY", "Chinese yuan"),
        ("HKD", "Hong Kong dollar"),
        ("IDR", "Indonesian rupiah"),
        ("INR", "Indian rupee"),
        ("KRW", "South Korean won"),
        ("NZD", "New Zealand dollar"),
        ("CAD", "Canadian dollar"),
        ("CHF", "Swiss franc"),
    )
]
SOURCES = {"demo": "synthetic-demo", "reference": "frankfurter"}
DEMO_ANCHOR = datetime(2026, 9, 30, tzinfo=UTC)
# Invented relative units for a useful deterministic fixture, never market rates.
DEMO_UNITS = dict(
    zip(
        (row["code"] for row in LEGACY_DEMO_CURRENCIES),
        map(
            Decimal,
            (
                "1",
                "26",
                "0.75",
                "0.68",
                "0.58",
                "110",
                "3.3",
                "1.1",
                "5.4",
                "5.8",
                "12000",
                "62",
                "990",
                "1.2",
                "1.02",
                "0.66",
            ),
        ),
    )
)
APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_currency_names (code TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS app_watchlist (
 base TEXT NOT NULL, counter TEXT NOT NULL, PRIMARY KEY(base,counter)
);
CREATE TABLE IF NOT EXISTS app_rules (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, base TEXT NOT NULL, counter TEXT NOT NULL,
 source TEXT NOT NULL, direction TEXT NOT NULL, threshold TEXT NOT NULL,
 cooldown_seconds INTEGER NOT NULL, enabled INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS app_refreshes (
 base TEXT NOT NULL, counter TEXT NOT NULL, mode TEXT NOT NULL, refreshed_at TEXT NOT NULL,
 PRIMARY KEY(base,counter,mode)
);
CREATE INDEX IF NOT EXISTS app_quote_pair_source_time ON quotes(base,counter,source,observed_at);
CREATE INDEX IF NOT EXISTS app_pending_events ON outbox(created_at,id) WHERE delivered_at IS NULL;
"""


class AppError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def fields(body, required, optional=()):
    if (
        not isinstance(body, dict)
        or not set(required) <= set(body)
        or set(body) - set(required) - set(optional)
    ):
        raise AppError("Request fields are missing or unexpected.")


def pair(base, counter):
    currency(base)
    currency(counter)
    if base == counter:
        raise AppError("Base and counter currencies must differ.")
    return base, counter


def source_for(mode):
    if not isinstance(mode, str) or mode not in SOURCES:
        raise AppError("Mode must be demo or reference.")
    return SOURCES[mode]


def number(value, label, maximum):
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AppError(f"{label} must be a decimal string.")
    try:
        result = Decimal(value)
    except InvalidOperation as error:
        raise AppError(f"{label} is not a valid decimal.") from error
    if not result.is_finite() or not 0 <= result <= maximum:
        raise AppError(f"{label} must be between 0 and {maximum}.")
    if len(result.as_tuple().digits) > 28 or result.as_tuple().exponent < -12:
        raise AppError(f"{label} supports at most 28 digits and 12 decimal places.")
    return result


def demo_unit(code):
    if code in DEMO_UNITS:
        return DEMO_UNITS[code]
    # Invented stable units for additional codes; never an estimate of market value.
    return Decimal(
        100 + int(hashlib.sha256(code.encode("ascii")).hexdigest()[:8], 16) % 99901
    ) / Decimal(1000)


def demo_points(base, counter):
    pair(base, counter)
    seed = int(hashlib.sha256(f"{base}/{counter}".encode()).hexdigest()[:8], 16)
    result = []
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = 60
        initial = demo_unit(counter) / demo_unit(base)
        for index in range(90):
            variation = Decimal(((index * 17 + seed) % 101) - 50) / Decimal(5000)
            value = (initial * (1 + variation)).quantize(Decimal("0.00000001"))
            when = timestamp(DEMO_ANCHOR - timedelta(days=89 - index))
            result.append({"rate": decimal_text(value), "observed_at": when, "received_at": when})
    return result


def change_percent(current, previous):
    if current is None or previous is None:
        return None
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)) as context:
        context.prec = 60
        value = ((Decimal(current) / Decimal(previous) - 1) * 100).quantize(Decimal("0.000001"))
        return decimal_text(value)


def rule_record(row):
    return {
        key: bool(row[key]) if key == "enabled" else row[key]
        for key in (
            "id",
            "name",
            "base",
            "counter",
            "source",
            "direction",
            "threshold",
            "cooldown_seconds",
            "enabled",
        )
    }


def domain_rule(row):
    return Rule(
        "app-" + row["id"],
        row["base"],
        row["counter"],
        row["source"],
        row["direction"],
        row["threshold"],
        row["cooldown_seconds"],
    )


class _DeliveryScope:
    """Journal only pending events for the explicitly refreshed pair and source."""

    def __init__(self, store, base, counter, source):
        self.store, self.identity = store, (base, counter, source)

    def __getattr__(self, name):
        return getattr(self.store, name)

    def pending(self):
        selected = []
        cursor = self.store.connection.execute(
            "SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY created_at,id"
        )
        try:
            for row in cursor:
                quote = json.loads(row["payload"])["quote"]
                if tuple(quote[key] for key in ("base", "counter", "source")) == self.identity:
                    selected.append(row)
                    if len(selected) == 1000:
                        break
        finally:
            cursor.close()
        return selected


class AppService:
    def __init__(
        self,
        db,
        *,
        clock=None,
        provider_factory=Frankfurter,
        history_factory=None,
        catalogue_factory=FrankfurterCatalogue,
    ):
        self.db = Path(db)
        self.clock = clock or SystemClock()
        self.provider_factory = provider_factory
        self.history_factory = history_factory
        self.catalogue_factory = catalogue_factory
        self._bundled = load_bundled()
        self._writes = threading.RLock()
        with Store(self.db) as store:
            con = store.connection
            con.executescript(APP_SCHEMA)
            con.execute("BEGIN IMMEDIATE")
            try:
                if not con.execute("SELECT 1 FROM app_settings WHERE key='initialized'").fetchone():
                    con.executemany(
                        "INSERT OR IGNORE INTO app_watchlist VALUES (?,?)",
                        [("SGD", "THB"), ("USD", "SGD"), ("EUR", "SGD")],
                    )
                    con.execute("INSERT INTO app_settings VALUES ('initialized','1')")
                con.executemany(
                    "INSERT OR IGNORE INTO app_currency_names VALUES (?,?)",
                    [(row["iso_code"], row["name"]) for row in self._bundled["currencies"]],
                )
                con.commit()
            except BaseException:
                con.rollback()
                raise

    def _catalogue(self, store):
        row = store.connection.execute(
            "SELECT value FROM app_settings WHERE key='currency_catalogue'"
        ).fetchone()
        if row:
            try:
                return validate_snapshot(decode_json(row[0])), "cached"
            except ValueError:
                # Corrupt local cache cannot prevent offline use of the bundled list.
                pass
        return self._bundled, "bundled"

    def _saved_codes(self, store):
        return {
            row[0]
            for row in store.connection.execute(
                "SELECT base FROM app_watchlist UNION SELECT counter FROM app_watchlist "
                "UNION SELECT base FROM app_rules UNION SELECT counter FROM app_rules "
                "UNION SELECT base FROM quotes UNION SELECT counter FROM quotes"
            )
        }

    def _pair(self, base, counter):
        pair(base, counter)
        with Store(self.db, readonly=True) as store:
            snapshot, _ = self._catalogue(store)
            codes = {row["iso_code"] for row in snapshot["currencies"]}
            if base not in codes or counter not in codes:
                codes |= self._saved_codes(store)
            if base not in codes or counter not in codes:
                raise AppError("Choose currencies from the provider catalogue or your saved data.")
        return base, counter

    def bootstrap(self):
        with Store(self.db, readonly=True) as store:
            watched = [
                dict(row)
                for row in store.connection.execute(
                    "SELECT base,counter FROM app_watchlist ORDER BY rowid"
                )
            ]
            snapshot, source = self._catalogue(store)
            current = {row["iso_code"] for row in snapshot["currencies"]}
            names = dict(store.connection.execute("SELECT code,name FROM app_currency_names"))
            saved = [
                {"code": code, "name": names.get(code, code)}
                for code in sorted(self._saved_codes(store) - current)
            ]
        return {
            **public_catalogue(snapshot, source),
            "saved_currencies": saved,
            "watchlist": watched,
            "mode": "demo",
            "provider": {"name": "Frankfurter", "url": "https://frankfurter.dev/"},
        }

    def refresh_currencies(self, body):
        fields(body, ())
        try:
            snapshot = validate_snapshot(self.catalogue_factory().fetch(clock=self.clock))
            if utc(snapshot["as_of"]) > utc(self.clock.now()):
                raise ValueError("catalogue receipt is in the future")
        except (ValueError, OSError) as error:
            raise AppError(
                "Currency catalogue refresh failed. The last good catalogue and all saved data remain available.",
                502,
            ) from error
        with self._writes, Store(self.db) as store, store.connection:
            previous, previous_source = self._catalogue(store)
            if previous_source == "cached" and utc(snapshot["as_of"]) < utc(previous["as_of"]):
                raise AppError(
                    "An older catalogue response was ignored. The newer saved catalogue remains available.",
                    409,
                )
            store.connection.execute(
                "INSERT OR REPLACE INTO app_settings VALUES ('currency_catalogue',?)",
                (json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),),
            )
            store.connection.executemany(
                "INSERT OR REPLACE INTO app_currency_names VALUES (?,?)",
                [(row["iso_code"], row["name"]) for row in snapshot["currencies"]],
            )
        updated = self.bootstrap()
        return {
            "ok": True,
            **{key: updated[key] for key in ("currencies", "catalogue", "saved_currencies")},
        }

    def set_watchlist(self, body, *, remove=False):
        fields(body, ("base", "counter"))
        base, counter = self._pair(body["base"], body["counter"])
        with self._writes, Store(self.db) as store, store.connection:
            con = store.connection
            if remove:
                con.execute("DELETE FROM app_watchlist WHERE base=? AND counter=?", (base, counter))
            else:
                exists = con.execute(
                    "SELECT 1 FROM app_watchlist WHERE base=? AND counter=?", (base, counter)
                ).fetchone()
                if (
                    not exists
                    and con.execute("SELECT COUNT(*) FROM app_watchlist").fetchone()[0] >= 20
                ):
                    raise AppError("The watchlist supports at most 20 pairs.")
                con.execute("INSERT OR IGNORE INTO app_watchlist VALUES (?,?)", (base, counter))
        return {"ok": True, "watchlist": self.bootstrap()["watchlist"]}

    def _points(self, store, base, counter, mode):
        if mode == "demo":
            return demo_points(base, counter)
        rows = store.connection.execute(
            "SELECT rate,observed_at,received_at FROM quotes WHERE base=? AND counter=? AND source=? ORDER BY observed_at DESC,id LIMIT 10000",
            (base, counter, source_for(mode)),
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def dashboard(self, base="SGD", counter="THB", mode="demo", days=30):
        base, counter = self._pair(base, counter)
        source = source_for(mode)
        if type(days) is not int or not 1 <= days <= 365:
            raise AppError("Chart range must be between 1 and 365 days.")
        with Store(self.db, readonly=True) as store:
            points = self._points(store, base, counter, mode)
            snapshot, _ = self._catalogue(store)
            current_codes = {row["iso_code"] for row in snapshot["currencies"]}
            catalogue_available = base in current_codes and counter in current_codes
            rules = [
                rule_record(row)
                for row in store.connection.execute(
                    "SELECT * FROM app_rules WHERE base=? AND counter=? AND source=? AND deleted=0 ORDER BY rowid",
                    (base, counter, source),
                )
            ]
            names = {
                "app-" + row["id"]: row["name"]
                for row in store.connection.execute("SELECT id,name FROM app_rules")
            }
            alerts = []
            cursor = store.connection.execute(
                "SELECT payload,delivered_at FROM outbox ORDER BY created_at DESC,id"
            )
            try:
                for row in cursor:
                    event = json.loads(row["payload"])
                    if tuple(event["quote"][key] for key in ("base", "counter", "source")) == (
                        base,
                        counter,
                        source,
                    ):
                        alerts.append(
                            {
                                "id": event["id"],
                                "rule": names.get(event["rule"]["name"], event["rule"]["name"]),
                                "rate": event["quote"]["rate"],
                                "queued_at": event["queued_at"],
                                "delivered_at": row["delivered_at"],
                            }
                        )
                        if len(alerts) == 1000:
                            break
            finally:
                cursor.close()
            refresh = store.connection.execute(
                "SELECT refreshed_at FROM app_refreshes WHERE base=? AND counter=? AND mode=?",
                (base, counter, mode),
            ).fetchone()
        anchor = DEMO_ANCHOR if mode == "demo" else utc(self.clock.now())
        points = [
            row
            for row in points
            if utc(row["observed_at"]) <= anchor and utc(row["received_at"]) <= anchor
        ]
        cutoff = anchor.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=days - 1
        )
        visible = [row for row in points if cutoff <= utc(row["observed_at"]) <= anchor]
        latest = points[-1] if points else None
        previous = points[-2]["rate"] if len(points) > 1 else None
        values = [Decimal(row["rate"]) for row in visible]
        note = (
            "Deterministic synthetic examples ending 30 September 2026; not market history. Refresh evaluates the latest example locally."
            if mode == "demo"
            else "Daily reference rates, not executable bank quotes. History shows collected or explicitly imported observations, up to the latest 10,000. Fees and spreads are not included."
        )
        if not catalogue_available:
            note += " This saved pair includes a code absent from the current provider catalogue; saved data are retained and new provider observations may be unavailable."
        return {
            "pair": {"base": base, "counter": counter},
            "mode": mode,
            "source": source,
            "catalogue_available": catalogue_available,
            "latest": latest,
            "previous_rate": previous,
            "change_percent": change_percent(latest["rate"] if latest else None, previous),
            "low": decimal_text(min(values)) if values else None,
            "high": decimal_text(max(values)) if values else None,
            "points": [{"observed_at": row["observed_at"], "rate": row["rate"]} for row in visible],
            "rules": rules,
            "alerts": alerts,
            "last_refresh": refresh[0] if refresh else None,
            "stale": latest is None or anchor - utc(latest["observed_at"]) > timedelta(days=4),
            "note": note,
        }

    def watchlist(self, mode="demo"):
        source_for(mode)
        result = []
        for item in self.bootstrap()["watchlist"]:
            data = self.dashboard(item["base"], item["counter"], mode)
            latest = data["latest"]
            result.append(
                {
                    **item,
                    "rate": latest["rate"] if latest else None,
                    "change_percent": data["change_percent"],
                    "observed_at": latest["observed_at"] if latest else None,
                }
            )
        return {"watchlist": result}

    def create_rule(self, body):
        fields(
            body, ("name", "base", "counter", "mode", "direction", "threshold", "cooldown_seconds")
        )
        base, counter = self._pair(body["base"], body["counter"])
        source = source_for(body["mode"])
        name = body["name"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or len(name.strip()) > 64
            or any(ord(char) < 32 for char in name)
        ):
            raise AppError("Rule name must contain 1-64 visible characters.")
        record = {
            "id": uuid.uuid4().hex,
            "name": name.strip(),
            "base": base,
            "counter": counter,
            "source": source,
            "direction": body["direction"],
            "threshold": body["threshold"],
            "cooldown_seconds": body["cooldown_seconds"],
            "enabled": True,
        }
        checked = domain_rule(record)
        record["threshold"] = decimal_text(checked.threshold)
        with self._writes, Store(self.db) as store, store.connection:
            if (
                store.connection.execute(
                    "SELECT COUNT(*) FROM app_rules WHERE deleted=0"
                ).fetchone()[0]
                >= 100
            ):
                raise AppError("At most 100 active rule definitions are supported.")
            store.connection.execute(
                "INSERT INTO app_rules VALUES (?,?,?,?,?,?,?,?,?,0)",
                tuple(
                    record[key]
                    for key in (
                        "id",
                        "name",
                        "base",
                        "counter",
                        "source",
                        "direction",
                        "threshold",
                        "cooldown_seconds",
                        "enabled",
                    )
                ),
            )
        return {"ok": True, "rule": record}

    def update_rule(self, body, *, delete=False):
        fields(body, ("id",) if delete else ("id", "enabled"))
        identity = body["id"]
        if (
            not isinstance(identity, str)
            or len(identity) != 32
            or any(char not in "0123456789abcdef" for char in identity)
        ):
            raise AppError("Invalid rule ID.")
        if not delete and type(body["enabled"]) is not bool:
            raise AppError("Enabled must be true or false.")
        with self._writes, Store(self.db) as store, store.connection:
            if not store.connection.execute(
                "SELECT 1 FROM app_rules WHERE id=? AND deleted=0", (identity,)
            ).fetchone():
                raise AppError("Rule was not found.", 404)
            if delete:
                store.connection.execute(
                    "UPDATE app_rules SET enabled=0,deleted=1 WHERE id=?", (identity,)
                )
            else:
                store.connection.execute(
                    "UPDATE app_rules SET enabled=? WHERE id=?", (int(body["enabled"]), identity)
                )
        return {"ok": True, "id": identity}

    def _record_refresh(self, store, base, counter, mode):
        with store.connection:
            store.connection.execute(
                "INSERT OR REPLACE INTO app_refreshes VALUES (?,?,?,?)",
                (base, counter, mode, timestamp(self.clock.now())),
            )

    def refresh(self, body):
        fields(body, ("base", "counter", "mode"))
        base, counter = self._pair(body["base"], body["counter"])
        mode, source = body["mode"], source_for(body["mode"])
        if mode == "demo":
            latest = demo_points(base, counter)[-1]
            quote = Quote(
                base,
                counter,
                latest["rate"],
                utc(latest["observed_at"]),
                utc(latest["received_at"]),
                source,
            )
            decision_clock = FakeClock(quote.received_at)
        else:
            try:
                quote = self.provider_factory().fetch(base, counter, clock=self.clock)
            except RateUnavailable as error:
                raise AppError(
                    "The provider has no reference rate for this pair. Catalogue membership does not guarantee pair availability; saved data are unchanged.",
                    422,
                ) from error
            except (ValueError, OSError) as error:
                raise AppError(
                    "Reference-rate refresh failed. Existing observations are unchanged; try again later.",
                    502,
                ) from error
            if not isinstance(quote, Quote) or (quote.base, quote.counter, quote.source) != (
                base,
                counter,
                source,
            ):
                raise AppError("The provider returned an incompatible observation.", 502)
            decision_clock = self.clock
        with self._writes, Store(self.db) as store:
            rules = [
                domain_rule(row)
                for row in store.connection.execute(
                    "SELECT * FROM app_rules WHERE base=? AND counter=? AND source=? AND enabled=1 AND deleted=0",
                    (base, counter, source),
                )
            ]
            outcome = store.process(quote, rules, decision_clock.now())
            delivery = dispatch(_DeliveryScope(store, base, counter, source), clock=self.clock)
            self._record_refresh(store, base, counter, mode)
            if delivery["failed"]:
                raise AppError(
                    "Observation recorded, but local alert journaling failed. Refresh again to retry pending alerts.",
                    500,
                )
        return {
            "ok": True,
            "mode": mode,
            "quote": quote.to_dict(),
            "processing": outcome,
            "delivery": delivery,
        }

    def import_history(self, body):
        fields(body, ("base", "counter"))
        base, counter = self._pair(body["base"], body["counter"])
        factory = self.history_factory
        if factory is None:
            from .history_provider import FrankfurterHistory

            factory = FrankfurterHistory
        try:
            quotes = factory().fetch(base, counter, days=90, clock=self.clock)
        except RateUnavailable as error:
            raise AppError(
                "The provider has no history for this pair and date range. Saved observations are unchanged.",
                422,
            ) from error
        except (ValueError, OSError) as error:
            raise AppError(
                "Reference history could not be loaded. Existing observations are unchanged.", 502
            ) from error
        if not isinstance(quotes, list) or not 1 <= len(quotes) <= 90:
            raise AppError("The history provider returned an invalid batch.", 502)
        expected = {}
        today = utc(self.clock.now()).date()
        first_day = today - timedelta(days=89)
        for quote in quotes:
            if (
                not isinstance(quote, Quote)
                or (quote.base, quote.counter, quote.source) != (base, counter, "frankfurter")
                or quote.received_at > utc(self.clock.now())
            ):
                raise AppError("The history provider returned an incompatible observation.", 502)
            if not first_day <= quote.observed_at.date() <= today:
                raise AppError("History must fall within the requested last 90 days.", 502)
            value = quote.to_dict()["rate"]
            if quote.key in expected and expected[quote.key] != value:
                raise AppError(
                    "Historical observations contain a conflicting revision; nothing was imported.",
                    409,
                )
            expected[quote.key] = value
        imported = 0
        with self._writes, Store(self.db) as store:
            for identity, value in expected.items():
                existing = store.connection.execute(
                    "SELECT rate FROM quotes WHERE id=?", (identity,)
                ).fetchone()
                if existing and existing[0] != value:
                    raise AppError(
                        "Historical observations conflict with recorded rates; nothing was imported.",
                        409,
                    )
            try:
                for quote in sorted(quotes, key=lambda item: item.observed_at):
                    imported += int(store.process(quote, [], quote.received_at)["inserted"])
            except (ValueError, OSError, sqlite3.Error) as error:
                raise AppError(
                    f"History import stopped after {imported} new observations; earlier records remain saved.",
                    409,
                ) from error
            self._record_refresh(store, base, counter, "reference")
        return {"ok": True, "mode": "reference", "imported": imported, "historical_alerts": 0}

    def convert(self, body):
        fields(body, ("base", "counter", "mode", "amount", "direction"), ("fee_percent",))
        base, counter = self._pair(body["base"], body["counter"])
        mode = body["mode"]
        source_for(mode)
        direction = body["direction"]
        if direction not in ("base_to_counter", "counter_to_base"):
            raise AppError("Choose a supported conversion direction.")
        amount = number(body["amount"], "Amount", Decimal("1000000000000"))
        fee = number(body.get("fee_percent", "0"), "Fee percentage", Decimal(100))
        with Store(self.db, readonly=True) as store:
            points = self._points(store, base, counter, mode)
        if mode == "reference":
            now = utc(self.clock.now())
            points = [
                row
                for row in points
                if utc(row["observed_at"]) <= now and utc(row["received_at"]) <= now
            ]
        if not points:
            raise AppError(
                "No eligible reference observation is available at the current clock. Refresh or load history first.",
                409,
            )
        latest = points[-1]
        with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)) as context:
            context.prec = 60
            net = amount * (1 - fee / 100)
            converted = (
                net * Decimal(latest["rate"])
                if direction == "base_to_counter"
                else net / Decimal(latest["rate"])
            )
            quantum = Decimal("0.00000001")
            if 0 < abs(converted) < quantum:
                quantum = Decimal(1).scaleb(converted.adjusted() - 7)
            result = decimal_text(converted.quantize(quantum))
        return {
            "ok": True,
            "result": result,
            "rate": latest["rate"],
            "base": base,
            "counter": counter,
            "amount": decimal_text(amount),
            "direction": direction,
            "fee_percent": decimal_text(fee),
            "observed_at": latest["observed_at"],
            "mode": mode,
            "note": "Fee is deducted from the input amount before conversion. Results use 8 decimal places; tiny nonzero amounts below 0.00000001 retain 8 significant digits. This is not an executable quote.",
        }

    def export(self, base, counter, mode):
        base, counter = self._pair(base, counter)
        source = source_for(mode)
        with Store(self.db, readonly=True) as store:
            points = self._points(store, base, counter, mode)
        target = io.StringIO(newline="")
        writer = csv.writer(target)
        writer.writerow(("base", "counter", "source", "observed_at", "received_at", "rate"))
        writer.writerows(
            (base, counter, source, row["observed_at"], row["received_at"], row["rate"])
            for row in points
        )
        return target.getvalue()
