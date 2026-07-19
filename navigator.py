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
import datetime
import json
import os
import textwrap
import time

import policyengine
import resources
import state_rules
from constants import FPL, FPL_EACH_ADDITIONAL
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

# FPL table + FPL_EACH_ADDITIONAL now live in constants.py (imported above) — single source of
# truth shared with extract_llm.py so a January refresh can't update one file and miss the other.

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


def effective_date_display(row):
    """The policy effective date to show a patient — or None if missing or in the FUTURE.
    A future 'effective' date is an extraction artifact (already flagged needs_review); presenting
    it as the current policy date would mislead. Unparseable formats are shown as-is (best effort)."""
    d = (row.get("charity_effective_date") or "").strip()
    if not d:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y", "%m/%Y", "%B %d, %Y", "%b %d, %Y", "%B %Y", "%Y"):
        try:
            parsed = datetime.datetime.strptime(d, fmt).date()
            return None if parsed > datetime.date.today() else d
        except ValueError:
            continue
    return d


def _build_name_index(rows):
    """UPPER(name) -> row, but disambiguated so a duplicated hospital name can't silently overwrite a
    sibling campus. 4 CA hospitals share a name (Stanford Health Care ×2, UCI-Fountain Valley ×2 —
    same city, one hospital double-listed under two OSHPD IDs; Providence St. Joseph and Stanford
    Tri-Valley — genuinely different cities). For a colliding name we also register a "NAME (CITY)"
    key so each is addressable, and stamp row["_display"] for the autocomplete. The bare UPPER(name)
    still maps to the FIRST occurrence (deterministic) so a plain CLI / ?hospital= lookup never breaks."""
    from collections import Counter
    counts = Counter(r["hospital"].upper() for r in rows if r.get("hospital"))
    by_name = {}
    for r in rows:
        nm = r.get("hospital")
        if not nm:
            continue
        up = nm.upper()
        by_name.setdefault(up, r)                     # bare name -> first occurrence (back-compat)
        if counts[up] > 1 and r.get("city"):
            disp = f"{nm.title()} ({r['city'].title()})"    # same-city dups collapse to one label
            r["_display"] = disp
            by_name[disp.upper()] = r
        else:
            r["_display"] = nm.title()
    return by_name


def load_dataset():
    """Load the extracted dataset; index usable rows by oshpdid AND UPPER(hospital name)."""
    for name in DATASETS:
        path = os.path.join(HERE, "data", name)
        if not os.path.exists(path):
            continue
        rows = [r for r in json.load(open(path)) if r.get("status") == "extracted" and r.get("policy")]
        by_id = {r["oshpdid"]: r for r in rows if r.get("oshpdid")}
        by_name = _build_name_index(rows)
        return {"by_id": by_id, "by_name": by_name, "rows": rows}, name
    raise SystemExit("No extracted dataset found — run extract_llm.py then build_dataset.py.")


def load_statutory_dataset(filename):
    """Load a statute-driven state roster (T4.1 Phase 2): a plain list of CMS-derived rows with
    policy=None, where eligibility comes from the state's LAW (state_rules) not a per-hospital FAP.
    Returns [] when the file is absent so the app still boots CA-only (IL is additive, never required)."""
    path = os.path.join(HERE, "data", filename)
    if not os.path.exists(path):
        return []
    return [r for r in json.load(open(path)) if r.get("status") == "statutory"]


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

    # No readable income thresholds at all (free + discount ceiling + tiers all absent) — this is an
    # extraction gap, NOT a genuine "over the ceiling". Computing max([]) -> 0 would tell an eligible
    # low-income patient they're above a 0% FPL ceiling (the opposite of the truth). CA law still
    # requires charity care, so route them to apply rather than present a false rejection.
    if free is None and disc_ceiling is None and not tiers:
        return "unknown", t(lang, "cc_unknown", name=name, pct=pct)

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


def help_leads(pct, insurance, in_collections, lang="en"):
    """The 'where to get help' routes for this patient, labels localized. Turns the plan's advice
    (apply for Medi-Cal, enroll, dispute) into concrete free destinations. See resources.py for which
    door shows when. Returns [] when nothing applies (e.g. an insured patient not in collections)."""
    return [{"url": r["url"], "phone": r["phone"], "label": t(lang, r["label"])}
            for r in resources.help_resources(pct, insurance, in_collections)]


