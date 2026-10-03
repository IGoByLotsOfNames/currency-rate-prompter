"""Recompute published replay results from bundled synthetic input, offline."""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from currency_prompter.domain import FakeClock  # noqa: E402 -- standalone source-tree script
from currency_prompter.monitor import dispatch, load_quotes, load_rules, replay  # noqa: E402
from currency_prompter.report import chart_svg, write_report  # noqa: E402
from currency_prompter.storage import Store  # noqa: E402

target = ROOT / "docs/evidence"
target.mkdir(parents=True, exist_ok=True)
assets = ROOT / "docs/assets"
assets.mkdir(parents=True, exist_ok=True)
data = ROOT / "src/currency_prompter/data"
quotes, rules = load_quotes(data / "demo-quotes.json"), load_rules(data / "demo-rules.json")
with tempfile.TemporaryDirectory() as temporary:
    with Store(Path(temporary) / "replay.sqlite") as store:
        first = replay(store, quotes, rules)
        second = replay(store, quotes, rules)
        delivery = dispatch(store, clock=FakeClock(quotes[-1].received_at))
        result = {
            "scenario": "48 synthetic SGD/THB observations at six-hour intervals; not market history",
            "first_replay": first,
            "second_replay": second,
            "delivery": delivery,
            "stored": store.counts(),
            "decisions": store.outcomes(),
        }
        write_report(store, target / "demo-report.html")
        (assets / "history.svg").write_text(
            chart_svg(store.quotes(), title="Synthetic SGD/THB history · 48 observations"),
            encoding="utf-8",
        )
(target / "replay-results.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2))
