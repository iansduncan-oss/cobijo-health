#!/usr/bin/env python3
"""
Navigator — turns a patient intake into a personalized action plan and a ready-to-send
hospital financial-assistance request letter, using the structured CA charity-care dataset.

End-to-end UX the plan describes:
  intake -> match (charity care + benefit screen + debt defense) -> plan + documents

Consumes the LLM-extracted dataset (cobijo_charity_care_dataset.json, produced by
build_dataset.py from extract_llm.py + extract_scanned.py). Each hospital row carries a
`policy` object with free-care ceiling, discount tiers + basis, high-medical-cost provision,
payment plans, and per-household dollar tables — so the match is grounded in that hospital's
CURRENT published policy, not a heuristic.

Benefit screening now calls PolicyEngine (open-source, real CA Medi-Cal + ACA rules) via
policyengine.py, with a coarse FPL heuristic as the offline/failure fallback so a plan is
always produced. If a hospital row is `needs_review`, the plan says so — we never present
unverified numbers as settled.

Usage:
  python3 navigator.py                         # built-in demo scenarios (uses PolicyEngine)
  python3 navigator.py --offline               # skip PolicyEngine, use FPL heuristic
  python3 navigator.py --lang es               # patient plan in Spanish (letter stays English)
  python3 navigator.py --hospital "MOUNTAINS COMMUNITY HOSPITAL" --income 40000 --household 4
"""
import argparse
import json
import os
import textwrap
import time

import policyengine
from messages import t

HERE = os.path.dirname(os.path.abspath(__file__))

# PolicyEngine gives real CA eligibility; the FPL heuristic below is the offline fallback.
# Set to False by main() from --offline (a hard, manual off switch).
USE_POLICYENGINE = True
# Soft circuit breaker: on a transient API failure we skip PolicyEngine for this long, then
# retry. A permanent latch would silently kill the Medi-Cal figure for a whole server process
# after one blip; this self-heals (and still spares a fully-down API from being hammered).
PE_COOLDOWN_SECONDS = 60
_PE_COOLDOWN_UNTIL = 0.0

# 2026 HHS Federal Poverty Guidelines (48 states + DC), annual USD. Effective 1/13/2026.
# Verified 2026-07 against ASPE: https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines
# Re-verify each January when new guidelines publish — every eligibility % divides by these.
FPL = {1: 15960, 2: 21640, 3: 27320, 4: 33000, 5: 38680, 6: 44360, 7: 50040, 8: 55720}
FPL_EACH_ADDITIONAL = 5680

# Full datasets are gitignored (regenerate via extract_llm.py + build_dataset.py); sample_dataset.json
# is a small tracked fallback so a fresh clone / CI / the public repo runs out of the box.
DATASETS = ("cobijo_charity_care_dataset.json", "extracted_full.json", "extracted_smoke.json",
            "sample_dataset.json")


def poverty_limit(n):
    n = max(1, n)          # smallest household is 1; guards bad/crafted input (0, negative) from KeyError
    return FPL[n] if n <= 8 else FPL[8] + (n - 8) * FPL_EACH_ADDITIONAL


def fpl_percent(income, household):
    income = max(0, income)     # a negative income (e.g. business loss) is treated as 0 so we never
    return round(income / poverty_limit(household) * 100, 1)   # show a negative FPL% or a bogus tier


def load_dataset():
    """Load the extracted dataset; index usable rows by oshpdid AND UPPER(hospital name)."""
    for name in DATASETS:
        path = os.path.join(HERE, "data", name)
        if not os.path.exists(path):
            continue
        rows = [r for r in json.load(open(path)) if r.get("status") == "extracted" and r.get("policy")]
        by_id = {r["oshpdid"]: r for r in rows if r.get("oshpdid")}
        by_name = {r["hospital"].upper(): r for r in rows if r.get("hospital")}
        return {"by_id": by_id, "by_name": by_name, "rows": rows}, name
    raise SystemExit("No extracted dataset found — run extract_llm.py then build_dataset.py.")


