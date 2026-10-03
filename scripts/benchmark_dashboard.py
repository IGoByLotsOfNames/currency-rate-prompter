"""Synthetic dashboard-preparation baseline; --check performs no timing.

Only dashboard calls (including their fresh SQLite connections) and loop overhead
are timed. These are repeated processes on one machine, not independent machines,
datasets, real provider observations, browser latency, or a before/after speedup.
"""

import argparse
import gc
import hashlib
import json
import math
import os
import platform
import random
import secrets
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from pathlib import Path

import currency_prompter
from currency_prompter.app_service import AppService
from currency_prompter.domain import FakeClock, Quote, decimal_text, timestamp, utc
from currency_prompter.storage import Store

ROOT = Path(__file__).resolve().parents[1]
CLOCK_TEXT = "2026-10-03T12:00:00Z"
DAYS = 90
SELECTED_PAIR = ("SGD", "THB")
PAIRS = tuple(
    ("SGD", code)
    for code in (
        "THB",
        "USD",
        "EUR",
        "GBP",
        "JPY",
        "MYR",
        "AUD",
        "CNY",
        "HKD",
        "IDR",
        "INR",
        "KRW",
        "NZD",
        "CAD",
        "CHF",
        "AED",
        "BRL",
        "ZAR",
        "VND",
        "PHP",
    )
)
COUNT_TABLES = (
    "quotes",
    "rules",
    "decisions",
    "rule_state",
    "rule_processing",
    "outbox",
    "notifications",
    "app_rules",
    "app_watchlist",
    "app_refreshes",
)
PROJECTION_KEYS = (
    "pair",
    "mode",
    "source",
    "catalogue_available",
    "latest",
    "previous_rate",
    "change_percent",
    "low",
    "high",
    "points",
    "rules",
    "alerts",
    "last_refresh",
    "stale",
)


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def parse_sizes(text):
    if not isinstance(text, str) or not text or len(text) > 128:
        raise ValueError("sizes must be a comma-separated list of row counts")
    parts = text.split(",")
    if not 1 <= len(parts) <= 6 or any(not part.isascii() or not part.isdigit() for part in parts):
        raise ValueError("sizes must contain 1-6 distinct decimal row counts")
    values = tuple(_integer(int(part), "rows per pair", 90, 10000) for part in parts)
    if len(set(values)) != len(values):
        raise ValueError("sizes must be distinct")
    return values


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_package(root):
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix in (".py", ".json", ".html", ".css", ".js")
    )
    if not paths:
        raise ValueError("application source files were not found")
    return {str(path.relative_to(root)).replace("\\", "/"): _sha(path) for path in paths}


def source_hashes():
    expected = _hash_package(ROOT / "src/currency_prompter")
    loaded = _hash_package(Path(currency_prompter.__file__).resolve().parent)
    if loaded != expected:
        raise ValueError("the imported application package differs from the working source")
    return expected


def _json(path, value):
    # New evidence paths only: interrupted files are retained, never silently replaced.
    with Path(path).open("x", encoding="utf-8") as target:
        json.dump(value, target, ensure_ascii=False, allow_nan=False, indent=2)
        target.write("\n")


def _forbid_network():
    raise RuntimeError("provider factories must never be called by this offline benchmark")


def _application(db):
    return AppService(
        db,
        clock=FakeClock(utc(CLOCK_TEXT)),
        provider_factory=_forbid_network,
        history_factory=_forbid_network,
        catalogue_factory=_forbid_network,
    )


def _rate(pair_index, age, seed):
    # Age, not fixture length, determines each row: common recent windows are identical.
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        return Decimal(
            100000 + (pair_index + 1) * 1000 + ((age * 37 + seed % 1009) % 1009)
        ) / Decimal(10000)


