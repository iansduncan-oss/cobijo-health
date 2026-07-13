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


if __name__ == "__main__":
    unittest.main()