def find_hospital(ds, *, oshpdid=None, name=None):
    if oshpdid and oshpdid in ds["by_id"]:
        return ds["by_id"][oshpdid]
    if name:
        if name.upper() in ds["by_name"]:
            return ds["by_name"][name.upper()]
        needle = name.upper().strip()
        if len(needle) >= 4:                            # forgiving substring match, but a 1-3 char
            for hn, row in ds["by_name"].items():       # fragment ("a") would match the wrong hospital
                if needle in hn:
                    return row
    return None


def dollars_for(row, table, household):
    # Tolerate both str keys (JSON-loaded dataset) and int keys (in-memory dollar_table output).
    tbl = row.get(table) or {}
    return tbl.get(str(household), tbl.get(household))


def match_charity_care(row, pct, household, insured, lang="en"):
    """Return (tier, plain_language) for this patient at this hospital, from its policy."""
    pol = row["policy"]
    name = row["hospital"].title()
    free = (pol.get("free_care") or {}).get("fpl_ceiling_pct")
    dp = pol.get("discount_payment") or {}
    tiers = dp.get("tiers") or []
    disc_ceiling = dp.get("fpl_ceiling_pct")
    hmc = pol.get("high_medical_cost") or {}

    # 1. Free / full charity care.
    if free is not None and pct <= free:
        ceil = dollars_for(row, "free_care_income_ceiling_by_household", household)
        upto = t(lang, "cc_free_upto", household=household, ceil=ceil) if ceil else ""
        return "free", t(lang, "cc_free", name=name, free=free, upto=upto, pct=pct)

    # 2. Sliding-scale discount tier.
    for tier in tiers:
        lo, hi = tier.get("fpl_low_pct"), tier.get("fpl_high_pct")
        if lo is not None and hi is not None and lo <= pct <= hi:
            benefit = tier.get("benefit")
            basis = tier.get("discount_basis")
            how = benefit or (t(lang, "cc_how_basis", basis=basis) if basis else t(lang, "cc_how_default"))
            return "discount", t(lang, "cc_discount_tier", pct=pct, lo=lo, hi=hi, how=how)

    # 3. Under the discount ceiling but no explicit tier matched.
    if disc_ceiling is not None and (free is None or pct > free) and pct <= disc_ceiling:
        return "discount", t(lang, "cc_discount_ceiling", name=name, disc_ceiling=disc_ceiling)

    # 4. Insured patient with high out-of-pocket costs.
    if insured and hmc.get("oop_threshold_pct_of_income"):
        thr = hmc["oop_threshold_pct_of_income"]
        return "high_cost", t(lang, "cc_high_cost", name=name, thr=thr)

    ceiling = max([v for v in (free, disc_ceiling) if v is not None] or [0])
    return "over", t(lang, "cc_over", pct=pct, name=name, ceiling=ceiling)


def screen_benefits(pct, insurance, income=None, household=None, lang="en"):
    """Benefit leads for this patient.

    Primary path = PolicyEngine (real CA rules, grounded dollar estimates). Falls back to the
    FPL heuristic below on any API failure (offline, timeout, schema change) so a plan is
    always produced. `income`/`household` are required for the PolicyEngine path.
    """
    if insurance and insurance.lower() not in ("none", "uninsured", "self-pay"):
        return [t(lang, "ben_insured")]

    global _PE_COOLDOWN_UNTIL
    if USE_POLICYENGINE and time.time() >= _PE_COOLDOWN_UNTIL and income and household:
        try:
            leads, _ = policyengine.benefit_leads(income, household, lang=lang)
            return leads
        except Exception as e:                      # network/timeout/schema — degrade, don't crash
            _PE_COOLDOWN_UNTIL = time.time() + PE_COOLDOWN_SECONDS   # back off, then self-heal
            print(f"[PolicyEngine unavailable ({type(e).__name__}) — FPL heuristic for {PE_COOLDOWN_SECONDS}s]")

    # --- Fallback: coarse FPL heuristic ---
    if pct <= 138:
        return [t(lang, "ben_medicaid_heuristic", pct=pct)]
    if pct <= 400:
        return [t(lang, "ben_aca_heuristic", pct=pct)]
    return [t(lang, "ben_over_heuristic")]


