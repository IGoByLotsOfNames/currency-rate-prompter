"""Run a fresh, offline reviewer demo directly from the source tree."""

import argparse
import json
import os
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from currency_prompter.app_service import AppError, AppService  # noqa: E402
from currency_prompter.domain import FakeClock, utc  # noqa: E402
from currency_prompter.webapp import make_app_server  # noqa: E402

KIND = "currency-prompter-offline-demo"
DEMO_CLOCK = "2026-10-03T12:00:00Z"
OFFLINE_NOTE = "This offline demo cannot fetch reference data. Select Demo to continue."
EXAMPLE_RULE = {
    "name": "SGD buy target (demo)",
    "base": "SGD",
    "counter": "THB",
    "mode": "demo",
    "direction": "at_or_below",
    "threshold": "26.0364",
    "cooldown_seconds": 86400,
}


def offline_provider(*args, **kwargs):
    """Fail closed if a future demo path reaches a provider factory."""
    raise AppError(OFFLINE_NOTE, 409)


class DemoService(AppService):
    """Keep the normal application intact; restrict only this demo instance."""

    def __init__(self, db):
        super().__init__(
            db,
            clock=FakeClock(utc(DEMO_CLOCK)),
            provider_factory=offline_provider,
            history_factory=offline_provider,
            catalogue_factory=offline_provider,
        )

    def refresh(self, body):
        if isinstance(body, dict) and body.get("mode") == "reference":
            raise AppError(OFFLINE_NOTE, 409)
        return super().refresh(body)

    def import_history(self, body):
        raise AppError(OFFLINE_NOTE, 409)

    def refresh_currencies(self, body):
        raise AppError(OFFLINE_NOTE, 409)


class SessionLock:
    """An OS-owned lock releases even if the process is forcibly stopped."""

    def __init__(self, directory):
        path = directory / ".session.lock"
        if path.resolve().parent != directory:
            raise ValueError("The session lock must stay inside its demo folder.")
        self.file = path.open("a+b")
        try:
            if self.file.seek(0, 2) == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise ValueError(
                "This demo session is already in use; stop its server first."
            ) from error

    def close(self):
        self.file.close()


def save_session(directory, record):
    """Replace a small manifest atomically so readers never see half-written JSON."""
    temporary = directory / "session.json.tmp"
    temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    temporary.replace(directory / "session.json")


def prepare_session(output=None, resume=None):
    if resume is not None:
        directory = resume.resolve()
        for name in ("session.json", "session.json.tmp", "demo.sqlite"):
            if (directory / name).resolve().parent != directory:
                raise ValueError("Resumed files must stay inside their demo session directory.")
        record = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        if (
            not isinstance(record, dict)
            or record.get("kind") != KIND
            or record.get("schema") != 1
            or record.get("database") != "demo.sqlite"
            or not (directory / "demo.sqlite").is_file()
        ):
            raise ValueError("Resume requires an existing offline demo session and its database.")
        return directory, record
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    directory = (output if output is not None else ROOT / "demo-sessions" / name).resolve()
    # Refuse even an empty existing directory: this command never resets user data.
    directory.mkdir(parents=True, exist_ok=False)
    record = {
        "kind": KIND,
        "schema": 1,
        "database": "demo.sqlite",
        "demo_clock": DEMO_CLOCK,
        "status": "preparing",
    }
    save_session(directory, record)
    return directory, record


def check_ready(port):
    """Read the real loopback routes without consuming the walkthrough's alert."""
    routes = {
        "/health": "application/json",
        "/": "text/html",
        "/app.js": "application/javascript",
        "/app.css": "text/css",
    }
    for route, content_type in routes.items():
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", route)
            response = connection.getresponse()
            body = response.read(1_000_001)
            if (
                response.status != 200
                or not response.getheader("Content-Type", "").startswith(content_type)
                or not body
                or len(body) > 1_000_000
            ):
                raise ValueError(f"The demo readiness check failed for {route}.")
            if route == "/health" and json.loads(body).get("status") != "ok":
                raise ValueError("The demo health check failed.")
        finally:
            connection.close()
    return list(routes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    session = parser.add_mutually_exclusive_group()
    session.add_argument("--output", type=Path, help="new session directory; must not exist")
    session.add_argument("--resume", type=Path, help="reopen a previous offline demo session")
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 chooses a free port")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be 0-65535")
    server, thread, directory, record, lock = None, None, None, None, None
    try:
        directory, record = prepare_session(args.output, args.resume)
        lock = SessionLock(directory)
        record["status"] = "starting"
        service = DemoService(directory / "demo.sqlite")
        if args.resume is None:
            service.create_rule(EXAMPLE_RULE)
        server = make_app_server(service.db, args.port, service=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        routes = check_ready(server.server_port)
        url = f"http://127.0.0.1:{server.server_port}/"
        record.update(status="ready", url=url, readiness_routes=routes)
        save_session(directory, record)
        print(f"OFFLINE DEMO ready: {url}", flush=True)
        print(f"Session: {directory}", flush=True)
        print("Select Demo and SGD / THB. Synthetic rate: 1 SGD = 26.0364 THB.", flush=True)
        if args.resume is None:
            print(
                "Fresh session: one example target; first Refresh creates one local alert.",
                flush=True,
            )
        else:
            print("Resumed session: saved targets and alerts are retained.", flush=True)
        print("Reference downloads are disabled. No browser opens automatically.", flush=True)
        print("Keep this terminal open; Ctrl+C stops the demo. See docs/demo.md.", flush=True)
        while thread.is_alive():
            threading.Event().wait(0.25)
        raise OSError("The local demo server stopped unexpectedly.")
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error, HTTPException) as error:
        print(f"Could not start the offline demo: {error}", file=sys.stderr)
        print(
            "Use a new --output folder, or --resume for a saved demo; --port 0 is automatic.",
            file=sys.stderr,
        )
        return 2
    finally:
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
            server.server_close()
        if thread is not None and thread.ident is not None:
            thread.join(timeout=5)
        if directory is not None and record is not None and lock is not None:
            record["status"] = "stopped" if record.get("status") == "ready" else "failed"
            try:
                save_session(directory, record)
            except OSError:
                pass  # Preserve the original error if storage also became unavailable.
        if lock is not None:
            lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
