#!/usr/bin/env python3
"""
MVP web front door — a thin prototype so there's something clickable to show funders and board.

Deliberately reuses the REAL navigator logic (navigator.build_plan / generate_letter) rather than
reimplementing anything in JS, so the web output can never drift from the CLI. Stdlib only
(http.server) — no framework, no build step.

  python3 web/server.py            # serves http://localhost:8000
  python3 web/server.py --port 9000 --offline

GET  /            -> the intake page (web/index.html)
GET  /hospitals   -> JSON list of hospital names (for the datalist autocomplete)
POST /plan        -> {hospital, income, household, insurance, in_collections, lang}
                     -> {tier, plan, letter, hospital}
"""
import argparse
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
import navigator

HERE = os.path.dirname(os.path.abspath(__file__))
DS, SRC = navigator.load_dataset()
HOSPITALS = sorted(r["hospital"].title() for r in DS["rows"])


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):            # quiet the default per-request logging
        pass

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path in ("/landing", "/landing.html"):
            with open(os.path.join(HERE, "landing.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/hospitals":
            return self._send(200, json.dumps(HOSPITALS))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/plan":
            return self._send(404, json.dumps({"error": "not found"}))
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "invalid JSON"}))

        row = navigator.find_hospital(DS, name=(req.get("hospital") or "").strip())
        if not row:
            return self._send(404, json.dumps({"error": "hospital not found"}))
        try:
            intake = {
                "first_name": (req.get("first_name") or "Patient").strip(),
                "last_name": "", "household_size": int(req.get("household") or 1),
                "annual_income": int(req.get("income") or 0),
                "insurance": req.get("insurance") or "uninsured",
                "in_collections": bool(req.get("in_collections")),
            }
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "income and household must be numbers"}))

        lang = "es" if req.get("lang") == "es" else "en"
        pct, tier, plan = navigator.build_plan(intake, row, lang=lang)
        result = navigator.build_plan_struct(intake, row, lang=lang)   # structured, for the rich UI
        letter = navigator.generate_letter(intake, row, pct, tier)     # always English (for the hospital)
        self._send(200, json.dumps({
            "tier": tier, "plan": plan, "result": result, "letter": letter,
            "hospital": row["hospital"].title(),
        }))


def main():
    ap = argparse.ArgumentParser(description="Cobijo Health MVP web server")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--offline", action="store_true", help="skip PolicyEngine (FPL heuristic only)")
    args = ap.parse_args()
    if args.offline:
        navigator.USE_POLICYENGINE = False
    print(f"Cobijo Health MVP — http://localhost:{args.port}  "
          f"[{len(HOSPITALS)} hospitals from {SRC}]", file=sys.stderr)
    # Threaded so one slow PolicyEngine call can't block every other request (public-facing).
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
