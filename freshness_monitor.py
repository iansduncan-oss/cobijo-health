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

The URL+date signal misses one real failure mode: a hospital swaps the PDF behind the SAME URL
with the SAME stated effective date (a silent content edit). To catch that, `--content` re-fetches
each unchanged hospital's policy corpus and hashes it (the exact sha extract_llm.py stored as
`source_sha256`), flagging any whose live content no longer matches what we last extracted.

Workflow:
  1. Refresh the source:   python3 hcai_lookup_scraper.py        (rewrites data/dataset_current.json)
  2. See what changed:     python3 freshness_monitor.py           (fast, offline: URL/date diff)
  3. Deep check (weekly):  python3 freshness_monitor.py --content (re-hash PDFs: catch same-URL edits)
  4. Re-extract only the CHANGED/NEW hospitals, then:
     python3 freshness_monitor.py --update                       (adopt current as the new baseline)

Read-only unless --update. Exit code 1 if any change is detected (so a cron job can alert). The
default run is offline (dataset_current.json only); only --content touches the network.

Cron (weekly deep check; alert on exit 1):
  0 7 * * 1  cd /opt/cobijo && python3 hcai_lookup_scraper.py && python3 freshness_monitor.py --content
"""
import argparse
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "output")
CURRENT = os.path.join(DATA, "dataset_current.json")
BASELINE = os.path.join(DATA, "freshness_baseline.json")
# Extracted datasets (gitignored full ones first, tracked sample last) — for the last-known
# source_sha256 per hospital. Mirrors navigator.DATASETS.
EXTRACTED = ("cobijo_charity_care_dataset.json", "extracted_full.json", "extracted_smoke.json",
             "sample_dataset.json")


def fingerprint(hosp, content_sha=None):
    """The change-signal for one hospital: identity + each policy's current URL + effective date,
    plus (optionally) a sha256 of its policy-PDF *content* so a same-URL silent re-upload is caught."""
    pols = hosp.get("policies") or {}
    def sig(kind):
        p = pols.get(kind) or {}
        return {"url": p.get("current_policy_url"), "effective_date": p.get("current_effective_date")}
    fp = {
        "hospital": hosp.get("post_title"),
        "charity_care": sig("charity_care"),
        "discount_payment": sig("discount_payment"),
    }
    if content_sha is not None:
        fp["content_sha"] = content_sha
    return fp


def _stored_shas():
    """{hospital key -> source_sha256} from the most recent extracted dataset (what we last read)."""
    for name in EXTRACTED:
        path = os.path.join(DATA, name)
        if not os.path.exists(path):
            continue
        rows = json.load(open(path))
        return {r.get("permalink") or r.get("hospital"): r.get("source_sha256")
                for r in rows if r.get("source_sha256")}
    return {}


def _live_sha(rec):
    """sha256 of a hospital's live policy corpus — the exact digest extract_llm.py stores as
    source_sha256 — so an unchanged URL hiding new content shows up as a content_sha change."""
    import extract_llm
    corpus, _ = extract_llm.build_corpus(rec)
    if len(corpus.strip()) < 500:      # scanned/empty: no reliable text hash (extract flags needs_ocr)
        return None
    return hashlib.sha256(corpus.encode()).hexdigest()


def load_current(content=False):
    if not os.path.exists(CURRENT):
        sys.exit(f"{CURRENT} not found — run hcai_lookup_scraper.py first.")
    rows = json.load(open(CURRENT))
    if content:
        out = {}
        for i, r in enumerate(rows, 1):
            key = r.get("permalink") or r.get("post_title")
            try:
                sha = _live_sha(r)
            except Exception as e:                 # a single unreachable PDF must not abort the sweep
                sha = None
                print(f"  [content {i}/{len(rows)}] {(r.get('post_title') or '')[:40]}: "
                      f"fetch failed ({type(e).__name__})", file=sys.stderr)
            out[key] = fingerprint(r, content_sha=sha)
        return out
    # Offline: stamp each hospital with its LAST-EXTRACTED content hash so a later --content run (or
    # the baseline) has something to diff against; the network is never touched here.
    shas = _stored_shas()
    return {(r.get("permalink") or r.get("post_title")):
            fingerprint(r, content_sha=shas.get(r.get("permalink") or r.get("post_title")))
            for r in rows}


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
        # Same URL + date, but the PDF's content hash moved -> a silent re-upload. Only compare when
        # both sides carry a hash (a None from a fetch failure or a not-yet-extracted row is unknown,
        # not "changed"), and only if the URL/date signal didn't already flag this hospital.
        cs, bs = current[k].get("content_sha"), baseline[k].get("content_sha")
        if not deltas and cs and bs and cs != bs:
            deltas.append("policy content changed at same URL (silent re-upload)")
        if deltas:
            changed.append((k, current[k]["hospital"], deltas))
    return new, removed, changed


def main():
    ap = argparse.ArgumentParser(description="Detect HCAI policy changes vs a saved baseline")
    ap.add_argument("--update", action="store_true", help="adopt the current scrape as the new baseline")
    ap.add_argument("--content", action="store_true",
                    help="re-fetch + hash each policy PDF to catch same-URL silent re-uploads (network)")
    ap.add_argument("--report", default="freshness_report.json")
    args = ap.parse_args()

    current = load_current(content=args.content)

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
