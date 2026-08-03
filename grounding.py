#!/usr/bin/env python3
"""Numeric grounding gate — every published threshold must be findable in its own source document.

WHAT THIS IS FOR
----------------
`source_sha256` proves *which* document a row was read from. It proves nothing about whether the
number we serve actually appears in it. This module closes that gap: it rebuilds the exact corpus
the extraction saw, confirms the hash still matches, and then checks that each published FPL
threshold really occurs in that text next to poverty-level language.

That converts hallucination from an invisible risk into a *detectable condition*, which is what lets
`last_verified_at` mean anything. Until a number passes here, it has never been checked by anything
but the model that produced it.

WHY NUMERIC AND NORMALIZED, NOT VERBATIM
----------------------------------------
The obvious implementation — check the stored `source_quotes` string appears verbatim in the
document — was measured at a **17% false-flag rate** on a live sample. The failures were table
reformattings, not hallucinations: `pdftotext -layout` renders a policy's threshold table
differently than the model transcribed it, so the characters differ while the meaning is identical.
A gate that cries wolf on one row in six buries the real review queue and gets ignored, which is
worse than no gate.

So this matches **values, not strings**. `200`, `200%`, `200 %`, `200 percent`, and `200.0%` all
collapse to the number 200, and the number must appear within a window of FPL/poverty wording.
Measured expectation from the design sample (n=60): ~98% auto-verification.

VERDICTS
--------
Deliberately four, not two — "I could not check this" is a different fact from "this is wrong", and
conflating them is how a review queue fills with noise:

    grounded      the value occurs near poverty-level language in the source text
    ungrounded    the source text was read successfully and the value is NOT in it  <- the real flag
    unverifiable  no usable text (scanned PDF / empty extract) — unverifiable *through this channel*
    stale_source  the corpus no longer hashes to source_sha256 — the document moved under us

Only `ungrounded` accuses the extraction of being wrong. `unverifiable` and `stale_source` are
routing conditions: under the tier rule, any non-`grounded` verdict degrades the row to the
statutory floor rather than publishing a number nothing stands behind.

Stdlib only, and it reads CACHED PDFs — so it runs offline, and in particular it runs from a host
whose IP the HCAI WAF blocks (403 from the datacenter, 200 from a residential IP as of 2026-08-03).
"""

import hashlib
import re

# Poverty-level wording, in the forms these policies actually use. FPG and "poverty guideline" are
# included because a minority of policies use HHS's own terminology rather than "FPL".
FPL_MARKER = re.compile(
    r"\bFPL\b|\bFPG\b|federal\s+poverty|poverty\s+level|poverty\s+guideline|poverty\s+income",
    re.I,
)

# A percentage in any of the spellings these documents use. The trailing unit is required — a bare
# "200" in a phone number or an address is not a threshold, and requiring the unit is what keeps the
# false-positive rate down without needing a smarter parser.
PCT = re.compile(r"(\d{1,4}(?:\.\d+)?)\s*(?:%|percent\b|per\s*cent\b)", re.I)

# How far a value may sit from poverty wording and still count as grounded. Tables put the number and
# the header in different places; 300 characters covers a table row plus its header without spanning
# unrelated sections of the policy.
DEFAULT_WINDOW = 300

GROUNDED, UNGROUNDED, UNVERIFIABLE, STALE = "grounded", "ungrounded", "unverifiable", "stale_source"

# Below this, `pdftotext` produced nothing usable — a scanned document with no text layer. Mirrors
# the threshold extract_llm.py uses to route a hospital to needs_ocr, so the two agree on what
# "there is no text here" means.
MIN_USABLE_CHARS = 500


def percent_mentions(text):
    """Every percentage in the text as (value, start_offset). Normalizing happens here.

    Values are floats so that 200 and 200.0 compare equal; callers see one number per spelling.
    """
    return [(float(m.group(1)), m.start()) for m in PCT.finditer(text)]


def _marker_spans(text):
    return [(m.start(), m.end()) for m in FPL_MARKER.finditer(text)]


