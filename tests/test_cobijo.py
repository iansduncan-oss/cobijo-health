#!/usr/bin/env python3
"""
Test suite — stdlib unittest, no external deps or network.

Covers the logic this project can't afford to get wrong: the charity-care match (which tier a
patient lands in), the benefit heuristic fallback, the message catalog's en/es integrity (a
missing or drifted translation would silently show English or crash), and the QA harness's
semantic checks (which gate the dataset a real patient's plan is built from).

Run (from repo root):  python3 -m unittest discover -s tests
"""
import base64
import hashlib
import hmac
import os
import string
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # repo root

import navigator
import qa_dataset
import sms
from extract_llm import dollar_table, validate, effective_date_issue
from messages import MESSAGES, t


def make_row(free=200, disc=400, tiers=None, hmc=None, phone="(555) 555-0100"):
    """A synthetic extracted row shaped exactly like build_dataset output."""
    if tiers is None:
        tiers = [{"fpl_low_pct": 201, "fpl_high_pct": 300, "patient_pays_pct": 50, "benefit": "pays 50%"},
                 {"fpl_low_pct": 301, "fpl_high_pct": 400, "patient_pays_pct": 80, "benefit": "pays 80%"}]
    policy = {
        "free_care": {"fpl_ceiling_pct": free},
        "discount_payment": {"fpl_ceiling_pct": disc, "tiers": tiers},
        "high_medical_cost": hmc or {},
        "payment_plans": {"interest_free": True},
        "contact": {"phone": phone},
        "extraction_confidence": 0.9,
        "source_quotes": {},
    }
    return {
        "hospital": "Test Community Hospital", "status": "extracted", "permalink": "http://x/test",
        "source_sha256": "sha_test", "policy": policy,
        "free_care_income_ceiling_by_household": dollar_table(free),
        "discount_income_ceiling_by_household": dollar_table(disc),
    }


class TestFPL(unittest.TestCase):
    def test_poverty_limit(self):
        self.assertEqual(navigator.poverty_limit(1), 15960)
        self.assertEqual(navigator.poverty_limit(4), 33000)
        self.assertEqual(navigator.poverty_limit(9), navigator.FPL[8] + navigator.FPL_EACH_ADDITIONAL)

    def test_poverty_limit_clamps_bad_input(self):
        # A household of 0/negative must not KeyError — it's a public endpoint (frontend sends
        # the field as a string, so "0" slips past the falsy guard). Clamp to a household of 1.
        self.assertEqual(navigator.poverty_limit(0), navigator.poverty_limit(1))
        self.assertEqual(navigator.poverty_limit(-3), navigator.poverty_limit(1))

    def test_fpl_percent(self):
        self.assertEqual(navigator.fpl_percent(33000, 4), 100.0)
        self.assertEqual(navigator.fpl_percent(16500, 4), 50.0)

    def test_fpl_percent_negative_income_is_zero(self):
        # A negative income (business loss) must read as 0% FPL, never a negative % / bogus tier.
        self.assertEqual(navigator.fpl_percent(-5000, 4), 0.0)


class TestMatchCharityCare(unittest.TestCase):
    def test_free_tier(self):
        tier, msg = navigator.match_charity_care(make_row(), pct=100, household=4, insured=False)
        self.assertEqual(tier, "free")
        self.assertIn("FREE", msg)

    def test_discount_tier(self):
        tier, msg = navigator.match_charity_care(make_row(), pct=250, household=4, insured=False)
        self.assertEqual(tier, "discount")
        self.assertIn("201", msg)      # names the band it fell into

    def test_discount_ceiling_no_tier(self):
        # pct above the last tier's high but at/below the discount ceiling -> ceiling branch
        row = make_row(tiers=[{"fpl_low_pct": 201, "fpl_high_pct": 300, "patient_pays_pct": 50}])
        tier, _ = navigator.match_charity_care(row, pct=350, household=4, insured=False)
        self.assertEqual(tier, "discount")

    def test_high_medical_cost(self):
        row = make_row(hmc={"oop_threshold_pct_of_income": 30})
        tier, msg = navigator.match_charity_care(row, pct=500, household=4, insured=True)
        self.assertEqual(tier, "high_cost")
        self.assertIn("high-medical-cost", msg)

    def test_over_ceiling(self):
        tier, _ = navigator.match_charity_care(make_row(), pct=500, household=4, insured=False)
        self.assertEqual(tier, "over")

    def test_spanish_free_tier(self):
        tier, msg = navigator.match_charity_care(make_row(), pct=100, household=4, insured=False, lang="es")
        self.assertEqual(tier, "free")
        self.assertIn("GRATUITA", msg)


