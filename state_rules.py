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
    # A MONTHLY-PAYMENT cap the statute imposes above the free tier: the hospital may not require monthly
    # payments above this % of monthly family income for patients up to payment_cap_ceiling_pct% FPL (ME:
    # 4% up to 400% FPL, 22 M.R.S. §1716-A). Distinct from income_cap_pct (an ANNUAL %-of-income collection
    # ceiling inside a discount tier). Both fields set -> a flag-gated payment-cap note is surfaced; the pair
    # models an affordability band a free-ONLY state (no statutory % discount) still guarantees above its
    # free floor. (CO has the same 4%/2% cap deferred — reuse these fields when that note is added.)
    payment_cap_pct: Optional[int] = None
    payment_cap_ceiling_pct: Optional[int] = None
    # True when the eligibility guarantee comes from a binding STATEWIDE PROGRAM (e.g. a Medicaid directed-payment
    # condition every hospital accepted) rather than a statute. Same modelable free/discount bands, but the pages
    # must NOT say "{state} law" — they use "_program" i18n variants ("{state}'s hospital financial assistance
    # program") so the authority is stated honestly. The {law}/fap_law slots carry the program's name.
    authority_is_program: bool = False

    @property
    def is_statutory(self):
        """True when this state's own law sets the eligibility thresholds (statute-driven / no extraction).
        True for a free+discount state (discount_pct set), a discount-ONLY state (CO: free_pct None), AND a
        free-ONLY state (ME: free_pct set, no statutory % discount) — any state whose LAW pins a tier."""
        return self.statutory_discount_pct is not None or self.statutory_free_pct is not None

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

# Colorado — Hospital Discounted Care (HB21-1198, codified C.R.S. §25.5-3-501 et seq.; eff. 2022-09-01).
# STATUTE-DRIVEN and STATEWIDE. DIFFERENT SHAPE from the free≤200/discount≤300 states: CO's law sets NO
# statutory FREE tier — instead every uninsured (or underinsured, on the balance) patient AT OR BELOW 250%
# FPL is guaranteed DISCOUNTED care (charges capped at the greater of the Medicare or Medicaid base rate,
# set annually by HCPF), with monthly payments capped at 4% of monthly household income (2% for a bill
# from a health-care professional) and the balance treated as PAID IN FULL after a cumulative 36 monthly
# payments. Source-verified vs leg.colorado.gov HB21-1198 + CO HCPF Hospital Discounted Care + Justia CRS
# (2026-07). So CO is the first DISCOUNT-ONLY statutory state: statutory_free_pct=None (the engine's tier
# logic returns 'discount' for pct≤250 and never mis-asserts 'free'). income_cap_pct stays None — CO's cap
# is a MONTHLY-PAYMENT cap (4%/2%), semantically unlike IL's annual %-of-income cap, so it's surfaced (if
# at all) as a note, not the income-cap clause. Many CO hospitals ADD free care by their own FAP above this
# floor — the s_minimum_note ("legal minimum; the hospital may offer more — apply") carries that honestly.
# immigration_excluded stays False (HDC conditions on income/residency, not a statutory immigration bar).
CO = StateRules(
    code="CO", name="Colorado",
    fpl_floor_pct=250, discount_implausible_pct=800,
    free_care_unusual_pct=250, free_care_implausible_pct=400,
    fap_law="Colorado Hospital Discounted Care law (HB21-1198, C.R.S. §25.5-3-501 et seq.)",
    statutory_free_pct=None, statutory_discount_pct=250, income_cap_pct=None,
)

# Oregon — Hospital Financial Assistance (HB 3076 (2019), codified ORS 442.614). STATUTE-DRIVEN and
# STATEWIDE (no hospital-type distinction — applies to every nonprofit hospital + its affiliated clinics).
# A free+discount state, same SHAPE as NJ/MD/WA but with a higher discount ceiling: 100% free ≤200% FPL,
# then a sliding-scale minimum reduction — ≥75% (200–300%), ≥50% (300–350%), ≥25% (350–400%). We model the
# clean free≤200 / discount≤400 envelope (the 75/50/25 sub-bands are a note-level refinement like NY's cap).
# Source-verified vs primary ORS 442.614 (oregon.public.law) + OHA + DollarFor (2026-07). VERIFIED CURRENT:
# the 2026 omnibus HB 4040 only raised the presumptive-screening DOLLAR threshold for INSURED patients
# ($500→$1,500/visit) — it did NOT change the FPL eligibility bands (uninsured/OHP patients are still
# screened+discounted before any bill). income_cap_pct None (a %-reduction sliding scale, not an income cap).
# immigration_excluded False (ORS 442.614 conditions on income, not a statutory immigration bar). Because OR
# HAS a free tier (free_pct=200), it reuses the existing free-tier strings on every surface — no new i18n.
OR = StateRules(
    code="OR", name="Oregon",
    fpl_floor_pct=400, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="Oregon's hospital financial assistance law (ORS 442.614, HB 3076)",
    statutory_free_pct=200, statutory_discount_pct=400, income_cap_pct=None,
)

