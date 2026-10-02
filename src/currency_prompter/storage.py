"""One SQLite transaction records a quote, a decision and an outbox event."""

from datetime import timedelta
import json
from pathlib import Path
import sqlite3

from .domain import Quote, Rule, digest, timestamp, utc


SCHEMA = """
CREATE TABLE IF NOT EXISTS quotes (
 id TEXT PRIMARY KEY, base TEXT NOT NULL, counter TEXT NOT NULL,
 rate TEXT NOT NULL, observed_at TEXT NOT NULL, received_at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS quote_pair_time ON quotes(base,counter,observed_at);
CREATE TABLE IF NOT EXISTS rules (id TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
 rule_id TEXT NOT NULL, quote_id TEXT NOT NULL, outcome TEXT NOT NULL, decided_at TEXT NOT NULL,
 PRIMARY KEY(rule_id,quote_id), FOREIGN KEY(rule_id) REFERENCES rules(id),
 FOREIGN KEY(quote_id) REFERENCES quotes(id)
);
CREATE TABLE IF NOT EXISTS rule_state (
 rule_id TEXT PRIMARY KEY, latest_observed TEXT NOT NULL, latest_decision TEXT NOT NULL,
 last_queued TEXT, FOREIGN KEY(rule_id) REFERENCES rules(id)
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL,
 delivered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
 id TEXT PRIMARY KEY, payload TEXT NOT NULL, recorded_at TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path)
        if readonly:
            self.connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(self.path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.executescript(SCHEMA)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def process(self, quote: Quote, rules: list[Rule], now):
        """Store once; evaluate each rule/quote once; never rewind rule chronology."""
        now = utc(now)
        if quote.received_at > now:
            raise ValueError("receipt is in the future relative to the processing clock")
        if len({r.name for r in rules}) != len(rules):
            raise ValueError("rule names must be unique within a run")
        con = self.connection
        outcomes = []
        con.execute("BEGIN IMMEDIATE")
        try:
            previous = con.execute("SELECT rate FROM quotes WHERE id=?", (quote.key,)).fetchone()
            if previous and previous["rate"] != quote.to_dict()["rate"]:
                raise ValueError("conflicting revision for the same source, pair and observation time")
            inserted = con.execute("INSERT OR IGNORE INTO quotes VALUES (?,?,?,?,?,?,?)",
                (quote.key, quote.base, quote.counter, quote.to_dict()["rate"], timestamp(quote.observed_at),
                 timestamp(quote.received_at), quote.source)).rowcount == 1
            for rule in rules:
                if not rule.matches(quote):
                    continue
                con.execute("INSERT OR IGNORE INTO rules VALUES (?,?)", (rule.key, json.dumps(rule.to_dict(), sort_keys=True)))
                if con.execute("SELECT 1 FROM decisions WHERE rule_id=? AND quote_id=?", (rule.key, quote.key)).fetchone():
                    outcomes.append("duplicate")
                    continue
                state = con.execute("SELECT * FROM rule_state WHERE rule_id=?", (rule.key,)).fetchone()
                if state and (quote.observed_at <= utc(state["latest_observed"]) or now < utc(state["latest_decision"])):
                    outcome = "out_of_order"
                elif now - quote.observed_at > timedelta(seconds=rule.max_age_seconds):
                    outcome = "stale"
                else:
                    last_queued = state["last_queued"] if state else None
                    if not rule.qualifies(quote):
                        outcome = "below_condition"
                    elif last_queued and now - utc(last_queued) < timedelta(seconds=rule.cooldown_seconds):
                        outcome = "cooldown"
                    else:
                        outcome = "queued"
                        event_id = digest({"rule": rule.key, "quote": quote.key})
                        payload = {"id": event_id, "rule": rule.to_dict(), "quote": quote.to_dict(), "queued_at": timestamp(now)}
                        con.execute("INSERT INTO outbox(id,payload,created_at) VALUES (?,?,?)",
                                    (event_id, json.dumps(payload, sort_keys=True), timestamp(now)))
                        last_queued = timestamp(now)
                    con.execute("INSERT OR REPLACE INTO rule_state VALUES (?,?,?,?)",
                                (rule.key, timestamp(quote.observed_at), timestamp(now), last_queued))
                con.execute("INSERT INTO decisions VALUES (?,?,?,?)", (rule.key, quote.key, outcome, timestamp(now)))
                outcomes.append(outcome)
            con.commit()
            return {"inserted": inserted, "outcomes": outcomes}
        except BaseException:
            con.rollback()
            raise

    def pending(self, limit=1000):
        return self.connection.execute("SELECT * FROM outbox WHERE delivered_at IS NULL ORDER BY created_at,id LIMIT ?", (limit,)).fetchall()

    def record_notification(self, event, now):
        with self.connection:
            return self.connection.execute("INSERT OR IGNORE INTO notifications VALUES (?,?,?)",
                (event["id"], json.dumps(event, sort_keys=True), timestamp(now))).rowcount == 1

    def acknowledge(self, event_id, now):
        with self.connection:
            self.connection.execute("UPDATE outbox SET delivered_at=?, attempts=attempts+1, last_error=NULL WHERE id=? AND delivered_at IS NULL",
                                    (timestamp(now), event_id))

    def failed(self, event_id, error):
        # Store the exception type only: provider messages may contain private data.
        with self.connection:
            self.connection.execute("UPDATE outbox SET attempts=attempts+1,last_error=? WHERE id=? AND delivered_at IS NULL",
                                    (type(error).__name__, event_id))

    def counts(self):
        return {table: self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("quotes", "decisions", "outbox", "notifications")}

    def quotes(self, *, base=None, counter=None, limit=1000):
        sql, values = "SELECT * FROM quotes", []
        if base is not None and counter is not None:
            sql += " WHERE base=? AND counter=?"
            values += [base, counter]
        sql += " ORDER BY observed_at DESC,id LIMIT ?"
        values.append(limit)
        return [dict(row) for row in self.connection.execute(sql, values)]

    def alerts(self, limit=1000):
        return [{"event": json.loads(row["payload"]), "delivered_at": row["delivered_at"],
                 "attempts": row["attempts"], "last_error": row["last_error"]}
                for row in self.connection.execute("SELECT * FROM outbox ORDER BY created_at DESC,id LIMIT ?", (limit,))]

    def outcomes(self):
        return {row[0]: row[1] for row in self.connection.execute("SELECT outcome,COUNT(*) FROM decisions GROUP BY outcome")}