def statutory_help_leads(state, insurance, in_collections, lang="en"):
    """help_leads for a statute-driven state: the state's own coverage portal + national clinic locator +
    state legal-aid, labels localized. See resources.statutory_resources for which door shows when."""
    return [{"url": r["url"], "phone": r["phone"], "label": t(lang, r["label"])}
            for r in resources.statutory_resources(state, insurance, in_collections)]


def build_plan(intake, row, lang="en"):
    pct = fpl_percent(intake["annual_income"], intake["household_size"])
    insured = bool(intake.get("insurance")) and intake["insurance"].lower() not in ("none", "uninsured", "self-pay")
    tier, cc_msg = match_charity_care(row, pct, intake["household_size"], insured, lang=lang)
    pol = row["policy"]
    name = row["hospital"].title()

    first = (intake.get("first_name") or "").strip()
    greeting = (t(lang, "plan_greeting", first=first, name=name) if first and first != "there"
                else t(lang, "plan_greeting_anon", name=name))   # no name → localized greeting, not the English "there"
    L = [greeting,
         t(lang, "plan_household", household=intake["household_size"],
           income=intake["annual_income"], pct=pct), ""]
    eff = effective_date_display(row)
    if eff:
        L.append(t(lang, "plan_effective", name=name, date=eff))
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
    leads = help_leads(pct, intake.get("insurance"), intake.get("in_collections"), lang=lang)
    if leads:
        L.append(t(lang, "res_heading"))
        L += ["  • " + r["label"] + " — " + r["url"] + (f" ({r['phone']})" if r["phone"] else "")
              for r in leads] + [""]
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
                     "effective_date": effective_date_display(row),
                     "needs_review": bool(row.get("needs_review")),
                     "policy_url": row.get("charity_policy_url"),
                     "application_url": row.get("application_url"),
                     "discount_policy_url": row.get("discount_policy_url")},
        "patient": {"first": intake["first_name"], "household": household,
                    "income": intake["annual_income"]},
        "charity": {"message": cc_msg, "income_ceiling": ceiling, "apply": apply_steps},
        "benefits": benefits,
        "debt": debt,
        "resources": help_leads(pct, intake.get("insurance"), intake.get("in_collections"), lang=lang),
        "res_heading": t(lang, "res_heading"),
        "closing": t(lang, "step4", n=closing_n),
        "lang_note": t(lang, "letter_note_english") if lang != "en" else None,
    }


def build_generic_plan_struct(intake, lang="en"):
    """A hospital-independent plan for when the typed hospital isn't in our dataset — so a not-found
    lookup helps instead of dead-ending. Every California hospital must offer charity care by law, and
    the benefit-screen + debt-defense guidance is hospital-agnostic. No hospital-specific numbers are
    invented: the charity tier is `unknown` ("apply anyway"). Same shape as build_plan_struct."""
    pct = fpl_percent(intake["annual_income"], intake["household_size"])
    name = (intake.get("hospital_name") or "").strip()
    benefits = screen_benefits(pct, intake.get("insurance"), income=intake.get("annual_income"),
                               household=intake.get("household_size"), lang=lang)
    debt = debt_defense(intake.get("in_collections"), lang=lang)
    closing_n = 2 + (1 if benefits else 0) + (1 if debt else 0)
    return {
        "fpl_pct": pct,
        "tier": "unknown",
        "not_in_directory": True,
        "headline": t(lang, "result_unknown"),
        "hospital": {"name": name.title() if name else None, "phone": None,
                     "effective_date": None, "needs_review": False,
                     "policy_url": None, "application_url": None, "discount_policy_url": None},
        "patient": {"first": intake.get("first_name", "there"), "household": intake["household_size"],
                    "income": intake["annual_income"]},
        "charity": {"message": t(lang, "cc_no_hospital", pct=pct), "income_ceiling": None,
                    "apply": [t(lang, "step1_apply_nophone"), t(lang, "step1_retroactive")]},
        "benefits": benefits,
        "debt": debt,
        "resources": help_leads(pct, intake.get("insurance"), intake.get("in_collections"), lang=lang),
        "res_heading": t(lang, "res_heading"),
        "closing": t(lang, "step4", n=closing_n),
        "lang_note": t(lang, "letter_note_english") if lang != "en" else None,
    }


