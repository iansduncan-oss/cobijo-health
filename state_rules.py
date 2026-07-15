"""Per-state charity-care rules — the ONE place a state's statutory thresholds, plausibility bounds,
and prompt/display strings live.

CA is the only populated state today. This module exists so the extractor, validator, QA harness, and
page renderer stop hardcoding "California" / 400% and can be pointed at a new state by adding a row to
STATES (the T4.1 "thin national" refactor). `rules_for()` defaults to CA and every existing CA code
path resolves to the exact same numbers/strings it used before — so introducing this module is a pure
refactor with zero behavior change for California.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StateRules:
    code: str                       # USPS state code, e.g. "CA"
    name: str                       # display name used in the extraction prompt ("" for the generic default)
    fpl_floor_pct: int              # statutory FLOOR hospitals must cover at/below (0 = no numeric floor)
    discount_implausible_pct: int   # discount ceiling above this ~= a units/decimal misread
    free_care_unusual_pct: int      # free-care ceiling above this -> verify (MEDIUM in qa)
    free_care_implausible_pct: int  # free-care ceiling above this ~= a misread (HIGH in qa)
    fap_law: str                    # the statute a patient-facing plan can cite
    # --- STATUTE-DRIVEN eligibility (T4.1 Phase 2 "thin national") ---------------------------------
    # For states whose LAW sets the eligibility thresholds directly (so a correct patient answer needs
    # no per-hospital FAP extraction — the CA model needs the PDF, these don't). All default None so CA
    # and the generic §501(r) default are byte-identical: they keep their per-hospital extracted model.
    statutory_free_pct: Optional[int] = None      # FPL% at/below which state law guarantees 100% free care
    statutory_discount_pct: Optional[int] = None  # FPL% at/below which state law mandates a discount
    income_cap_pct: Optional[int] = None          # max % of annual family income the hospital may collect
    # Rural / Critical-Access-Hospital tier (lower FPL bands than metro). None -> use the metro bands.
    statutory_free_rural_pct: Optional[int] = None
    statutory_discount_rural_pct: Optional[int] = None

    @property
    def is_statutory(self):
        """True when this state's own law sets the eligibility thresholds (statute-driven / no extraction)."""
        return self.statutory_discount_pct is not None

    def free_pct_for(self, rural=False):
        if rural and self.statutory_free_rural_pct is not None:
            return self.statutory_free_rural_pct
        return self.statutory_free_pct

    def discount_pct_for(self, rural=False):
        if rural and self.statutory_discount_rural_pct is not None:
            return self.statutory_discount_rural_pct
        return self.statutory_discount_pct


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

# Illinois — Hospital Uninsured Patient Discount Act (210 ILCS 89). STATUTE-DRIVEN: the law itself sets
# the eligibility thresholds, so an IL patient gets a correct answer from these numbers alone — no
# per-hospital FAP extraction needed (unlike CA). Values PINNED from 210 ILCS 89 §5/§10 (metropolitan
# tier; rural/Critical-Access hospitals use lower FPL bands — 125% free / 300% discount — a Phase-2.x
# refinement, modeled here at the metro tier that covers the Chicago-dominated majority of IL hospitals):
#   • 100% free care at/below 200% FPL (§10(a));  • mandated uninsured discount at/below 600% FPL (§10(a));
#   • the hospital may collect at most 20% of annual family income over 12 months (§10(c)(1)).
# The extraction-plausibility fields are unused for a statute-driven state (nothing is extracted) but set
# to sane values for completeness. fpl_floor_pct mirrors the discount ceiling the law guarantees.
IL = StateRules(
    code="IL", name="Illinois",
    fpl_floor_pct=600, discount_implausible_pct=900,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="Illinois Hospital Uninsured Patient Discount Act (210 ILCS 89)",
    statutory_free_pct=200, statutory_discount_pct=600, income_cap_pct=20,
    statutory_free_rural_pct=125, statutory_discount_rural_pct=300,   # rural/Critical-Access tier (§10(a))
)

STATES = {"CA": CA, "IL": IL}


def rules_for(state="CA"):
    """The rules for a USPS state code. Defaults to CA (the only populated state). An unknown or missing
    state falls back to the §501(r) generic defaults — wide bounds, no statutory floor — so a valid
    national FAP isn't mistaken for a misread."""
    return STATES.get((state or "CA").upper(), _DEFAULT)
