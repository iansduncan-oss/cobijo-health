#!/usr/bin/env python3
"""
Assemble the final Cobijo Health CA charity-care dataset + print a coverage/quality report.

Inputs (both produced upstream):
  - extracted_full.json     : 469 hospitals, text-layer PDFs extracted (extract_llm.py)
  - extracted_scanned.json  : the ~11 scanned/image-only hospitals (extract_scanned.py)

Output:
  - cobijo_charity_care_dataset.json : one row per hospital, scanned rows spliced in over
    the `needs_ocr` placeholders. This is the moat dataset the navigator consumes.

The report is the honest read on the dataset: how many are clean vs need human review vs
still missing, the free-care FPL% distribution, and county coverage.

Usage:
  python3 build_dataset.py
  python3 build_dataset.py --full extracted_smoke.json --scanned extracted_scanned.json
"""
import argparse
import json
import os
import sys
from collections import Counter


def key(row):
    return row.get("permalink") or row.get("hospital")


def main():
    ap = argparse.ArgumentParser(description="Assemble the final charity-care dataset + report")
    ap.add_argument("--full", default="data/extracted_full.json")
    ap.add_argument("--scanned", default="data/extracted_scanned.json")
    ap.add_argument("--out", default="data/cobijo_charity_care_dataset.json")
    args = ap.parse_args()

    if not os.path.exists(args.full):
        sys.exit(f"{args.full} not found yet — run extract_llm.py (or wait for the batch).")
    full = json.load(open(args.full))

    scanned = {}
    if os.path.exists(args.scanned):
        for r in json.load(open(args.scanned)):
            if r.get("status") == "extracted":
                scanned[key(r)] = r

    # Splice scanned extractions over the needs_ocr placeholders.
    merged, spliced = [], 0
    for row in full:
        if row.get("status") == "needs_ocr" and key(row) in scanned:
            merged.append(scanned[key(row)])
            spliced += 1
        else:
            merged.append(row)
    json.dump(merged, open(args.out, "w"), indent=2, ensure_ascii=False)

    # ---- report ----
    st = Counter(r.get("status") for r in merged)
    review = sum(1 for r in merged if r.get("needs_review"))
    # require a dict policy (matches navigator.load_dataset) so the report can't KeyError/TypeError
    # on an "extracted" row whose policy is null/malformed
    usable = [r for r in merged if r.get("status") == "extracted" and isinstance(r.get("policy"), dict)]
    with_free = sum(1 for r in usable if (r.get("policy", {}).get("free_care") or {}).get("fpl_ceiling_pct") is not None)
    free_dist = Counter((r["policy"]["free_care"] or {}).get("fpl_ceiling_pct")
                        for r in usable if (r.get("policy", {}).get("free_care")))
    disc_dist = Counter((r["policy"].get("discount_payment") or {}).get("fpl_ceiling_pct")
                        for r in usable if r.get("policy"))
    counties = Counter(r.get("county") for r in usable)

    def pct(n):
        return f"{n} ({100*n//max(len(merged),1)}%)"

    print(f"\n=== Cobijo charity-care dataset: {len(merged)} hospitals ===", file=sys.stderr)
    print(f"  status: {dict(st)}", file=sys.stderr)
    print(f"  spliced scanned-PDF extractions: {spliced}", file=sys.stderr)
    print(f"  usable (extracted): {pct(len(usable))}   with free-care FPL%: {with_free}", file=sys.stderr)
    print(f"  flagged needs_review: {review}", file=sys.stderr)
    print(f"\n  free-care ceiling (% FPL) distribution:", file=sys.stderr)
    for v, n in sorted(free_dist.items(), key=lambda x: (x[0] is None, x[0])):
        print(f"    {v}% : {n}", file=sys.stderr)
    print(f"  discount ceiling (% FPL) distribution:", file=sys.stderr)
    for v, n in sorted(disc_dist.items(), key=lambda x: (x[0] is None, x[0])):
        print(f"    {v}% : {n}", file=sys.stderr)
    print(f"\n  counties covered: {len(counties)}  (top: "
          f"{', '.join(f'{c}={n}' for c, n in counties.most_common(5))})", file=sys.stderr)
    print(f"\nWrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