def ground_value(value, text, window=DEFAULT_WINDOW, _markers=None):
    """Is `value` present as a percentage within `window` chars of poverty wording?

    Returns (bool, evidence) where evidence is the surrounding text of the first match — the gate
    has to be able to SHOW its work, or a reviewer can't act on a flag.
    """
    if value is None or not text:
        return False, None
    markers = _marker_spans(text) if _markers is None else _markers
    if not markers:
        return False, None
    target = float(value)
    for val, pos in percent_mentions(text):
        if val != target:
            continue
        for ms, me in markers:
            # Distance between the two spans, zero if they overlap.
            if min(abs(pos - me), abs(ms - pos)) <= window or (ms <= pos <= me):
                lo, hi = max(0, min(pos, ms) - 60), min(len(text), max(pos, me) + 60)
                return True, " ".join(text[lo:hi].split())
    return False, None


def published_thresholds(row):
    """The numbers this row serves that were TRANSCRIBED from the document, as (label, value).

    Deliberately excludes tier **lower** bounds, and that exclusion is the difference between a
    usable gate and an ignored one.

    Tier lows are not transcribed, they are boundary arithmetic. A policy writes ceilings — "at or
    below 200%", "up to 250% of the FPL", "greater than 250% and less than or equal to 400%" — and
    the schema turns each band's floor into `previous_ceiling + 1`. The number 201 frequently
    appears nowhere in a document that plainly means "the band above 200%". Measured on the live
    corpus: **1,043 of 1,134 tier lows (92%) are exactly the preceding ceiling + 1 or 0**, and
    nearly all of the remainder are the same rule applied to the free-care ceiling instead of a
    previous tier (e.g. AHMC Anaheim's first tier starts at 201 because free care ends at 200).

    Grounding a derived value measures the schema convention, not the extraction's fidelity. The
    first version of this gate did include them and scored **40% auto-verification, with 30 of its
    34 failures being nothing but a synthesized `fpl_low_pct = 0`** — a worse false-flag rate than
    the verbatim matching this module was written to avoid. Boundary arithmetic is already checked,
    correctly, by qa_dataset's tier_geometry rules (overlaps, gaps, ordering); it does not belong
    here.

    What remains are the values a document actually states, and the ones a patient is quoted.
    """
    pol = row.get("policy") or {}
    out = []
    fc = (pol.get("free_care") or {}).get("fpl_ceiling_pct")
    if fc is not None:
        out.append(("free_care.fpl_ceiling_pct", fc))
    dp = pol.get("discount_payment") or {}
    if dp.get("fpl_ceiling_pct") is not None:
        out.append(("discount_payment.fpl_ceiling_pct", dp["fpl_ceiling_pct"]))
    for i, tier in enumerate(dp.get("tiers") or []):
        if tier.get("fpl_high_pct") is not None:
            out.append((f"discount_payment.tiers[{i}].fpl_high_pct", tier["fpl_high_pct"]))
    return out


def check_row(row, corpus, window=DEFAULT_WINDOW):
    """Ground every published threshold in `row` against `corpus`.

    Returns {"verdict": ..., "checked": n, "findings": [...]}. The row-level verdict is the worst
    of its parts: one ungrounded number makes the row ungrounded, because the patient sees the row.
    """
    if corpus is None:
        return {"verdict": UNVERIFIABLE, "checked": 0,
                "findings": [{"label": "*", "reason": "corpus could not be rebuilt"}]}
    if len(corpus.strip()) < MIN_USABLE_CHARS:
        return {"verdict": UNVERIFIABLE, "checked": 0,
                "findings": [{"label": "*", "reason": "no usable text layer (scanned document)"}]}

    stored = row.get("source_sha256")
    if stored and hashlib.sha256(corpus.encode()).hexdigest() != stored:
        # Grounding against a document that is not the one we extracted from would produce a
        # confident answer about the wrong text — the exact failure this module exists to prevent.
        return {"verdict": STALE, "checked": 0,
                "findings": [{"label": "*", "reason": "corpus no longer matches source_sha256"}]}

    markers = _marker_spans(corpus)
    findings, checked, ok = [], 0, True
    for label, value in published_thresholds(row):
        checked += 1
        grounded, evidence = ground_value(value, corpus, window, _markers=markers)
        if grounded:
            findings.append({"label": label, "value": value, "grounded": True, "evidence": evidence})
        else:
            ok = False
            findings.append({"label": label, "value": value, "grounded": False,
                             "reason": f"{value}% not found near poverty-level language in the source"})
    if checked == 0:
        return {"verdict": UNVERIFIABLE, "checked": 0,
                "findings": [{"label": "*", "reason": "row publishes no thresholds to ground"}]}
    return {"verdict": GROUNDED if ok else UNGROUNDED, "checked": checked, "findings": findings}