def debt_defense(in_collections, lang="en"):
    if not in_collections:
        return []
    return [t(lang, k) for k in ("debt_rights", "debt_retroactive", "debt_legalaid", "debt_credit_note")]


def build_plan(intake, row, lang="en"):
    pct = fpl_percent(intake["annual_income"], intake["household_size"])
    insured = bool(intake.get("insurance")) and intake["insurance"].lower() not in ("none", "uninsured", "self-pay")
    tier, cc_msg = match_charity_care(row, pct, intake["household_size"], insured, lang=lang)
    pol = row["policy"]
    name = row["hospital"].title()

    L = [t(lang, "plan_greeting", first=intake["first_name"], name=name),
         t(lang, "plan_household", household=intake["household_size"],
           income=intake["annual_income"], pct=pct), ""]
    if row.get("charity_effective_date"):
        L.append(t(lang, "plan_effective", name=name, date=row["charity_effective_date"]))
    if row.get("needs_review"):
        L.append(t(lang, "plan_needs_review"))
    L.append("")

    n = 1                                     # number steps dynamically so an omitted step leaves no gap
    L.append(t(lang, "step1", n=n)); n += 1
    L.append("  " + cc_msg)
    phone = (pol.get("contact") or {}).get("phone")
    L.append("  " + (t(lang, "step1_apply_phone", phone=phone) if phone else t(lang, "step1_apply_nophone")))
    L.append("  " + t(lang, "step1_retroactive"))
    pp = pol.get("payment_plans") or {}
    if pp.get("interest_free"):
        L.append("  " + t(lang, "step1_interest_free"))
    if lang != "en":                       # the generated letter (below) stays English for the hospital
        L.append("  " + t(lang, "letter_note_english"))
    L.append("")

    benefits = screen_benefits(pct, intake.get("insurance"),
                               income=intake.get("annual_income"),
                               household=intake.get("household_size"), lang=lang)
    if benefits:
        L.append(t(lang, "step2", n=n)); n += 1
        L += ["  • " + b for b in benefits] + [""]
    dd = debt_defense(intake.get("in_collections"), lang=lang)
    if dd:
        L.append(t(lang, "step3", n=n)); n += 1
        L += ["  • " + d for d in dd] + [""]
    L.append(t(lang, "step4", n=n))
    return pct, tier, "\n".join(L)


def build_plan_struct(intake, row, lang="en"):
    """Structured form of the plan for rich UIs (web/SMS). Same content as build_plan, as data
    not text — reuses the exact match/screen/debt logic so it can never drift from the CLI."""
    pct = fpl_percent(intake["annual_income"], intake["household_size"])
    household = intake["household_size"]
    insured = bool(intake.get("insurance")) and intake["insurance"].lower() not in ("none", "uninsured", "self-pay")
    tier, cc_msg = match_charity_care(row, pct, household, insured, lang=lang)
    pol = row["policy"]
    name = row["hospital"].title()
    phone = (pol.get("contact") or {}).get("phone")

    apply_steps = [t(lang, "step1_apply_phone", phone=phone) if phone else t(lang, "step1_apply_nophone"),
                   t(lang, "step1_retroactive")]
    if (pol.get("payment_plans") or {}).get("interest_free"):
        apply_steps.append(t(lang, "step1_interest_free"))

    benefits = screen_benefits(pct, intake.get("insurance"), income=intake.get("annual_income"),
                               household=household, lang=lang)
    debt = debt_defense(intake.get("in_collections"), lang=lang)
    ceiling = dollars_for(row, "free_care_income_ceiling_by_household", household) if tier == "free" else None
    closing_n = 2 + (1 if benefits else 0) + (1 if debt else 0)   # charity=1; +coverage/+debt if present

    return {
        "fpl_pct": pct,
        "tier": tier,
        "headline": t(lang, "result_" + tier),
        "hospital": {"name": name, "phone": phone,
                     "effective_date": row.get("charity_effective_date"),
                     "needs_review": bool(row.get("needs_review"))},
        "patient": {"first": intake["first_name"], "household": household,
                    "income": intake["annual_income"]},
        "charity": {"message": cc_msg, "income_ceiling": ceiling, "apply": apply_steps},
        "benefits": benefits,
        "debt": debt,
        "closing": t(lang, "step4", n=closing_n),
        "lang_note": t(lang, "letter_note_english") if lang != "en" else None,
    }


