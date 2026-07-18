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
    # True when the state's statute EXPLICITLY bars using immigration status as an eligibility criterion
    # (NY §2807-k(9-a)). Surfaced on the page as a reassurance for immigrant patients — set only where the
    # law says so in terms, not merely where §501(r) is silent on citizenship.
    immigration_excluded: bool = False
    # True when the statute bars suing the patient / forcing the sale or foreclosure of a primary home to
    # collect the bill (NY §2807-k, patients ≤400% FPL). Surfaced as a collection-protection reassurance.
    bars_debt_lawsuits: bool = False
    # True when the statute caps the discounted bill at a NAMED share of the Medicaid rate the patient can
    # cite to challenge an inflated bill (NY: 10% of Medicaid ≤300% FPL, 20% ≤400%). Gates the cap note.
    names_medicaid_cap: bool = False
    # Days after the bill within which the patient must APPLY to claim the discount (IL §10: 60). None =
    # no statutory deadline modeled. Gates the "apply by" actionable note.
    apply_deadline_days: Optional[int] = None

    @property
    def is_statutory(self):
        """True when this state's own law sets the eligibility thresholds (statute-driven / no extraction)."""
        return self.statutory_discount_pct is not None

    @property
    def has_rural_bands(self):
        """True when the state's law sets LOWER FPL bands for rural/Critical-Access hospitals (IL does;
        NY doesn't — its 200/400 bands are statewide). Gates the 'rural hospital → lower limits' note."""
        return self.statutory_free_rural_pct is not None or self.statutory_discount_rural_pct is not None

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
    apply_deadline_days=60,          # §10: patient must apply within 60 days of the bill (covers bills > $300)
)

# New York — Hospital Financial Assistance Law (Public Health Law §2807-k), as amended (eff. 2024–25).
# STATUTE-DRIVEN and STATEWIDE (no rural/Critical-Access distinction — the bands apply to every DOH-licensed
# hospital). Source-verified vs NY DOH implementation guidance + HCFANY + NYC Bar (2026-07):
#   • 100% free care (all charges waived) at/below 200% FPL;
#   • sliding-scale discount 200%–400% FPL, charges capped at % of the MEDICAID rate — 10% for 200–300% FPL,
#     20% for 300–400% FPL (a charge cap, NOT a % of the patient's income — so income_cap_pct stays None; the
#     plan/page omit the income-cap clause);
#   • immigration status may NOT be used as an eligibility criterion (§2807-k) — surfaced to Cobijo's audience.
# No collection lawsuits against patients ≤400% FPL. fpl_floor_pct mirrors the 400% discount ceiling.
NY = StateRules(
    code="NY", name="New York",
    fpl_floor_pct=400, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="New York Hospital Financial Assistance Law (Public Health Law §2807-k)",
    statutory_free_pct=200, statutory_discount_pct=400, income_cap_pct=None,   # charge cap is % of Medicaid rate, not income
    immigration_excluded=True,   # §2807-k(9-a): immigration status may NOT be an eligibility criterion (eff. 2024-10-20)
    bars_debt_lawsuits=True,      # §2807-k: no lawsuits + no forced sale/foreclosure of a home, patients ≤400% FPL
    names_medicaid_cap=True,      # §2807-k: bill capped at 10% of Medicaid rate ≤300% FPL, 20% ≤400% (citable by patient)
)

# Maryland — Hospital Financial Assistance Law (Health-General §19-214.1, + COMAR 10.37.13.06, HSCRC).
# STATUTE-DRIVEN and STATEWIDE (uniform statewide policy since 2020-10-01). Source-verified vs the statute
# text (Justia/FindLaw) + HSCRC (2026-07):
#   • 100% free medically necessary care at/below 200% FPL;
#   • reduced-cost care above 200% FPL (sliding scale) — the statute extends reduced-cost care to 500% FPL for
#     patients with documented FINANCIAL HARDSHIP (medical debt > 25% of income). We model the clean 300%
#     non-hardship guarantee here; the 300–500% hardship tier needs a hardship concept the model lacks (a
#     follow-on, like WA's large-system tier);
#   • §19-214.1 EXPLICITLY bars using citizenship/immigration status as an eligibility criterion.
# MD's collection cap is a MONTHLY-PAYMENT cap (5% of monthly income, §19-214.2), semantically different from
# IL's annual-income cap -> income_cap_pct stays None (surface separately later). No rural/CAH distinction.
MD = StateRules(
    code="MD", name="Maryland",
    fpl_floor_pct=300, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="Maryland Hospital Financial Assistance Law (Health-General §19-214.1)",
    statutory_free_pct=200, statutory_discount_pct=300, income_cap_pct=None,
    immigration_excluded=True,   # §19-214.1 bars citizenship/immigration status as an eligibility criterion
)

