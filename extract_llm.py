#!/usr/bin/env python3
"""
Extraction stage (production) — turn the HCAI charity-care policy PDFs into a complete,
structured, verifiable dataset using an LLM with a strict schema.

Why an LLM (not regex): docs/hcai-data-access.md shows the current HCAI lookup no longer
exposes FPL% thresholds as fields — they live inside each hospital's policy PDFs, in
wildly varied tables + legal prose that regex misreads (it confuses the discount ceiling
with the free-care threshold). The PDFs are digital and DO contain every rule; a schema-
constrained model reads them reliably.

Design for "clean & correct, all information":
  * Comprehensive schema — free care, high-medical-cost, discount tiers + basis, AGB,
    payment plans, application/eligibility process, presumptive eligibility, covered/
    excluded services, debt-collection/ECA terms, contact — plus `additional_provisions`
    and `source_quotes` so nothing is dropped and every key number is auditable.
  * Content-hash dedup — many hospitals in a system (Adventist, Kaiser, Sutter, Dignity/
    CommonSpirit…) share an identical policy PDF; we extract once per unique text and map
    the result back to every hospital that shares it. Cheaper AND internally consistent.
  * Provenance — source URLs, effective dates, model, extracted_at, sha256 of the source
    text (drift detection), and per-run caching for resume.
  * Verification, not blind trust (per the "AI enhances, never decides alone" rule) —
    rule-based validation (free ≤ discount ceiling, FPL% in range, tiers ascending) sets
    `needs_review` + `review_reasons`; scanned PDFs are flagged `needs_ocr` (never guessed).

Backend: Anthropic Messages API via stdlib HTTPS (no SDK dep). Key from $ANTHROPIC_API_KEY
or `security find-generic-password -s claude-memory -a anthropic-api-key -w`.

Usage:
  python3 extract_llm.py --limit 5 --dry-run     # build corpora + prompts, NO API calls
  python3 extract_llm.py --limit 5               # extract 5 hospitals (needs key)
  python3 extract_llm.py                         # full run, resumable
"""
import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.error import URLError, HTTPError

UA = "cobijo-health-research/0.2 (nonprofit charity-care dataset; contact ian@aviontechs.com)"
PDF_DIR = "data/pdfs"
CACHE_DIR = "data/extract_cache"     # one <sha>.json per unique policy corpus (resume)
API_URL = "https://api.anthropic.com/v1/messages"
BATCH_URL = "https://api.anthropic.com/v1/messages/batches"
DEFAULT_MODEL = "claude-opus-4-8"
MAX_CHARS = 120_000                  # ~30k tokens; policies run 15-70k chars
MAX_OUTPUT_TOKENS = 8000             # rich policies emit long structured output; 4096 truncated

# 2026 HHS Federal Poverty Guidelines (48 states + DC), effective 1/13/2026 — update each Jan.
FPL = {1: 15960, 2: 21640, 3: 27320, 4: 33000, 5: 38680, 6: 44360, 7: 50040, 8: 55720}
FPL_EACH_ADDITIONAL = 5680

# CA charity-care statutory thresholds — Health & Safety Code §127405 (Hospital Fair Pricing Act).
# 400% FPL is a FLOOR, not a cap: hospitals MUST offer charity care or discount payment to patients at
# or below it, and "may choose to grant eligibility ... to patients with incomes over 400 percent."
# So ceilings above 400% are legal — and common (~114 CA hospitals discount to 450-700% FPL) — NOT
# misreads. We flag a ceiling as a likely units/decimal misread only past a realistic maximum, and hold
# FREE (100% write-off) care to a tighter bound than partial DISCOUNTs, since free care rarely exceeds
# the floor. (These were previously conflated at a single 500% bound, which false-flagged legit rows.)
STATUTORY_FPL_FLOOR = 400          # hospitals must cover <= this; may exceed it (not a cap)
DISCOUNT_IMPLAUSIBLE_PCT = 800     # discount ceiling above this ~= units/decimal misread (legit max ~700%)
FREE_CARE_UNUSUAL_PCT = 400        # free care above the floor is unusual -> verify (MEDIUM in qa)
FREE_CARE_IMPLAUSIBLE_PCT = 600    # free care this high ~= misread/misinforming (HIGH)


