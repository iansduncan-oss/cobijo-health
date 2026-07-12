#!/usr/bin/env python3
"""
QA harness for the assembled charity-care dataset — the SEMANTIC review layer.

`extract_llm.validate()` already checks structure (FPL% in range, free ≤ discount, tiers
ascending, confidence ≥ 0.6) and `build_dataset.py` prints coverage. Those catch syntax. They do
NOT catch a confident-but-misread policy: a discount ceiling of 500% (above CA's 400% cap), a
sliding scale whose patient-pays share runs backwards, a dollar table that doesn't match its
FPL%, or two hospitals that share one source policy PDF but got different extractions. This
harness adds those checks and emits a PRIORITIZED human-review queue so a person can verify the
flagged rows against the source PDFs.

Read-only: it never edits an extraction. It reuses `extract_llm.validate` and
`extract_llm.dollar_table` so its checks stay consistent with how the data was produced.

Input resolution (so it's usable before the batch lands):
  cobijo_charity_care_dataset.json  ->  extracted_full.json  ->  extracted_smoke.json
  (scanned rows from extracted_scanned.json are spliced over `needs_ocr` placeholders, same as
  build_dataset.py).

Outputs:
  - console summary (counts by severity + coverage)
  - qa_review_worksheet.md : per-hospital review queue, HIGH -> MEDIUM -> LOW, with source quotes
  - qa_report.json         : machine-readable findings
Exit code 1 if any HIGH finding (so it can gate a pipeline).

Usage:
  python3 qa_dataset.py
  python3 qa_dataset.py --dataset cobijo_charity_care_dataset.json
"""
import argparse
import json
import os
import sys
from collections import Counter, defaultdict

from extract_llm import (validate, dollar_table, STATUTORY_FPL_FLOOR,
                         DISCOUNT_IMPLAUSIBLE_PCT, FREE_CARE_UNUSUAL_PCT, FREE_CARE_IMPLAUSIBLE_PCT)

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "output")

# Statutory thresholds live in extract_llm (single source of truth) — HSC §127405: 400% FPL is a
# FLOOR, not a cap. Hospitals may extend eligibility above it, so a >400% ceiling is legal (and common),
# not a misread. We flag as "likely misread" only past a realistic maximum (see the imported constants).

SEV_ORDER = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
DATASETS = ("cobijo_charity_care_dataset.json", "extracted_full.json", "extracted_smoke.json")
# At/above this confidence, an extraction that found ZERO income thresholds is treated as a silent
# failure (a re-extraction candidate) rather than a hospital that genuinely publishes no income rules.
REEXTRACT_CONF = 0.8


def key(row):
    return row.get("permalink") or row.get("hospital")


def load_rows():
    """Load the best available dataset; splice scanned extractions over needs_ocr placeholders."""
    src = next((n for n in DATASETS if os.path.exists(os.path.join(DATA, n))), None)
    if not src:
        sys.exit("No dataset found — run build_dataset.py (or wait for the batch).")
    rows = json.load(open(os.path.join(DATA, src)))

    scanned_path = os.path.join(DATA, "extracted_scanned.json")
    if os.path.exists(scanned_path):
        scanned = {key(r): r for r in json.load(open(scanned_path)) if r.get("status") == "extracted"}
        rows = [scanned[key(r)] if r.get("status") == "needs_ocr" and key(r) in scanned else r
                for r in rows]
    return rows, src


def _tables_match(recomputed, stored):
    """dollar_table() yields int keys; JSON round-trips them to str. Compare by str key."""
    a = {str(k): v for k, v in (recomputed or {}).items()}
    b = {str(k): v for k, v in (stored or {}).items()}
    return a == b


