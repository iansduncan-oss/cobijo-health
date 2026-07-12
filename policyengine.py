#!/usr/bin/env python3
"""
PolicyEngine bridge — real California benefit eligibility, replacing the navigator's coarse
FPL heuristic.

Calls the open-source PolicyEngine US API (https://api.policyengine.org/us/calculate) to
compute, for a household, the two signals the navigator screens for:
  * Medi-Cal (California Medicaid) eligibility + estimated annual value  [per-person var]
  * ACA premium tax credit (aca_ptc)                                     [per-tax_unit var]

Why: FPL cutoffs alone get Medi-Cal/subsidy lines roughly right, but real eligibility depends
on state rules, household composition, and current law (e.g. the post-2025 subsidy changes).
PolicyEngine encodes that; we don't rebuild it. Verified against the live API 2026-07-09:
  size 1 @ $30k CA -> not Medi-Cal eligible, aca_ptc ~$5,895
  size 4 @ $30k CA -> Medi-Cal eligible (~$36,946 total), aca_ptc $0 (Medicaid crowds out PTC)

stdlib only (urllib) so it drops into the navigator with no new dependency. Exceptions
propagate — the navigator catches them and falls back to the heuristic, so a network blip or
API change never breaks a patient's plan.

CLI self-test:  python3 policyengine.py --income 30000 --household 4
"""
import argparse
import json
import urllib.request

from constants import BENEFIT_YEAR

PE_API = "https://api.policyengine.org/us/calculate"
# Cap the MODELED household so a crafted/typo'd size (e.g. 100000) can't build a giant payload
# and tie up a request. Benefit estimates are stable well below this; the exact FPL % is computed
# separately in navigator.poverty_limit and uses the true household, so eligibility stays correct.
PE_MAX_HOUSEHOLD = 15


def policyengine_benefits(annual_income, household_size, state="CA", year=None, timeout=8):
    """Compute Medicaid + ACA PTC for a household via the PolicyEngine US API.

    Member 0 = the adult earner (carries all income). If size >= 2, member 1 is a second
    adult (spouse, age 30); any remaining members are children (age 10). A marital_unit holds
    at most 2 people (PolicyEngine errors otherwise). The modeled size is clamped to
    [1, PE_MAX_HOUSEHOLD]. Returns:
        {"medicaid_eligible": bool, "medicaid_value": float, "aca_ptc": float, "raw": dict}
    Raises urllib/JSON/KeyError on failure — caller decides the fallback.
    """
    if year is None:
        year = BENEFIT_YEAR                 # current eligibility year, not a hardcoded 2026
    y = str(year)
    household_size = max(1, min(int(household_size), PE_MAX_HOUSEHOLD))
    members = [f"person_{i}" for i in range(household_size)]

    people = {}
    for i, name in enumerate(members):
        if i == 0:
            age, income = 30, annual_income
        elif i == 1:
            age, income = 30, 0            # second adult / spouse
        else:
            age, income = 10, 0            # children
        people[name] = {
            "age": {y: age},
            "employment_income": {y: income},
            "medicaid": {y: None},
            "is_medicaid_eligible": {y: None},
            "is_aca_ptc_eligible": {y: None},
        }

    adults = members[:2] if household_size >= 2 else members[:1]
    household = {
        "people": people,
        "tax_units": {"tax_unit": {"members": members, "aca_ptc": {y: None}}},
        "families": {"family": {"members": members}},
        "spm_units": {"spm_unit": {"members": members}},
        "marital_units": {"marital_unit": {"members": adults}},
        "households": {"household": {"members": members, "state_name": {y: state}}},
    }

    payload = json.dumps({"household": household}).encode("utf-8")
    req = urllib.request.Request(
        PE_API, data=payload, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))["result"]

    people_out = result["people"]
    medicaid_value = sum(p["medicaid"][y] for p in people_out.values())
    medicaid_eligible = any(p["is_medicaid_eligible"][y] for p in people_out.values())
    aca_ptc = result["tax_units"]["tax_unit"]["aca_ptc"][y]

    return {
        "medicaid_eligible": medicaid_eligible,
        "medicaid_value": round(medicaid_value),
        "aca_ptc": round(aca_ptc),
        "raw": result,
    }


def benefit_leads(annual_income, household_size, state="CA", year=None, timeout=8, lang="en"):
    """PolicyEngine -> patient-facing lead strings (same shape screen_benefits returns).

    Grounds each lead in the computed dollar figure. Raises on API failure so the navigator
    can fall back to its FPL heuristic.
    """
    from messages import t
    b = policyengine_benefits(annual_income, household_size, state=state, year=year, timeout=timeout)
    leads = []
    if b["medicaid_eligible"]:
        leads.append(t(lang, "pe_medicaid", value=b["medicaid_value"]))
    if b["aca_ptc"] > 0:
        leads.append(t(lang, "pe_aca", ptc=b["aca_ptc"]))
    if not leads:
        leads.append(t(lang, "pe_over"))
    return leads, b


def main():
    ap = argparse.ArgumentParser(description="PolicyEngine CA benefit self-test")
    ap.add_argument("--income", type=int, required=True)
    ap.add_argument("--household", type=int, default=1)
    ap.add_argument("--state", default="CA")
    ap.add_argument("--year", type=int, default=None, help="eligibility year (default: current year)")
    args = ap.parse_args()
    leads, b = benefit_leads(args.income, args.household, state=args.state, year=args.year)
    print(json.dumps({k: v for k, v in b.items() if k != "raw"}, indent=2))
    for lead in leads:
        print("•", lead)


if __name__ == "__main__":
    main()
