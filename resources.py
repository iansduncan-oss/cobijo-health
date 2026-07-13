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