def main(argv=None):
    """Run the gate across the live corpus and report the auto-verification rate.

    Rebuilds each corpus from the CACHED pdfs via extract_llm.build_corpus, so this is offline and
    costs nothing — no LLM call, no HCAI fetch.
    """
    import argparse
    import collections
    import json
    import sys

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dataset", default="data/cobijo_charity_care_dataset.json")
    ap.add_argument("--records", default="data/dataset_current.json",
                    help="scraped records (needed to rebuild the corpus exactly)")
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW)
    ap.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    ap.add_argument("--out", default="output/grounding_report.json")
    ap.add_argument("--annotate", action="store_true",
                    help="write each row's verdict back into the dataset as row['grounding']. "
                         "This is what navigator.py reads to decide whether to serve a number, so "
                         "the gate is enforced at serve time with no fetch and no LLM call.")
    args = ap.parse_args(argv)

    import extract_llm

    rows = json.load(open(args.dataset, encoding="utf-8"))
    recs = json.load(open(args.records, encoding="utf-8"))
    by_link = {r.get("permalink"): r for r in rows}

    tally = collections.Counter()
    report = []
    for i, rec in enumerate(recs, 1):
        row = by_link.get(rec.get("permalink"))
        if not row:
            continue
        try:
            corpus, _ = extract_llm.build_corpus(rec)
        except Exception as e:                       # noqa: BLE001 - any fetch/parse failure is
            corpus = None                            # "unverifiable", never a silent pass
            print(f"  corpus failed for {rec.get('post_title')}: {e}", file=sys.stderr)
        res = check_row(row, corpus, args.window)
        tally[res["verdict"]] += 1
        if args.annotate:
            # Provenance only — never the findings blob, which is large and belongs in the report.
            row["grounding"] = {"verdict": res["verdict"], "checked": res["checked"]}
        if res["verdict"] != GROUNDED:
            report.append({"hospital": row.get("hospital"), "permalink": rec.get("permalink"),
                           **res})
        if args.limit and i >= args.limit:
            break

    total = sum(tally.values())
    print(f"\ngrounding gate — {total} rows")
    for v in (GROUNDED, UNGROUNDED, UNVERIFIABLE, STALE):
        if tally[v]:
            print(f"  {tally[v]:4d}  {v}  ({tally[v] / total * 100:.1f}%)")
    checkable = total - tally[UNVERIFIABLE] - tally[STALE]
    if checkable:
        print(f"\nauto-verification rate (of checkable rows): "
              f"{tally[GROUNDED] / checkable * 100:.1f}%")

    import os
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"tally": dict(tally), "not_grounded": report}, f, indent=2)
    print(f"\nwrote {args.out} ({len(report)} rows needing attention)")

    if args.annotate:
        # A row the loop never reached (no matching scraped record) must not silently keep a stale
        # verdict from a previous run — drop it so absence reads as "unknown", not "still fine".
        seen = {rec.get("permalink") for rec in recs}
        cleared = 0
        for row in rows:
            if row.get("permalink") not in seen and "grounding" in row:
                del row["grounding"]
                cleared += 1
        with open(args.dataset, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=2, ensure_ascii=False)
        note = f", cleared {cleared} stale" if cleared else ""
        print(f"annotated {args.dataset} with grounding verdicts{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