# Rhode Island — hospital charity care (216-RICR-40-10-23 §23.14.1, "Provision of Charity Care,
# Uncompensated Care, and Community Benefits"; effective 2022-01-04). STATUTE/REGULATION-DRIVEN and
# STATEWIDE. Free+discount shape identical to NJ/MD/WA: 100% free charity care ≤200% FPL, partial charity
# care (a sliding discount <100%) 200–300% FPL. Source-verified vs the primary RICR text (rules.sos.ri.gov)
# + RI DOH (2026-07). The partial-discount AMOUNT is set by each hospital ("its own evaluation of service
# area needs"), so we model the clean free≤200 / discount≤300 ELIGIBILITY envelope the regulation guarantees
# (the s_minimum_note already says the hospital may offer more). income_cap_pct None (no %-of-income cap);
# immigration_excluded False (not addressed in the regulation). Reuses existing free-tier strings — no i18n.
RI = StateRules(
    code="RI", name="Rhode Island",
    fpl_floor_pct=300, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="Rhode Island's hospital charity care regulation (216-RICR-40-10-23)",
    statutory_free_pct=200, statutory_discount_pct=300, income_cap_pct=None,
)

# Maine — hospital financial assistance / charity care (22 M.R.S. §1716-A, as REWRITTEN by PL 2025, c. 488,
# EFFECTIVE 2026-07-01). STATUTE-DRIVEN and STATEWIDE. ⚠️ STALE-LAW CATCH (verify-first, per the CT/OR
# lesson): the prior law was FREE ≤150% FPL since ~1995; c. 488 RAISED the free floor to 200% FPL and ADDED
# an affordability band — so the old "free-only ≤150%" model would have been WRONG. Source-verified vs the
# primary statute (mainelegislature.org, banner "WHOLE SECTION TEXT EFFECTIVE 7/01/26") + press + MaineHealth
# (2026-07). The FIRST FREE-ONLY statutory shape: 100% FREE care ≤200% FPL, and NO statutory %-discount tier
# — instead, patients up to 400% FPL are guaranteed a PAYMENT PLAN capped at 4% of monthly family income (a
# payment cap, not a charge discount). So statutory_free_pct=200, statutory_discount_pct=None (the engine's
# tier logic returns 'free' ≤200 and 'over' above it, never a bogus 'discount ... at or below None%'); the
# 200–400% affordability band is surfaced as a flag-gated payment-cap NOTE (payment_cap_pct/ceiling), not a
# discount tier. income_cap_pct None (no annual %-of-income collection cap). immigration_excluded stays False:
# the statute is SILENT on immigration status (income+residency, MAGI-based, no asset test) — not an explicit
# bar like NY §2807-k(9-a), so no affirmative reassurance. (Maine's debt-collection protection lives in a
# SEPARATE statute, 32 M.R.S. ch. 109-A — deferred: exact subsection not yet pinned to primary source.)
ME = StateRules(
    code="ME", name="Maine",
    fpl_floor_pct=200, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="Maine's hospital financial assistance law (22 M.R.S. §1716-A)",
    statutory_free_pct=200, statutory_discount_pct=None, income_cap_pct=None,
    payment_cap_pct=4, payment_cap_ceiling_pct=400,   # §1716-A: payment plan ≤4% of monthly income to 400% FPL
)

