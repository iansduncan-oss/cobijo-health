#!/usr/bin/env python3
"""
Freshness monitor — detect when a hospital's financial-assistance policy changes, so the moat
dataset can be re-extracted only where it actually went stale.

HCAI hospitals republish their charity-care / discount-payment policies over time (each carries
a `current_effective_date` and a policy PDF URL). When either changes, our extracted numbers for
that hospital may be out of date. This script fingerprints every hospital's policy pointers +
effective dates and diffs the current scrape against a saved baseline, reporting:
  NEW      hospitals added to the lookup
  REMOVED  hospitals no longer listed
  CHANGED  policy URL or effective date moved  -> re-extract these

Workflow:
  1. Refresh the source:   python3 hcai_lookup_scraper.py        (rewrites data/dataset_current.json)
  2. See what changed:     python3 freshness_monitor.py           (diff vs baseline)
  3. Re-extract only the CHANGED/NEW hospitals, then:
     python3 freshness_monitor.py --update                       (adopt current as the new baseline)

Read-only unless --update. Exit code 1 if any change is detected (so a cron job can alert).
Offline: operates on the already-refreshed dataset_current.json; no network of its own.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "output")
CURRENT = os.path.join(DATA, "dataset_current.json")
BASELINE = os.path.join(DATA, "freshness_baseline.json")


def fingerprint(hosp):
    """The change-signal for one hospital: identity + each policy's current URL + effective date."""
    pols = hosp.get("policies") or {}
    def sig(kind):
        p = pols.get(kind) or {}
        return {"url": p.get("current_policy_url"), "effective_date": p.get("current_effective_date")}
    return {
        "hospital": hosp.get("post_title"),
        "charity_care": sig("charity_care"),
        "discount_payment": sig("discount_payment"),
    }


def load_current():
    if not os.path.exists(CURRENT):
        sys.exit(f"{CURRENT} not found — run hcai_lookup_scraper.py first.")
    rows = json.load(open(CURRENT))
    return {r.get("permalink") or r.get("post_title"): fingerprint(r) for r in rows}


def diff(current, baseline):
    cur_keys, base_keys = set(current), set(baseline)
    new = sorted(cur_keys - base_keys)
    removed = sorted(base_keys - cur_keys)
    changed = []
    for k in sorted(cur_keys & base_keys):
        deltas = []
        for kind in ("charity_care", "discount_payment"):
            c, b = current[k][kind], baseline[k][kind]
            if c != b:
                if c["effective_date"] != b["effective_date"]:
                    deltas.append(f"{kind} effective {b['effective_date']} -> {c['effective_date']}")
                if c["url"] != b["url"]:
                    deltas.append(f"{kind} policy URL changed")
        if deltas:
            changed.append((k, current[k]["hospital"], deltas))
    return new, removed, changed


def main():
    ap = argparse.ArgumentParser(description="Detect HCAI policy changes vs a saved baseline")
    ap.add_argument("--update", action="store_true", help="adopt the current scrape as the new baseline")
    ap.add_argument("--report", default="freshness_report.json")
    args = ap.parse_args()

    current = load_current()

    if not os.path.exists(BASELINE):
        if args.update:
            json.dump(current, open(BASELINE, "w"), indent=2, ensure_ascii=False)
            print(f"Baseline established: {len(current)} hospitals -> {BASELINE}", file=sys.stderr)
            return 0
        sys.exit("No baseline yet — run `python3 freshness_monitor.py --update` to establish one.")

    baseline = json.load(open(BASELINE))
    new, removed, changed = diff(current, baseline)

    os.makedirs(OUT, exist_ok=True)
    report = {
        "new": [{"key": k, "hospital": current[k]["hospital"]} for k in new],
        "removed": [{"key": k, "hospital": baseline[k]["hospital"]} for k in removed],
        "changed": [{"key": k, "hospital": h, "deltas": d} for k, h, d in changed],
    }
    json.dump(report, open(os.path.join(OUT, args.report), "w"), indent=2, ensure_ascii=False)

    print(f"\n=== Freshness: {len(current)} hospitals vs baseline ({len(baseline)}) ===", file=sys.stderr)
    print(f"  NEW={len(new)}  REMOVED={len(removed)}  CHANGED (re-extract)={len(changed)}", file=sys.stderr)
    for k, h, deltas in changed[:40]:
        print(f"  ~ {h}: {'; '.join(deltas)}", file=sys.stderr)
    if len(changed) > 40:
        print(f"  … +{len(changed) - 40} more (see output/{args.report})", file=sys.stderr)
    for k in new[:20]:
        print(f"  + NEW {current[k]['hospital']}", file=sys.stderr)
    for k in removed[:20]:
        print(f"  - REMOVED {baseline[k]['hospital']}", file=sys.stderr)

    if args.update:
        json.dump(current, open(BASELINE, "w"), indent=2, ensure_ascii=False)
        print(f"  baseline updated -> {len(current)} hospitals", file=sys.stderr)

    return 1 if (new or removed or changed) else 0


if __name__ == "__main__":
    sys.exit(main())
