#!/usr/bin/env python3
"""Curated 'where to get help' routing — the layer that turns advice into a real door.

Every recommendation the navigator makes (apply for Medi-Cal, get enrollment help, dispute a bill,
find a free clinic) should end at a concrete, free, statewide destination, not a dead end. Each entry
here is authoritative (state agency, federal locator, or an established CA legal-aid network), free,
multilingual, and either zip/county-searchable or reachable on one intake line — so we route patients
without maintaining 58 county mappings.

Labels are i18n keys resolved at render time via messages.t(), so the same list feeds the web plan
(build_plan_struct) and the CLI/text plan (build_plan) and can never drift. Which entries show is a
pure function of the patient's own signals (income %FPL, insured?, in collections?) — see
`help_resources` — so a patient sees only the doors that actually apply to them.

Verified 2026-07-13. URLs/phones are covered by tests/test_cobijo.py::TestHelpResources; re-verify the
live links periodically (state portals do move).
"""

# Each resource: a stable id, the destination url, an optional staffed help-line phone, the i18n label
# key (resolved by the caller), and `when` — a predicate over the patient context that decides whether
# this door is relevant. Keeping `when` here (not in navigator) keeps all routing rules in one file.
RESOURCES = [
    # Apply for Medi-Cal — free coverage that can even reach back to recent bills. Shown to uninsured
    # patients at or near the Medi-Cal income range (a buffer above 138% FPL catches other pathways —
    # kids, pregnancy, disability — without over-claiming; the label says "if you may qualify").
    {"id": "medical", "url": "https://benefitscal.com/", "phone": None,
     "label": "res_medical", "when": lambda c: c["uninsured"] and c["pct"] <= 200},
    # Free, in-person help enrolling in a subsidized Covered California plan (certified counselors).
    {"id": "coveredca",
     "url": "https://apply.coveredca.com/static/lw-enrollment/anon/locateAssistance/locateAssistanceType",
     "phone": "(800) 300-1506", "label": "res_coveredca",
     "when": lambda c: c["uninsured"] and c["pct"] > 138},
    # A federally funded health center: primary/dental/mental care on a sliding scale, insured or not.
    {"id": "clinic", "url": "https://findahealthcenter.hrsa.gov/", "phone": None,
     "label": "res_clinic", "when": lambda c: c["uninsured"]},
    # Health Consumer Alliance — free legal help with medical bills in every language, any income; the
    # intake line routes by zip to a local CA legal-aid program. Shown when a bill is in collections.
    {"id": "legalaid", "url": "https://healthconsumer.org/", "phone": "(888) 804-3536",
     "label": "res_legalaid", "when": lambda c: c["in_collections"]},
]

_UNINSURED = ("", "none", "uninsured", "self-pay")

# Statute-driven states (IL/NY/MD/WA…) route to their OWN coverage portals + legal-aid networks — CA's
# BenefitsCal / Covered California / Health Consumer Alliance don't serve them. Per state: the official
# coverage-application portal (Medicaid + marketplace in one door), plus a statewide legal-aid network for
# medical-debt / collections help. The national HRSA clinic locator is shared with CA. Labels reuse the
# generic res_clinic / res_legalaid keys (already state-neutral in all 10 langs); the coverage door uses
# the generic res_coverage (NOT CA's "Medi-Cal" res_medical). URLs verified live 2026-07-17; re-verify
# periodically (state portals move) — same discipline as RESOURCES above.
_STATE_RESOURCES = {
    "IL": {"coverage": "https://abe.illinois.gov/",                 "legalaid": "https://www.illinoislegalaid.org/"},
    "NY": {"coverage": "https://nystateofhealth.ny.gov/",           "legalaid": "https://www.lawhelpny.org/"},
    "MD": {"coverage": "https://www.marylandhealthconnection.gov/", "legalaid": "https://www.mdlab.org/"},
    "WA": {"coverage": "https://www.wahealthplanfinder.org/",       "legalaid": "https://nwjustice.org/get-legal-help"},
    "NJ": {"coverage": "https://www.getcovered.nj.gov/",            "legalaid": "https://www.lsnjlaw.org/"},
    "CO": {"coverage": "https://connectforhealthco.com/",           "legalaid": "https://coloradolegalservices.org/"},
    "OR": {"coverage": "https://one.oregon.gov/",                   "legalaid": "https://oregonlawhelp.org/"},
    "RI": {"coverage": "https://healthsourceri.com/",               "legalaid": "https://helprilaw.org/"},
    "ME": {"coverage": "https://www.coverme.gov/",                  "legalaid": "https://www.ptla.org/"},
    "MA": {"coverage": "https://www.mahealthconnector.org/",        "legalaid": "https://healthlawadvocates.org/"},
    "OH": {"coverage": "https://www.healthcare.gov/",               "legalaid": "https://www.ohiolegalhelp.org/"},
    "VT": {"coverage": "https://portal.healthconnect.vermont.gov/", "legalaid": "https://vtlawhelp.org/"},
}


def statutory_resources(state, insurance, in_collections):
    """Resource doors for a statute-driven state — same shape/contract as help_resources (label stays an
    UNRESOLVED i18n key). Uninsured -> the state coverage portal + the national clinic locator; a bill in
    collections -> the state legal-aid network. An un-modeled state returns [] (the plan just omits the
    resource block). Kept parallel to help_resources so both plans route through one localizer."""
    st = _STATE_RESOURCES.get((state or "").upper())
    if not st:
        return []
    out = []
    if (insurance or "").strip().lower() in _UNINSURED:
        out.append({"id": "coverage", "url": st["coverage"], "phone": None, "label": "res_coverage"})
        out.append({"id": "clinic", "url": "https://findahealthcenter.hrsa.gov/", "phone": None, "label": "res_clinic"})
    if in_collections:
        out.append({"id": "legalaid", "url": st["legalaid"], "phone": None, "label": "res_legalaid"})
    return out


def help_resources(pct, insurance, in_collections):
    """The resource entries that apply to this patient, in priority order.

    Returns a list of {id, url, phone, label} dicts with the label as an *unresolved* i18n key — the
    caller resolves it with messages.t(lang, label) so this module stays free of any language logic.
    """
    ctx = {
        "pct": pct,
        "uninsured": (insurance or "").strip().lower() in _UNINSURED,
        "in_collections": bool(in_collections),
    }
    return [{"id": r["id"], "url": r["url"], "phone": r["phone"], "label": r["label"]}
            for r in RESOURCES if r["when"](ctx)]