def _oracle(rows_per_pair, seed):
    # Independent of dashboard, _points, and application percentage helpers.
    anchor = utc(CLOCK_TEXT).replace(hour=0, minute=0, second=0, microsecond=0)
    index = PAIRS.index(SELECTED_PAIR)
    points = [
        {
            "observed_at": timestamp(anchor - timedelta(days=age)),
            "rate": decimal_text(_rate(index, age, seed)),
        }
        for age in range(min(DAYS, rows_per_pair) - 1, -1, -1)
    ]
    latest = {**points[-1], "received_at": points[-1]["observed_at"]}
    previous = decimal_text(_rate(index, 1, seed)) if rows_per_pair >= 2 else None
    with localcontext(Context(prec=60, rounding=ROUND_HALF_EVEN)):
        change = (
            decimal_text(
                ((Decimal(latest["rate"]) / Decimal(previous) - 1) * 100).quantize(
                    Decimal("0.000001")
                )
            )
            if previous is not None
            else None
        )
    values = [Decimal(row["rate"]) for row in points]
    return {
        "pair": {"base": SELECTED_PAIR[0], "counter": SELECTED_PAIR[1]},
        "mode": "reference",
        "source": "frankfurter",
        "catalogue_available": True,
        "latest": latest,
        "previous_rate": previous,
        "change_percent": change,
        "low": decimal_text(min(values)),
        "high": decimal_text(max(values)),
        "points": points,
        "rules": [],
        "alerts": [],
        "last_refresh": None,
        "stale": False,
    }


def _counts(db):
    with Store(db, readonly=True) as store:
        return {
            table: store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in COUNT_TABLES
        }