# --------------------------------------------------------------------------- #
# Extraction schema (Anthropic tool input_schema). Everything nullable so the
# model reports honestly rather than inventing; catch-alls prevent data loss.
# --------------------------------------------------------------------------- #
def _pct(desc):
    return {"type": ["number", "null"], "description": desc}


TIER = {
    "type": "object",
    "properties": {
        "fpl_low_pct": _pct("Bottom of the income band, as % of FPL (inclusive)."),
        "fpl_high_pct": _pct("Top of the income band, as % of FPL (inclusive)."),
        "benefit": {"type": ["string", "null"],
                    "description": "What the patient gets in this band, verbatim-ish: e.g. 'Full charity (100%)', "
                                   "'discount to AGB', 'pays 20% of Medicare rate'."},
        "discount_basis": {"type": ["string", "null"],
                           "description": "Basis for the discounted price: 'AGB', 'Medicare', 'Medi-Cal', "
                                          "'percent of charges', etc., exactly as the policy states."},
        "patient_pays_pct": _pct("If stated, the % the patient pays (of AGB/charges/Medicare per discount_basis)."),
    },
    "additionalProperties": False,
}

SCHEMA = {
    "type": "object",
    "properties": {
        "policy_type": {"type": ["string", "null"],
                        "description": "e.g. 'Combined Charity Care & Discount Payment', 'Charity Care only'."},
        "free_care": {
            "type": "object",
            "properties": {
                "fpl_ceiling_pct": _pct("Household-income ceiling (% of FPL) for FREE / full (100%) charity care. "
                                        "The threshold at or below which care is free. NOT the discount ceiling."),
                "applies_to": {"type": ["string", "null"],
                               "description": "Who qualifies for free care: 'uninsured', 'insured/underinsured', 'both'."},
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "high_medical_cost": {
            "type": "object",
            "description": "Provision for INSURED patients with high out-of-pocket costs (often income ≤ some FPL% "
                           "AND OOP > a % of income).",
            "properties": {
                "oop_threshold_pct_of_income": _pct("Out-of-pocket costs must exceed this % of annual income (e.g. 10)."),
                "fpl_ceiling_pct": _pct("Income ceiling (% of FPL) for this provision, if any."),
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "discount_payment": {
            "type": "object",
            "properties": {
                "fpl_ceiling_pct": _pct("Top income (% of FPL) eligible for ANY discount (usually 350 or 400)."),
                "tiers": {"type": "array", "items": TIER,
                          "description": "Every sliding-scale band, ascending. Empty if the policy has no tiers."},
                "agb_percentage": _pct("Amounts Generally Billed as a % of gross charges, if stated."),
                "agb_method": {"type": ["string", "null"],
                               "description": "'look-back' or 'prospective Medicare', etc., if stated."},
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "payment_plans": {
            "type": "object",
            "properties": {
                "interest_free": {"type": ["boolean", "null"]},
                "reasonable_payment_plan": {"type": ["boolean", "null"],
                                            "description": "References HSC 127400 'reasonable payment plan'."},
                "max_monthly_pct_of_income": _pct("Cap on monthly payment as % of income, if stated."),
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "eligibility_process": {
            "type": "object",
            "properties": {
                "application_required": {"type": ["boolean", "null"]},
                "documentation_required": {"type": "array", "items": {"type": "string"},
                                           "description": "Docs the patient must provide (income proof, tax return, etc.)."},
                "application_window_days": {"type": ["integer", "null"],
                                            "description": "Days after billing/service to apply, if stated."},
                "presumptive_eligibility": {"type": ["boolean", "null"]},
                "presumptive_criteria": {"type": ["string", "null"]},
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "scope": {
            "type": "object",
            "properties": {
                "covered_services": {"type": "array", "items": {"type": "string"}},
                "excluded_services": {"type": "array", "items": {"type": "string"},
                                      "description": "e.g. 'ER physician fees', 'radiology', 'separately-billed physicians'."},
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "debt_collection": {
            "type": "object",
            "properties": {
                "waiting_period_days_before_ecas": {"type": ["integer", "null"],
                                                    "description": "Days before Extraordinary Collection Actions (often 180)."},
                "reports_to_credit_bureaus": {"type": ["boolean", "null"]},
                "sells_debt": {"type": ["boolean", "null"]},
                "ecas_used": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "contact": {
            "type": "object",
            "properties": {
                "phone": {"type": ["string", "null"]},
                "website": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "additional_provisions": {"type": "array", "items": {"type": "string"},
                                  "description": "ANY other material rule not captured above, so no information is lost."},
        "source_quotes": {
            "type": "object",
            "description": "Verbatim snippets backing the key numbers, for audit.",
            "properties": {
                "free_care": {"type": ["string", "null"]},
                "discount_ceiling": {"type": ["string", "null"]},
                "agb": {"type": ["string", "null"]},
            },
            "additionalProperties": False,
        },
        "extraction_confidence": {"type": "number",
                                  "description": "0-1 self-assessed confidence the structured values match the policy."},
        "extraction_notes": {"type": ["string", "null"],
                             "description": "Ambiguities, conflicts, or anything the reviewer should know."},
    },
    "required": ["free_care", "discount_payment", "extraction_confidence"],
    "additionalProperties": False,
}

SYSTEM = (
    "You are a meticulous health-policy data analyst extracting California hospital "
    "Charity Care / Discount Payment policy terms into a strict schema for a nonprofit that "
    "helps low-income patients. Accuracy is critical — patients rely on this. Rules: "
    "(1) Extract ONLY what the document states; never infer or fill from general knowledge. "
    "(2) Use null / empty when a field is absent — do NOT guess. "
    "(3) The FREE-care ceiling is the income at/below which care is 100% free; the DISCOUNT "
    "ceiling is the top income getting any reduced (non-free) price — never conflate them. "
    "(4) Capture every material rule; put anything not matching a field in additional_provisions. "
    "(5) Provide verbatim source_quotes for the key numbers. "
    "(6) Set extraction_confidence honestly; lower it when the policy is ambiguous or conflicting."
)


# --------------------------------------------------------------------------- #
# PDF fetch + text
# --------------------------------------------------------------------------- #
def _guid(url):
    return (url or "").split("id=")[-1][:36]


def download_pdf(url, dest):
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        data = r.read()
    with open(dest, "wb") as f:
        f.write(data)


def pdf_text(url):
    """Download (cached) + extract text. Returns '' if unreachable or scanned."""
    if not url:
        return ""
    guid = _guid(url)
    pdf = os.path.join(PDF_DIR, f"{guid}.pdf")
    try:
        download_pdf(url, pdf)
    except (URLError, HTTPError):
        return ""
    try:
        out = subprocess.run(["pdftotext", "-layout", pdf, "-"],
                             capture_output=True, text=True, timeout=60)
        return out.stdout
    except Exception:
        return ""


def build_corpus(rec):
    """Concatenate a hospital's unique policy texts (charity, discount, debt) in order."""
    pols = rec["policies"]
    urls, seen = [], set()
    for sec in ("charity_care", "discount_payment", "debt_collection"):
        u = pols[sec].get("current_policy_url")
        g = _guid(u)
        if u and g not in seen:
            seen.add(g)
            urls.append((sec, u))
    parts = []
    for sec, u in urls:
        t = pdf_text(u)
        if t.strip():
            parts.append(f"===== {sec.upper()} POLICY =====\n{t}")
    corpus = "\n\n".join(parts)
    return corpus[:MAX_CHARS], len(corpus) > MAX_CHARS


# --------------------------------------------------------------------------- #
# LLM call (Anthropic Messages API, tool-forced structured output)
# --------------------------------------------------------------------------- #
def _api_key():
    k = os.environ.get("ANTHROPIC_API_KEY")
    if k:
        return k
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", "claude-memory",
             "-a", "anthropic-api-key", "-w"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _message_params(corpus, model):
    """The Messages-API params for one extraction — shared by sync + batch paths."""
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "system": SYSTEM,
        "tools": [{"name": "record_policy",
                   "description": "Record the extracted charity-care / discount-payment policy terms.",
                   "input_schema": SCHEMA}],
        "tool_choice": {"type": "tool", "name": "record_policy"},
        "messages": [{"role": "user",
                      "content": "Extract the policy terms from this California hospital "
                                 "Charity Care / Discount Payment policy:\n\n" + corpus}],
    }


def _tool_input(message):
    """Pull the forced tool_use input out of a Messages response."""
    for block in message.get("content", []):
        if block.get("type") == "tool_use":
            return block["input"]
    return None


def extract_one(corpus, model, key, max_retries=4):
    data = json.dumps(_message_params(corpus, model)).encode()
    for attempt in range(max_retries):
        req = urllib.request.Request(
            API_URL, data=data,
            headers={"content-type": "application/json",
                     "x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = json.load(r)
            inp = _tool_input(resp)
            if inp is not None:
                return inp, resp.get("usage", {})
            raise ValueError("no tool_use block in response")
        except HTTPError as e:
            if e.code in (429, 500, 529) and attempt < max_retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise
    raise RuntimeError("exhausted retries")


# --------------------------------------------------------------------------- #
# Batches API path (50% cheaper; ideal for a one-shot 249-corpus run)
# --------------------------------------------------------------------------- #
def _api_call(method, url, key, body=None, timeout=300):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def run_batch(items, model, key, poll_interval=30):
    """items: list of (sha, corpus). Submit one batch, poll to completion, return {sha: result|error}."""
    id_to_sha, requests = {}, []
    for i, (sha, corpus) in enumerate(items):
        cid = f"c{i}"                       # custom_id ≤ 64 chars; index keeps it short
        id_to_sha[cid] = sha
        requests.append({"custom_id": cid, "params": _message_params(corpus, model)})

    batch = _api_call("POST", BATCH_URL, key, {"requests": requests})
    bid = batch["id"]
    # Persist id→sha map so a killed poller can re-attach (see fetch_batch.py) — no re-submit.
    json.dump({"batch_id": bid, "model": model, "id_to_sha": id_to_sha,
               "truncated": {sha: False for sha, _ in items}},
              open(os.path.join(CACHE_DIR, "pending_batch.json"), "w"), indent=2)
    print(f"Submitted batch {bid} ({len(items)} requests). Polling every {poll_interval}s...", file=sys.stderr)

    while True:
        b = _api_call("GET", f"{BATCH_URL}/{bid}", key)
        counts = b.get("request_counts", {})
        if b.get("processing_status") == "ended":
            break
        print(f"  …processing {counts}", file=sys.stderr)
        time.sleep(poll_interval)
    print(f"  batch ended {counts}", file=sys.stderr)

    # Stream the JSONL results.
    out = {}
    req = urllib.request.Request(
        b["results_url"], headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        for raw in r:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            item = json.loads(line)
            sha = id_to_sha.get(item.get("custom_id"))
            res = item.get("result", {})
            if res.get("type") == "succeeded":
                inp = _tool_input(res.get("message", {}))
                out[sha] = inp if inp is not None else {"error": "no tool_use block"}
            else:
                detail = res.get("error") or res.get("type")
                out[sha] = {"error": f"batch_{res.get('type')}: {json.dumps(detail)[:200]}"}
    return out


# --------------------------------------------------------------------------- #
# Validation (verify, don't trust)
# --------------------------------------------------------------------------- #
def validate(rec):
    reasons = []
    fc = (rec.get("free_care") or {}).get("fpl_ceiling_pct")
    dp = rec.get("discount_payment") or {}
    dc = dp.get("fpl_ceiling_pct")
    tiers = dp.get("tiers") or []

    def implausible(v, hi):
        # >400% FPL is legal (§127405 lets a hospital exceed the floor), so we flag a ceiling as a likely
        # units/decimal misread (e.g. "$800" read as 800%) only past a realistic maximum. Free care gets a
        # tighter bound than partial discounts, since free (100% write-off) care rarely exceeds the floor.
        return v is not None and not (0 <= v <= hi)

    if fc is None and dc is None and not tiers:
        reasons.append("no free-care ceiling, discount ceiling, or tiers extracted")
    if implausible(fc, FREE_CARE_IMPLAUSIBLE_PCT):
        reasons.append(f"free-care FPL% out of range: {fc}")
    if implausible(dc, DISCOUNT_IMPLAUSIBLE_PCT):
        reasons.append(f"discount FPL% out of range: {dc}")
    if fc is not None and dc is not None and fc > dc:
        reasons.append(f"free-care ceiling ({fc}%) exceeds discount ceiling ({dc}%)")
    lows = [t.get("fpl_low_pct") for t in tiers if t.get("fpl_low_pct") is not None]
    if lows != sorted(lows):
        reasons.append("discount tiers not in ascending order")
    for tr in tiers:                          # a tier with a band but no stated benefit is uninformative
        if (tr.get("fpl_low_pct") is not None or tr.get("fpl_high_pct") is not None) \
                and not tr.get("benefit") and tr.get("patient_pays_pct") is None \
                and not tr.get("discount_basis"):
            reasons.append("a discount tier has an FPL band but no stated benefit/discount")
            break
    if rec.get("_truncated"):                  # the model saw a clipped policy — tiers may be missing
        reasons.append("policy text was truncated before extraction — may be incomplete")
    if (rec.get("extraction_confidence") or 0) < 0.6:
        reasons.append(f"low model confidence ({rec.get('extraction_confidence')})")
    return reasons


def effective_date_issue(date_str):
    """A patient-facing effective date that's in the future or unparseable is a data error worth a
    review flag (we show it as authoritative). Returns a reason string or None. Expects MM/DD/YYYY."""
    if not date_str:
        return None
    try:
        d = datetime.datetime.strptime(str(date_str).strip(), "%m/%d/%Y").date()
    except (ValueError, TypeError):
        return f"unparseable effective date: {date_str!r}"
    if d > datetime.date.today():
        return f"effective date is in the future: {date_str}"
    return None


def dollar_table(fpl_pct):
    if not fpl_pct:
        return {}
    def ceil(n):
        return FPL[n] if n <= 8 else FPL[8] + (n - 8) * FPL_EACH_ADDITIONAL
    return {n: round(ceil(n) * fpl_pct / 100) for n in range(1, 9)}


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="LLM charity-care policy extraction")
    ap.add_argument("--dataset", default="data/dataset_current.json")
    ap.add_argument("--limit", type=int, default=0, help="max hospitals (0 = all)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--batch", action="store_true",
                    help="use the Batches API (50%% cheaper; one submit + poll instead of N sync calls)")
    ap.add_argument("--dry-run", action="store_true", help="build corpora only; no API calls")
    ap.add_argument("--out", default="data/extracted_full.json")
    args = ap.parse_args()

    data = json.load(open(args.dataset))
    if args.limit:
        data = data[:args.limit]
    os.makedirs(CACHE_DIR, exist_ok=True)

    key = None if args.dry_run else _api_key()
    if not args.dry_run and not key:
        sys.exit("No API key. Set $ANTHROPIC_API_KEY or store it: "
                 "security add-generic-password -s claude-memory -a anthropic-api-key -w <KEY>")

    # 1. Build corpora (download + extract), dedup by content hash.
    corpora = {}   # sha -> {text, truncated}
    per_hosp = []  # (rec, sha|None, scanned)
    for i, rec in enumerate(data, 1):
        corpus, truncated = build_corpus(rec)
        if len(corpus.strip()) < 500:
            per_hosp.append((rec, None, True))
            print(f"  [{i}/{len(data)}] {rec['post_title'][:40]:40} SCANNED/EMPTY -> needs_ocr", file=sys.stderr)
            continue
        sha = hashlib.sha256(corpus.encode()).hexdigest()
        corpora.setdefault(sha, {"text": corpus, "truncated": truncated})
        per_hosp.append((rec, sha, False))
    uniq = len(corpora)
    print(f"\n{len(data)} hospitals -> {uniq} unique policy corpora "
          f"({sum(1 for _,s,sc in per_hosp if sc)} scanned/empty).", file=sys.stderr)

    if args.dry_run:
        print(f"[dry-run] would extract {uniq} unique corpora via {args.model}. No API calls made.",
              file=sys.stderr)
        return

    # 2. Extract each unique corpus once. Cached shas are reused (resume); the rest
    #    run either as one Batches submission (--batch, 50% cheaper) or sync calls.
    def cache_path(sha):
        return os.path.join(CACHE_DIR, f"{sha}.json")

    extracted, to_do = {}, []
    for sha, c in corpora.items():
        if os.path.exists(cache_path(sha)):
            extracted[sha] = json.load(open(cache_path(sha)))
        else:
            to_do.append((sha, c))
    print(f"{len(extracted)} cached, {len(to_do)} to extract"
          f"{' via Batches API' if args.batch else ''} on {args.model}.", file=sys.stderr)

    def persist(sha, result, truncated):
        if "error" not in result:
            result["_truncated"] = truncated
        json.dump(result, open(cache_path(sha), "w"), indent=2, ensure_ascii=False)
        extracted[sha] = result

    if to_do and args.batch:
        results = run_batch([(sha, c["text"]) for sha, c in to_do], args.model, key)
        for sha, c in to_do:
            persist(sha, results.get(sha, {"error": "missing from batch results"}), c["truncated"])
        ok = sum(1 for sha, _ in to_do if "error" not in extracted[sha])
        print(f"  batch done: {ok}/{len(to_do)} succeeded.", file=sys.stderr)
    elif to_do:
        for j, (sha, c) in enumerate(to_do, 1):
            try:
                result, usage = extract_one(c["text"], args.model, key)
                persist(sha, result, c["truncated"])
                print(f"  [{j}/{len(to_do)}] {sha[:10]} conf={result.get('extraction_confidence')} "
                      f"tok={usage.get('input_tokens','?')}/{usage.get('output_tokens','?')}", file=sys.stderr)
            except Exception as e:  # noqa: BLE001
                persist(sha, {"error": str(e)}, c["truncated"])
                print(f"  [{j}/{len(to_do)}] ERROR {sha[:10]}: {e}", file=sys.stderr)
            time.sleep(0.3)

    # 3. Map back to hospitals, validate, attach provenance + dollar tables.
    out = []
    for rec, sha, scanned in per_hosp:
        pols = rec["policies"]
        base = {
            "hospital": rec["post_title"],
            "oshpdid": rec.get("archive_oshpdid"),
            "city": rec.get("city"), "county": rec.get("county"), "zip": rec.get("zip"),
            "permalink": rec.get("permalink"),
            "charity_policy_url": pols["charity_care"].get("current_policy_url"),
            "charity_effective_date": pols["charity_care"].get("current_effective_date"),
            "discount_policy_url": pols["discount_payment"].get("current_policy_url"),
            "application_url": pols["charity_care"].get("application_url"),
            "extracted_at": None, "model": args.model, "source_sha256": sha,
        }
        if scanned:
            out.append({**base, "status": "needs_ocr", "needs_review": True,
                        "review_reasons": ["scanned PDF — no text layer; OCR required"]})
            continue
        ext = extracted.get(sha, {})
        if ext.get("error"):
            out.append({**base, "status": "extraction_error", "error": ext["error"],
                        "needs_review": True, "review_reasons": [ext["error"]]})
            continue
        reasons = validate(ext)
        date_issue = effective_date_issue(base["charity_effective_date"])
        if date_issue:
            reasons = reasons + [date_issue]
        fc = (ext.get("free_care") or {}).get("fpl_ceiling_pct")
        dc = (ext.get("discount_payment") or {}).get("fpl_ceiling_pct")
        out.append({
            **base, "status": "extracted",
            "policy": ext,
            "free_care_income_ceiling_by_household": dollar_table(fc),
            "discount_income_ceiling_by_household": dollar_table(dc),
            "needs_review": bool(reasons),
            "review_reasons": reasons,
        })

    json.dump(out, open(args.out, "w"), indent=2, ensure_ascii=False)
    n_ok = sum(1 for r in out if r["status"] == "extracted")
    n_rev = sum(1 for r in out if r.get("needs_review"))
    n_ocr = sum(1 for r in out if r["status"] == "needs_ocr")
    print(f"\nWrote {len(out)} -> {args.out}", file=sys.stderr)
    print(f"extracted={n_ok}  needs_review={n_rev}  needs_ocr={n_ocr}", file=sys.stderr)


if __name__ == "__main__":
    main()
