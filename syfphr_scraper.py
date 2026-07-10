#!/usr/bin/env python3
"""
Prototype scraper — California HCAI SYFPHR hospital Financial Assistance data.

Pipeline (proves the plan's "moat" build is a structured scrape, not PDF OCR):
  1. Enumerate every hospital + oshpdid from FacilityList.aspx
  2. Pull structured eligibility fields from each SearchDetail.aspx?oshpdid=<id>
  3. Compute per-household-size dollar income limits from the FPL% thresholds
     (the detail page gives the %, the dollars are FPL% x federal poverty guideline)
  4. Emit JSON — the open dataset that doesn't exist publicly yet

Stdlib only (urllib + re + html) — no `pip install` needed. Python 3.8+.

Usage:
  python3 syfphr_scraper.py --limit 5 --out sample.json     # quick prototype run
  python3 syfphr_scraper.py --raw --limit 2                 # dump cleaned page text to design the parser
  python3 syfphr_scraper.py --out full.json                 # full run (be polite; it sleeps between requests)

NOTE (verify before trusting at scale): a banner on the legacy syfphr.oshpd.ca.gov
domain suggests SYFPHR may no longer accept new hospital submissions. Confirm this
is still HCAI's authoritative, current source (vs. a newer portal) before relying on
freshness. The scraping approach is identical regardless of which portal is current.
"""
import argparse
import json
import re
import sys
import time
import html
import urllib.request
from urllib.error import URLError, HTTPError

BASE = "https://syfphr.hcai.ca.gov"
LIST_URL = f"{BASE}/FacilityList.aspx"
DETAIL_URL = BASE + "/SearchDetail.aspx?oshpdid={oshpdid}"
UA = "medical-navigator-research/0.1 (nonprofit charity-care dataset prototype)"

# 2026 HHS Federal Poverty Guidelines — 48 contiguous states + DC, annual USD.
# Source: ASPE 2026 (effective 1/13/2026). Update each January when HHS publishes.
FPL = {1: 15960, 2: 21640, 3: 27320, 4: 33000, 5: 38680, 6: 44360, 7: 50040, 8: 55720}
FPL_EACH_ADDITIONAL = 5680  # per person beyond 8 (2026)


def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        charset = r.headers.get_content_charset() or "utf-8"
        return r.read().decode(charset, "replace")


def clean_text(fragment):
    fragment = re.sub(r"(?is)<(script|style).*?</\1>", " ", fragment)
    fragment = re.sub(r"(?s)<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", html.unescape(fragment)).strip()


def poverty_limit(household_size):
    if household_size <= 8:
        return FPL[household_size]
    return FPL[8] + (household_size - 8) * FPL_EACH_ADDITIONAL


def dollar_table(fpl_percent, max_size=8):
    """FPL% -> annual household income ceiling for sizes 1..max_size."""
    if not fpl_percent:
        return {}
    return {n: round(poverty_limit(n) * fpl_percent / 100) for n in range(1, max_size + 1)}


def get_facilities(limit=None):
    page = fetch(LIST_URL)
    # Links use single quotes and wrap an icon image (name lives elsewhere in the
    # row), so we collect oshpdids here and get the hospital name from the detail page.
    ids = re.findall(r"href='/SearchDetail\.aspx\?oshpdid=(\d+)'", page)
    seen, out = [], []
    for oshpdid in ids:
        if oshpdid in seen:
            continue
        seen.append(oshpdid)
        out.append({"oshpdid": oshpdid})
        if limit and len(out) >= limit:
            break
    return out


PHONE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")


def parse_detail(oshpdid, raw_mode=False):
    text = clean_text(fetch(DETAIL_URL.format(oshpdid=oshpdid)))
    if raw_mode:
        return {"oshpdid": oshpdid, "raw_text": text[:4000]}

    def rx(pat):
        m = re.search(pat, text, re.I)
        return m.group(1).strip() if m else None

    free = rx(r"Free Care:\s*(\d+(?:\.\d+)?)\s*%")
    free_fpl = float(free) if free else None

    # Discount tiers: up to 3, each "FPL Range: LO %- HI %"; skip empty 0%-0% tiers.
    tiers = []
    for lo, hi in re.findall(r"FPL Range:\s*(\d+(?:\.\d+)?)\s*%\s*-\s*(\d+(?:\.\d+)?)\s*%", text, re.I):
        lo, hi = float(lo), float(hi)
        if lo == 0 and hi == 0:
            continue
        tiers.append({"fpl_low_pct": lo, "fpl_high_pct": hi})
    disc_high = max((t["fpl_high_pct"] for t in tiers), default=None)
    phone = PHONE.search(text)

    return {
        "oshpdid": oshpdid,
        "source_url": DETAIL_URL.format(oshpdid=oshpdid),
        "hospital": rx(r"Hospital:\s*(.+?)\s+\d+\s+\w"),
        "total_beds": rx(r"Total Beds:\s*(\d+)"),
        "emergency_room": rx(r"Emergency Room:\s*(\w+)"),
        "website": rx(r"(https?://[^\s]+)"),
        "free_care_fpl_pct": free_fpl,
        "discount_tiers": tiers,
        "discount_payment_basis": rx(r"FPL Range:.*?Payment Basis:\s*([A-Za-z]+)"),
        "policy_effective_date": rx(r"Policy Effective Date:\s*(\d{1,2}/\d{1,2}/\d{4})"),
        "discount_effective_date": rx(r"Discount Payment Effective Date:\s*(\d{1,2}/\d{1,2}/\d{4})"),
        "application_effective_date": rx(r"Application Form Eff Date:\s*(\d{1,2}/\d{1,2}/\d{4})"),
        "business_office_phone": phone.group(0) if phone else None,
        "free_care_income_ceiling_by_household": dollar_table(free_fpl),
        "discount_income_ceiling_by_household": dollar_table(disc_high),
    }


def main():
    ap = argparse.ArgumentParser(description="Prototype HCAI SYFPHR charity-care scraper")
    ap.add_argument("--limit", type=int, default=5, help="max facilities (0 = all)")
    ap.add_argument("--out", help="write JSON here (else stdout)")
    ap.add_argument("--raw", action="store_true", help="dump cleaned page text to design the parser")
    ap.add_argument("--sleep", type=float, default=1.0, help="seconds between requests (be polite)")
    args = ap.parse_args()

    try:
        facilities = get_facilities(limit=args.limit or None)
    except (URLError, HTTPError) as e:
        sys.exit(f"Could not reach {LIST_URL}: {e}\n(If this box has no network, run the script where it does.)")

    print(f"Found {len(facilities)} facilities. Fetching detail pages...", file=sys.stderr)
    results = []
    for i, fac in enumerate(facilities, 1):
        try:
            record = {**fac, **parse_detail(fac["oshpdid"], raw_mode=args.raw)}
            results.append(record)
            print(f"  [{i}/{len(facilities)}] {record.get('hospital') or '?'} ({fac['oshpdid']})", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 - prototype: keep going, record the failure
            results.append({**fac, "error": str(e)})
            print(f"  [{i}/{len(facilities)}] ERROR {fac['oshpdid']}: {e}", file=sys.stderr)
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
