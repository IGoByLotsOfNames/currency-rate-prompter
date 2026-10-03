"""A local, read-only inspection server. It cannot ingest quotes or send alerts."""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit

from .domain import currency
from .report import html_report
from .storage import Store


def make_server(db, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, content, content_type="application/json"):
            body = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            # Consume a small declared body before closing. Closing over unread bytes can
            # reset the connection on Windows before a client receives the rejection.
            self.connection.settimeout(2)
            lengths = self.headers.get_all("Content-Length", [])
            if (
                len(lengths) == 1
                and lengths[0].isascii()
                and lengths[0].isdigit()
                and len(lengths[0]) <= 5
                and int(lengths[0]) <= 32768
                and self.headers.get("Transfer-Encoding") is None
            ):
                try:
                    self.rfile.read(int(lengths[0]))
                except (TimeoutError, OSError):
                    self.close_connection = True
                    return
            self.respond(501, '{"error":"This inspection server is read-only."}')

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

        def do_GET(self):
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            if self.headers.get("Host") not in allowed:
                self.respond(403, '{"error":"local host required"}')
                return
            try:
                if (
                    not self.path.startswith("/")
                    or self.path.startswith("//")
                    or len(self.path) > 2048
                ):
                    raise ValueError(
                        "an origin-form request target of at most 2048 characters is required"
                    )
                parts = urlsplit(self.path)
                params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=6)
            except ValueError as exc:
                self.respond(400, json.dumps({"error": str(exc)}))
                return
            if parts.path not in ("/", "/health", "/api/quotes", "/api/alerts"):
                self.respond(404, '{"error":"not found"}')
                return
            try:
                if any(len(value) != 1 for value in params.values()):
                    raise ValueError("duplicate query parameters")
                allowed_params = (
                    {"limit", "base", "counter"}
                    if parts.path == "/api/quotes"
                    else {"limit"}
                    if parts.path == "/api/alerts"
                    else set()
                )
                if set(params) - allowed_params:
                    raise ValueError("unexpected query parameter")
                limit = int(params.get("limit", ["100"])[0])
                if not 1 <= limit <= 1000:
                    raise ValueError("limit must be 1-1000")
                base, counter = params.get("base", [None])[0], params.get("counter", [None])[0]
                if (base is None) != (counter is None):
                    raise ValueError("base and counter must be provided together")
                if base is not None:
                    currency(base)
                    currency(counter)
                with Store(db, readonly=True) as store:
                    if parts.path == "/":
                        self.respond(200, html_report(store), "text/html")
                    elif parts.path == "/health":
                        self.respond(
                            200,
                            json.dumps(
                                {"status": "ok", "mode": "read-only", "counts": store.counts()}
                            ),
                        )
                    elif parts.path == "/api/quotes":
                        self.respond(
                            200,
                            json.dumps(
                                {"quotes": store.quotes(base=base, counter=counter, limit=limit)}
                            ),
                        )
                    else:
                        self.respond(200, json.dumps({"alerts": store.alerts(limit)}))
            except ValueError as exc:
                self.respond(400, json.dumps({"error": str(exc)}))

    return HTTPServer(("127.0.0.1", port), Handler)
