#!/usr/bin/env python3
"""
Extraction stage (prototype) — pull structured charity-care eligibility rules out of the
HCAI policy PDFs that `hcai_lookup_scraper.py` points at.

This is the moat build: the current HCAI lookup no longer exposes FPL% thresholds as
fields (see docs/hcai-data-access.md) — they live inside each hospital's Charity Care /
Discount Payment policy PDF. This script downloads those PDFs, extracts text, flags
scanned docs (which need OCR — the SheltrIQ pipeline), and runs a first-pass heuristic
parser for the two numbers that matter most:

  * free_care_fpl_pct  — income ceiling (% of FPL) for FREE / full charity care
  * discount_max_fpl_pct — top of the sliding-scale discount band

Heuristics are format-fragile by design here: the goal is to MEASURE how far plain
parsing gets across the ~505 distinct hospital policy formats, and to isolate the docs
that need the LLM structured-extraction path (reuse SheltrIQ's text model) or OCR.
Production accuracy = LLM structured output over this same extracted text.

Usage:
  python3 pdf_extract.py --limit 8                      # sample: download+extract, print a report
  python3 pdf_extract.py --limit 8 --out extracted.json
  python3 pdf_extract.py --dataset dataset_current.json --limit 0   # all (be patient/polite)
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from urllib.error import URLError, HTTPError

UA = "cobijo-health-research/0.2 (nonprofit charity-care dataset; contact ian@aviontechs.com)"
PDF_DIR = "data/pdfs"


def download_pdf(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def pdf_to_text(path):
    """Digital text via poppler's pdftotext -layout. Empty => likely scanned (needs OCR)."""
    try:
        out = subprocess.run(["pdftotext", "-layout", path, "-"],
                             capture_output=True, text=True, timeout=60)
        return out.stdout
    except Exception:
        return ""


# --- Heuristic parse --------------------------------------------------------

# "200% or less of the Federal Poverty Level ... (Full) Charity" / "at or below 200% FPL ... free"
FREE_PATTERNS = [
    r"(\d{2,3})\s*%\s*(?:or less|and below|or below).{0,60}?(?:federal poverty|fpl)",
    r"(?:at or below|below|up to|less than or equal to)\s*(\d{2,3})\s*%.{0,40}?(?:federal poverty|fpl)",
    r"(?:federal poverty level|fpl)[^.\n]{0,60}?(\d{2,3})\s*%\s*(?:or less|and below)",
]
# any FPL% mentioned in an eligibility context — the max is usually the discount ceiling
FPL_PCT = re.compile(r"(\d{2,3})\s*%\s*(?:of\s*(?:the\s*)?)?(?:federal poverty|fpl)", re.I)


def _first_pct(text, patterns):
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            v = int(m.group(1))
            if 50 <= v <= 400:  # sanity band
                return v
    return None


def parse_rules(text):
    """Return best-effort {free_care_fpl_pct, discount_max_fpl_pct, confidence}."""
    lo = text.lower()
    free = None
    # Prefer a % that co-occurs with a "full charity / free / zero" cue on the same line.
    for line in text.splitlines():
        ll = line.lower()
        if re.search(r"full charity|free care|zero \(full|100%\s*(?:charity|assistance)|charity care", ll):
            m = FPL_PCT.search(line)
            if m and 50 <= int(m.group(1)) <= 300:
                free = int(m.group(1))
                break
    if free is None:
        free = _first_pct(text, FREE_PATTERNS)

    pcts = [int(m) for m in FPL_PCT.findall(text) if 50 <= int(m) <= 400]
    discount_max = max(pcts) if pcts else None

    # confidence: both found and free<=discount_max => high; one found => medium; none => low
    if free and discount_max and free <= discount_max:
        conf = "high"
    elif free or discount_max:
        conf = "medium"
    else:
        conf = "low"
    return {
        "free_care_fpl_pct": free,
        "discount_max_fpl_pct": discount_max,
        "fpl_pcts_seen": sorted(set(pcts)),
        "confidence": conf,
    }


# --- Driver -----------------------------------------------------------------

def process(rec, sleep=0.5):
    cc = rec.get("policies", {}).get("charity_care", {})
    url = cc.get("current_policy_url")
    out = {
        "post_title": rec.get("post_title"),
        "county": rec.get("county"),
        "charity_policy_url": url,
        "effective_date": cc.get("current_effective_date"),
    }
    if not url:
        return {**out, "status": "no_policy_pdf"}
    os.makedirs(PDF_DIR, exist_ok=True)
    guid = url.split("id=")[-1][:36]
    dest = os.path.join(PDF_DIR, f"{guid}.pdf")
    try:
        download_pdf(url, dest)
    except (URLError, HTTPError) as e:
        return {**out, "status": f"download_error: {e}"}
    text = pdf_to_text(dest)
    if len(text.strip()) < 500:
        return {**out, "status": "scanned_needs_ocr", "text_len": len(text)}
    rules = parse_rules(text)
    time.sleep(sleep)
    return {**out, "status": "extracted", "text_len": len(text), **rules}


def main():
    ap = argparse.ArgumentParser(description="Charity-care PDF extraction prototype")
    ap.add_argument("--dataset", default="data/dataset_current.json")
    ap.add_argument("--limit", type=int, default=8, help="max hospitals (0 = all)")
    ap.add_argument("--diverse", action="store_true",
                    help="dedupe by charity PDF (one hospital per distinct policy doc) for a format-diverse sample")
    ap.add_argument("--out", help="write JSON here (else stdout report)")
    args = ap.parse_args()

    data = json.load(open(args.dataset))
    if args.diverse:
        seen, uniq = set(), []
        for r in data:
            u = r.get("policies", {}).get("charity_care", {}).get("current_policy_url")
            g = (u or "").split("id=")[-1][:36]
            if g and g in seen:
                continue
            seen.add(g)
            uniq.append(r)
        data = uniq
    if args.limit:
        data = data[:args.limit]

    results = []
    for i, rec in enumerate(data, 1):
        r = process(rec)
        results.append(r)
        tag = r["status"]
        extra = ""
        if tag == "extracted":
            extra = (f"free≤{r['free_care_fpl_pct']}% FPL, discount→{r['discount_max_fpl_pct']}% "
                     f"[{r['confidence']}]")
        print(f"  [{i}/{len(data)}] {r['post_title'][:38]:38} {tag:18} {extra}", file=sys.stderr)

    # summary
    from collections import Counter
    statuses = Counter(r["status"] for r in results)
    conf = Counter(r.get("confidence") for r in results if r["status"] == "extracted")
    print("\n=== SUMMARY ===", file=sys.stderr)
    print("status:", dict(statuses), file=sys.stderr)
    print("confidence (extracted):", dict(conf), file=sys.stderr)

    payload = json.dumps(results, indent=2, ensure_ascii=False)
    if args.out:
        open(args.out, "w", encoding="utf-8").write(payload)
        print(f"Wrote {len(results)} -> {args.out}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    main()