class TestScreenBenefits(unittest.TestCase):
    """Exercise the offline FPL heuristic (no network)."""
    def setUp(self):
        self._orig = navigator.USE_POLICYENGINE
        navigator.USE_POLICYENGINE = False

    def tearDown(self):
        navigator.USE_POLICYENGINE = self._orig

    def test_insured(self):
        self.assertIn("insurance", navigator.screen_benefits(100, "aetna")[0].lower())

    def test_medicaid_band(self):
        self.assertIn("Medi-Cal", navigator.screen_benefits(100, "uninsured")[0])

    def test_aca_band(self):
        self.assertIn("ACA", navigator.screen_benefits(300, "uninsured")[0])

    def test_over_band(self):
        self.assertIn("above", navigator.screen_benefits(500, "uninsured")[0].lower())

    def test_spanish_heuristic(self):
        self.assertIn("Medicaid de California", navigator.screen_benefits(100, "uninsured", lang="es")[0])


class TestPolicyEngineCircuitBreaker(unittest.TestCase):
    """PolicyEngine soft circuit breaker (S#5 fix): a transient API failure must degrade to the
    FPL heuristic for a cooldown window and then self-heal — it must NEVER permanently disable the
    Medi-Cal figure for a long-running server process (the bug this replaced)."""

    def setUp(self):
        self._use = navigator.USE_POLICYENGINE
        self._cd = navigator._PE_COOLDOWN_UNTIL
        self._fn = navigator.policyengine.benefit_leads
        navigator.USE_POLICYENGINE = True
        navigator._PE_COOLDOWN_UNTIL = 0.0
        self.calls = 0

    def tearDown(self):
        navigator.USE_POLICYENGINE = self._use
        navigator._PE_COOLDOWN_UNTIL = self._cd
        navigator.policyengine.benefit_leads = self._fn

    def _boom(self, *a, **k):
        self.calls += 1
        raise RuntimeError("simulated PolicyEngine outage")

    def _ok(self, *a, **k):
        self.calls += 1
        return (["__PE_LEAD__"], {})

    def test_failure_falls_back_and_trips_breaker(self):
        navigator.policyengine.benefit_leads = self._boom
        out = navigator.screen_benefits(100, "uninsured", income=25000, household=3)
        self.assertEqual(self.calls, 1)                        # PolicyEngine was attempted
        self.assertNotIn("__PE_LEAD__", out)                   # ...and we fell back
        self.assertIn("Medi-Cal", out[0])                      # to the FPL heuristic at 100% FPL
        self.assertGreater(navigator._PE_COOLDOWN_UNTIL, 0)    # breaker tripped

    def test_skips_policyengine_during_cooldown(self):
        navigator.policyengine.benefit_leads = self._boom
        navigator.screen_benefits(100, "uninsured", income=25000, household=3)   # trips the breaker
        navigator.policyengine.benefit_leads = self._ok         # would succeed if called...
        out = navigator.screen_benefits(100, "uninsured", income=25000, household=3)
        self.assertEqual(self.calls, 1)                         # ...but PE is skipped during cooldown
        self.assertNotIn("__PE_LEAD__", out)

    def test_self_heals_after_cooldown(self):
        navigator._PE_COOLDOWN_UNTIL = 0.0                      # cooldown elapsed
        navigator.policyengine.benefit_leads = self._ok
        out = navigator.screen_benefits(100, "uninsured", income=25000, household=3)
        self.assertEqual(self.calls, 1)
        self.assertIn("__PE_LEAD__", out)                       # PolicyEngine is used again


class TestBuildPlanStruct(unittest.TestCase):
    """The structured plan the web/SMS UIs render — must stay aligned with the text plan."""
    def setUp(self):
        self._orig = navigator.USE_POLICYENGINE
        navigator.USE_POLICYENGINE = False      # offline heuristic, no network

    def tearDown(self):
        navigator.USE_POLICYENGINE = self._orig

    def _intake(self, income, household=4, insurance="uninsured", collections=True):
        return {"first_name": "Maria", "last_name": "L", "household_size": household,
                "annual_income": income, "insurance": insurance, "in_collections": collections}

    def test_free_case_shape(self):
        r = navigator.build_plan_struct(self._intake(33000), make_row(), lang="en")
        self.assertEqual(r["tier"], "free")
        self.assertIn("FREE", r["headline"])
        self.assertTrue(r["hospital"]["name"])
        self.assertIsNotNone(r["charity"]["income_ceiling"])   # free tier surfaces the $ ceiling
        self.assertTrue(r["charity"]["apply"])                 # at least the apply-and-retroactive steps
        self.assertTrue(r["benefits"])
        self.assertEqual(len(r["debt"]), 4)                    # in_collections -> full debt-defense set

    def test_over_case_has_no_ceiling(self):
        r = navigator.build_plan_struct(self._intake(200000, collections=False), make_row(), lang="en")
        self.assertEqual(r["tier"], "over")
        self.assertIsNone(r["charity"]["income_ceiling"])
        self.assertEqual(r["debt"], [])                        # not in collections -> no debt section

    def test_spanish_headline_differs(self):
        en = navigator.build_plan_struct(self._intake(33000), make_row(), lang="en")["headline"]
        es = navigator.build_plan_struct(self._intake(33000), make_row(), lang="es")["headline"]
        self.assertNotEqual(en, es)
        self.assertIn("GRATUITA", es)


