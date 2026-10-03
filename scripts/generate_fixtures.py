"""Rebuild the explicitly synthetic, deterministic demonstration data."""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
target = ROOT / "src/currency_prompter/data"
target.mkdir(parents=True, exist_ok=True)
start = datetime(2026, 9, 1, tzinfo=timezone.utc)
quotes = []
for i in range(48):
    when = (start + timedelta(hours=6 * i)).isoformat().replace("+00:00", "Z")
    value = (Decimal(2500) + Decimal((i * 7) % 19 - 9)) / 100
    quotes.append(
        dict(
            base="SGD",
            counter="THB",
            rate=str(value),
            observed_at=when,
            received_at=when,
            source="synthetic-demo",
        )
    )
rules = [
    dict(
        name="upper-threshold",
        base="SGD",
        counter="THB",
        source="synthetic-demo",
        direction="at_or_above",
        threshold="25.04",
        cooldown_seconds=86400,
        max_age_seconds=345600,
    ),
    dict(
        name="lower-threshold",
        base="SGD",
        counter="THB",
        source="synthetic-demo",
        direction="at_or_below",
        threshold="24.95",
        cooldown_seconds=129600,
        max_age_seconds=345600,
    ),
]
for name, data in (("demo-quotes.json", quotes), ("demo-rules.json", rules)):
    (target / name).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
example = ROOT / "examples"
example.mkdir(exist_ok=True)
(example / "frankfurter-rules.json").write_text(
    json.dumps([dict(rules[0], source="frankfurter")], indent=2) + "\n", encoding="utf-8"
)
print(f"Wrote {len(quotes)} synthetic quotes and {len(rules)} rules.")