def statutory_tier(pct, rules, rural=False):
    """The tier a STATUTE-DRIVEN state's law assigns a patient at `pct`% FPL — no per-hospital FAP needed
    (the law sets the thresholds). Rural/Critical-Access hospitals use the lower bands. free -> discount -> over."""
    free_pct = rules.free_pct_for(rural)
    disc_pct = rules.discount_pct_for(rural)
    if free_pct is not None and pct <= free_pct:
        return "free"
    if disc_pct is not None and pct <= disc_pct:
        return "discount"
    return "over"


def statutory_facts(intake, row):
    """Language-neutral eligibility FACTS for a statute-driven hospital (T4.1 Phase 2). Derived from
    state_rules — the law itself — not an extracted policy, so it works for ANY hospital in a statute-driven
    state (the CMS roster) with zero per-hospital data. A localized renderer turns these facts into
    patient-facing prose (next Phase-2 increment). Returns None for a non-statutory state/row."""
    rules = state_rules.rules_for(row.get("state"))
    if not rules.is_statutory:
        return None
    rural = row.get("hospital_type") == "Critical Access Hospitals"
    pct = fpl_percent(intake["annual_income"], intake["household_size"])
    return {
        "state": rules.code,
        "fap_law": rules.fap_law,
        "fpl_pct": pct,
        "tier": statutory_tier(pct, rules, rural),
        "free_pct": rules.free_pct_for(rural),
        "discount_pct": rules.discount_pct_for(rural),
        "income_cap_pct": rules.income_cap_pct,
        "payment_cap_pct": rules.payment_cap_pct,
        "payment_cap_ceiling_pct": rules.payment_cap_ceiling_pct,
        "payment_cap_pct_professional": rules.payment_cap_pct_professional,
        "payment_cap_pct_comprehensive": rules.payment_cap_pct_comprehensive,
        "payment_cap_payoff_months": rules.payment_cap_payoff_months,
        "hardship_ceiling_pct": rules.hardship_ceiling_pct,
        "hardship_debt_pct": rules.hardship_debt_pct,
        "catastrophic_cap_pct": rules.catastrophic_cap_pct,
        "catastrophic_ceiling_pct": rules.catastrophic_ceiling_pct,
        "medical_hardship_entry_pct": rules.medical_hardship_entry_pct,
        "program_suspended": rules.program_suspended,
        "rural": rural,
        "hospital": row["hospital"].title(),
    }