def fake_ds(row=None):
    row = row or make_row()
    return {"by_id": {}, "by_name": {row["hospital"].upper(): row}, "rows": [row]}


class TestSMS(unittest.TestCase):
    """The text-message conversation state machine — same navigator, phone channel."""
    def setUp(self):
        self._orig = navigator.USE_POLICYENGINE
        navigator.USE_POLICYENGINE = False
        self.c = sms.Conversation(fake_ds())

    def tearDown(self):
        navigator.USE_POLICYENGINE = self._orig

    def _run(self, msgs):
        out = [self.c.reply("")]                 # welcome
        for m in msgs:
            out.append(self.c.reply(m))
        return out

    def test_full_free_flow_en(self):
        out = self._run(["1", "Test Community Hospital", "33000", "4", "no", "yes"])
        self.assertEqual(self.c.step, "done")
        self.assertIn("FREE", out[-1])
        self.assertIn("Test Community Hospital", out[-1])
        letter = self.c.reply("LETTER")
        self.assertIn("Financial Assistance", letter)

    def test_spanish_flow(self):
        out = self._run(["2", "Test Community Hospital", "33000", "4", "no", "no"])
        self.assertEqual(self.c.lang, "es")
        self.assertIn("GRATUITA", out[-1])

    def test_bad_income_reasks(self):
        self.c.reply(""); self.c.reply("1"); self.c.reply("Test Community Hospital")
        r = self.c.reply("lots")                 # not a number
        self.assertIn("number", r.lower())
        self.assertEqual(self.c.step, "income")

    def test_unknown_hospital_reasks(self):
        self.c.reply(""); self.c.reply("1")
        r = self.c.reply("Nonexistent Medical Center")
        self.assertEqual(self.c.step, "hospital")
        self.assertIn("couldn't find", r.lower())

    def test_tiny_fragment_does_not_match_wrong_hospital(self):
        # A 1-3 char fragment must not lock onto a hospital via substring (e.g. "a" in HOSPITAL).
        self.c.reply(""); self.c.reply("1")
        r = self.c.reply("a")
        self.assertEqual(self.c.step, "hospital")
        self.assertIn("couldn't find", r.lower())

    def test_restart_resets(self):
        self._run(["1", "Test Community Hospital", "33000", "4", "no", "yes"])
        r = self.c.reply("START")
        self.assertEqual(self.c.step, "lang")
        self.assertIn("English", r)


class TestSMSWebhookSecurity(unittest.TestCase):
    def test_twilio_signature_roundtrip(self):
        tok, url = "tok", "https://h/sms"
        params = {"From": ["+1"], "Body": ["hi"]}
        data = url + "".join(k + params[k][0] for k in sorted(params))
        sig = base64.b64encode(hmac.new(tok.encode(), data.encode(), hashlib.sha1).digest()).decode()
        self.assertTrue(sms.twilio_signature_ok(tok, url, params, sig))
        self.assertFalse(sms.twilio_signature_ok(tok, url, params, "nope"))
        self.assertFalse(sms.twilio_signature_ok(tok, url, params, None))


class TestSMSCatalog(unittest.TestCase):
    def test_en_es_keys_match(self):
        self.assertEqual(set(sms.SMS["en"]), set(sms.SMS["es"]))

    def test_format_fields_match(self):
        for key, en in sms.SMS["en"].items():
            with self.subTest(key=key):
                self.assertEqual(_fields(en), _fields(sms.SMS["es"][key]))


def _fields(template):
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class TestMessages(unittest.TestCase):
    def test_translation_differs(self):
        self.assertNotEqual(t("en", "step1", n=1), t("es", "step1", n=1))
        self.assertTrue(t("es", "step1", n=1))

    def test_unknown_lang_falls_back_to_en(self):
        self.assertEqual(t("fr", "step1", n=1), t("en", "step1", n=1))

    def test_every_en_key_has_es(self):
        missing = set(MESSAGES["en"]) - set(MESSAGES["es"])
        self.assertEqual(missing, set(), f"Spanish catalog missing keys: {missing}")

    def test_format_fields_match_across_languages(self):
        # A translation that drops or renames a {field} will crash at render time.
        for key, en in MESSAGES["en"].items():
            with self.subTest(key=key):
                self.assertEqual(_fields(en), _fields(MESSAGES["es"][key]),
                                 f"format fields differ for '{key}'")