# Washington — Charity Care Law (RCW 70.170.060, as amended by SHB 1616, eff. 2022-07-01). STATUTE-DRIVEN.
# The law sets TWO tiers by hospital SIZE/SYSTEM (not rural): large systems (owned by a system with 3+ acute
# hospitals, or big-bed hospitals in the most populous / a southern-border county) free ≤300%/discount→400%;
# ALL OTHER hospitals free ≤200%/discount→300%. Source-verified vs app.leg.wa.gov RCW 70.170.060 + WA AG
# guidance (2026-07). We pin the "other-hospital" tier as the UNIVERSAL FLOOR every WA hospital guarantees —
# fits Cobijo's honest "legal minimum, the hospital may offer more, apply to find out" framing (large systems
# exceed it). The large-system 300/400 tier needs a system-size classifier the CMS roster lacks (a follow-on).
# income_cap_pct None (mechanism is a 75%/50% discount schedule, no income/Medicaid cap). immigration_excluded
# stays False: the immigration bar is AG+DOH GUIDANCE, not statute text (set the flag only where the law says
# so in terms — as NY's §2807-k(9-a) does). No rural/CAH bands.
WA = StateRules(
    code="WA", name="Washington",
    fpl_floor_pct=300, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="Washington Charity Care Law (RCW 70.170.060)",
    statutory_free_pct=200, statutory_discount_pct=300, income_cap_pct=None,
)

# New Jersey — Hospital Care Payment Assistance Program / "Charity Care" (N.J.S.A. 26:2H-18.60; N.J.A.C.
# 10:52-11). STATUTE-DRIVEN and STATEWIDE (uniform statewide program, no rural/CAH distinction). The state
# sets the eligibility thresholds, so an NJ patient gets a correct answer from these numbers alone — no
# per-hospital FAP extraction. Source-verified vs Legal Services of NJ + NJ DOH (nj.gov/health/hcf) +
# DollarFor (2026-07):
#   • 100% free care at/below 200% FPL;
#   • sliding-scale reduced-charge 200%–300% FPL (patient pays 20/40/60/80% across 25-pt bands) — we model
#     the clean 300% envelope as the discount ceiling (the sub-bands are like NY's Medicaid-cap: a note-level
#     refinement, a follow-on). A hardship path can extend past 300% when out-of-pocket medical > 30% of
#     income — needs the same hardship concept MD's 300–500% tier lacks, so not modeled here.
# NJ's collection limit is an ASSET test ($7,500 individual / $15,000 family, spend-down allowed) + a NJ
# RESIDENCY requirement, NOT a % -of-income cap -> income_cap_pct stays None (the model has no asset/residency
# concept; Cobijo's honest "apply to confirm" framing covers it). immigration_excluded stays False: NJ Charity
# Care is residency+income+asset-based and does not require immigration status, but that's program practice /
# secondary-source characterization, not statute text barring it "in terms" as NY's §2807-k(9-a) does — so we
# do NOT surface the affirmative reassurance (a native/legal-review upgrade candidate). Mirrors WA's shape.
NJ = StateRules(
    code="NJ", name="New Jersey",
    fpl_floor_pct=300, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="New Jersey Hospital Care Payment Assistance Program (N.J.S.A. 26:2H-18.60)",
    statutory_free_pct=200, statutory_discount_pct=300, income_cap_pct=None,
)

STATES = {"CA": CA, "IL": IL, "NY": NY, "MD": MD, "WA": WA, "NJ": NJ}


def rules_for(state="CA"):
    """The rules for a USPS state code. Defaults to CA (the only populated state). An unknown or missing
    state falls back to the §501(r) generic defaults — wide bounds, no statutory floor — so a valid
    national FAP isn't mistaken for a misread."""
    return STATES.get((state or "CA").upper(), _DEFAULT)
