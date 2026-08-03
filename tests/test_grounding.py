"""Tests for the numeric grounding gate.

The gate's whole value is its false-flag rate: a checker that cries wolf gets switched off, and a
checker that never fires proves nothing. These tests pin BOTH ends — it must catch a wrong number,
and it must not flag the ordinary ways a real policy writes a right one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import grounding


FPL_DOC = (
    "FINANCIAL ASSISTANCE POLICY\n"
    "Patients with Family Income at or below 200% of the Federal Poverty Level are eligible for "
    "free care. Patients above 200% but at or below 400 percent of the FPL receive a discount.\n"
) + ("Filler text to exceed the usable-text threshold. " * 40)


class TestNormalization(unittest.TestCase):
    """`200`, `200%`, `200 percent` must collapse to one value — the point of not matching strings."""

    def test_percent_spellings_all_parse(self):
        for spelling in ("200%", "200 %", "200 percent", "200 per cent", "200.0%"):
            text = f"income at or below {spelling} of the Federal Poverty Level"
            ok, _ = grounding.ground_value(200, text + " padding" * 200)
            self.assertTrue(ok, f"failed to ground {spelling!r}")

    def test_bare_number_without_a_unit_is_not_a_threshold(self):
        """A bare 200 is a room number or a phone fragment; requiring the unit is what keeps
        the false-positive rate down."""
        text = "Call 200 for the Federal Poverty Level office. " + "padding " * 200
        ok, _ = grounding.ground_value(200, text)
        self.assertFalse(ok)


class TestProximity(unittest.TestCase):
    def test_value_far_from_poverty_language_is_not_grounded(self):
        text = ("The Federal Poverty Level is used in this policy. " + "x" * 5000 +
                " Parking validation is 200% of the daily rate.")
        ok, _ = grounding.ground_value(200, text)
        self.assertFalse(ok, "a percentage 5000 chars from any FPL wording must not count")

    def test_value_next_to_poverty_language_is_grounded_with_evidence(self):
        ok, evidence = grounding.ground_value(200, FPL_DOC)
        self.assertTrue(ok)
        self.assertIn("200", evidence)
        self.assertRegex(evidence.lower(), r"poverty")


class TestVerdicts(unittest.TestCase):
    def _row(self, free=200, disc=400, tiers=None, sha=None):
        return {"hospital": "Test", "source_sha256": sha,
                "policy": {"free_care": {"fpl_ceiling_pct": free},
                           "discount_payment": {"fpl_ceiling_pct": disc, "tiers": tiers or []}}}

    def test_correct_values_are_grounded(self):
        res = grounding.check_row(self._row(), FPL_DOC)
        self.assertEqual(res["verdict"], grounding.GROUNDED)
        self.assertEqual(res["checked"], 2)

    def test_a_value_absent_from_the_document_is_ungrounded(self):
        """The case the gate exists for: a number nothing in the source supports."""
        res = grounding.check_row(self._row(free=237), FPL_DOC)
        self.assertEqual(res["verdict"], grounding.UNGROUNDED)
        self.assertTrue(any("237" in f.get("reason", "") for f in res["findings"]))

    def test_scanned_document_is_unverifiable_not_wrong(self):
        """'I could not check this' must never be reported as 'this is wrong'."""
        res = grounding.check_row(self._row(), "tiny")
        self.assertEqual(res["verdict"], grounding.UNVERIFIABLE)

    def test_hash_mismatch_is_stale_not_wrong(self):
        """Grounding against the wrong document would give a confident answer about the wrong text."""
        res = grounding.check_row(self._row(sha="deadbeef"), FPL_DOC)
        self.assertEqual(res["verdict"], grounding.STALE)

    def test_derived_tier_lows_are_not_gated(self):
        """Tier lower bounds are boundary arithmetic, not transcription.

        Including them scored 40% auto-verification, 30 of 34 failures being a synthesized
        fpl_low_pct of 0 — worse than the verbatim matching this module replaced.
        """
        row = self._row(tiers=[{"fpl_low_pct": 0, "fpl_high_pct": 200},
                               {"fpl_low_pct": 201, "fpl_high_pct": 400}])
        labels = [lbl for lbl, _ in grounding.published_thresholds(row)]
        self.assertFalse([l for l in labels if "fpl_low_pct" in l])
        self.assertIn("discount_payment.tiers[1].fpl_high_pct", labels)
        # ...and the row still grounds, because both highs appear in the document.
        self.assertEqual(grounding.check_row(row, FPL_DOC)["verdict"], grounding.GROUNDED)


class TestGarbledTextLayer(unittest.TestCase):
    """A PDF whose text layer decodes to mojibake must not be treated as a readable source.

    Real failure: RIVERSIDE UNIVERSITY HEALTH SYSTEM shipped 60,442 characters of
    "456578\u00ff \u00ff4565\u00ff\u00ff  \u00ff !\"65\"\u00ff##$%&#'" — plenty of text, none of it
    language. It cleared the len<500 scanned check, was never routed to OCR, and the extractor
    answered with the statutory 400% for both ceilings. Those numbers reached patients.
    """

    GARBLE = "456578\u00ff \u00ff4565\u00ff\u00ff \u00ff !\"65\"\u00ff##$%&#'\u00ff 6\",\u00ff 76 " * 60
    ENGLISH = ("The patient is eligible for charity care if family income is at or below 200% of "
               "the Federal Poverty Level. The hospital will discount the account. ") * 12
    SPANISH = ("Los pacientes y las familias con bajos ingresos que no tienen seguro pueden "
               "recibir atencion medica gratuita del hospital para sus ingresos. ") * 12

    def test_mojibake_is_not_intelligible(self):
        self.assertFalse(grounding.text_is_intelligible(self.GARBLE))

    def test_english_policy_is_intelligible(self):
        self.assertTrue(grounding.text_is_intelligible(self.ENGLISH))

    def test_spanish_policy_is_intelligible(self):
        """THE REGRESSION THIS EXISTS TO PREVENT.

        An English-only stopword list scored NORTHERN INYO HOSPITAL — a perfectly legible
        Spanish-language policy — below the garbled threshold. Condemning a Spanish policy as
        unreadable would suppress answers for exactly the patients this project serves.
        """
        self.assertTrue(grounding.text_is_intelligible(self.SPANISH))

    def test_garbled_corpus_gets_its_own_verdict(self):
        """Not `unverifiable` — we DID read it, and it is not language."""
        row = {"hospital": "Test", "source_sha256": None,
               "policy": {"free_care": {"fpl_ceiling_pct": 400},
                          "discount_payment": {"fpl_ceiling_pct": 400, "tiers": []}}}
        self.assertEqual(grounding.check_row(row, self.GARBLE)["verdict"], grounding.UNREADABLE)

    def test_garbled_verdict_suppresses_at_serve_time(self):
        import navigator
        row = {"hospital": "Test", "grounding": {"verdict": grounding.UNREADABLE, "checked": 0},
               "policy": {"free_care": {"fpl_ceiling_pct": 400},
                          "discount_payment": {"fpl_ceiling_pct": 400, "tiers": []}}}
        self.assertTrue(navigator.free_care_ceiling_is_untrustworthy(row))


class TestWindowRegression(unittest.TestCase):
    def test_table_spanning_value_grounds(self):
        """A six-row sliding scale puts its last row ~360 chars below the column header.

        The original 300-char window reported STANISLAUS SURGICAL's CORRECT 100% free-care ceiling
        as ungrounded and suppressed it. 600 covers the table; it must stay wide enough.
        """
        doc = ("Federal Poverty Level    Charity Level    Patient Responsibility\n"
               + "".join(f"    {lo}-{hi}%        {c}%        {p}%\n"
                         for lo, hi, c, p in [(301, 400, 50, 50), (251, 300, 60, 40),
                                              (201, 250, 70, 30), (151, 200, 80, 20),
                                              (101, 150, 90, 10), (0, 100, 100, 0)])
               + "padding. " * 80)
        self.assertTrue(grounding.ground_value(100, doc)[0],
                        "the last row of a real sliding-scale table must ground")


if __name__ == "__main__":
    unittest.main()
