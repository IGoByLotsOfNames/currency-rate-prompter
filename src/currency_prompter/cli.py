"""Small explicit commands: demo, replay, poll, dispatch, report, serve and app."""

import argparse
import json
import sqlite3
import sys
from importlib.resources import files
from pathlib import Path

from .domain import FakeClock, SystemClock
from .monitor import dispatch, load_quotes, load_rules, replay
from .provider import Frankfurter
from .report import write_report
from .server import make_server
from .storage import Store


def parser():
    root = argparse.ArgumentParser(
        description="Track exchange-rate observations and explain every alert. Demo is entirely offline."
    )
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser(
        "demo", help="replay the bundled synthetic scenario twice and write a report"
    )
    demo.add_argument("--output", type=Path, default=Path("demo-output"))
    for name in ("replay", "poll", "dispatch", "report", "serve", "app"):
        command = commands.add_parser(name)
        command.add_argument("--db", type=Path, required=True)
        if name in ("replay", "poll"):
            command.add_argument("--rules", type=Path, required=True)
        if name == "replay":
            command.add_argument("--quotes", type=Path, required=True)
        if name == "poll":
            command.add_argument("--base", default="SGD")
            command.add_argument("--counter", default="THB")
        if name == "report":
            command.add_argument("--output", type=Path, default=Path("output/history.html"))
        if name in ("serve", "app"):
            command.add_argument("--port", type=int, default=8765)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            args.output.mkdir(parents=True, exist_ok=True)
            db = args.output / "demo.sqlite"
            if db.exists():
                raise ValueError(
                    "demo database already exists; choose a new --output folder (existing data is never deleted)"
                )
            resource = files("currency_prompter") / "data"
            quotes = load_quotes(resource / "demo-quotes.json")
            rules = load_rules(resource / "demo-rules.json")
            with Store(db) as store:
                first = replay(store, quotes, rules)
                second = replay(store, quotes, rules)
                delivery = dispatch(store, clock=FakeClock(quotes[-1].received_at))
                write_report(store, args.output / "history.html")
                result = {
                    "scenario": "synthetic; not real exchange-rate history",
                    "first_replay": first,
                    "second_replay": second,
                    "delivery": delivery,
                    "stored": store.counts(),
                    "decisions": store.outcomes(),
                    "report": str(args.output / "history.html"),
                }
            (args.output / "summary.json").write_text(
                json.dumps(result, indent=2) + "\n", encoding="utf-8"
            )
        elif args.command == "app":
            from .webapp import make_app_server

            if not 0 <= args.port <= 65535:
                raise ValueError("port must be 0-65535")
            server = make_app_server(args.db, args.port)
            print(
                f"Currency Rate Prompter: http://127.0.0.1:{server.server_port}/ (Ctrl+C to stop)",
                flush=True,
            )
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        elif args.command == "serve":
            with Store(args.db, readonly=True) as store:
                store.counts()
            if not 0 <= args.port <= 65535:
                raise ValueError("port must be 0-65535")
            server = make_server(args.db, args.port)
            print(
                f"Read-only dashboard: http://127.0.0.1:{server.server_port}/ (Ctrl+C to stop)",
                flush=True,
            )
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        else:
            # Validate external inputs before opening a new state database.
            rules = load_rules(args.rules) if args.command in ("replay", "poll") else None
            quotes = load_quotes(args.quotes) if args.command == "replay" else None
            if args.command == "poll":
                clock = SystemClock()
                quote = Frankfurter().fetch(args.base, args.counter, clock=clock)
            with Store(args.db, readonly=args.command == "report") as store:
                if args.command == "replay":
                    result = replay(store, quotes, rules)
                elif args.command == "poll":
                    result = store.process(quote, rules, clock.now())
                    result["quote"] = quote.to_dict()
                elif args.command == "dispatch":
                    result = dispatch(store)
                else:
                    write_report(store, args.output)
                    result = {"report": str(args.output)}
        print(json.dumps(result, indent=2))
        failures = (
            result.get("failed", 0)
            if args.command == "dispatch"
            else (result["delivery"]["failed"] if args.command == "demo" else 0)
        )
        return 2 if failures else 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