def build_statutory_plan_struct(intake, row, lang="en"):
    """A statute-driven plan (T4.1 Phase 2) — same struct shape as build_plan_struct so the web/CLI
    renderers need no changes. Eligibility comes from the state's LAW (statutory_facts), not a per-hospital
    FAP, so it works for any hospital in a statute-driven state with zero extracted data. Benefit screening
    is CA-only (PolicyEngine) so it's omitted here; the federal debt-defense guidance + the letter stay."""
    facts = statutory_facts(intake, row)
    tier = facts["tier"]
    msg_key = {"free": "cc_statutory_free", "discount": "cc_statutory_discount",
               "over": "cc_statutory_over"}[tier]
    # A state that caps charges on the Medicaid rate rather than a % of income (NY) has no income-cap
    # clause to cite, so the discount message drops it (income_cap_pct is None).
    # A discount-only statute (CO: no free tier) has no free-floor to name a band against — the patient is
    # simply at/below the discount ceiling — so it uses a band-less variant instead of the free_pct–disc_pct
    # range text (which would render "None%–250%"). Otherwise a Medicaid-rate-cap state (NY) drops the
    # income-cap clause.
    if tier == "discount" and facts["free_pct"] is None:
        msg_key = "cc_statutory_discount_only"
    elif tier == "discount" and facts["income_cap_pct"] is None:
        msg_key = "cc_statutory_discount_nocap"
    # A free-ONLY statute (ME: no % discount tier) has no {discount_pct} to name — the default 'over' message
    # cites "above the {discount_pct}% limit", which would render "None%". Use a free-floor variant instead.
    elif tier == "over" and facts["discount_pct"] is None and facts["free_pct"] is not None:
        msg_key = "cc_statutory_over_free_only"
    message = t(lang, msg_key, name=facts["hospital"], law=facts["fap_law"], pct=facts["fpl_pct"],
                free_pct=facts["free_pct"], discount_pct=facts["discount_pct"], cap=facts["income_cap_pct"])
    # Statutory monthly-payment-cap protections, two shapes:
    #  • CO (payment_cap_payoff_months set): the cap applies INSIDE the discount tier (qualified patients ≤250%
    #    FPL) and is tiered (4%/2%/6%) + extinguishes the balance after 36 payments — append in the 'discount' tier.
    #  • ME (payment_cap_pct only): the cap sits ABOVE the free floor (200–400% FPL) — append in the 'over' tier.
    if (facts["payment_cap_payoff_months"] and tier == "discount"
            and facts["payment_cap_ceiling_pct"] and facts["fpl_pct"] <= facts["payment_cap_ceiling_pct"]):
        message += " " + t(lang, "cc_payment_cap_payoff", law=facts["fap_law"],
                           payment_cap_pct=facts["payment_cap_pct"],
                           payment_cap_pct_professional=facts["payment_cap_pct_professional"],
                           payment_cap_pct_comprehensive=facts["payment_cap_pct_comprehensive"],
                           payment_cap_payoff_months=facts["payment_cap_payoff_months"],
                           payment_cap_ceiling_pct=facts["payment_cap_ceiling_pct"])
    elif (tier == "over" and facts["payment_cap_pct"] and facts["payment_cap_ceiling_pct"]
            and not facts["payment_cap_payoff_months"]
            and facts["fpl_pct"] <= facts["payment_cap_ceiling_pct"]):
        message += " " + t(lang, "cc_payment_cap", law=facts["fap_law"],
                           payment_cap_pct=facts["payment_cap_pct"],
                           payment_cap_ceiling_pct=facts["payment_cap_ceiling_pct"])
    # Above-the-tiers "hardship" help — surfaced in the 'over' tier so a patient the base bands would turn away
    # still hears about the path the law gives them (each state sets only its own field, so these are exclusive).
    if (tier == "over" and facts["hardship_ceiling_pct"] and facts["hardship_debt_pct"]
            and facts["fpl_pct"] <= facts["hardship_ceiling_pct"]):
        message += " " + t(lang, "cc_hardship_extend", law=facts["fap_law"],
                           hardship_ceiling_pct=facts["hardship_ceiling_pct"], hardship_debt_pct=facts["hardship_debt_pct"])
    elif (tier == "over" and facts["catastrophic_cap_pct"] and facts["catastrophic_ceiling_pct"]
            and facts["fpl_pct"] <= facts["catastrophic_ceiling_pct"]):
        message += " " + t(lang, "cc_catastrophic", law=facts["fap_law"],
                           catastrophic_cap_pct=facts["catastrophic_cap_pct"], catastrophic_ceiling_pct=facts["catastrophic_ceiling_pct"])
    elif tier == "over" and facts["medical_hardship_entry_pct"]:
        message += " " + t(lang, "cc_medical_hardship", law=facts["fap_law"],
                           medical_hardship_entry_pct=facts["medical_hardship_entry_pct"])
    # KILL SWITCH: a CONFIRMED-lapsed program (program_suspended) must not assert a guarantee in the tool
    # either — replace the message with the same honest "not currently active; apply anyway, it's free" the
    # pages show. (For NC the cap-append block above is already inert — payment_cap_pct is None.)
    if facts["program_suspended"]:
        message = t(lang, "cc_program_suspended", name=facts["hospital"])
    debt = debt_defense(intake.get("in_collections"), lang=lang)
    return {
        "fpl_pct": facts["fpl_pct"],
        "tier": tier,
        "statutory": True,
        "headline": t(lang, "result_" + tier),
        "hospital": {"name": facts["hospital"], "phone": row.get("phone"),
                     "effective_date": None, "needs_review": False,
                     "policy_url": None, "application_url": None, "discount_policy_url": None},
        "patient": {"first": intake.get("first_name", "there"), "household": intake["household_size"],
                    "income": intake["annual_income"]},
        "charity": {"message": message, "income_ceiling": None,
                    "apply": [t(lang, "step1_apply_nophone"), t(lang, "step1_retroactive")]},
        "benefits": [],                                   # CA-only PolicyEngine/Medi-Cal — omitted off-CA
        "debt": debt,
        "resources": statutory_help_leads((row.get("state") or "").upper(),
                                          intake.get("insurance"), intake.get("in_collections"), lang=lang),
        "res_heading": t(lang, "res_heading"),
        "closing": t(lang, "step4", n=2 + (1 if debt else 0)),
        "lang_note": t(lang, "letter_note_english") if lang != "en" else None,
    }