# Massachusetts — Health Safety Net (HSN): M.G.L. c.118E §§65–69 (enabling statute) + 101 CMR 613.00 (the
# operative eligibility regulation, current version effective 2024-04-01; thresholds stable through 2026).
# STATUTE/REGULATION-DRIVEN and STATEWIDE. Clean FREE+DISCOUNT shape like NJ/MD/RI but with a LOWER free floor:
# 100% FREE care (no deductible) ≤150% FPL, then a partial/sliding "HSN Partial" DEDUCTIBLE tier 150–300% FPL
# (deductible = greater of a ConnectorCare-premium proxy or 40% of income over 200% FPL — a sliding cost-share,
# NOT a flat % discount or a %-of-income collection cap, so income_cap_pct stays None). We model the clean
# free≤150 / discount≤300 ELIGIBILITY envelope the reg guarantees (the s_minimum_note carries "the hospital may
# offer more"). Above 300% FPL = Medical Hardship (case-by-case, needs the hardship concept the model lacks —
# a follow-on, like MD's 300–500% tier). Source-verified vs the primary reg (101 CMR 613.04, Cornell LII) +
# M.G.L. c.118E + mass.gov HSN (2026-07); thresholds NOT amended down 2023–2026 (2025 changes were dental
# prior-auth only). immigration_excluded=True (deliberate, user-approved 2026-07-18): MA HSN eligibility is
# residency+income-based and DOES cover undocumented residents, and 101 CMR 613.08 bars discrimination on
# "alienage"/"citizenship" — a strong, citeable non-exclusion for Cobijo's core audience. It's a residency
# mechanism + anti-discrimination reg rather than NY's single "immigration shall not be an eligibility
# criterion" clause, so it's a hair less "in terms" than NY, but well-sourced (HIGH confidence) and accurate:
# status can't decide eligibility. Reuses the existing translated s_immigration string (all 10 langs — a clean
# toggle, no new i18n). Because MA also HAS a free tier it reuses the existing free-tier strings — no new i18n.
MA = StateRules(
    code="MA", name="Massachusetts",
    fpl_floor_pct=300, discount_implausible_pct=800,
    free_care_unusual_pct=150, free_care_implausible_pct=400,
    fap_law="the Massachusetts Health Safety Net (M.G.L. c.118E; 101 CMR 613.00)",
    statutory_free_pct=150, statutory_discount_pct=300, income_cap_pct=None,
    immigration_excluded=True,   # 101 CMR 613.08 bars alienage/citizenship discrimination + HSN covers undocumented residents
)

# Ohio — Hospital Care Assurance Program (HCAP): the individual free-care mandate is Ohio Rev. Code §5168.14
# (within ORC ch. 5168), implemented by OAC 5160-2-17. STATUTE-DRIVEN and STATEWIDE — every non-federal general
# acute-care hospital is assessed the HCAP fee (§5168.06) and §5168.14 binds "each hospital that receives funds
# distributed under" the chapter, so the free-care duty reaches essentially all of them. The SECOND FREE-ONLY
# shape (like ME): a hospital "shall provide, without charge ... basic, medically necessary hospital-level
# services to individuals who are residents of this state, are not medicaid recipients, and whose income is at
# or below the federal poverty line" — i.e. 100% FREE ≤100% FPL, NO statutory discount tier above it (any
# higher-income discount is the hospital's own §501(r) policy, not HCAP). statutory_free_pct=100,
# statutory_discount_pct=None. Source-verified vs primary ORC §5168.14 + §5168.01 + OAC 5160-2-17
# (codes.ohio.gov); current eff. 2023-10-03 (HB33), the 100% FPL standard did NOT move. income_cap_pct None.
# immigration_excluded stays False: the statute is SILENT on immigration (residency+income+non-Medicaid only) —
# the "no immigration consequence" reassurance is UHCAN advocacy guidance, not statute text, so we don't surface
# the affirmative note. Honest follow-on (not modeled): HCAP gives an unusually long 3-YEAR window to apply. Cite
# §5168.14 (NOT §5168.06, the funding side). Reuses the ME free-only i18n — no new i18n.
OH = StateRules(
    code="OH", name="Ohio",
    fpl_floor_pct=100, discount_implausible_pct=800,
    free_care_unusual_pct=100, free_care_implausible_pct=300,
    fap_law="Ohio's Hospital Care Assurance Program (Ohio Rev. Code §5168.14)",
    statutory_free_pct=100, statutory_discount_pct=None, income_cap_pct=None,
)

