#!/usr/bin/env python3
"""
Production scraper — California HCAI **Hospital Fair Pricing Policy Lookup** (current source).

This REPLACES syfphr_scraper.py, which scrapes the legacy SYFPHR portal. SYFPHR is now
an ARCHIVE: its structured free-care / discount FPL% fields reflect the pre-2025 rules
(the asset test that AB 2297 + SB 1061 eliminated effective 1/1/2025). The authoritative,
current data lives in the Hospital Fair Pricing Policy Lookup:
  https://hcai.ca.gov/affordability/hospital-fair-billing-program/hospital-fair-pricing-policy-lookup/

Architecture discovered 2026-07-08 (see docs/hcai-data-access.md):
  1. ENUMERATION — the lookup's client-side table is powered by a PUBLIC hosted
     Elasticsearch endpoint (ElasticPress "autosuggest"). Querying post_type
     `hospital_policies` returns all 469 hospitals with permalink + city/zip/county.
     Clean, complete, fast — no HTML list scraping, no timeouts.
  2. POLICY DETAIL — each hospital's permalink page links to its actual policy
     documents (PDFs) served from the HDC document API
     (api.hdc.hcai.ca.gov/Public/Extract/Attachment?id=<guid>), grouped into three
     sections: Charity Care, Discount Payment, Debt Collection. Each section lists the
     current policy + archived versions (with effective dates) and an application form,
     plus a "View Archive" link back to the legacy SYFPHR record (oshpdid) for join.

KEY FINDING that changes the build plan: the current lookup NO LONGER publishes the
structured FPL% / discount-tier NUMBERS as fields — they now live *inside* the policy
PDFs. So producing the structured eligibility dataset (the moat) requires PDF text
extraction of each Charity Care + Discount Payment policy. This scraper produces the
authoritative INDEX (hospitals + document pointers + effective dates), which is the
input to that extraction stage (reuse the SheltrIQ OCR pipeline). It is also the
cleanest thing to attach to an HCAI data request, should we prefer their structured feed.

Stdlib only (urllib + re + html) — no `pip install`. Python 3.8+.

Usage:
  python3 hcai_lookup_scraper.py --limit 3 --out sample_current.json   # quick prototype
  python3 hcai_lookup_scraper.py --index-only --out index.json          # 469 rows, no page fetch (~5 requests)
  python3 hcai_lookup_scraper.py --out dataset_current.json             # full run (index + per-hospital pages)
"""
import argparse
import json
import re
import sys
import time
import html
import urllib.request
from urllib.error import URLError, HTTPError

# Public hosted-ElasticPress endpoint backing the lookup table (found in the page's
# `var epas = {... endpointUrl ...}` config). Accepts standard ES query bodies via POST.
ES_ENDPOINT = (
    "https://hcai-prod.us-west-2.clients.hosted-elasticpress.io/"
    "hcai-prod--hcaicagov-post-1/autosuggest"
)
POLICY_POST_TYPE = "hospital_policies"
HDC_ATTACHMENT = "api.hdc.hcai.ca.gov/Public/Extract/Attachment"
UA = "cobijo-health-research/0.2 (nonprofit charity-care dataset; contact ian@aviontechs.com)"


# --- HTTP -------------------------------------------------------------------

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


def _post_json(url, payload, timeout=30):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"User-Agent": UA, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --- 1. Enumerate all hospital policy records via Elasticsearch --------------

def _meta_first(meta, key):
    vals = meta.get(key)
    if isinstance(vals, list) and vals:
        return vals[0].get("value")
    return None


def enumerate_hospitals(limit=None, page_size=100, sleep=0.5):
    """Return [{post_title, permalink, city, zip, county}] for all hospital_policies."""
    out, frm = [], 0
    while True:
        payload = {
            "from": frm,
            "size": page_size,
            "_source": ["post_title", "permalink",
                        "meta.hfbp_city", "meta.hfbp_zip", "meta.hfbp_county"],
            "sort": [{"post_title.raw": "asc"}],
            "query": {"bool": {"must": [
                {"term": {"post_type.raw": POLICY_POST_TYPE}},
                {"term": {"post_status": "publish"}},
            ]}},
        }
        data = _post_json(ES_ENDPOINT, payload)
        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value")
        for h in hits:
            s = h.get("_source", {})
            meta = s.get("meta", {})
            out.append({
                "post_title": s.get("post_title"),
                "permalink": s.get("permalink"),
                "city": _meta_first(meta, "hfbp_city"),
                "zip": _meta_first(meta, "hfbp_zip"),
                "county": _meta_first(meta, "hfbp_county"),
            })
            if limit and len(out) >= limit:
                return out
        frm += page_size
        if not hits or (total is not None and frm >= total):
            break
        time.sleep(sleep)
    return out


# --- 2. Parse a hospital's policy page for document pointers -----------------