class TestQAChecks(unittest.TestCase):
    def test_clean_row_no_high(self):
        findings = qa_dataset.check_row(make_row())
        self.assertFalse([f for f in findings if f[0] == "HIGH"], findings)

    def test_generous_discount_ceiling_not_flagged(self):
        # §127405: 400% FPL is a floor, not a cap — a hospital may extend discounts above it. A legit
        # generous ceiling (600%) must NOT produce a HIGH "misread" finding.
        self.assertFalse([f for f in qa_dataset.check_row(make_row(disc=600)) if f[0] == "HIGH"])

    def test_statutory_outlier(self):
        # ...but an implausibly high ceiling (units/decimal misread) is still caught.
        findings = qa_dataset.check_row(make_row(disc=1500))
        self.assertTrue(any(c == "statutory" and s == "HIGH" for s, c, _ in findings))

    def test_free_care_outlier_severity(self):
        # Free care above the 400% floor is unusual (MEDIUM verify), not an error; only implausibly
        # high free care (>600%) is HIGH.
        med = qa_dataset.check_row(make_row(free=500, disc=500))
        self.assertFalse([f for f in med if f[0] == "HIGH"])
        self.assertTrue(any(c == "outlier" and s == "MEDIUM" for s, c, _ in med))
        self.assertTrue(any(s == "HIGH" for s, *_ in qa_dataset.check_row(make_row(free=800, disc=800))))

    def test_dollar_table_mismatch(self):
        row = make_row()
        row["free_care_income_ceiling_by_household"] = {"1": 999}
        self.assertTrue(any(c == "dollar_table" and s == "HIGH" for s, c, _ in qa_dataset.check_row(row)))

    def test_non_monotonic_tiers(self):
        row = make_row(tiers=[{"fpl_low_pct": 201, "fpl_high_pct": 300, "patient_pays_pct": 80},
                              {"fpl_low_pct": 301, "fpl_high_pct": 400, "patient_pays_pct": 40}])
        self.assertTrue(any(c == "tier_geometry" for _, c, _ in qa_dataset.check_row(row)))

    def test_chain_divergence(self):
        a, b = make_row(), make_row(free=175)     # same source_sha256, different policy
        flags = qa_dataset.check_chains([a, b])
        self.assertIn(qa_dataset.key(a), flags)
        self.assertTrue(any(s == "HIGH" and c == "chain" for s, c, _ in flags[qa_dataset.key(a)]))

    def test_identical_chain_ok(self):
        a, b = make_row(), make_row()             # same sha, identical policy -> no divergence
        self.assertEqual(qa_dataset.check_chains([a, b]), {})


class TestValidateGate(unittest.TestCase):
    """validate() is the gate that sets needs_review on the SERVED row — it must catch
    misinforming extractions, not just structural ones."""
    def _pol(self, **kw):
        base = {"free_care": {"fpl_ceiling_pct": 200},
                "discount_payment": {"fpl_ceiling_pct": 400, "tiers": []},
                "extraction_confidence": 0.9}
        base.update(kw)
        return base

    def test_clean_policy_no_reasons(self):
        self.assertEqual(validate(self._pol()), [])

    def test_implausible_ceiling_flagged(self):
        # 800% FPL would tell nearly every patient they qualify for free care.
        self.assertTrue(validate(self._pol(free_care={"fpl_ceiling_pct": 800})))

    def test_generous_discount_ceiling_ok(self):
        # §127405: hospitals may exceed the 400% floor; a 600% discount ceiling is legal, not a misread.
        self.assertEqual(validate(self._pol(discount_payment={"fpl_ceiling_pct": 600, "tiers": []})), [])

    def test_implausible_discount_ceiling_flagged(self):
        # A discount ceiling past the realistic max (>800%) is a units/decimal misread.
        self.assertTrue(any("out of range" in r for r in
                            validate(self._pol(discount_payment={"fpl_ceiling_pct": 900, "tiers": []}))))

    def test_truncated_flagged(self):
        self.assertTrue(any("truncat" in r for r in validate(self._pol(_truncated=True))))

    def test_uninformative_tier_flagged(self):
        pol = self._pol(discount_payment={"fpl_ceiling_pct": 400,
                        "tiers": [{"fpl_low_pct": 200, "fpl_high_pct": 400}]})  # band, no benefit
        self.assertTrue(any("benefit" in r for r in validate(pol)))


class TestEffectiveDate(unittest.TestCase):
    def test_future_flagged(self):
        self.assertIn("future", effective_date_issue("01/01/2099"))

    def test_unparseable_flagged(self):
        self.assertIn("unparseable", effective_date_issue("last tuesday"))

    def test_valid_past_ok(self):
        self.assertIsNone(effective_date_issue("05/13/2025"))

    def test_none_ok(self):
        self.assertIsNone(effective_date_issue(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