# Vermont — Patient Financial Assistance and Medical Debt (18 V.S.A. §§9481–9485; Act 119 of 2022, FPL tiers
# operational 2022-07-01 with hospital implementation 2024-07-01; §9485 amended by Act 21 of 2025). STATUTE-
# DRIVEN and STATEWIDE — §9482 sets a MANDATORY statewide FPL floor every "large health care facility" must meet
# ("shall provide free or discounted care ... as follows"), the most generous free tier in the model: 100% FREE
# ≤250% FPL, then a ≥40% discount 250–400% FPL. (A separate catastrophic cap — bills capped at 20% of household
# income ≤600% FPL — needs the hardship concept the model lacks, a follow-on like MD's 300–500% tier.)
# statutory_free_pct=250, statutory_discount_pct=400. Source-verified vs primary 18 V.S.A. §9482/§9483
# (legislature.vermont.gov). income_cap_pct None (the discount is a %-reduction; §9483's 5%-of-monthly-income
# PAYMENT-plan cap is a separate protection, a follow-on note). immigration_excluded=True: §9483 EXPLICITLY
# protects undocumented immigrants (refusal to apply for public programs is not grounds for denial; they may
# submit alternative income documentation) — an in-terms statutory non-exclusion (stronger than MA's), so we
# surface the existing translated s_immigration reassurance. Reuses existing free+discount strings — no new i18n.
VT = StateRules(
    code="VT", name="Vermont",
    fpl_floor_pct=400, discount_implausible_pct=800,
    free_care_unusual_pct=250, free_care_implausible_pct=400,
    fap_law="Vermont's hospital financial assistance law (18 V.S.A. §9482)",
    statutory_free_pct=250, statutory_discount_pct=400, income_cap_pct=None,
    immigration_excluded=True,   # 18 V.S.A. §9483 explicitly protects undocumented immigrants from exclusion
)

# North Carolina — Medical Debt Relief Incentive Program (the charity-care standard hospitals must adopt as a
# condition of enhanced HASP Medicaid payments; CMS-approved 2024-07-26, renewed through mid-2026). NOT a statute
# — a binding STATEWIDE PROGRAM: all 99 eligible acute-care hospitals opted in, so it's effectively universal and
# enforced by NCDHHS. So authority_is_program=True (pages say "{state}'s hospital financial assistance program",
# NOT "North Carolina law" — which would misstate the authority). FREE+DISCOUNT shape: 100% FREE ≤200% FPL, then
# ≥75% off 200–250% and ≥50% off 250–300% — we model the clean free≤200 / discount≤300 envelope (the 75/50 sub-
# bands are a note-level refinement like NY's cap). Charity-care tiers began 2025-01-01. Source-verified vs
# NCDHHS + the CMS-approved HASP directed-payment docs + SHVS spotlight (2026-07). income_cap_pct None (the
# program's §-payment protections — 5%/3yr plans, debt-sale/credit-report bans, 3% interest — are follow-on
# notes). immigration_excluded stays False: the program is immigration-NEUTRAL (silent, and explicitly designed
# to reach undocumented patients) but has no in-terms non-exclusion clause, so no affirmative reassurance note.
# ⚠️ RE-CHECK at each annual CMS HASP renewal (~mid-2026) — the funding condition, not a permanent statute.
# Reuses existing free+discount strings + the new _program authority variants — no shape-specific i18n.
NC = StateRules(
    code="NC", name="North Carolina",
    fpl_floor_pct=300, discount_implausible_pct=800,
    free_care_unusual_pct=200, free_care_implausible_pct=400,
    fap_law="North Carolina's Medical Debt Relief Program",
    statutory_free_pct=200, statutory_discount_pct=300, income_cap_pct=None,
    authority_is_program=True,   # binding Medicaid HASP program condition (all 99 acute hospitals), not a statute
)

STATES = {"CA": CA, "IL": IL, "NY": NY, "MD": MD, "WA": WA, "NJ": NJ, "CO": CO, "OR": OR, "RI": RI, "ME": ME,
          "MA": MA, "OH": OH, "VT": VT, "NC": NC}


def rules_for(state="CA"):
    """The rules for a USPS state code. Defaults to CA (the only populated state). An unknown or missing
    state falls back to the §501(r) generic defaults — wide bounds, no statutory floor — so a valid
    national FAP isn't mistaken for a misread."""
    return STATES.get((state or "CA").upper(), _DEFAULT)
