from contextlib import redirect_stdout, redirect_stderr
from decimal import Decimal
from io import BytesIO, StringIO
from http.client import HTTPConnection
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from currency_prompter.cli import main
from currency_prompter.domain import utc
from currency_prompter.monitor import load_json
from currency_prompter.provider import Frankfurter, MAX_RESPONSE, parse_response
from currency_prompter.report import chart_svg, html_report
from currency_prompter.server import make_server
from currency_prompter.storage import Store
from test_monitor import quote, rule, START


class ProviderTests(unittest.TestCase):
    def body(self, **kwargs):
        return dict(dict(base="SGD", quote="THB", rate=Decimal("25.1234"), date="2026-01-01"), **kwargs)

    def test_response_contract(self):
        q = parse_response(self.body(), "SGD", "THB", START)
        self.assertEqual(str(q.value), "25.1234")
        self.assertEqual(q.source, "frankfurter")

    def test_invalid_provider_shapes(self):
        for body in ([], {}, self.body(base=None), self.body(quote=[]), self.body(base="USD"),
                     self.body(rate="NaN"), self.body(date="2026-02-30"), self.body(date=42)):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_response(body, "SGD", "THB", START)

    def test_exact_json_decimal_transport(self):
        def transport(request, timeout):
            self.assertEqual(timeout, 10)
            self.assertTrue(request.full_url.endswith("/sgd/thb"))
            return BytesIO(b'{"base":"SGD","quote":"THB","rate":25.123456789123456789,"date":"2026-01-01"}')
        self.assertEqual(Frankfurter(transport=transport).fetch("SGD", "THB", START).value, Decimal("25.123456789123456789"))

    def test_oversized_response_not_retried(self):
        calls = []
        def transport(*a, **k):
            calls.append(1)
            return BytesIO(b"x" * (MAX_RESPONSE + 1))
        with self.assertRaises(ValueError):
            Frankfurter(transport=transport).fetch("SGD", "THB", START)
        self.assertEqual(len(calls), 1)

    def test_retry_bound(self):
        calls, sleeps = [], []
        def transport(*a, **k):
            calls.append(1)
            raise URLError("offline")
        with self.assertRaises(ValueError):
            Frankfurter(transport=transport, sleep=sleeps.append).fetch("SGD", "THB", START)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [.25, .5])

    def test_http_404_not_retried(self):
        calls = []
        def transport(*a, **k):
            calls.append(1)
            raise HTTPError("url", 404, "missing", {}, None)
        with self.assertRaises(ValueError):
            Frankfurter(transport=transport).fetch("SGD", "THB", START)
        self.assertEqual(len(calls), 1)

    def test_bad_json(self):
        with self.assertRaises(ValueError):
            Frankfurter(transport=lambda *a, **k: BytesIO(b"not json")).fetch("SGD", "THB", START)

    def test_timeout_validation(self):
        for kwargs in ({"timeout": 0}, {"timeout": 100}, {"retries": 10}, {"retries": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Frankfurter(**kwargs)


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.sqlite"
        with Store(self.path) as store:
            store.process(quote(), [rule()], START)

    def tearDown(self):
        self.tmp.cleanup()

    def test_html_escapes_database_text(self):
        with Store(self.path) as store:
            store.connection.execute("UPDATE quotes SET source=?", ("<script>alert(1)</script>",))
            store.connection.commit()
            html = html_report(store)
            self.assertNotIn("<script>", html)
            self.assertIn("&lt;script&gt;", html)

    def test_empty_and_single_quote_chart(self):
        self.assertIn("No observations", chart_svg([]))
        with Store(self.path) as store:
            chart = chart_svg(store.quotes())
            self.assertNotIn("nan", chart)
            self.assertIn("<circle", chart)

    def test_local_api_readonly_routes(self):
        server = make_server(self.path, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        root = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(root + "/api/quotes?base=SGD&counter=THB&limit=1", timeout=3) as response:
                self.assertEqual(len(json.load(response)["quotes"]), 1)
            with urlopen(root + "/health", timeout=3) as response:
                self.assertEqual(json.load(response)["mode"], "read-only")
            with urlopen(root + "/", timeout=3) as response:
                self.assertIn(b"Currency Rate Prompter", response.read())
            for path, status in (("/missing", 404), ("/api/quotes?limit=0", 400),
                                 ("/api/quotes?base=SGD", 400), ("/api/quotes?limit=1&limit=2", 400),
                                 ("/api/quotes?base=%27--&counter=THB", 400)):
                with self.subTest(path=path), self.assertRaises(HTTPError) as cm:
                    urlopen(root + path, timeout=3)
                self.assertEqual(cm.exception.code, status)
            with self.assertRaises(HTTPError) as cm:
                urlopen(Request(root + "/", headers={"Host": "evil.example"}), timeout=3)
            self.assertEqual(cm.exception.code, 403)
            with self.assertRaises(HTTPError) as cm:
                urlopen(Request(root + "/api/quotes", data=b"{}"), timeout=3)
            self.assertEqual(cm.exception.code, 501)
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "http://[", headers={"Host": f"127.0.0.1:{server.server_port}"})
            self.assertEqual(connection.getresponse().status, 400)
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_cli_error_exit(self):
        with redirect_stderr(StringIO()) as output:
            result = main(["replay", "--db", str(self.path), "--rules", "no-such-file", "--quotes", "missing"])
        self.assertEqual(result, 2)
        self.assertIn("error:", output.getvalue())

    def test_demo_is_repeat_safe(self):
        target = Path(self.tmp.name) / "demo"
        with redirect_stdout(StringIO()):
            self.assertEqual(main(["demo", "--output", str(target)]), 0)
        data = json.loads((target / "summary.json").read_text())
        self.assertEqual(data["second_replay"]["inserted"], 0)
        self.assertEqual(data["second_replay"]["queued"], 0)
        self.assertEqual(data["stored"]["outbox"], data["stored"]["notifications"])
        with redirect_stderr(StringIO()):
            self.assertEqual(main(["demo", "--output", str(target)]), 2)

    def test_bounded_input(self):
        target = Path(self.tmp.name) / "too-large.json"
        target.write_bytes(b" " * 5_000_001)
        with self.assertRaises(ValueError):
            load_json(target)
