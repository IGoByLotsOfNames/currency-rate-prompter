"""Portable offline-demo launcher contracts using temporary, isolated sessions."""

import csv
import importlib.util
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "demo_app.py"
PAIR = {"base": "SGD", "counter": "THB", "mode": "demo"}


def load_launcher():
    spec = importlib.util.spec_from_file_location("currency_demo_launcher_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OfflineServiceTests(unittest.TestCase):
    def test_network_actions_reject_before_constructing_any_provider(self):
        launcher = load_launcher()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch(
                    "socket.create_connection", side_effect=AssertionError("network used")
                ) as net,
                patch("urllib.request.urlopen", side_effect=AssertionError("network used")) as url,
            ):
                service = launcher.DemoService(Path(directory) / "demo.sqlite")
                self.assertEqual("2026-10-03T12:00:00+00:00", service.clock.now().isoformat())
                for factory in (
                    service.provider_factory,
                    service.history_factory,
                    service.catalogue_factory,
                ):
                    with self.subTest(factory=factory), self.assertRaises(ValueError):
                        factory()
                providers = [
                    Mock(side_effect=AssertionError("provider constructed")) for _ in range(3)
                ]
                service.provider_factory, service.history_factory, service.catalogue_factory = (
                    providers
                )
                actions = (
                    lambda: service.refresh({**PAIR, "mode": "reference"}),
                    lambda: service.import_history({"base": "SGD", "counter": "THB"}),
                    lambda: service.refresh_currencies({}),
                )
                for action in actions:
                    with self.subTest(action=action), self.assertRaises(ValueError) as caught:
                        action()
                    self.assertEqual(409, caught.exception.status)
                    self.assertIn("offline demo", str(caught.exception).lower())
                self.assertEqual(90, len(service.dashboard(days=90)["points"]))
                self.assertIsNone(service.dashboard(mode="reference")["latest"])
                for provider in providers:
                    provider.assert_not_called()
                net.assert_not_called()
                url.assert_not_called()

    def test_interrupt_before_thread_start_closes_server_and_releases_session_lock(self):
        launcher = load_launcher()
        real_factory = launcher.make_app_server
        servers = []

        def record_server(*args, **kwargs):
            server = real_factory(*args, **kwargs)
            server.server_close = Mock(wraps=server.server_close)
            servers.append(server)
            return server

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "interrupted session"
            with (
                patch.object(launcher, "make_app_server", side_effect=record_server),
                patch.object(launcher.threading.Thread, "start", side_effect=KeyboardInterrupt),
            ):
                self.assertEqual(0, launcher.main(["--output", str(output)]))
            self.assertEqual(1, len(servers))
            servers[0].server_close.assert_called_once()
            self.assertEqual(-1, servers[0].socket.fileno())
            self.assertEqual("failed", json.loads((output / "session.json").read_text())["status"])
            acquired = launcher.SessionLock(output)
            acquired.close()


class DemoProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="currency demo tests ")
        self.root = Path(self.temp.name)
        self.bundle = self.root / "copied application"
        self.script = self.bundle / "scripts" / "demo_app.py"
        self.script.parent.mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.script)
        shutil.copytree(
            ROOT / "src", self.bundle / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        self.cwd = self.root / "unrelated current directory"
        self.cwd.mkdir()
        self.children = []
        self.logs = []

    def tearDown(self):
        try:
            for process in self.children:
                self.stop(process)
        finally:
            for stream in self.logs:
                stream.close()
        # Windows can briefly retain a file handle after an exited child releases it.
        # Never remove a running child's files or hide a persistent cleanup failure.
        self.assertTrue(all(process.poll() is not None for process in self.children))
        for attempt in range(31):
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 30:
                    raise
                time.sleep(0.1)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def command(self, *arguments):
        return [sys.executable, "-S", "-B", str(self.script), *map(str, arguments)]

    def request(self, origin, method, path, data=None, token=None):
        target = urlsplit(origin)
        self.assertEqual("127.0.0.1", target.hostname)
        connection = HTTPConnection(target.hostname, target.port, timeout=2)
        headers = {}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers = {"Content-Type": "application/json", "Origin": origin.rstrip("/")}
            if token is not None:
                headers["X-CSRF-Token"] = token
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            raw = response.read()
            metadata = dict(response.getheaders())
            payload = (
                json.loads(raw)
                if metadata.get("Content-Type", "").startswith("application/json")
                else raw.decode()
            )
            return response.status, metadata, payload
        finally:
            connection.close()

    def start(self, output=None, *, resume=False, old_token=None):
        log_path = self.root / f"child-{len(self.children)}.log"
        stream = log_path.open("wb")
        self.logs.append(stream)
        arguments = [] if output is None else ["--resume" if resume else "--output", output]
        process = subprocess.Popen(
            self.command(*arguments),
            cwd=self.cwd,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        self.children.append(process)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail(
                    f"Launcher exited {process.returncode}: {log_path.read_text(errors='replace')}"
                )
            candidates = (
                [output / "session.json"]
                if output
                else list((self.bundle / "demo-sessions").glob("*/session.json"))
            )
            for path in candidates:
                try:
                    marker = json.loads(path.read_text(encoding="utf-8"))
                    if marker.get("status") != "ready":
                        continue
                    status, _, bootstrap = self.request(marker["url"], "GET", "/api/app")
                    if status == 200 and bootstrap["csrf_token"] != old_token:
                        return process, path.parent, marker, bootstrap
                except (OSError, ValueError, KeyError):
                    pass
            time.sleep(0.05)
        self.fail(f"Launcher did not become ready: {log_path.read_text(errors='replace')}")

    def failed_run(self, *arguments):
        result = subprocess.run(
            self.command(*arguments), cwd=self.cwd, capture_output=True, timeout=15
        )
        self.assertNotEqual(0, result.returncode, result.stdout)
        self.assertNotIn(b"Traceback", result.stderr)
        return result

    def test_standalone_demo_contract_conversion_csv_and_deduplicated_alert(self):
        _, output, marker, app = self.start(self.root / "fresh demo with spaces")
        self.assertEqual("currency-prompter-offline-demo", marker["kind"])
        self.assertEqual(1, marker["schema"])
        self.assertTrue((output / "demo.sqlite").is_file())
        origin, token = marker["url"].rstrip("/"), app["csrf_token"]
        self.assertEqual(165, len(app["currencies"]))
        self.assertEqual(3, len(app["watchlist"]))
        for route, content_type in (
            ("/", "text/html"),
            ("/app.js", "application/javascript"),
            ("/app.css", "text/css"),
        ):
            status, headers, content = self.request(origin, "GET", route)
            self.assertEqual(200, status)
            self.assertTrue(headers["Content-Type"].startswith(content_type))
            self.assertTrue(content)
        self.assertEqual(
            {"status": "ok", "mode": "local-app"}, self.request(origin, "GET", "/health")[2]
        )
        status, _, dashboard = self.request(origin, "GET", "/api/dashboard?days=90")
        self.assertEqual(200, status)
        self.assertEqual("synthetic-demo", dashboard["source"])
        self.assertEqual(90, len(dashboard["points"]))
        self.assertEqual("26.0364", dashboard["latest"]["rate"])
        self.assertEqual([], dashboard["alerts"])
        self.assertEqual(1, len(dashboard["rules"]))
        rule = dashboard["rules"][0]
        for key, value in {
            "name": "SGD buy target (demo)",
            "direction": "at_or_below",
            "threshold": "26.0364",
            "cooldown_seconds": 86400,
        }.items():
            self.assertEqual(value, rule[key])
        status, _, conversion = self.request(
            origin,
            "POST",
            "/api/convert",
            {**PAIR, "amount": "100", "direction": "base_to_counter", "fee_percent": "2"},
            token,
        )
        self.assertEqual(200, status)
        self.assertEqual("2551.5672", conversion["result"])
        status, headers, exported = self.request(origin, "GET", "/api/export")
        self.assertEqual(200, status)
        self.assertIn("attachment", headers["Content-Disposition"])
        rows = list(csv.DictReader(io.StringIO(exported)))
        self.assertEqual(90, len(rows))
        self.assertEqual({"synthetic-demo"}, {row["source"] for row in rows})
        self.assertEqual(dashboard["latest"]["rate"], rows[-1]["rate"])
        for expected in ("queued", "duplicate"):
            status, _, refresh = self.request(origin, "POST", "/api/refresh", PAIR, token)
            self.assertEqual(200, status, refresh)
            self.assertEqual([expected], refresh["processing"]["outcomes"])
        self.assertEqual(1, len(self.request(origin, "GET", "/api/dashboard")[2]["alerts"]))
        for route, body in (
            ("/api/refresh", {**PAIR, "mode": "reference"}),
            ("/api/history", {"base": "SGD", "counter": "THB"}),
            ("/api/currencies/refresh", {}),
        ):
            status, _, error = self.request(origin, "POST", route, body, token)
            self.assertEqual(409, status, error)
            self.assertIn("offline demo", error["error"].lower())
        self.assertIsNone(self.request(origin, "GET", "/api/dashboard?mode=reference")[2]["latest"])

    def test_resume_preserves_state_without_reseeding_and_replaces_csrf_token(self):
        process, output, marker, app = self.start(self.root / "resumable session")
        origin, token = marker["url"].rstrip("/"), app["csrf_token"]
        extra = {"base": "GBP", "counter": "JPY"}
        self.assertEqual(200, self.request(origin, "POST", "/api/watchlist", extra, token)[0])
        self.assertEqual(200, self.request(origin, "POST", "/api/refresh", PAIR, token)[0])
        before = self.request(origin, "GET", "/api/dashboard")[2]
        self.stop(process)
        _, _, resumed, after_app = self.start(output, resume=True, old_token=token)
        self.assertNotEqual(token, after_app["csrf_token"])
        self.assertIn(extra, after_app["watchlist"])
        after = self.request(resumed["url"], "GET", "/api/dashboard")[2]
        self.assertEqual(before["rules"], after["rules"])
        self.assertEqual(before["alerts"], after["alerts"])
        self.assertEqual(403, self.request(resumed["url"], "POST", "/api/refresh", PAIR, token)[0])
        status, _, refresh = self.request(
            resumed["url"], "POST", "/api/refresh", PAIR, after_app["csrf_token"]
        )
        self.assertEqual(200, status)
        self.assertEqual(["duplicate"], refresh["processing"]["outcomes"])

    def test_concurrent_resume_refuses_without_touching_running_session(self):
        process, output, marker, app = self.start(self.root / "already running session")
        origin, token = marker["url"].rstrip("/"), app["csrf_token"]
        extra = {"base": "GBP", "counter": "JPY"}
        self.assertEqual(200, self.request(origin, "POST", "/api/watchlist", extra, token)[0])
        self.assertEqual(200, self.request(origin, "POST", "/api/refresh", PAIR, token)[0])
        before_state = self.request(origin, "GET", "/api/dashboard")[2]
        before_files = {
            name: (output / name).read_bytes() for name in ("session.json", "demo.sqlite")
        }
        result = self.failed_run("--resume", output)
        self.assertEqual(2, result.returncode)
        self.assertIn(b"already in use", result.stdout + result.stderr)
        self.assertIsNone(process.poll())
        self.assertEqual(
            before_files,
            {name: (output / name).read_bytes() for name in before_files},
        )
        self.assertEqual(before_state, self.request(origin, "GET", "/api/dashboard")[2])
        self.assertIn(extra, self.request(origin, "GET", "/api/app")[2]["watchlist"])

    def test_default_sessions_are_unique_and_relative_to_copied_application(self):
        process, first, _, app = self.start()
        self.stop(process)
        _, second, _, _ = self.start(old_token=app["csrf_token"])
        self.assertEqual(self.bundle / "demo-sessions", first.parent)
        self.assertEqual(first.parent, second.parent)
        self.assertNotEqual(first, second)
        self.assertTrue((first / "demo.sqlite").is_file())

    def test_existing_output_is_refused_without_changing_contents(self):
        output = self.root / "existing folder"
        output.mkdir()
        sentinel = output / "keep.bin"
        sentinel.write_bytes(b"original\x00\xff")
        self.failed_run("--output", output)
        self.assertEqual([sentinel], list(output.iterdir()))
        self.assertEqual(b"original\x00\xff", sentinel.read_bytes())
        self.failed_run("--output", sentinel)
        self.assertEqual(b"original\x00\xff", sentinel.read_bytes())

    def test_resume_requires_valid_marker_and_existing_database(self):
        missing = self.root / "not created"
        self.failed_run("--resume", missing)
        self.assertFalse(missing.exists())
        for index, (marker, with_db) in enumerate(
            (
                (b"not json", True),
                (b'{"kind":"other","schema":1}', True),
                (b'{"kind":"currency-prompter-offline-demo","schema":1}', False),
            )
        ):
            folder = self.root / f"invalid resume {index}"
            folder.mkdir()
            marker_path = folder / "session.json"
            marker_path.write_bytes(marker)
            if with_db:
                (folder / "demo.sqlite").write_bytes(b"do not overwrite")
            before = {p.name: p.read_bytes() for p in folder.iterdir()}
            self.failed_run("--resume", folder)
            self.assertEqual(before, {p.name: p.read_bytes() for p in folder.iterdir()})

    def test_occupied_port_fails_without_publishing_ready_marker(self):
        output = self.root / "occupied port session"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen(1)
            self.failed_run("--output", output, "--port", occupied.getsockname()[1])
        marker = output / "session.json"
        if marker.exists():
            self.assertNotEqual("ready", json.loads(marker.read_text())["status"])


if __name__ == "__main__":
    unittest.main()
