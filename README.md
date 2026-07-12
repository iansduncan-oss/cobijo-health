# Cobijo Health — CA charity-care navigator

A free, multilingual tool that helps low-income, uninsured, rural, and immigrant patients find
and access financial help for hospital bills — **charity care**, **government-benefit screening**
(Medi-Cal / ACA), and **medical-debt navigation**. California-first. Being built as a California
nonprofit public benefit corporation (501(c)(3) in formation).

**The moat:** no open, structured dataset of every CA hospital's financial-assistance rules
exists. California's HCAI publishes the policies only as per-hospital PDFs (the current lookup no
longer exposes the FPL% thresholds as fields). This project builds that structured dataset by
extraction, and wraps it in a navigator that turns a patient's situation into a concrete action
plan + a ready-to-send assistance-request letter.

---

## Repo layout
```
.                 pipeline modules (run from repo root) + poll_batch.sh + README
data/             all data artifacts — json datasets, extract_cache/ (batch state), pdfs/
docs/             grouped: product/ · legal/ (+incorporation/) · funding/ (+applications/) · board/
output/           generated (gitignored): assistance letters, QA worksheet/report, rendered PDFs
tests/            test_cobijo.py  (python3 -m unittest discover -s tests)
web/              MVP intake site — python3 web/server.py -> http://localhost:8000
```
Scripts are run from the repo root; data paths are `data/…`, generated files land in `output/`.

## Pipeline

```
  enumerate            extract (the moat)              assemble        QA              serve
 ┌──────────┐   ┌──────────────────────────────┐   ┌──────────┐  ┌───────────┐   ┌───────────┐
 │  HCAI    │   │ extract_llm.py  (text PDFs)   │   │  build_  │  │   qa_     │   │ navigator │
 │  lookup  │──▶│ extract_scanned.py (OCR PDFs) │──▶│ dataset  │─▶│ dataset   │──▶│   .py     │
 │ scraper  │   │  → per-hospital charity rules │   │   .py    │  │   .py     │   │ +policy-  │
 └──────────┘   └──────────────────────────────┘   └──────────┘  └───────────┘   │  engine   │
  469 hospitals   FPL% free/discount, tiers,          one row/      semantic       intake →     
  + PDF pointers  dollar tables, provenance           hospital      review queue   plan+letter  
```

### 1. Enumerate — `hcai_lookup_scraper.py`
Pulls all **469** CA hospitals from HCAI's Hospital Fair Pricing Policy Lookup (via its public
hosted Elasticsearch) → `index_current.json` (name, city, ZIP, county) and `dataset_current.json`
(per-hospital Charity Care / Discount Payment policy **PDF pointers** + effective dates).
```
python3 hcai_lookup_scraper.py            # full run
python3 hcai_lookup_scraper.py --index-only --limit 20
```

### 2. Extract (the moat) — `extract_llm.py` + `extract_scanned.py`
Fetches each hospital's policy PDFs and extracts the structured rules against a comprehensive
schema (free-care FPL%, discount tiers + basis + AGB, high-medical-cost, payment plans,
eligibility, presumptive, scope, debt-collection, contact, source quotes). Content-hash dedup
collapses system chains (Adventist/Kaiser/Sutter share one policy) — 469 hospitals → ~249 unique
corpora. Per-`sha` cache makes it resumable; `validate()` flags rows `needs_review`.
```
python3 extract_llm.py --batch            # Opus via the Batches API (50% cheaper); poll to finish
python3 fetch_batch.py                     # re-attach to an in-flight batch by ID (no re-submit)
python3 extract_scanned.py                 # ~11 image-only PDFs, native Claude PDF-OCR
```
Text-layer PDFs → `extracted_full.json`; scanned → `extracted_scanned.json`.

### 3. Assemble — `build_dataset.py`
Splices scanned extractions over `needs_ocr` placeholders → **`cobijo_charity_care_dataset.json`**
(one row per hospital, with per-household dollar tables) and prints a coverage report.
```
python3 build_dataset.py
```

