#!/usr/bin/env python3
"""
Integration tests — exercise the two live channels over real HTTP (in-process, ephemeral port,
no network beyond localhost, PolicyEngine forced off) plus the freshness-monitor diff. These
catch route/wiring regressions the unit tests can't (bad JSON, 404s, TwiML shape).

Run (from repo root):  python3 -m unittest discover -s tests
"""
import json
import os
import sys
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import HTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)                       # repo root (navigator, sms, freshness_monitor)
sys.path.insert(0, os.path.join(_ROOT, "web"))  # web/server.py

import navigator
import server
import sms
import freshness_monitor


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode("utf-8", "replace")


def _post_json(url, obj):
    req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status, json.loads(r.read())


def _serve(handler_cls):
    navigator.USE_POLICYENGINE = False
    httpd = HTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


class TestWebServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd, cls.base = _serve(server.Handler)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_index_served(self):
        status, body = _get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("Cobijo", body)

    def test_landing_served(self):
        status, body = _get(self.base + "/landing")
        self.assertEqual(status, 200)
        self.assertIn("<h1", body)

    def test_hospitals_json(self):
        status, body = _get(self.base + "/hospitals")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(body), list)

    def test_server_is_threaded(self):
        # The public server must be threaded so one slow PolicyEngine call can't block every
        # other request (S#5). Guards against reverting to the blocking HTTPServer.
        self.assertTrue(hasattr(server, "ThreadingHTTPServer"),
                        "web/server.py must import ThreadingHTTPServer, not the blocking HTTPServer")

    def test_plan_happy_path(self):
        hosp = server.HOSPITALS[0]
        status, d = _post_json(self.base + "/plan",
                               {"hospital": hosp, "income": "30000", "household": "4",
                                "insurance": "uninsured", "in_collections": True, "lang": "en"})
        self.assertEqual(status, 200)
        self.assertIn(d["result"]["tier"], ("free", "discount", "high_cost", "over"))
        self.assertTrue(d["letter"])

    def test_plan_unknown_hospital_404(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            _post_json(self.base + "/plan", {"hospital": "No Such Hospital ZZZ", "income": "1", "household": "1"})
        self.assertEqual(e.exception.code, 404)

    def test_bad_json_400(self):
        req = urllib.request.Request(self.base + "/plan", data=b"{not json",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 400)

    def test_unknown_route_404(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            _get(self.base + "/nope")
        self.assertEqual(e.exception.code, 404)


class TestSMSWebhook(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds, _ = navigator.load_dataset()
        cls.httpd, cls.base = _serve(sms.make_handler(ds))

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _sms(self, frm, body):
        data = urllib.parse.urlencode({"From": frm, "Body": body}).encode()
        with urllib.request.urlopen(self.base + "/sms", data=data, timeout=5) as r:
            return r.read().decode()

    def test_twiml_conversation(self):
        frm = "+15550000001"
        first = self._sms(frm, "hi")
        self.assertIn("<Response><Message>", first)          # valid TwiML
        self.assertIn("English", first)                      # welcome prompt
        self._sms(frm, "1")                                  # choose English (session persists per From)
        r = self._sms(frm, "not-a-real-hospital")            # unknown hospital -> re-ask
        self.assertIn("find", r.lower())

    def test_bad_content_length_does_not_crash(self):
        # A non-numeric Content-Length used to raise ValueError and reset the connection; now it
        # must return valid TwiML (never 500 a Twilio webhook).
        import socket
        host, port = self.httpd.server_address
        s = socket.create_connection((host, port), timeout=5)
        s.sendall(b"POST /sms HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\nFrom=x&Body=hi")
        chunks = []                                  # drain until close (HTTP/1.0) — body can
        while True:                                  # arrive in a separate segment from the headers
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
        s.close()
        resp = b"".join(chunks).decode("utf-8", "replace")
        self.assertIn("200", resp.split("\r\n", 1)[0])
        self.assertIn("<Response>", resp)

    def test_missing_from_no_shared_session(self):
        # Two anonymous requests (no From) must not share a Conversation — each gets a fresh
        # welcome, never advancing on a shared "sim" key.
        def post(body):
            with urllib.request.urlopen(self.base + "/sms", data=body.encode(), timeout=5) as r:
                return r.read().decode()
        self.assertIn("English", post("Body=1"))
        self.assertIn("English", post("Body=Adventist"))


class TestFreshnessDiff(unittest.TestCase):
    def _fp(self, url="u1", date="2025-01-01"):
        return {"post_title": "Test Hospital",
                "policies": {"charity_care": {"current_policy_url": url, "current_effective_date": date},
                             "discount_payment": {"current_policy_url": "d1", "current_effective_date": date}}}

    def test_no_change(self):
        cur = {"h1": freshness_monitor.fingerprint(self._fp())}
        new, removed, changed = freshness_monitor.diff(cur, dict(cur))
        self.assertEqual((new, removed, changed), ([], [], []))

    def test_effective_date_change_detected(self):
        base = {"h1": freshness_monitor.fingerprint(self._fp(date="2025-01-01"))}
        cur = {"h1": freshness_monitor.fingerprint(self._fp(date="2025-06-01"))}
        new, removed, changed = freshness_monitor.diff(cur, base)
        self.assertEqual((new, removed), ([], []))
        self.assertTrue(changed and "effective" in changed[0][2][0])

    def test_new_and_removed(self):
        base = {"old": freshness_monitor.fingerprint(self._fp())}
        cur = {"newh": freshness_monitor.fingerprint(self._fp())}
        new, removed, _ = freshness_monitor.diff(cur, base)
        self.assertEqual((new, removed), (["newh"], ["old"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
