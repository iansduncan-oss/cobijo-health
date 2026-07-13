#!/usr/bin/env python3
"""
MVP web front door — a thin prototype so there's something clickable to show funders and board.

Deliberately reuses the REAL navigator logic (navigator.build_plan / generate_letter) rather than
reimplementing anything in JS, so the web output can never drift from the CLI. Stdlib only
(http.server) — no framework, no build step.

  python3 web/server.py            # serves http://localhost:8000
  python3 web/server.py --port 9000 --offline

GET  /            -> the intake page (templates/home.html via i18n; /<lang>/ for other languages)
GET  /hospitals   -> JSON list of hospital names (for the datalist autocomplete)
POST /plan        -> {hospital, income, household, insurance, in_collections, lang}
                     -> {tier, plan, letter, hospital}
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root
import navigator
import hospital_pages
import i18n

HERE = os.path.dirname(os.path.abspath(__file__))
DS, SRC = navigator.load_dataset()
# Disambiguated, de-duplicated labels for the autocomplete: a name shared by two campuses shows as
# "Name (City)" (or collapses to one entry when it's the same hospital double-listed). find_hospital
# resolves these labels back to the row. See navigator._build_name_index.
HOSPITALS = sorted({r.get("_display") or r["hospital"].title() for r in DS["rows"]})
# Per-hospital SEO pages: slug -> row (built once at startup)
HOSPITAL_INDEX, OSHPDID_TO_SLUG = hospital_pages.build_index(DS["rows"])
# Per-county hub pages: county-slug -> canonical county name (built once at startup)
COUNTY_INDEX = hospital_pages.county_index(HOSPITAL_INDEX)

# --- Security headers applied to every response (defense-in-depth; also set at the CF edge) ---
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Pages use inline <style>/<script> + inline handlers, so script/style need 'unsafe-inline'.
    # analytics.aviontechs.com = self-hosted Plausible (script load + POST /api/event); /plan is same-origin.
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://analytics.aviontechs.com; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "connect-src 'self' https://analytics.aviontechs.com; "
        "form-action 'self'; base-uri 'self'; frame-ancestors 'none'"
    ),
}

# The /embed widget is meant to be framed by any partner site, so it must OPT OUT of the site-wide
# anti-clickjacking headers: drop X-Frame-Options (no "allow-all" value exists) and swap the CSP's
# `frame-ancestors 'none'` for `*`. Everything else (script/style/connect policy) stays locked down.
EMBED_HEADERS = {
    "X-Frame-Options": None,      # None => omitted by _send (can't be framed otherwise)
    "Content-Security-Policy": SECURITY_HEADERS["Content-Security-Policy"].replace(
        "frame-ancestors 'none'", "frame-ancestors *"),
}

# --- Static assets (favicon set, OG image) served from web/ ---
STATIC = {
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/og-image.png": ("og-image.png", "image/png"),
}
ROBOTS = "User-agent: *\nAllow: /\nSitemap: https://cobijohealth.org/sitemap.xml\n"
_SITEMAP_URLS = (i18n.sitemap_paths()                         # home + landing + about/privacy/faq + guides × 10 languages
                 + hospital_pages.hospital_paths(HOSPITAL_INDEX)
                 + hospital_pages.county_paths(HOSPITAL_INDEX))   # 56 county hubs × 10 languages
SITEMAP = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + "".join("  <url><loc>%s</loc></url>\n" % u for u in _SITEMAP_URLS)
    + "</urlset>\n"
)

# --- Simple in-memory per-IP rate limit for POST /plan (abuse guard; CF rate-limiting is the outer layer) ---
RATE_LIMIT = 30          # max requests
RATE_WINDOW = 60         # per seconds
_rl_lock = threading.Lock()
_rl_hits = defaultdict(deque)


def _rate_limited(ip):
    now = time.time()
    with _rl_lock:
        dq = _rl_hits[ip]
        while dq and dq[0] <= now - RATE_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT:
            return True
        dq.append(now)
        if not dq:
            _rl_hits.pop(ip, None)
        return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json", extra_headers=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        # Security headers are the baseline; a route may override one (the /embed widget relaxes the
        # frame headers so it can be embedded) or drop one by passing None.
        headers = {**SECURITY_HEADERS, **(extra_headers or {})}
        # Deterministic-per-URL HTML — a short cache spares low-bandwidth users the full re-download
        # of the 466 server-rendered hospital pages / the tool on every visit.
        if code == 200 and ctype.startswith("text/html") and "Cache-Control" not in headers:
            headers["Cache-Control"] = "public, max-age=300"
        for h, v in headers.items():
            if v is not None:
                self.send_header(h, v)
        self.end_headers()
        if not getattr(self, "_head", False):
            self.wfile.write(data)

    def _serve_contained(self, path, subdir, ctype):
        """Serve a pre-generated static asset from web/<subdir>/, contained via realpath so a crafted
        ../ path can't escape the directory. Used for the /og/ cards and /qr/ codes."""
        root = os.path.realpath(os.path.join(HERE, subdir))
        target = os.path.realpath(os.path.join(HERE, path.lstrip("/")))
        if (target == root or target.startswith(root + os.sep)) and os.path.isfile(target):
            with open(target, "rb") as f:
                return self._send(200, f.read(), ctype,
                                  extra_headers={"Cache-Control": "public, max-age=86400"})
        return self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):            # quiet the default per-request logging
        pass

    def do_HEAD(self):
        # Mirror GET (headers + status) without a body — for uptime checks and link-preview bots.
        self._head = True
        try:
            self.do_GET()
        finally:
            self._head = False

    def do_GET(self):
        path = self.path.split("?", 1)[0]        # ignore query string (e.g. /?hospital=…)
        if path in ("/", "/index.html"):
            return self._send(200, i18n.render("home", "en"), "text/html; charset=utf-8")
        if path in ("/landing", "/landing.html"):
            return self._send(200, i18n.render("landing", "en"), "text/html; charset=utf-8")
        if path == "/embed.js":
            # The loader partners drop on their site (injects the /embed iframe + auto-resizes it).
            try:
                with open(os.path.join(HERE, "embed.js"), "rb") as f:
                    return self._send(200, f.read(), "application/javascript; charset=utf-8",
                                      extra_headers={"Cache-Control": "public, max-age=3600"})
            except FileNotFoundError:
                return self._send(404, json.dumps({"error": "not found"}))
        if path == "/healthz":
            # Cheap, meaningful readiness probe (vs. the 400KB /hospitals list): proves the process
            # is up AND the dataset actually loaded. For deploy/uptime checks.
            return self._send(200, json.dumps({"ok": True, "hospitals": len(DS["rows"])}),
                              extra_headers={"Cache-Control": "no-store"})
        if path == "/hospitals":
            return self._send(200, json.dumps(HOSPITALS))
        if path == "/california-hospitals":
            return self._send(200, hospital_pages.render_directory(HOSPITAL_INDEX), "text/html; charset=utf-8")
        if path.startswith("/hospital/"):
            slug = path[len("/hospital/"):].strip("/")
            row = HOSPITAL_INDEX.get(slug)
            if row:
                return self._send(200, hospital_pages.render_hospital(row, slug, HOSPITAL_INDEX),
                                  "text/html; charset=utf-8")
            return self._send(404, json.dumps({"error": "hospital not found"}))
        if path in STATIC:
            fn, ctype = STATIC[path]
            try:
                with open(os.path.join(HERE, fn), "rb") as f:
                    return self._send(200, f.read(), ctype,
                                      extra_headers={"Cache-Control": "public, max-age=86400"})
            except FileNotFoundError:
                return self._send(404, json.dumps({"error": "not found"}))
        if path.startswith("/og/") and path.endswith(".png"):
            # Per-hospital / per-county / per-guide OG share cards (scripts/gen_og_images.py).
            return self._serve_contained(path, "og", "image/png")
        if path.startswith("/qr/") and path.endswith(".svg"):
            # Per-page QR codes for the print handouts (scripts/gen_qr_codes.py).
            return self._serve_contained(path, "qr", "image/svg+xml")
        if path == "/robots.txt":
            return self._send(200, ROBOTS, "text/plain; charset=utf-8")
        if path == "/sitemap.xml":
            return self._send(200, SITEMAP, "application/xml; charset=utf-8")
        # Localized routes: /<lang>/ (the tool) and /<lang>/<page> (e.g. /es/about, /fa/landing).
        # English lives at / and /<page>; other languages are prefixed. Home is served at the root
        # of each language (never /home) so the canonical URL stays /<lang>/.
        parts = [p for p in path.strip("/").split("/") if p]
        lang, rest = "en", parts
        if parts and parts[0] in i18n.LANGS and parts[0] != "en":
            lang, rest = parts[0], parts[1:]
        if len(rest) == 0 and lang != "en":
            return self._send(200, i18n.render("home", lang), "text/html; charset=utf-8")
        if len(rest) == 1 and rest[0] in ("landing", "about", "privacy", "faq", "support", "for-partners"):
            return self._send(200, i18n.render(rest[0], lang), "text/html; charset=utf-8")
        if len(rest) == 1 and rest[0] == "embed":     # /embed and /<lang>/embed — the iframe widget
            return self._send(200, i18n.render("home", lang, embed=True), "text/html; charset=utf-8",
                              extra_headers=EMBED_HEADERS)
        # Localized SEO pages (this block also serves English, since /guides/… and /hospitals/… fall
        # through to here with lang="en"): the directory, per-hospital, per-county hubs, and guides.
        if rest == ["california-hospitals"]:
            return self._send(200, hospital_pages.render_directory(HOSPITAL_INDEX, lang), "text/html; charset=utf-8")
        if len(rest) == 2 and rest[0] == "hospital":
            row = HOSPITAL_INDEX.get(rest[1])
            if row:
                return self._send(200, hospital_pages.render_hospital(row, rest[1], HOSPITAL_INDEX, lang),
                                  "text/html; charset=utf-8")
            return self._send(404, json.dumps({"error": "hospital not found"}))
        if len(rest) == 2 and rest[0] == "hospitals":     # per-county hub: /hospitals/<county>
            county = COUNTY_INDEX.get(rest[1])
            if county:
                return self._send(200, hospital_pages.render_county(county, HOSPITAL_INDEX, lang),
                                  "text/html; charset=utf-8")
            return self._send(404, json.dumps({"error": "county not found"}))
        if len(rest) == 2 and rest[0] == "guides":        # evergreen explainer: /guides/<slug>
            page = i18n.render_guide(rest[1], lang)
            if page:
                return self._send(200, page, "text/html; charset=utf-8")
            return self._send(404, json.dumps({"error": "guide not found"}))
        self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path != "/plan":
            return self._send(404, json.dumps({"error": "not found"}))
        ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        if _rate_limited(ip):
            return self._send(429, json.dumps({"error": "Too many requests — please wait a moment."}),
                              extra_headers={"Retry-After": str(RATE_WINDOW)})
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "invalid JSON"}))

        # Prefer an explicit oshpdid (the SEO hospital-page CTA sends it) so a shared hospital name
        # resolves to the exact campus the patient was reading about; fall back to the typed name.
        row = navigator.find_hospital(DS, oshpdid=(req.get("oshpdid") or "").strip() or None,
                                      name=(req.get("hospital") or "").strip())
        # Not in our dataset: a bare miss stays a 404 (so the UI can nudge "pick from the list" for a
        # typo). But if the user opted into general guidance (`generic`), don't dead-end — every CA
        # hospital owes charity care and the benefit/debt screen is hospital-agnostic.
        if not row and not req.get("generic"):
            return self._send(404, json.dumps({"error": "hospital not found"}))
        try:
            full_name = (req.get("full_name") or "").strip()
            intake = {
                "full_name": full_name,                     # "" -> letter shows [Your full name]
                "first_name": full_name.split()[0] if full_name else "there",   # greeting only (not shown in web UI)
                "last_name": "", "household_size": int(req.get("household") or 1),
                "annual_income": int(req.get("income") or 0),
                "insurance": req.get("insurance") or "uninsured",
                "in_collections": bool(req.get("in_collections")),
            }
        except (ValueError, TypeError):
            return self._send(400, json.dumps({"error": "income and household must be numbers"}))

        lang = req.get("lang") if req.get("lang") in i18n.LANGS else "en"
        if not row:
            # Hospital-independent recovery plan (charity tier = unknown, "apply anyway").
            intake["hospital_name"] = (req.get("hospital") or "").strip()
            result = navigator.build_generic_plan_struct(intake, lang=lang)
            syn_row = {"hospital": intake["hospital_name"] or "[Hospital name]"}
            letter = navigator.generate_letter(intake, syn_row, result["fpl_pct"], "unknown")
            return self._send(200, json.dumps({
                "tier": "unknown", "plan": None, "result": result, "letter": letter,
                "hospital": result["hospital"]["name"] or "",
            }))
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
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address (default loopback; set 0.0.0.0 behind a firewalled reverse proxy)")
    ap.add_argument("--offline", action="store_true", help="skip PolicyEngine (FPL heuristic only)")
    args = ap.parse_args()
    if args.offline:
        navigator.USE_POLICYENGINE = False
    print(f"Cobijo Health MVP — http://localhost:{args.port}  "
          f"[{len(HOSPITALS)} hospitals from {SRC}]", file=sys.stderr)
    # Threaded so one slow PolicyEngine call can't block every other request (public-facing).
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
