"""Replay and delivery boundaries; the included notifier never sends messages."""

import json
from pathlib import Path

from .domain import Quote, Rule, SystemClock

MAX_INPUT_BYTES = 5_000_000


def load_json(path):
    with Path(path).open("rb") as handle:
        data = handle.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("input exceeds the 5 MB limit")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("input must be valid UTF-8 JSON") from exc


def load_rules(path):
    data = load_json(path)
    if not isinstance(data, list) or not 1 <= len(data) <= 100:
        raise ValueError("rules must be a list of 1-100 objects")
    rules = [Rule.from_dict(row) for row in data]
    if len({rule.name for rule in rules}) != len(rules):
        raise ValueError("rule names must be unique")
    return rules


def load_quotes(path):
    data = load_json(path)
    if not isinstance(data, list) or not 1 <= len(data) <= 10000:
        raise ValueError("quotes must be a list of 1-10000 objects")
    # Validate the entire input before creating any database rows.
    return [Quote.from_dict(row) for row in data]


class JournalNotifier:
    """Durable local dry-run sink. Event IDs make retries idempotent."""
    def __init__(self, store):
        self.store = store

    def send(self, event, now):
        return self.store.record_notification(event, now)


def dispatch(store, notifier=None, clock=None):
    notifier = notifier or JournalNotifier(store)
    clock = clock or SystemClock()
    result = {"acknowledged": 0, "new_notifications": 0, "failed": 0}
    for row in store.pending():
        event = json.loads(row["payload"])
        try:
            is_new = notifier.send(event, clock.now())
            store.acknowledge(row["id"], clock.now())
            result["acknowledged"] += 1
            result["new_notifications"] += int(bool(is_new))
        except Exception as exc:
            store.failed(row["id"], exc)
            result["failed"] += 1
    return result


def replay(store, quotes, rules, *, clock=None):
    """Replay historical receipt times, in input order. No sorting hides late data."""
    result = {"input_quotes": 0, "inserted": 0, "queued": 0, "duplicates": 0}
    for quote in quotes:
        now = clock.now() if clock else quote.received_at
        outcome = store.process(quote, rules, now)
        result["input_quotes"] += 1
        result["inserted"] += int(outcome["inserted"])
        result["queued"] += outcome["outcomes"].count("queued")
        result["duplicates"] += outcome["outcomes"].count("duplicate")
    return result