def generate_letter(intake, row, pct, tier):
    ask = "free (fully charity) care" if tier == "free" else "a charity-care discount"
    # Name: prefer an explicit full_name (the web always sets it, possibly ""), else first+last (CLI);
    # an empty value becomes a bracketed placeholder the sender must fill in — never a stand-in like
    # "Patient" that would be mailed verbatim.
    name = intake.get("full_name")
    if name is None:                       # CLI intake carries first/last, not full_name
        name = f"{intake.get('first_name', '')} {intake.get('last_name', '')}".strip()
    name = (name or "").strip() or "[Your full name]"
    _t = datetime.date.today()                      # auto-fill the date so it's one less [bracket] to complete
    today = f"{_t:%B} {_t.day}, {_t.year}"          # e.g. "July 14, 2026" (English, for the hospital)
    # Cite the governing law: CA stays byte-identical; a statute-driven state cites its own act.
    _state = (row.get("state") or "CA").upper()
    law = "California's Hospital Fair Pricing Act" if _state == "CA" else state_rules.rules_for(_state).fap_law
    return f"""{name}
{intake.get('address', '[Your address]')}
{intake.get('phone', '[Your phone]')}

Date: {today}

{row['hospital'].title()}
Attn: Financial Assistance / Business Office
Re: Request for Financial Assistance (Charity Care)
Account/Bill #: {intake.get('account', '[account number if known]')}
Date(s) of service: {intake.get('service_date', '[date of service]')}

To whom it may concern:

I am writing to request financial assistance under your hospital's Financial
Assistance Policy, as required by {law} and
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
{name}
"""


def letter_reference(intake, row, pct, tier, lang):
    """A translation of the English request letter, shown UNDER it so a non-English patient can read
    exactly what they're sending. Returns {heading, warning, body} in `lang`. Mirrors generate_letter's
    field resolution 1:1 (same brackets when a field is empty) so the reference matches the sent copy.
    Callers skip this for en (the reference would be identical to the letter already shown)."""
    name = intake.get("full_name")
    if name is None:                       # CLI intake carries first/last, not full_name
        name = f"{intake.get('first_name', '')} {intake.get('last_name', '')}".strip()
    name = (name or "").strip() or "[Your full name]"
    _t = datetime.date.today()
    today = f"{_t:%B} {_t.day}, {_t.year}"
    ask_key = "letter_ask_free" if tier == "free" else "letter_ask_discount"
    # Cite the governing law, mirroring generate_letter 1:1: CA stays byte-identical (short name kept in
    # English inside the translation), a statute-driven state cites its own act.
    _state = (row.get("state") or "CA").upper()
    law = "California's Hospital Fair Pricing Act" if _state == "CA" else state_rules.rules_for(_state).fap_law
    body = t(lang, "letter_ref_template",
             name=name,
             law=law,
             address=intake.get("address", "[Your address]"),
             phone=intake.get("phone", "[Your phone]"),
             today=today,
             hospital=row["hospital"].title(),
             account=intake.get("account", "[account number if known]"),
             service_date=intake.get("service_date", "[date of service]"),
             ask=t(lang, ask_key),
             household=intake["household_size"],
             income=intake["annual_income"],
             pct=pct)
    return {"heading": t(lang, "letter_ref_heading"),
            "warning": t(lang, "letter_ref_warning"),
            "body": body}


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
