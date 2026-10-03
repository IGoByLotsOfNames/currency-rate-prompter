"""Benchmark correctness tests use synthetic fixtures and fake timer readings."""

import importlib.util
import math
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from currency_prompter.app_service import AppService
from currency_prompter.domain import FakeClock, utc

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_dashboard.py"
SPEC = importlib.util.spec_from_file_location("dashboard_benchmark_under_test", SCRIPT)
benchmark = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = benchmark
SPEC.loader.exec_module(benchmark)


class StatisticsTests(unittest.TestCase):
    def test_reports_variation_across_process_means_with_ddof_one(self):
        summary = benchmark.summarize_process_means([10, 20, 30])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["mean_ns"], 20)
        self.assertEqual(summary["median_ns"], 20)
        self.assertEqual(summary["sample_sd_ns"], 10)
        self.assertEqual(summary["sample_variance_ns2"], 100)
        self.assertEqual(summary["min_ns"], 10)
        self.assertEqual(summary["max_ns"], 30)

    def test_incomplete_or_nonfinite_statistics_are_rejected(self):
        for samples in ([], [1], [1, math.nan], [1, math.inf], [1, -math.inf], [-1, 1]):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                benchmark.summarize_process_means(samples)

    def test_timed_mode_refuses_active_instrumentation(self):
        with patch.object(benchmark.sys, "gettrace", return_value=object()):
            with self.assertRaisesRegex(ValueError, "trace or profiling"):
                benchmark._reject_instrumentation()
        with (
            patch.object(benchmark.sys, "gettrace", return_value=None),
            patch.object(benchmark.sys, "getprofile", return_value=object()),
        ):
            with self.assertRaisesRegex(ValueError, "trace or profiling"):
                benchmark._reject_instrumentation()

    def test_sizes_are_bounded_and_unambiguous(self):
        self.assertEqual(benchmark.parse_sizes("100,1000,10000"), (100, 1000, 10000))
        for text in ("", "0", "-1", "10001", "2,2", "1.5", "1,,2", "x"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                benchmark.parse_sizes(text)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, name="fixture.sqlite", rows=3, seed=1729):
        return benchmark.build_fixture(self.directory / name, rows, seed)

    def spec(self, *, measure=False):
        return dict(
            self.build(rows=90 if measure else 3),
            schema_version=1,
            worker_id="test",
            case_id="tiny",
            batches=2,
            calls_per_batch=2,
            warmups=1,
            measure=measure,
        )

    def quotes(self, path):
        with closing(sqlite3.connect(path)) as connection:
            return connection.execute(
                "SELECT * FROM quotes ORDER BY base,counter,observed_at,id"
            ).fetchall()

    def test_fixed_seed_makes_identical_valid_fixture_content(self):
        a, b = self.build("a.sqlite"), self.build("b.sqlite")
        self.assertEqual(self.quotes(a["db"]), self.quotes(b["db"]))
        with closing(sqlite3.connect(a["db"])) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quotes").fetchone()[0], 60)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM app_watchlist").fetchone()[0], 20
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(DISTINCT base || '/' || counter) FROM quotes"
                ).fetchone()[0],
                20,
            )
            for table in ("app_rules", "rules", "outbox", "notifications"):
                self.assertEqual(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0
                )

    def test_newest_ninety_are_identical_as_older_history_grows(self):
        a, b = self.build("short.sqlite", rows=91), self.build("long.sqlite", rows=97)
        clock = FakeClock(utc(benchmark.CLOCK_TEXT))
        first = AppService(a["db"], clock=clock).dashboard("SGD", "THB", "reference", 90)
        second = AppService(b["db"], clock=clock).dashboard("SGD", "THB", "reference", 90)
        self.assertEqual(first, second)
        self.assertEqual(len(first["points"]), 90)
        self.assertEqual(first["pair"], {"base": "SGD", "counter": "THB"})
        self.assertEqual(first["rules"], [])
        self.assertEqual(first["alerts"], [])

    def test_existing_file_is_not_overwritten(self):
        path = self.directory / "mine.sqlite"
        path.write_bytes(b"keep this existing file")
        with self.assertRaises((FileExistsError, ValueError)):
            benchmark.build_fixture(path, 3, 1729)
        self.assertEqual(path.read_bytes(), b"keep this existing file")

    def test_untimed_worker_never_reads_the_performance_clock(self):
        spec = self.spec()
        with patch.object(
            benchmark.time,
            "perf_counter_ns",
            side_effect=AssertionError("untimed check used timer"),
        ):
            result = benchmark.run_worker(spec, measure=False)
        self.assertEqual(result["raw_batches_ns"], [])
        self.assertNotIn("process_mean_ns", result)

    def test_batch_normalization_uses_all_calls_without_timing_validation(self):
        spec = self.spec(measure=True)
        with (
            patch.object(benchmark, "_reject_instrumentation"),
            patch.object(
                benchmark.time, "perf_counter_ns", side_effect=[0, 10000, 11000, 31000]
            ) as timer,
        ):
            result = benchmark.run_worker(spec, measure=True)
        self.assertEqual(timer.call_count, 4)
        self.assertEqual(result["raw_batches_ns"], [10000, 20000])
        self.assertEqual(result["process_mean_ns"], 7500)

    def test_parent_binds_summary_to_exact_worker_and_raw_batches(self):
        spec = self.spec(measure=True)
        with (
            patch.object(benchmark, "_reject_instrumentation"),
            patch.object(benchmark.time, "perf_counter_ns", side_effect=[0, 10000, 11000, 31000]),
        ):
            result = {"status": "complete", **benchmark.run_worker(spec, measure=True)}
        benchmark._validate_worker_result(result, spec)
        for alteration in (
            {"case_id": "wrong"},
            {"worker_id": "wrong"},
            {"process_mean_ns": 1},
            {"raw_batches_ns": [10000]},
            {"raw_batches_ns": [True, 20000]},
            {"gc_enabled": False},
            {"measure": False},
            {"status": "incomplete"},
        ):
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                benchmark._validate_worker_result({**result, **alteration}, spec)

    def test_modified_input_fails_before_a_measurement_can_start(self):
        spec = self.spec(measure=True)
        with closing(sqlite3.connect(spec["db"])) as connection:
            connection.execute("UPDATE quotes SET rate='123.5' WHERE base='SGD' AND counter='THB'")
            connection.commit()
        with (
            patch.object(benchmark, "_reject_instrumentation"),
            patch.object(
                benchmark.time,
                "perf_counter_ns",
                side_effect=AssertionError("must reject before timing"),
            ),
        ):
            with self.assertRaises(ValueError):
                benchmark.run_worker(spec, measure=True)

    def test_wrong_dashboard_result_is_not_accepted(self):
        spec = self.spec()
        with patch.object(
            benchmark.AppService,
            "dashboard",
            return_value={"pair": {"base": "USD", "counter": "THB"}},
        ):
            with self.assertRaises((ValueError, AssertionError)):
                benchmark.run_worker(spec, measure=False)