def check_row(row):
    """Return a list of (severity, check, detail) findings for one row. Additive to validate()."""
    f = []
    status = row.get("status")

    if status == "needs_ocr":
        return [("MEDIUM", "unresolved", "scanned PDF still needs OCR — no policy extracted")]
    if status == "extraction_error":
        return [("MEDIUM", "unresolved", f"extraction error: {row.get('error')}")]
    if status != "extracted":
        return [("MEDIUM", "unresolved", f"unexpected status: {status}")]

    pol = row.get("policy") or {}
    fc = (pol.get("free_care") or {}).get("fpl_ceiling_pct")
    dp = pol.get("discount_payment") or {}
    dc = dp.get("fpl_ceiling_pct")
    tiers = dp.get("tiers") or []

    # Carry the structural validator's reasons into the same queue (HIGH: unusable/contradictory).
    for r in validate(pol):
        sev = "HIGH" if ("no free-care" in r or "out of range" in r or "exceeds" in r) else "MEDIUM"
        f.append((sev, "validator", r))

    # T2.1 — high-confidence extraction that found NO income thresholds (free ceiling, discount
    # ceiling, tiers all absent) is almost always a SILENT extraction gap (wrong PDF section, or the
    # discount PDF wasn't fetched), not a hospital that truly publishes no income rules — a confident
    # model shouldn't return nothing. Tag it distinctly so a periodic sweep can queue re-extraction
    # (vs. the generic "no thresholds" reason validate() already emits).
    conf = pol.get("extraction_confidence")
    if fc is None and dc is None and not tiers and conf is not None and conf >= REEXTRACT_CONF:
        f.append(("HIGH", "reextract_candidate",
                  f"high confidence ({conf}) but zero income thresholds extracted — likely a silent "
                  f"extraction gap (wrong PDF section / discount PDF missed); queue for re-extraction"))

    # 1. Ceiling sanity — >400% FPL is LEGAL (§127405 floor, not cap), so only an implausibly high
    #    value signals a units/decimal misread. Free care above the floor is unusual (verify), and free
    #    care that's implausibly high would misinform nearly every patient.
    if dc is not None and dc > DISCOUNT_IMPLAUSIBLE_PCT:
        f.append(("HIGH", "statutory", f"discount ceiling {dc}% implausibly high (>{DISCOUNT_IMPLAUSIBLE_PCT}%) — likely units/decimal misread"))
    if fc is not None and fc > FREE_CARE_IMPLAUSIBLE_PCT:
        f.append(("HIGH", "statutory", f"free-care ceiling {fc}% implausibly high (>{FREE_CARE_IMPLAUSIBLE_PCT}%) — likely misread"))
    elif fc is not None and fc > FREE_CARE_UNUSUAL_PCT:
        f.append(("MEDIUM", "outlier", f"free-care ceiling {fc}% above the {STATUTORY_FPL_FLOOR}% floor — unusually generous, verify against PDF"))
    for t in tiers:
        hi = t.get("fpl_high_pct")
        if hi is not None and hi > DISCOUNT_IMPLAUSIBLE_PCT:
            f.append(("HIGH", "statutory", f"tier upper bound {hi}% implausibly high (>{DISCOUNT_IMPLAUSIBLE_PCT}%) — likely misread"))

    # 2. Tier geometry.
    ordered = [t for t in tiers if t.get("fpl_low_pct") is not None and t.get("fpl_high_pct") is not None]
    ordered.sort(key=lambda t: t["fpl_low_pct"])
    pays = [t.get("patient_pays_pct") for t in ordered if t.get("patient_pays_pct") is not None]
    if pays and pays != sorted(pays):
        f.append(("MEDIUM", "tier_geometry", f"patient-pays not monotonically increasing across bands: {pays}"))
    for a, b in zip(ordered, ordered[1:]):
        if a["fpl_high_pct"] >= b["fpl_low_pct"]:
            f.append(("MEDIUM", "tier_geometry",
                      f"overlapping bands: {a['fpl_low_pct']}-{a['fpl_high_pct']}% & {b['fpl_low_pct']}-{b['fpl_high_pct']}%"))
        elif b["fpl_low_pct"] - a["fpl_high_pct"] > 1:
            f.append(("LOW", "tier_geometry",
                      f"gap between bands {a['fpl_high_pct']}% and {b['fpl_low_pct']}% (may be legitimate)"))
    if dc is not None:
        for t in ordered:
            if t["fpl_high_pct"] > dc:
                f.append(("MEDIUM", "tier_geometry", f"tier to {t['fpl_high_pct']}% exceeds stated discount ceiling {dc}%"))

    # 3. Dollar-table integrity — recompute from the same helper that produced them.
    if not _tables_match(dollar_table(fc), row.get("free_care_income_ceiling_by_household")):
        f.append(("HIGH", "dollar_table", "free-care dollar table does not match its FPL% — stale/misaligned"))
    if not _tables_match(dollar_table(dc), row.get("discount_income_ceiling_by_household")):
        f.append(("HIGH", "dollar_table", "discount dollar table does not match its FPL% — stale/misaligned"))

    # 4. Coverage niceties.
    if not (pol.get("contact") or {}).get("phone"):
        f.append(("LOW", "coverage", "no application phone number extracted"))

    return f


def check_chains(rows):
    """Rows sharing a source_sha256 came from ONE policy PDF -> extractions must be identical."""
    by_sha = defaultdict(list)
    for r in rows:
        if r.get("status") == "extracted" and r.get("source_sha256"):
            by_sha[r["source_sha256"]].append(r)
    chain_findings = {}
    for sha, group in by_sha.items():
        if len(group) < 2:
            continue
        canon = json.dumps(group[0].get("policy"), sort_keys=True)
        divergent = [r for r in group if json.dumps(r.get("policy"), sort_keys=True) != canon]
        if divergent:
            names = ", ".join(sorted({r["hospital"] for r in group}))
            for r in group:
                chain_findings.setdefault(key(r), []).append(
                    ("HIGH", "chain", f"shares source policy {sha[:10]} with {len(group)} hospitals but extractions diverge ({names})"))
    return chain_findings