def _check_database(db, fixture_sha256, counts):
    if _sha(db) != fixture_sha256 or _counts(db) != counts:
        raise ValueError("fixture hash or table counts changed")
    wal = Path(str(db) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("fixture has uncheckpointed WAL content")


def _check_sources(expected, script_hash):
    if source_hashes() != expected or _sha(__file__) != script_hash:
        raise ValueError("application or benchmark source changed during the run")


def build_fixture(db, rows_per_pair, seed):
    """Create a new synthetic fixture in one bulk quote transaction; never reuse a DB."""
    _integer(rows_per_pair, "rows per pair", 1, 10000)
    _integer(seed, "seed", 0, 2**32 - 1)
    if Path(db).is_symlink():
        raise ValueError("fixture path must not be a symlink")
    db = Path(db).resolve()
    marker = db.with_suffix(db.suffix + ".fixture.json")
    if (
        db.exists()
        or marker.exists()
        or any(Path(str(db) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    ):
        raise ValueError("fixture path or associated evidence already exists; refusing reuse")
    db.parent.mkdir(parents=True, exist_ok=True)
    with db.open("xb"):
        pass
    hashes = source_hashes()
    script_hash = _sha(__file__)
    _application(db)
    anchor = utc(CLOCK_TEXT).replace(hour=0, minute=0, second=0, microsecond=0)

    def rows():
        for index, (base, counter) in enumerate(PAIRS):
            for age in range(rows_per_pair - 1, -1, -1):
                when = anchor - timedelta(days=age)
                quote = Quote(base, counter, _rate(index, age, seed), when, when, "frankfurter")
                record = quote.to_dict()
                yield (
                    quote.key,
                    base,
                    counter,
                    record["rate"],
                    record["observed_at"],
                    record["received_at"],
                    quote.source,
                )

    with Store(db) as store:
        with store.connection:
            store.connection.execute("DELETE FROM app_watchlist")
            store.connection.executemany("INSERT INTO app_watchlist VALUES (?,?)", PAIRS)
            store.connection.executemany("INSERT INTO quotes VALUES (?,?,?,?,?,?,?)", rows())
        grouped = [
            tuple(row)
            for row in store.connection.execute(
                "SELECT base,counter,COUNT(*) FROM quotes GROUP BY base,counter ORDER BY base,counter"
            )
        ]
        if grouped != sorted((base, counter, rows_per_pair) for base, counter in PAIRS):
            raise ValueError("fixture pair counts differ from the protocol")
        store.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    counts = _counts(db)
    expected_counts = {table: 0 for table in COUNT_TABLES}
    expected_counts.update(quotes=rows_per_pair * len(PAIRS), app_watchlist=len(PAIRS))
    if counts != expected_counts:
        raise ValueError("fixture contains unexpected rules, alerts, or row counts")
    evidence = {
        "db": str(db),
        "rows_per_pair": rows_per_pair,
        "seed": seed,
        "fixture_sha256": _sha(db),
        "expected": _oracle(rows_per_pair, seed),
        "counts": counts,
        "source_hashes": hashes,
        "script_sha256": script_hash,
        "synthetic": True,
        "pair_counts": grouped,
        "insertion_order": "pair-major in PAIRS order; oldest-to-newest within each pair",
    }
    _check_sources(hashes, script_hash)
    _check_database(db, evidence["fixture_sha256"], counts)
    _json(marker, evidence)
    return evidence


def summarize_process_means(values):
    values = list(values)
    if len(values) < 2 or any(
        type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values
    ):
        raise ValueError("at least two finite nonnegative process means are required")
    return {
        "count": len(values),
        "mean_ns": statistics.mean(values),
        "median_ns": statistics.median(values),
        "sample_sd_ns": statistics.stdev(values),
        "sample_variance_ns2": statistics.variance(values),
        "min_ns": min(values),
        "max_ns": max(values),
    }


def _validate_worker_result(result, spec):
    """Bind each receipt to its worker and derive the aggregation input from raw ns."""
    if (
        not isinstance(result, dict)
        or result.get("status") != "complete"
        or result.get("worker_id") != spec["worker_id"]
        or result.get("case_id") != spec["case_id"]
        or result.get("measure") is not spec["measure"]
        or result.get("gc_enabled") is not True
    ):
        raise ValueError("worker returned incomplete or mismatched identity/mode/GC evidence")
    verification = result.get("verification")
    if (
        not isinstance(verification, dict)
        or verification.get("oracle") != "passed"
        or verification.get("fixture_sha256") != spec["fixture_sha256"]
        or verification.get("counts") != spec["counts"]
        or not isinstance(verification.get("counts"), dict)
        or any(type(value) is not int for value in verification["counts"].values())
        or verification.get("application_and_script_unchanged") is not True
        or verification.get("database_unchanged") is not True
    ):
        raise ValueError("worker verification does not match its frozen fixture and source checks")
    raw = result.get("raw_batches_ns")
    if not isinstance(raw, list) or any(type(value) is not int or value < 0 for value in raw):
        raise ValueError("worker batch durations must be nonnegative integer nanoseconds")
    validated = dict(result)
    if spec["measure"]:
        if len(raw) != spec["batches"]:
            raise ValueError("worker raw batch count differs from its specification")
        derived = sum(raw) / (spec["batches"] * spec["calls_per_batch"])
        reported = result.get("process_mean_ns")
        if type(reported) not in (int, float) or not math.isfinite(reported) or reported != derived:
            raise ValueError("worker process mean does not equal its raw batch normalization")
        validated["process_mean_ns"] = derived
    elif raw or "process_mean_ns" in result:
        raise ValueError("untimed worker must not report timing observations or a process mean")
    return validated


def _render_summary(report, fixtures, output):
    if not report["measure"]:
        return f"PASSED: untimed correctness and multi-process wiring check. No performance measurement.\nEvidence: {output}\n"
    lines = [
        "Dashboard preparation baseline: synthetic cached observations",
        "Values describe process means, not individual-request tail latency.",
        "case         total rows    mean ms   median ms   sample SD ms   variance ms^2   min mean ms   max mean ms",
    ]
    for case, fixture in fixtures.items():
        stats = report["summary"][case]
        lines.append(
            f"{case:<12} {fixture['counts']['quotes']:>10} {stats['mean_ns'] / 1e6:>10.6g} "
            f"{stats['median_ns'] / 1e6:>11.6g} {stats['sample_sd_ns'] / 1e6:>14.6g} "
            f"{stats['sample_variance_ns2'] / 1e12:>15.6g} {stats['min_ns'] / 1e6:>13.6g} "
            f"{stats['max_ns'] / 1e6:>13.6g}"
        )
    lines.extend(
        [
            "Sample SD and variance use ddof=1 across all process means; no outliers removed.",
            "Baseline only: one machine, fixed synthetic data, no speedup comparison.",
            f"Evidence: {output}",
        ]
    )
    return "\n".join(lines) + "\n"


def _reject_instrumentation():
    if sys.gettrace() is not None or sys.getprofile() is not None:
        raise ValueError("timed measurements refuse an active trace or profiling hook")
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is not None and any(monitoring.get_tool(tool) is not None for tool in range(6)):
        raise ValueError("timed measurements refuse active Python monitoring tools")


def _verify_result(actual, expected):
    if not isinstance(actual, dict) or not set(PROJECTION_KEYS) <= actual.keys():
        raise ValueError("dashboard output is missing expected fields")
    if {key: actual[key] for key in PROJECTION_KEYS} != expected:
        raise ValueError("dashboard output differs from the independent fixture oracle")


def run_worker(spec, measure=True):
    if type(measure) is not bool or spec.get("measure", measure) is not measure:
        raise ValueError("worker measurement mode is inconsistent")
    if measure:
        _reject_instrumentation()
    _integer(spec["rows_per_pair"], "rows per pair", 90 if measure else 1, 10000)
    _integer(spec["seed"], "seed", 0, 2**32 - 1)
    batches = _integer(spec["batches"], "batches", 2 if measure else 1, 100)
    calls = _integer(spec["calls_per_batch"], "calls per batch", 1, 1000)
    warmups = _integer(spec["warmups"], "warmups", 1, 1000)
    db = Path(spec["db"]).resolve()
    marker = json.loads(db.with_suffix(db.suffix + ".fixture.json").read_text(encoding="utf-8"))
    for key in (
        "db",
        "rows_per_pair",
        "seed",
        "fixture_sha256",
        "expected",
        "counts",
        "source_hashes",
        "script_sha256",
    ):
        if marker[key] != spec[key]:
            raise ValueError(f"worker specification differs from fixture marker: {key}")
    if spec["expected"] != _oracle(spec["rows_per_pair"], spec["seed"]):
        raise ValueError("fixture oracle does not match its deterministic configuration")
    _check_sources(spec["source_hashes"], spec["script_sha256"])
    _check_database(db, spec["fixture_sha256"], spec["counts"])
    app = _application(db)
    _check_database(db, spec["fixture_sha256"], spec["counts"])
    gc.enable()
    _verify_result(app.dashboard(*SELECTED_PAIR, "reference", DAYS), spec["expected"])
    for _ in range(warmups):
        _verify_result(app.dashboard(*SELECTED_PAIR, "reference", DAYS), spec["expected"])
    _check_sources(spec["source_hashes"], spec["script_sha256"])
    raw = []
    for _ in range(batches):
        # Slots are allocated before timing. Capturing results adds minimal loop
        # overhead and allows every returned report to be checked afterward.
        batch_results = [None] * calls
        if measure:
            started = time.perf_counter_ns()
            for call in range(calls):
                batch_results[call] = app.dashboard(*SELECTED_PAIR, "reference", DAYS)
            elapsed = time.perf_counter_ns() - started
            if type(elapsed) is not int or elapsed < 0:
                raise ValueError("timer returned an invalid batch duration")
            raw.append(elapsed)
        else:
            for call in range(calls):
                batch_results[call] = app.dashboard(*SELECTED_PAIR, "reference", DAYS)
        for actual in batch_results:
            _verify_result(actual, spec["expected"])
    _check_database(db, spec["fixture_sha256"], spec["counts"])
    _check_sources(spec["source_hashes"], spec["script_sha256"])
    result = {
        "worker_id": spec["worker_id"],
        "case_id": spec["case_id"],
        "measure": measure,
        "pid": os.getpid(),
        "gc_enabled": gc.isenabled(),
        "raw_batches_ns": raw,
        "verification": {
            "oracle": "passed",
            "fixture_sha256": spec["fixture_sha256"],
            "counts": _counts(db),
            "application_and_script_unchanged": True,
            "database_unchanged": True,
        },
    }
    if measure:
        result.update(raw_batches_ns=raw, process_mean_ns=sum(raw) / (batches * calls))
    return result


def _environment(measure, notes):
    result = {
        "python": sys.version,
        "executable": sys.executable,
        "sqlite": sqlite3.sqlite_version,
        "os": platform.platform(),
        "processor": platform.processor() or "not reported",
        "machine": platform.machine(),
        "logical_cpu_count": os.cpu_count(),
        "imported_package": str(Path(currency_prompter.__file__).resolve()),
        "gc_policy": "enabled; default thresholds; no explicit collections during worker",
        "gc_thresholds": list(gc.get_threshold()),
        "user_power_and_background_notes": notes,
    }
    if measure:
        info = time.get_clock_info("perf_counter")
        result["perf_counter"] = {
            key: getattr(info, key)
            for key in ("implementation", "monotonic", "adjustable", "resolution")
        }
    return result


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="100,1000,10000")
    parser.add_argument("--processes", type=int, default=5)
    parser.add_argument("--batches", type=int, default=10)
    parser.add_argument("--calls-per-batch", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--notes", default="not supplied")
    parser.add_argument(
        "--output-parent",
        "--output-root",
        dest="output_parent",
        type=Path,
        default=ROOT / "benchmark-results",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="untimed tiny multi-process wiring and correctness check",
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.worker is not None:
        spec = json.loads(args.worker.read_text(encoding="utf-8"))
        result_path = args.worker.with_name(args.worker.stem + ".result.json")
        try:
            result = run_worker(spec, measure=spec["measure"])
            _json(result_path, {"status": "complete", **result})
            return 0
        except Exception as error:
            failure = {
                "status": "incomplete",
                "worker_id": spec.get("worker_id"),
                "error": f"{type(error).__name__}: {error}",
            }
            if not result_path.exists():
                _json(result_path, failure)
            return 1
    try:
        sizes = parse_sizes(args.sizes)
        _integer(args.processes, "processes", 2, 20)
        _integer(args.batches, "batches", 2, 100)
        _integer(args.calls_per_batch, "calls per batch", 1, 1000)
        _integer(args.warmups, "warmups", 1, 1000)
        _integer(args.seed, "seed", 0, 2**32 - 1)
        if not isinstance(args.notes, str) or len(args.notes) > 2000:
            raise ValueError("notes must contain at most 2000 characters")
        if not args.check:
            _reject_instrumentation()
        source = source_hashes()
    except ValueError as error:
        parser.error(str(error))
    if args.check:
        sizes, args.processes, args.batches, args.calls_per_batch, args.warmups = (3, 5), 2, 1, 2, 1
    parent = args.output_parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = (
        parent / f"dashboard-{'check' if args.check else 'baseline'}-{stamp}-{secrets.token_hex(3)}"
    )
    output.mkdir(exist_ok=False)
    print(f"Evidence directory: {output}", flush=True)
    config = {
        "schema_version": 1,
        "measure": not args.check,
        "sizes": list(sizes),
        "processes_per_case": args.processes,
        "batches": args.batches,
        "calls_per_batch": args.calls_per_batch,
        "warmups": args.warmups,
        "preflight_correctness_calls": 1,
        "untimed_calls_before_batches": 1 + args.warmups,
        "seed": args.seed,
        "clock": CLOCK_TEXT,
        "pair": list(SELECTED_PAIR),
        "days": DAYS,
        "pairs": [list(pair) for pair in PAIRS],
        "workload": "20-pair maximum synthetic watchlist; zero rules, outbox, notifications",
        "query_cap": "dashboard reads at most the latest 10000 selected-pair observations",
        "insertion_order": "pair-major in PAIRS order; oldest-to-newest within each pair",
        "scope": "AppService.dashboard calls including fresh readonly SQLite connections and loop/result-slot overhead; excludes setup, warmup, checks, process startup, JSON, network and UI",
        "warmup": "one additional preflight correctness call precedes configured warmups; interpreter and OS filesystem/cache warmup, no cached-connection or cold-start claim",
        "data": "Entirely synthetic. frankfurter source is used only inside these isolated fixtures to exercise the reference read path.",
        "inference_limits": "Baseline only; no speedup. Same machine/cache state, fixed synthetic data. Process-mean dispersion is not request-tail latency.",
        "source_hashes": source,
        "script_sha256": _sha(__file__),
        "environment": _environment(not args.check, args.notes),
    }
    _json(output / "config.json", config)
    completed, fixtures, schedule = [], {}, []
    try:
        for size in sizes:
            case = f"rows-{size}"
            fixtures[case] = build_fixture(output / case / "fixture.sqlite", size, args.seed)
        if not args.check and any(
            fixtures[f"rows-{size}"]["expected"] != fixtures[f"rows-{sizes[0]}"]["expected"]
            for size in sizes
        ):
            raise ValueError("common newest-90-observation oracle differs across sizes")
        rng = random.Random(args.seed)
        for round_number in range(1, args.processes + 1):
            cases = list(fixtures)
            rng.shuffle(cases)
            schedule.extend(
                {
                    "round": round_number,
                    "case_id": case,
                    "worker_id": f"{case}-process-{round_number:02}",
                }
                for case in cases
            )
        _json(output / "schedule.json", schedule)
        for index, entry in enumerate(schedule, 1):
            _check_sources(source, config["script_sha256"])
            spec = {
                "schema_version": 1,
                **fixtures[entry["case_id"]],
                **entry,
                "batches": args.batches,
                "calls_per_batch": args.calls_per_batch,
                "warmups": args.warmups,
                "measure": not args.check,
            }
            spec_path = output / f"{index:03}-{entry['worker_id']}.spec.json"
            _json(spec_path, spec)
            _json(output / f"started-{index:03}.json", entry)
            print(f"{index}/{len(schedule)}: {entry['worker_id']}", flush=True)
            with (
                spec_path.with_suffix(".stdout.txt").open("x", encoding="utf-8") as stdout,
                spec_path.with_suffix(".stderr.txt").open("x", encoding="utf-8") as stderr,
            ):
                process = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-B",
                        str(Path(__file__).resolve()),
                        "--worker",
                        str(spec_path),
                    ],
                    cwd=output,
                    stdout=stdout,
                    stderr=stderr,
                    check=False,
                    timeout=3600,
                )
            result_path = spec_path.with_name(spec_path.stem + ".result.json")
            if process.returncode != 0 or not result_path.is_file():
                raise ValueError(
                    f"worker {entry['worker_id']} failed with exit {process.returncode}; logs retained"
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result = _validate_worker_result(result, spec)
            completed.append(result)
            _json(
                output / f"progress-{index:03}.json",
                {"completed": len(completed), "planned": len(schedule), "last": entry},
            )
        for fixture in fixtures.values():
            _check_database(fixture["db"], fixture["fixture_sha256"], fixture["counts"])
        _check_sources(source, config["script_sha256"])
        report = {
            "status": "complete",
            "measure": not args.check,
            "workers_completed": len(completed),
            "schedule": schedule,
            "fixtures_unchanged": True,
            "application_and_script_unchanged": True,
            "results": completed,
        }
        if not args.check:
            report["summary"] = {
                case: summarize_process_means(
                    result["process_mean_ns"] for result in completed if result["case_id"] == case
                )
                for case in fixtures
            }
        else:
            report["note"] = (
                "Untimed correctness and multi-process wiring check only; no performance result."
            )
        readable = _render_summary(report, fixtures, output)
        with (output / "summary.txt").open("x", encoding="utf-8") as target:
            target.write(readable)
        _json(output / "summary.json", report)
        print(readable, end="", flush=True)
        return 0
    except (Exception, KeyboardInterrupt) as error:
        _json(
            output / "incomplete.json",
            {
                "status": "incomplete",
                "measure": not args.check,
                "error": f"{type(error).__name__}: {error}",
                "workers_completed": len(completed),
                "schedule": schedule,
                "results": completed,
            },
        )
        print(f"Incomplete evidence retained: {output}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
