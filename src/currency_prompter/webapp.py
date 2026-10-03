"""Loopback-only tracker UI with explicit, CSRF-protected mutation endpoints."""

import hmac
import json
import secrets
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from .app_service import AppError, AppService

MAX_BODY = 32_768
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise AppError("Duplicate JSON fields are not accepted.")
        value[key] = item
    return value


def _constant(value):
    raise AppError("JSON numbers must be finite.")


def make_app_server(db, port=8765, *, service=None):
    """Create a local server; nothing starts listening for requests in a thread here."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("port must be an integer from 0 to 65535")
    application = service if service is not None else AppService(db)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass

        def respond(self, status, payload, content_type="application/json", *, filename=None):
            if content_type == "application/json":
                body = json.dumps(payload, allow_nan=False).encode("utf-8")
            else:
                body = payload if isinstance(payload, bytes) else payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            if filename is not None:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(body)

        def request_target(self, mutation=False):
            allowed = {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in allowed:
                raise AppError("A local Host header is required.", 403)
            origins = self.headers.get_all("Origin", [])
            if origins and (len(origins) != 1 or origins[0] != "http://" + hosts[0]):
                raise AppError("This request must come from the local app origin.", 403)
            if mutation:
                if not origins:
                    raise AppError("A same-origin Origin header is required.", 403)
                tokens = self.headers.get_all("X-CSRF-Token", [])
                if (
                    len(tokens) != 1
                    or not tokens[0].isascii()
                    or not hmac.compare_digest(tokens[0], token)
                ):
                    raise AppError("The app token is missing or expired. Reload the page.", 403)
            if not self.path.startswith("/") or self.path.startswith("//") or len(self.path) > 2048:
                raise AppError("Invalid request target.")
            try:
                target = urlsplit(self.path)
                if target.fragment:
                    raise AppError("URL fragments are not accepted in requests.")
                query = parse_qs(target.query, keep_blank_values=True, max_num_fields=8)
            except ValueError as error:
                raise AppError("Invalid request query.") from error
            if any(len(values) != 1 for values in query.values()):
                raise AppError("Duplicate query parameters are not accepted.")
            if mutation and query:
                raise AppError("Mutation endpoints do not accept query parameters.")
            return target.path, {key: values[0] for key, values in query.items()}

        def body(self):
            if self.headers.get("Transfer-Encoding") is not None:
                raise AppError("Transfer-Encoding is not supported.")
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                raise AppError("One valid Content-Length header is required.", 411)
            size = int(lengths[0])
            if size > MAX_BODY:
                raise AppError("Request body is too large.", 413)
            content_types = self.headers.get_all("Content-Type", [])
            if (
                len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip().lower() != "application/json"
            ):
                raise AppError("Content-Type must be application/json.", 415)
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise AppError("The request body is incomplete.")
            try:
                value = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant
                )
            except (ValueError, UnicodeError, RecursionError) as error:
                raise AppError(
                    "Request body must be a valid UTF-8 JSON object with unique fields."
                ) from error
            if not isinstance(value, dict):
                raise AppError("Request body must be a JSON object.")
            return value

        def guarded(self, action):
            try:
                action()
            except AppError as error:
                self.respond(error.status, {"error": str(error)})
            except ValueError as error:
                self.respond(400, {"error": str(error)})
            except sqlite3.Error:
                self.respond(
                    503,
                    {
                        "error": "Local storage is unavailable. Existing records have not been replaced."
                    },
                )
            except (TimeoutError, ConnectionError, BrokenPipeError):
                self.close_connection = True
            except OSError:
                self.respond(503, {"error": "The local app could not access a required file."})
            except Exception:
                self.respond(
                    500,
                    {
                        "error": "The request could not be completed. Check the current state before retrying."
                    },
                )

        def do_GET(self):
            self.guarded(self.get)

        def get(self):
            path, query = self.request_target()
            permitted = {
                "/api/dashboard": {"base", "counter", "mode", "days"},
                "/api/export": {"base", "counter", "mode"},
                "/api/watchlist": {"mode"},
            }
            if set(query) - permitted.get(path, set()):
                raise AppError("Unexpected query parameter.")
            base, counter, mode = (
                query.get("base", "SGD"),
                query.get("counter", "THB"),
                query.get("mode", "demo"),
            )
            if path == "/health":
                self.respond(200, {"status": "ok", "mode": "local-app"})
            elif path == "/api/app":
                self.respond(200, {**application.bootstrap(), "csrf_token": token})
            elif path == "/api/dashboard":
                try:
                    days = int(query.get("days", "30"))
                except ValueError as error:
                    raise AppError("Chart range must be a whole number of days.") from error
                self.respond(200, application.dashboard(base, counter, mode, days))
            elif path == "/api/watchlist":
                self.respond(200, application.watchlist(mode))
            elif path == "/api/export":
                data = application.export(base, counter, mode)
                self.respond(200, data, "text/csv", filename=f"{base}-{counter}-{mode}.csv")
            else:
                static = {
                    "/": ("index.html", "text/html"),
                    "/app.css": ("app.css", "text/css"),
                    "/app.js": ("app.js", "application/javascript"),
                    "/static/app.css": ("app.css", "text/css"),
                    "/static/app.js": ("app.js", "application/javascript"),
                }
                if path not in static:
                    raise AppError("Route was not found.", 404)
                name, kind = static[path]
                self.respond(
                    200, files("currency_prompter").joinpath("static", name).read_bytes(), kind
                )

        def mutate(self):
            path, _ = self.request_target(mutation=True)
            actions = {
                ("POST", "/api/currencies/refresh"): application.refresh_currencies,
                ("POST", "/api/watchlist"): application.set_watchlist,
                ("DELETE", "/api/watchlist"): lambda body: application.set_watchlist(
                    body, remove=True
                ),
                ("POST", "/api/rules"): application.create_rule,
                ("PATCH", "/api/rules"): application.update_rule,
                ("DELETE", "/api/rules"): lambda body: application.update_rule(body, delete=True),
                ("POST", "/api/refresh"): application.refresh,
                ("POST", "/api/history"): application.import_history,
                ("POST", "/api/convert"): application.convert,
            }
            action = actions.get((self.command, path))
            if action is None:
                raise AppError("This method and route are not supported.", 405)
            self.respond(200, action(self.body()))

        def do_POST(self):
            self.guarded(self.mutate)

        def do_PATCH(self):
            self.guarded(self.mutate)

        def do_DELETE(self):
            self.guarded(self.mutate)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.application = application
    return server