def main():
    ap = argparse.ArgumentParser(description="Semantic QA + human-review queue for the charity-care dataset")
    ap.add_argument("--dataset", help="override input file")
    ap.add_argument("--worksheet", default="qa_review_worksheet.md")
    ap.add_argument("--report", default="qa_report.json")
    args = ap.parse_args()

    if args.dataset:
        rows = json.load(open(os.path.join(DATA, args.dataset)))
        src = args.dataset
    else:
        rows, src = load_rows()
    os.makedirs(OUT, exist_ok=True)

    chain = check_chains(rows)
    findings = {}
    for r in rows:
        fs = check_row(r) + chain.get(key(r), [])
        if fs:
            findings[key(r)] = {"row": r, "findings": fs}

    # ---- tallies ----
    sev_counts = Counter(sev for v in findings.values() for sev, _, _ in v["findings"])
    extracted = [r for r in rows if r.get("status") == "extracted"]
    clean = sum(1 for r in extracted if not findings.get(key(r)))

    # ---- worksheet (prioritized) ----
    def row_rank(item):
        return min(SEV_ORDER[s] for s, _, _ in item[1]["findings"])
    ordered = sorted(findings.items(), key=lambda kv: (row_rank(kv), kv[1]["row"].get("hospital", "")))

    lines = ["# Charity-care dataset — human review worksheet", "",
             f"Source: `{src}` · {len(rows)} rows · {len(extracted)} extracted · "
             f"{clean} clean · {len(findings)} flagged", "",
             f"Findings: HIGH={sev_counts['HIGH']} MEDIUM={sev_counts['MEDIUM']} LOW={sev_counts['LOW']}", "",
             "Verify each flagged row against its source policy PDF (permalink below). "
             "HIGH = likely wrong patient guidance; fix before the navigator uses it.", ""]
    for _, item in ordered:
        r, fs = item["row"], sorted(item["findings"], key=lambda x: SEV_ORDER[x[0]])
        pol = r.get("policy") or {}
        fc = (pol.get("free_care") or {}).get("fpl_ceiling_pct")
        dc = (pol.get("discount_payment") or {}).get("fpl_ceiling_pct")
        lines.append(f"## {r.get('hospital', '?')}  ({r.get('city', '')}, {r.get('county', '')} County)")
        if r.get("permalink"):
            lines.append(f"- Policy: {r['permalink']}")
        lines.append(f"- Extracted: free-care ≤{fc}% FPL · discount ≤{dc}% FPL · "
                     f"conf {pol.get('extraction_confidence')} · status {r.get('status')}")
        for sev, chk, detail in fs:
            lines.append(f"  - **[{sev}] {chk}** — {detail}")
        sq = pol.get("source_quotes") or {}
        if isinstance(sq, dict):
            quotes = [f'{k}: "{v}"' for k, v in sq.items() if v]
        elif isinstance(sq, list):
            quotes = [f'"{v}"' for v in sq if v]
        elif isinstance(sq, str) and sq.strip():
            quotes = [f'"{sq}"']
        else:
            quotes = []
        if quotes:
            lines.append(f"  - source quotes — {' | '.join(quotes)}")
        lines.append("")
    open(os.path.join(OUT, args.worksheet), "w").write("\n".join(lines))

    # ---- machine-readable ----
    report = [{"hospital": item["row"].get("hospital"), "permalink": item["row"].get("permalink"),
               "source_sha256": item["row"].get("source_sha256"),
               "findings": [{"severity": s, "check": c, "detail": d} for s, c, d in item["findings"]]}
              for _, item in ordered]
    json.dump(report, open(os.path.join(OUT, args.report), "w"), indent=2, ensure_ascii=False)

    # ---- console ----
    print(f"\n=== QA: {src} — {len(rows)} rows ===", file=sys.stderr)
    print(f"  extracted={len(extracted)}  clean={clean}  flagged={len(findings)}", file=sys.stderr)
    print(f"  findings: HIGH={sev_counts['HIGH']} MEDIUM={sev_counts['MEDIUM']} LOW={sev_counts['LOW']}", file=sys.stderr)
    print(f"  wrote {args.worksheet} + {args.report}", file=sys.stderr)
    if sev_counts["HIGH"]:
        print(f"  ⚠ {sev_counts['HIGH']} HIGH findings — review before the navigator consumes this dataset.", file=sys.stderr)
    return 1 if sev_counts["HIGH"] else 0


if __name__ == "__main__":
    sys.exit(main())