def generate_letter(intake, row, pct, tier):
    ask = "free (fully charity) care" if tier == "free" else "a charity-care discount"
    return f"""{intake['first_name']} {intake['last_name']}
{intake.get('address', '[Your address]')}
{intake.get('phone', '[Your phone]')}

Date: [today's date]

{row['hospital'].title()}
Attn: Financial Assistance / Business Office
Re: Request for Financial Assistance (Charity Care)
Account/Bill #: {intake.get('account', '[account number if known]')}
Date(s) of service: {intake.get('service_date', '[date of service]')}

To whom it may concern:

I am writing to request financial assistance under your hospital's Financial
Assistance Policy, as required by California's Hospital Fair Pricing Act and
IRS Section 501(r). I am requesting {ask}.

My household size is {intake['household_size']} and my annual household income is
approximately ${intake['annual_income']:,}, which is about {pct:.0f}% of the Federal
Poverty Level. Based on your published policy, this appears to make me eligible.

Please send me your Financial Assistance application and a list of the documents
you require. I am also requesting that any collection activity on the above
account be paused while my application is reviewed, and that this request be
applied retroactively to the balance already billed.

Please also provide an itemized copy of my bill so I can review it for accuracy.

Thank you for your time. You can reach me at the phone number above.

Sincerely,
{intake['first_name']} {intake['last_name']}
"""


DEMO_SCENARIOS = [
    {"first_name": "Maria", "last_name": "Lopez", "address": "123 Main St, CA",
     "phone": "(510) 555-0100", "household_size": 4, "annual_income": 38000,
     "insurance": "uninsured", "service_date": "05/2026", "in_collections": True},
    {"first_name": "James", "last_name": "Nguyen", "address": "45 Oak Ave, CA",
     "phone": "(510) 555-0177", "household_size": 2, "annual_income": 55000,
     "insurance": "uninsured", "in_collections": False},
]


def run(intake, row, lang="en"):
    pct, tier, plan = build_plan(intake, row, lang=lang)
    print("=" * 78)
    print(f"{intake['first_name']} {intake['last_name']} — {row['hospital'].title()}")
    print("=" * 78)
    print(plan)
    print("\n--- GENERATED FINANCIAL ASSISTANCE REQUEST LETTER ---\n")
    letter = generate_letter(intake, row, pct, tier)
    print(textwrap.indent(letter, "  "))
    outdir = os.path.join(HERE, "output")
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"letter_{intake['last_name'].lower()}.txt")
    open(out, "w").write(letter)
    print(f"[saved letter -> {os.path.basename(out)}]\n")


def main():
    ap = argparse.ArgumentParser(description="Cobijo charity-care navigator")
    ap.add_argument("--hospital", help="hospital name (substring ok)")
    ap.add_argument("--income", type=int)
    ap.add_argument("--household", type=int, default=1)
    ap.add_argument("--insurance", default="uninsured")
    ap.add_argument("--in-collections", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="skip PolicyEngine, use the FPL heuristic (no network)")
    ap.add_argument("--lang", choices=("en", "es"), default="en",
                    help="patient-facing language for the plan (the hospital letter stays English)")
    args = ap.parse_args()

    global USE_POLICYENGINE
    if args.offline:
        USE_POLICYENGINE = False

    ds, src = load_dataset()
    print(f"[dataset: {src}, {len(ds['rows'])} usable hospitals]\n")

    if args.hospital and args.income:
        row = find_hospital(ds, name=args.hospital)
        if not row:
            raise SystemExit(f"'{args.hospital}' not found in {src}.")
        run({"first_name": "Patient", "last_name": "Example", "household_size": args.household,
             "annual_income": args.income, "insurance": args.insurance,
             "in_collections": args.in_collections}, row, lang=args.lang)
        return

    # Demo: bind each scenario to a real hospital from whatever dataset is loaded.
    default = ds["rows"][0]
    for intake in DEMO_SCENARIOS:
        run(intake, default, lang=args.lang)


if __name__ == "__main__":
    main()
