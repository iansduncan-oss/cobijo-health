"""Per-state charity-care rules — the ONE place a state's statutory thresholds, plausibility bounds,
and prompt/display strings live.

CA is the only populated state today. This module exists so the extractor, validator, QA harness, and
page renderer stop hardcoding "California" / 400% and can be pointed at a new state by adding a row to
STATES (the T4.1 "thin national" refactor). `rules_for()` defaults to CA and every existing CA code
path resolves to the exact same numbers/strings it used before — so introducing this module is a pure
refactor with zero behavior change for California.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class StateRules:
    code: str                       # USPS state code, e.g. "CA"
    name: str                       # display name used in the extraction prompt ("" for the generic default)
    fpl_floor_pct: int              # statutory FLOOR hospitals must cover at/below (0 = no numeric floor)
    discount_implausible_pct: int   # discount ceiling above this ~= a units/decimal misread
    free_care_unusual_pct: int      # free-care ceiling above this -> verify (MEDIUM in qa)
    free_care_implausible_pct: int  # free-care ceiling above this ~= a misread (HIGH in qa)
    fap_law: str                    # the statute a patient-facing plan can cite


# HSC §127405 (Hospital Fair Pricing Act): 400% FPL is a FLOOR, not a cap — hospitals MUST offer charity
# care at/below it and may (as ~114 CA hospitals do) extend eligibility to 450-700% FPL. So a >400%
# ceiling is legal, not a misread; free (100% write-off) care gets a tighter plausibility bound than
# partial discounts, since free care rarely exceeds the floor. These are exactly the values that were
# the module-level constants in extract_llm.py — moving them here changes nothing for California.
CA = StateRules(
    code="CA", name="California",
    fpl_floor_pct=400, discount_implausible_pct=800,
    free_care_unusual_pct=400, free_care_implausible_pct=600,
    fap_law="California's Hospital Fair Pricing Act (Health & Safety Code §127405)",
)

# Generic §501(r) default for any not-yet-modeled state. §501(r) imposes NO numeric FPL floor (unlike
# CA's 400%): a nonprofit hospital may set any threshold — or none — in its Financial Assistance Policy.
# A no-threshold FAP is therefore VALID nationally (the navigator already degrades to "call them to
# apply" when thresholds are absent), so the default uses wide plausibility bounds and no statutory floor
# to avoid false-flagging a legitimate national FAP as an extraction error.
_DEFAULT = StateRules(
    code="US", name="",
    fpl_floor_pct=0, discount_implausible_pct=800,
    free_care_unusual_pct=800, free_care_implausible_pct=800,
    fap_law="Section 501(r) of the Internal Revenue Code",
)

STATES = {"CA": CA}


def rules_for(state="CA"):
    """The rules for a USPS state code. Defaults to CA (the only populated state). An unknown or missing
    state falls back to the §501(r) generic defaults — wide bounds, no statutory floor — so a valid
    national FAP isn't mistaken for a misread."""
    return STATES.get((state or "CA").upper(), _DEFAULT)