### 4. QA — `qa_dataset.py`
Read-only **semantic** review layer on top of `validate()` (which only checks structure). Flags
implausibly-high ceilings (a likely units/decimal misread — note 400% FPL is the statutory *floor*
per HSC §127405, not a cap, so generous >400% policies are legitimate and NOT flagged), tier-geometry
problems, dollar-table mismatches, and **system-chain divergence** (rows sharing a source PDF that
extracted differently). Emits a prioritized human-review worksheet + JSON; exits non-zero on any HIGH.
```
python3 qa_dataset.py                      # → qa_review_worksheet.md + qa_report.json
```

### 5. Serve — `navigator.py` (+ `policyengine.py`)
Turns an intake (income, household, insurance, collections) into a personalized plan and a
ready-to-send assistance-request letter, grounded in that hospital's extracted policy. Benefit
screening calls **PolicyEngine** (real CA Medi-Cal + ACA rules) with an offline FPL-heuristic
fallback. Patient-facing text is multilingual via the `messages.py` catalog.
```
python3 navigator.py --hospital "MOUNTAINS COMMUNITY HOSPITAL" --income 40000 --household 4
python3 navigator.py --lang es ...         # plan in Spanish (hospital letter stays English)
python3 navigator.py --offline ...         # skip PolicyEngine (no network)
```

---

## Files
| File | Role |
|---|---|
| `hcai_lookup_scraper.py` | Stage 1 — enumerate hospitals + policy PDF pointers |
| `extract_llm.py` | Stage 2 — LLM extraction (schema, `validate()`, dollar tables, Batches API) |
| `extract_scanned.py` | Stage 2 — OCR extraction for image-only PDFs |
| `fetch_batch.py` / `poll_batch.sh` | Re-attach to / poll an in-flight extraction batch |
| `build_dataset.py` | Stage 3 — assemble the final dataset + coverage report |
| `qa_dataset.py` | Stage 4 — semantic QA + human-review worksheet |
| `navigator.py` | Stage 5 — intake → plan + assistance letter |
| `policyengine.py` | Real CA benefit eligibility (PolicyEngine API, stdlib) |
| `messages.py` | en/es message catalog + `t(lang, key, …)` |
| `make_index_pdf.py` | Renders `index_current.json` as a PDF (HCAI attachment) |
| `syfphr_scraper.py` | Legacy SYFPHR scrape — **archive baseline only** (pre-2025 rules) |
| `golden_mountains_community.json` | Hand-verified extraction, schema/QA sanity anchor |

## Docs (`docs/`, grouped)
- **`product/`** — `sharpened-plan-v2.md` (living plan), `hcai-data-access.md` (how the data was
  reverse-engineered), `hcai-data-request.md` (the CPRA request, sent 2026-07-09).
- **`legal/`** — `founding-runbook.md` (turnkey steps to incorporate), `trademark-attorney-inquiry.md`,
  `privacy-posture.md` + `privacy-policy-draft.md`, and `incorporation/` (Articles, bylaws, 1023-EZ
  prep). Legal/privacy items are **DRAFT — attorney/counsel review pending**.
- **`funding/`** — `funding-targets.md` (prioritized funders; fiscal sponsorship as the
  pre-501(c)(3) unlock) and `applications/` (ready-to-use application drafts + executive summary).
- **`board/`** — `board-recruitment-brief.md` (the 5 seats) + `board-outreach-kit.md` (templates).

## Tests
`python3 -m unittest test_cobijo` — stdlib, no deps or network. Covers the charity-care match,
the benefit heuristic, the en/es catalog integrity (missing/drifted translations), and the QA
harness's semantic checks.

## Requirements
Python 3, stdlib for most stages. Extraction needs an Anthropic API key (keychain
`claude-memory/anthropic-api-key`); `qa_dataset.py`/`navigator.py` run offline (navigator's
PolicyEngine call needs network, with a heuristic fallback). `reportlab` for `make_index_pdf.py`.

## License
- **Code:** Apache License 2.0 — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
- **Compiled dataset** (the CA charity-care data in `data/*.json`): CC BY 4.0 — see
  [`DATA-LICENSE.md`](DATA-LICENSE.md). Open source + open data, so the tools and the dataset are
  a genuine public good.

## Status
The moat dataset is **built — 465 of 469 California hospitals** extracted and assembled, validated
against HSC §127405. The full pipeline — extraction, assembly, QA, navigator, PolicyEngine, Spanish
— is built, tested (66 unit tests), and running. Org formation is gated on recruiting the founding
board (see `docs/legal/founding-runbook.md`).
