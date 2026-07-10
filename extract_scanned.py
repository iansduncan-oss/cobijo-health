#!/usr/bin/env python3
"""
Scanned-PDF extraction — closes the ~11 hospitals whose policy PDFs have no text layer
(image-only scans), so `pdftotext` returns nothing and extract_llm.py flags them needs_ocr.

No local OCR engine needed: the Anthropic API reads scanned PDFs natively via a `document`
content block (it OCRs / visually analyzes the pages). We send the raw PDF + the SAME
tool-forced extraction schema as extract_llm.py, so the output is identical in shape and
merges cleanly into the main dataset.

Reuses extract_llm.py's SCHEMA / SYSTEM / validation / dollar tables — single source of truth.

Usage:
  python3 extract_scanned.py --limit 1     # test on one scanned hospital
  python3 extract_scanned.py               # all scanned hospitals -> extracted_scanned.json
"""
import argparse
import base64
import json
import os
import sys
import time

import extract_llm as ex


def scanned_hospitals(data):
    """Hospitals whose concatenated policy text is empty/near-empty (image-only scans)."""
    out = []
    for rec in data:
        corpus, _ = ex.build_corpus(rec)          # downloads (cached) + pdftotext
        if len(corpus.strip()) < 500:
            out.append(rec)
    return out


def extract_pdf(url, model, key, max_retries=4):
    """Send a scanned PDF as a document block; force the record_policy tool; return its input."""
    guid = ex._guid(url)
    pdf = os.path.join(ex.PDF_DIR, f"{guid}.pdf")
    ex.download_pdf(url, pdf)
    b64 = base64.standard_b64encode(open(pdf, "rb").read()).decode()
    body = {
        "model": model,
        "max_tokens": ex.MAX_OUTPUT_TOKENS,
        "system": ex.SYSTEM,
        "tools": [{"name": "record_policy",
                   "description": "Record the extracted charity-care / discount-payment policy terms.",
                   "input_schema": ex.SCHEMA}],
        "tool_choice": {"type": "tool", "name": "record_policy"},
        "messages": [{"role": "user", "content": [
            {"type": "document",
             "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text",
             "text": "This is a scanned California hospital Charity Care / Discount Payment "
                     "policy PDF. Read it and extract the policy terms."},
        ]}],
    }
    from urllib.error import HTTPError
    for attempt in range(max_retries):
        try:
            resp = ex._api_call("POST", ex.API_URL, key, body, timeout=180)
            return ex._tool_input(resp)
        except HTTPError as e:
            if e.code in (429, 500, 529) and attempt < max_retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("exhausted retries")


def main():
    ap = argparse.ArgumentParser(description="Scanned-PDF charity-care extraction (native PDF OCR via Claude)")
    ap.add_argument("--dataset", default="data/dataset_current.json")
    ap.add_argument("--model", default=ex.DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="data/extracted_scanned.json")
    args = ap.parse_args()

    key = ex._api_key()
    if not key:
        sys.exit("No API key (env ANTHROPIC_API_KEY or keychain claude-memory/anthropic-api-key).")

    data = json.load(open(args.dataset))
    scanned = scanned_hospitals(data)
    if args.limit:
        scanned = scanned[:args.limit]
    print(f"{len(scanned)} scanned hospitals to extract via native PDF OCR.", file=sys.stderr)

    out = []
    for i, rec in enumerate(scanned, 1):
        pols = rec["policies"]
        url = pols["charity_care"].get("current_policy_url") or pols["discount_payment"].get("current_policy_url")
        base = {
            "hospital": rec["post_title"], "oshpdid": rec.get("archive_oshpdid"),
            "city": rec.get("city"), "county": rec.get("county"), "zip": rec.get("zip"),
            "permalink": rec.get("permalink"), "charity_policy_url": url,
            "charity_effective_date": pols["charity_care"].get("current_effective_date"),
            "model": args.model, "source": "scanned_pdf_ocr",
        }
        if not url:
            out.append({**base, "status": "no_policy_pdf", "needs_review": True,
                        "review_reasons": ["no policy PDF url"]})
            print(f"  [{i}/{len(scanned)}] {rec['post_title'][:40]:40} no_policy_pdf", file=sys.stderr)
            continue
        try:
            ext = extract_pdf(url, args.model, key)
            if ext is None:
                raise ValueError("no tool_use block")
            reasons = ex.validate(ext)
            fc = (ext.get("free_care") or {}).get("fpl_ceiling_pct")
            dc = (ext.get("discount_payment") or {}).get("fpl_ceiling_pct")
            out.append({**base, "status": "extracted", "policy": ext,
                        "free_care_income_ceiling_by_household": ex.dollar_table(fc),
                        "discount_income_ceiling_by_household": ex.dollar_table(dc),
                        "needs_review": bool(reasons), "review_reasons": reasons})
            print(f"  [{i}/{len(scanned)}] {rec['post_title'][:40]:40} "
                  f"free≤{fc} disc→{dc} conf={ext.get('extraction_confidence')}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            out.append({**base, "status": "extraction_error", "error": str(e),
                        "needs_review": True, "review_reasons": [str(e)]})
            print(f"  [{i}/{len(scanned)}] {rec['post_title'][:40]:40} ERROR: {e}", file=sys.stderr)
        time.sleep(0.5)

    json.dump(out, open(args.out, "w"), indent=2, ensure_ascii=False)
    ok = sum(1 for r in out if r["status"] == "extracted")
    print(f"\nWrote {len(out)} -> {args.out}  ({ok} extracted, {sum(1 for r in out if r.get('needs_review'))} need review)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