SECTIONS = [
    ("charity_care", "Charity Care Policy"),
    ("discount_payment", "Discount Payment Policy"),
    ("debt_collection", "Debt Collection Policy"),
]
# Each document is one <li> in a <ul class="policy-links">, pairing a
# <span class="date">MM/DD/YYYY</span> with an <a href="...Attachment?id=...">.
LI_RE = re.compile(r"<li\b[^>]*>(.*?)</li>", re.I | re.S)
LI_ATTACH_RE = re.compile(r'href="(https?://[^"]*' + re.escape(HDC_ATTACHMENT) + r'\?id=[^"]+)"', re.I)
LI_DATE_RE = re.compile(r'class="date"[^>]*>\s*(\d{1,2}/\d{1,2}/\d{4})', re.I)
ARCHIVE_RE = re.compile(r'SearchDetail\.aspx\?oshpdid=(\d+)', re.I)


def _links_with_dates(section_html):
    """Extract [{url, effective_date}] for each policy-link <li>, in document order."""
    results = []
    for li in LI_RE.findall(section_html):
        a = LI_ATTACH_RE.search(li)
        if not a:
            continue
        d = LI_DATE_RE.search(li)
        results.append({"url": a.group(1), "effective_date": d.group(1) if d else None})
    # de-dupe identical urls (a combined policy PDF can appear under Charity+Discount)
    seen, deduped = set(), []
    for r in results:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
        deduped.append(r)
    return deduped


def parse_policy_page(html_text):
    """Split the page into the three policy sections and pull document pointers."""
    # Cut the page into section spans by the <h2> headings.
    markers = []
    for key, heading in SECTIONS:
        idx = html_text.find(">" + heading + "<")
        if idx == -1:
            idx = html_text.find(heading)  # fallback: raw label
        markers.append((idx, key))
    markers = [(i, k) for i, k in markers if i != -1]
    markers.sort()

    parsed = {}
    for n, (start, key) in enumerate(markers):
        end = markers[n + 1][0] if n + 1 < len(markers) else len(html_text)
        section_html = html_text[start:end]
        # Split policies (before "Application" h3) from the application form (after).
        app_split = re.search(r'<h3[^>]*>\s*Application', section_html, re.I)
        policies_html = section_html[:app_split.start()] if app_split else section_html
        application_html = section_html[app_split.start():] if app_split else ""

        docs = _links_with_dates(policies_html)
        apps = _links_with_dates(application_html)
        archive = ARCHIVE_RE.search(section_html)
        parsed[key] = {
            "current_policy_url": docs[0]["url"] if docs else None,
            "current_effective_date": docs[0]["effective_date"] if docs else None,
            "archived_policies": docs[1:],
            "application_url": apps[0]["url"] if apps else None,
            "application_effective_date": apps[0]["effective_date"] if apps else None,
            "archive_oshpdid": archive.group(1) if archive else None,
        }
    return parsed


def fetch_hospital_detail(record):
    html_text = _get(record["permalink"])
    detail = parse_policy_page(html_text)
    # Surface a single archive oshpdid at top level (same across sections) for joins.
    oshpdid = next((v.get("archive_oshpdid") for v in detail.values()
                    if v.get("archive_oshpdid")), None)
    return {**record, "archive_oshpdid": oshpdid, "policies": detail}


# --- CLI --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="HCAI Hospital Fair Pricing Policy Lookup scraper (current source)")
    ap.add_argument("--limit", type=int, default=0, help="max hospitals (0 = all 469)")
    ap.add_argument("--index-only", action="store_true",
                    help="just the ES enumeration (name/permalink/location), skip per-page fetch")
    ap.add_argument("--out", help="write JSON here (else stdout)")
    ap.add_argument("--sleep", type=float, default=0.75, help="seconds between page fetches (be polite)")
    args = ap.parse_args()

    try:
        hospitals = enumerate_hospitals(limit=args.limit or None)
    except (URLError, HTTPError) as e:
        sys.exit(f"Could not reach the Elasticsearch endpoint: {e}")
    print(f"Enumerated {len(hospitals)} hospitals from the lookup index.", file=sys.stderr)

    if args.index_only:
        results = hospitals
    else:
        results = []
        for i, rec in enumerate(hospitals, 1):
            try:
                full = fetch_hospital_detail(rec)
                results.append(full)
                cc = full["policies"].get("charity_care", {})
                print(f"  [{i}/{len(hospitals)}] {rec['post_title']} "
                      f"(charity policy: {'yes' if cc.get('current_policy_url') else 'NO'})",
                      file=sys.stderr)
            except Exception as e:  # noqa: BLE001 - keep going, record the failure
                results.append({**rec, "error": str(e)})
                print(f"  [{i}/{len(hospitals)}] ERROR {rec['post_title']}: {e}", file=sys.stderr)
            time.sleep(args.sleep)

    payload = json.dumps(results, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"Wrote {len(results)} records -> {args.out}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
