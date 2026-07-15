#!/usr/bin/env python3
"""
T4.1 Phase 1 — state-aware infra refactor.

These tests pin the contract of the CA-hardcode extraction into state_rules.py: (a) every California
code path is byte-identical to before (the module-level constants, the extraction prompt, validate(),
qa check_row, the hospital-page location line), and (b) the plumbing is genuinely state-aware — a value
that is a likely-misread in CA (>600% free-care ceiling) is a VALID national FAP under §501(r) and must
NOT be flagged when the row's state isn't CA.

Run (from repo root):  python3 -m unittest tests.test_national
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
for p in (_ROOT, _WEB):
    if p not in sys.path:
        sys.path.insert(0, p)

import extract_llm
import navigator
import qa_dataset
import state_rules
from tests.test_cobijo import make_row


class TestStateRules(unittest.TestCase):
    def test_default_is_ca(self):
        self.assertIs(state_rules.rules_for(), state_rules.CA)
        self.assertIs(state_rules.rules_for(None), state_rules.CA)
        self.assertIs(state_rules.rules_for("ca"), state_rules.CA)   # case-insensitive

    def test_ca_values_match_legacy_constants(self):
        ca = state_rules.rules_for("CA")
        self.assertEqual(ca.fpl_floor_pct, 400)
        self.assertEqual(ca.discount_implausible_pct, 800)
        self.assertEqual(ca.free_care_unusual_pct, 400)
        self.assertEqual(ca.free_care_implausible_pct, 600)
        self.assertEqual(ca.name, "California")

    def test_unknown_state_falls_back_to_generic_501r(self):
        # §501(r) has no numeric FPL floor; the default uses wide bounds + an empty name so a legit
        # national FAP isn't false-flagged and the prompt drops the state word.
        d = state_rules.rules_for("TX")
        self.assertEqual(d.fpl_floor_pct, 0)
        self.assertEqual(d.free_care_implausible_pct, 800)
        self.assertEqual(d.name, "")


class TestBackCompatSurface(unittest.TestCase):
    """The refactor must not change the CA-facing module surface other modules import/read."""

    def test_extract_llm_constants_unchanged(self):
        self.assertEqual(extract_llm.STATUTORY_FPL_FLOOR, 400)
        self.assertEqual(extract_llm.DISCOUNT_IMPLAUSIBLE_PCT, 800)
        self.assertEqual(extract_llm.FREE_CARE_UNUSUAL_PCT, 400)
        self.assertEqual(extract_llm.FREE_CARE_IMPLAUSIBLE_PCT, 600)

    def test_ca_prompt_is_byte_identical(self):
        # The SYSTEM constant + the user message must still say "California hospital" for CA.
        self.assertIn("extracting California hospital", extract_llm.SYSTEM)
        self.assertEqual(extract_llm._system("CA"), extract_llm.SYSTEM)
        params = extract_llm._message_params("CORPUS", "m")
        self.assertIn("this California hospital", params["messages"][0]["content"])
        self.assertEqual(params["system"], extract_llm.SYSTEM)

    def test_generic_prompt_drops_the_state_word(self):
        sys_txt = extract_llm._system("TX")
        self.assertIn("extracting hospital", sys_txt)          # no state name, no double space
        self.assertNotIn("California", sys_txt)
        params = extract_llm._message_params("CORPUS", "m", state="TX")
        self.assertIn("this hospital Charity Care", params["messages"][0]["content"])


class TestValidateStateAware(unittest.TestCase):
    # 650% free-care ceiling: a likely misread in CA (>600), a legal FAP nationally (< generic 800).
    REC = {"free_care": {"fpl_ceiling_pct": 650},
           "discount_payment": {"fpl_ceiling_pct": 700, "tiers": []},
           "extraction_confidence": 0.9}

    def test_ca_flags_out_of_range(self):
        self.assertIn("free-care FPL% out of range: 650", extract_llm.validate(self.REC, "CA"))

    def test_default_is_ca(self):
        self.assertIn("free-care FPL% out of range: 650", extract_llm.validate(self.REC))

    def test_generic_state_does_not_flag(self):
        self.assertNotIn("free-care FPL% out of range: 650", extract_llm.validate(self.REC, "TX"))


class TestQaCheckRowStateAware(unittest.TestCase):
    def _row(self, state=None):
        r = make_row(free=650, disc=700)     # dollar tables recompute to match; fc<dc so no "exceeds" noise
        if state:
            r["state"] = state
        return r

    def test_ca_row_flags_implausible_free_ceiling(self):
        details = [d for _, _, d in qa_dataset.check_row(self._row())]     # no state -> CA default
        self.assertTrue(any("implausibly high (>600%)" in d for d in details), details)

    def test_non_ca_row_does_not_flag_it(self):
        details = [d for _, _, d in qa_dataset.check_row(self._row("TX"))]
        self.assertFalse(any("implausibly high" in d and "free-care" in d for d in details), details)


class TestHospitalPageState(unittest.TestCase):
    import hospital_pages as _hp

    def _row(self, state=None):
        r = make_row(); r["hospital"] = "Alpha Regional Medical Center"; r["city"] = "Fresno"
        r["county"] = "Fresno"; r["oshpdid"] = "12345"
        r["charity_policy_url"] = "https://example.org/policy.pdf"
        r["charity_effective_date"] = "05/13/2025"
        if state:
            r["state"] = state
        return r

    def _render(self, row):
        idx, _ = self._hp.build_index([row])
        slug = next(iter(idx))
        return self._hp.render_hospital(idx[slug], slug, idx, "en")

    def test_ca_row_shows_ca(self):
        self.assertIn("Fresno, CA", self._render(self._row()))       # default state

    def test_state_field_threads_into_the_page(self):
        self.assertIn("Fresno, TX", self._render(self._row("TX")))


class TestStatutoryRulesIL(unittest.TestCase):
    """T4.1 Phase 2 — Illinois statute-driven rules (210 ILCS 89). CA must stay NON-statutory."""

    def test_ca_and_default_are_not_statutory(self):
        self.assertFalse(state_rules.rules_for("CA").is_statutory)
        self.assertFalse(state_rules.rules_for("TX").is_statutory)   # generic default
        self.assertIsNone(state_rules.rules_for("CA").statutory_discount_pct)

    def test_il_values_pinned_from_statute(self):
        il = state_rules.rules_for("il")                             # case-insensitive
        self.assertTrue(il.is_statutory)
        self.assertEqual((il.statutory_free_pct, il.statutory_discount_pct), (200, 600))
        self.assertEqual((il.statutory_free_rural_pct, il.statutory_discount_rural_pct), (125, 300))
        self.assertEqual(il.income_cap_pct, 20)
        self.assertIn("210 ILCS 89", il.fap_law)

    def test_metro_vs_rural_bands(self):
        il = state_rules.rules_for("IL")
        self.assertEqual(il.free_pct_for(rural=False), 200)
        self.assertEqual(il.free_pct_for(rural=True), 125)
        self.assertEqual(il.discount_pct_for(rural=True), 300)


class TestStatutoryTier(unittest.TestCase):
    IL = state_rules.rules_for("IL")

    def test_metro_bands(self):
        self.assertEqual(navigator.statutory_tier(150, self.IL), "free")      # <=200
        self.assertEqual(navigator.statutory_tier(400, self.IL), "discount")  # 200<x<=600
        self.assertEqual(navigator.statutory_tier(700, self.IL), "over")      # >600

    def test_rural_bands_are_stricter(self):
        # A patient at 150% FPL is free at a metro hospital but only discount-eligible at a rural CAH.
        self.assertEqual(navigator.statutory_tier(150, self.IL, rural=True), "discount")  # >125, <=300
        self.assertEqual(navigator.statutory_tier(120, self.IL, rural=True), "free")       # <=125
        self.assertEqual(navigator.statutory_tier(350, self.IL, rural=True), "over")       # >300


class TestStatutoryFacts(unittest.TestCase):
    def _intake(self, income, hh=2):
        return {"annual_income": income, "household_size": hh, "insurance": "uninsured"}

    def _il_row(self, cah=False):
        return {"hospital": "MERCY MEDICAL CENTER", "state": "IL",
                "hospital_type": "Critical Access Hospitals" if cah else "Acute Care Hospitals"}

    def test_returns_none_for_non_statutory_state(self):
        self.assertIsNone(navigator.statutory_facts(self._intake(20000), {"hospital": "X", "state": "CA"}))

    def test_il_metro_facts(self):
        f = navigator.statutory_facts(self._intake(20000), self._il_row())     # low income -> free
        self.assertEqual(f["state"], "IL")
        self.assertEqual(f["tier"], "free")
        self.assertEqual((f["free_pct"], f["discount_pct"], f["income_cap_pct"]), (200, 600, 20))
        self.assertIn("210 ILCS 89", f["fap_law"])
        self.assertFalse(f["rural"])
        self.assertEqual(f["hospital"], "Mercy Medical Center")

    def test_il_critical_access_uses_rural_bands(self):
        # Same income that is "free" at a metro hospital lands as "discount" at a rural CAH.
        income = 30000        # ~150% FPL for a 2-person household
        metro = navigator.statutory_facts(self._intake(income), self._il_row(cah=False))
        rural = navigator.statutory_facts(self._intake(income), self._il_row(cah=True))
        self.assertTrue(rural["rural"])
        self.assertEqual((rural["free_pct"], rural["discount_pct"]), (125, 300))
        self.assertIn(metro["tier"], ("free", "discount"))
        # rural bands are never more generous than metro for the same income
        self.assertLessEqual(rural["free_pct"], metro["free_pct"])


class TestCmsNormalize(unittest.TestCase):
    """cms_hospitals normalizes a CMS roster row to the app's statute-driven dataset shape (no network)."""

    def setUp(self):
        import importlib
        self.cms = importlib.import_module("cms_hospitals")

    def test_normalize_shape(self):
        row = {"Facility Name": "graham hospital association", "Facility ID": "140001",
               "Address": "210 W WALNUT", "City/Town": "canton", "State": "IL", "ZIP Code": "61520",
               "County/Parish": "FULTON", "Telephone Number": "(309) 555-0100",
               "Hospital Ownership": "Voluntary non-profit - Private", "Hospital Type": "Acute Care Hospitals"}
        n = self.cms.normalize_cms_row(row, "IL")
        self.assertEqual(n["hospital"], "GRAHAM HOSPITAL ASSOCIATION")
        self.assertEqual(n["ccn"], "140001")
        self.assertEqual(n["county"], "Fulton")
        self.assertEqual(n["state"], "IL")
        self.assertEqual(n["status"], "statutory")
        self.assertIsNone(n["policy"])                     # statute-driven: no extracted policy


if __name__ == "__main__":
    unittest.main()
