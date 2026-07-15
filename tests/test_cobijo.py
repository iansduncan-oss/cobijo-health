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


class TestConstantsSingleSource(unittest.TestCase):
    """The FPL table + benefit year must have ONE source (constants.py). Before that, the FPL table
    lived byte-identical in two files and the PolicyEngine year was hardcoded to 2026 in three —
    a January refresh could touch one and miss another, silently mis-computing every eligibility %.
    These assert the modules share the SAME objects, so a re-introduced copy can't drift unnoticed."""

    def test_fpl_table_is_single_shared_object(self):
        import constants
        import extract_llm
        # `from constants import FPL` binds the SAME dict object — identity, not just equality, so a
        # future literal copy pasted back into navigator/extract_llm fails here even if it starts equal.
        self.assertIs(navigator.FPL, constants.FPL)
        self.assertIs(extract_llm.FPL, constants.FPL)
        self.assertIs(navigator.FPL_EACH_ADDITIONAL, constants.FPL_EACH_ADDITIONAL)
        self.assertIs(extract_llm.FPL_EACH_ADDITIONAL, constants.FPL_EACH_ADDITIONAL)

    def test_benefit_year_is_derived_not_hardcoded(self):
        import datetime
        import constants
        import policyengine
        # Guards the 2027 bug: BENEFIT_YEAR must track the calendar, and PolicyEngine must read it
        # (default year=None -> BENEFIT_YEAR), so the benefit call never silently models a stale year.
        self.assertEqual(constants.BENEFIT_YEAR, datetime.date.today().year)
        self.assertIs(policyengine.BENEFIT_YEAR, constants.BENEFIT_YEAR)


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


class TestNameIndexDisambiguation(unittest.TestCase):
    """A duplicated hospital name must not silently overwrite a sibling campus in the name index."""
    def _rows(self):
        def r(name, city, oid):
            row = make_row(); row["hospital"] = name; row["city"] = city; row["oshpdid"] = oid
            return row
        return [r("Alpha Medical Center", "Fresno", "1"),          # unique name
                r("Shared Hospital", "Eureka", "2"),               # dup name, different cities
                r("Shared Hospital", "Orange", "3")]

    def test_both_dup_campuses_addressable(self):
        rows = self._rows()
        idx = navigator._build_name_index(rows)
        self.assertIn("SHARED HOSPITAL (EUREKA)", idx)
        self.assertIn("SHARED HOSPITAL (ORANGE)", idx)
        self.assertIs(idx["SHARED HOSPITAL (EUREKA)"], rows[1])
        self.assertIs(idx["SHARED HOSPITAL (ORANGE)"], rows[2])

    def test_bare_name_resolves_to_first(self):     # back-compat: a plain lookup never breaks
        rows = self._rows()
        idx = navigator._build_name_index(rows)
        self.assertIs(idx["SHARED HOSPITAL"], rows[1])

    def test_unique_name_gets_plain_display(self):
        rows = self._rows()
        navigator._build_name_index(rows)
        self.assertEqual(rows[0]["_display"], "Alpha Medical Center")
        self.assertEqual(rows[1]["_display"], "Shared Hospital (Eureka)")

    def test_find_by_oshpdid_hits_exact_campus(self):
        rows = self._rows()
        ds = {"by_id": {r["oshpdid"]: r for r in rows}, "by_name": navigator._build_name_index(rows),
              "rows": rows}
        self.assertIs(navigator.find_hospital(ds, oshpdid="3", name="Shared Hospital"), rows[2])


class TestGenericPlan(unittest.TestCase):
    """T1.9 — a hospital not in the dataset must produce a helpful hospital-independent plan, not a
    dead-end. It invents no hospital-specific numbers (tier stays 'unknown')."""
    def setUp(self):
        self._orig = navigator.USE_POLICYENGINE
        navigator.USE_POLICYENGINE = False      # offline heuristic

    def tearDown(self):
        navigator.USE_POLICYENGINE = self._orig

    def _intake(self, **kw):
        base = {"hospital_name": "Some Clinic", "first_name": "there", "household_size": 4,
                "annual_income": 25000, "insurance": "uninsured", "in_collections": True}
        base.update(kw)
        return base

    def test_generic_shape(self):
        r = navigator.build_generic_plan_struct(self._intake(), lang="en")
        self.assertEqual(r["tier"], "unknown")
        self.assertTrue(r["not_in_directory"])
        self.assertIsNone(r["charity"]["income_ceiling"])   # no invented dollar figure
        self.assertTrue(r["benefits"])                      # benefit screen is hospital-agnostic
        self.assertEqual(len(r["debt"]), 4)                 # in_collections -> full debt defense
        self.assertIn("California", r["charity"]["message"])

    def test_generic_no_name_is_none(self):
        r = navigator.build_generic_plan_struct(self._intake(hospital_name=""), lang="en")
        self.assertIsNone(r["hospital"]["name"])

    def test_generic_letter_uses_placeholder_when_no_name(self):
        letter = navigator.generate_letter({"household_size": 4, "annual_income": 25000},
                                           {"hospital": "[Hospital name]"}, 76, "unknown")
        self.assertIn("[Hospital Name]", letter)      # .title()-cased, still a fill-in placeholder


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

    def test_high_conf_no_thresholds_is_reextract_candidate(self):
        # A confident extraction with zero income thresholds is a silent gap, not a real "no rules"
        # policy — it must be tagged for re-extraction, not just the generic validator reason.
        row = make_row(free=None, disc=None, tiers=[])   # make_row sets confidence 0.9
        findings = qa_dataset.check_row(row)
        self.assertTrue(any(c == "reextract_candidate" and s == "HIGH" for s, c, _ in findings), findings)

    def test_low_conf_no_thresholds_not_reextract_candidate(self):
        # Below the confidence bar it's already low-trust; don't double-flag it as a re-extract candidate.
        row = make_row(free=None, disc=None, tiers=[])
        row["policy"]["extraction_confidence"] = 0.3
        self.assertFalse(any(c == "reextract_candidate" for _, c, _ in qa_dataset.check_row(row)))


class TestFreshnessContent(unittest.TestCase):
    """Same URL + same effective date but a changed PDF content hash = a silent re-upload; the diff
    must surface it (URL/date-only comparison would miss it entirely)."""
    import freshness_monitor as _fm

    def _fp(self, url="u", date="01/01/2025", sha=None):
        h = {"policies": {"charity_care": {"current_policy_url": url, "current_effective_date": date},
                          "discount_payment": {}}, "post_title": "H"}
        return self._fm.fingerprint(h, content_sha=sha)

    def test_same_url_new_content_flagged(self):
        cur = {"h": self._fp(sha="aaa")}
        base = {"h": self._fp(sha="bbb")}
        _, _, changed = self._fm.diff(cur, base)
        self.assertTrue(changed and "content changed" in changed[0][2][0])

    def test_identical_content_not_flagged(self):
        cur = base = {"h": self._fp(sha="aaa")}
        self.assertEqual(self._fm.diff(cur, base), ([], [], []))

    def test_missing_sha_is_not_a_change(self):
        # An unknown hash (fetch failure / not-yet-extracted) must read as unknown, never "changed".
        cur = {"h": self._fp(sha=None)}
        base = {"h": self._fp(sha="bbb")}
        self.assertEqual(self._fm.diff(cur, base)[2], [])


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


class TestNoTruncation(unittest.TestCase):
    """T2.2 regression guard. A `_truncated` extraction means the policy corpus was clipped at
    extract_llm.MAX_CHARS, silently dropping any sliding-scale tiers / discount ceiling on the later
    PDF pages. All 72 formerly-truncated rows were re-extracted at MAX_CHARS=200_000 (covers the
    observed max corpus, 184,555 chars). Two invariants keep it that way: the served dataset must
    carry NO truncated row, and the cap must not be lowered below the observed max. If either fails,
    re-extract before shipping — see scripts/reextract_truncated.py."""

    def test_served_dataset_has_no_truncated_rows(self):
        rows = navigator.load_dataset()[0]["rows"]
        truncated = [r.get("hospital") for r in rows if (r.get("policy") or {}).get("_truncated")]
        self.assertEqual(truncated, [], f"{len(truncated)} truncated row(s) in the served dataset — "
                         f"re-extract via scripts/reextract_truncated.py: {truncated[:5]}")

    def test_max_chars_covers_observed_max_corpus(self):
        import extract_llm
        self.assertGreaterEqual(extract_llm.MAX_CHARS, 200_000,
                                "MAX_CHARS lowered below the observed max corpus (184,555) — would "
                                "silently re-truncate policies. Raise it back + re-extract.")


class TestEffectiveDate(unittest.TestCase):
    def test_future_flagged(self):
        self.assertIn("future", effective_date_issue("01/01/2099"))

    def test_unparseable_flagged(self):
        self.assertIn("unparseable", effective_date_issue("last tuesday"))

    def test_valid_past_ok(self):
        self.assertIsNone(effective_date_issue("05/13/2025"))

    def test_none_ok(self):
        self.assertIsNone(effective_date_issue(None))


class TestUnknownTier(unittest.TestCase):
    """A hospital whose income thresholds didn't extract must NOT tell an eligible patient they're
    'over the ceiling' (a max([]) -> 0% FPL artifact). It routes them to apply instead."""
    def test_no_thresholds_returns_unknown_not_over(self):
        row = make_row(free=None, disc=None, tiers=[])
        tier, msg = navigator.match_charity_care(row, pct=80, household=4, insured=False)
        self.assertEqual(tier, "unknown")
        self.assertNotIn("0% FPL", msg)      # never present a false 0% ceiling

    def test_unknown_headline_present_en_es(self):
        self.assertTrue(t("en", "result_unknown"))
        self.assertTrue(t("es", "result_unknown"))

    def test_real_over_still_matches(self):        # guard: the branch didn't shadow a genuine over-ceiling
        tier, _ = navigator.match_charity_care(make_row(free=200, disc=400), pct=999, household=4, insured=False)
        self.assertEqual(tier, "over")


class TestEffectiveDateDisplay(unittest.TestCase):
    def test_future_suppressed(self):
        self.assertIsNone(navigator.effective_date_display({"charity_effective_date": "06/25/2099"}))

    def test_past_kept(self):
        self.assertEqual(navigator.effective_date_display({"charity_effective_date": "05/13/2025"}), "05/13/2025")

    def test_missing_is_none(self):
        self.assertIsNone(navigator.effective_date_display({}))

    def test_unparseable_kept_as_is(self):
        self.assertEqual(navigator.effective_date_display({"charity_effective_date": "Spring 2024"}), "Spring 2024")


class TestLetterName(unittest.TestCase):
    """The letter is the tool's deliverable — it must never be mailed signed 'Patient' or blank."""
    def _intake(self, **kw):
        base = {"household_size": 4, "annual_income": 33000, "insurance": "uninsured", "in_collections": False}
        base.update(kw)
        return base

    def test_blank_web_name_becomes_placeholder(self):
        letter = navigator.generate_letter(self._intake(full_name="", first_name="there", last_name=""),
                                           make_row(), 100, "free")
        self.assertIn("[Your full name]", letter)
        self.assertNotIn("Patient", letter)
        self.assertNotIn("there", letter)

    def test_provided_name_used(self):
        letter = navigator.generate_letter(self._intake(full_name="Maria Lopez"), make_row(), 100, "free")
        self.assertIn("Maria Lopez", letter)

    def test_cli_first_last_still_works(self):     # CLI path has no full_name key
        letter = navigator.generate_letter(self._intake(first_name="James", last_name="Nguyen"),
                                           make_row(), 100, "free")
        self.assertIn("James Nguyen", letter)


class TestBilingualLetter(unittest.TestCase):
    """S#20: English letter auto-fills the date + optional address/phone; a non-EN patient also gets a
    translated REFERENCE copy so they can read what they're sending (the English copy is what's mailed)."""
    import datetime as _dt
    LANGS = ("en", "es", "zh", "vi", "tl", "ko", "hy", "fa", "ar", "ru")

    def _intake(self, **kw):
        base = {"full_name": "Maria Lopez", "household_size": 4, "annual_income": 38000,
                "insurance": "uninsured", "in_collections": True}
        base.update(kw)
        return base

    def test_date_auto_filled(self):
        letter = navigator.generate_letter(self._intake(), make_row(), 180, "free")
        self.assertNotIn("[today", letter)
        self.assertIn(f"{self._dt.date.today():%B}", letter)   # current month name present

    def test_address_phone_fill_and_fallback(self):
        filled = navigator.generate_letter(self._intake(address="123 Main St", phone="(530) 555-0100"),
                                           make_row(), 180, "free")
        self.assertIn("123 Main St", filled)
        self.assertNotIn("[Your address]", filled)
        blank = navigator.generate_letter(self._intake(), make_row(), 180, "free")   # empty -> bracket kept
        self.assertIn("[Your address]", blank)

    def test_reference_renders_translated_in_every_lang(self):
        for lg in self.LANGS:
            with self.subTest(lang=lg):
                ref = navigator.letter_reference(self._intake(), make_row(), 180, "free", lg)
                self.assertTrue(ref["heading"] and ref["warning"] and ref["body"])
                self.assertIn("38,000", ref["body"])                  # income token filled
                self.assertNotRegex(ref["body"], r"\{[a-z_]+\}")      # no unfilled {tokens}

    def test_reference_is_actually_translated_not_english(self):
        en = navigator.letter_reference(self._intake(), make_row(), 180, "free", "en")
        es = navigator.letter_reference(self._intake(), make_row(), 180, "free", "es")
        self.assertNotEqual(en["heading"], es["heading"])
        self.assertNotEqual(en["body"], es["body"])

    def test_ask_reflects_tier(self):
        free = navigator.letter_reference(self._intake(), make_row(), 180, "free", "es")["body"]
        disc = navigator.letter_reference(self._intake(), make_row(), 350, "discount", "es")["body"]
        self.assertNotEqual(free, disc)   # the {ask} phrase differs between free and discount


# The web i18n JSONs have NO parity guarantee elsewhere (only messages.py/sms.py do) — a dropped key
# or renamed {field} in one of 10 languages would ship silently and render literal {{...}} or crash.
import json as _json
_WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)
import i18n as web_i18n


class TestWebI18n(unittest.TestCase):
    I18N = os.path.join(_WEB, "i18n")

    def _raw(self, lang):
        with open(os.path.join(self.I18N, f"{lang}.json"), encoding="utf-8") as f:
            return _json.load(f)

    @staticmethod
    def _fields(s):
        return {n for _, n, _, _ in string.Formatter().parse(s) if n}

    def test_all_langs_have_english_keys(self):
        en = self._raw("en")
        for lang in web_i18n.LANGS:
            if lang == "en":
                continue
            data = self._raw(lang)
            for sec, keys in en.items():
                if not isinstance(keys, dict):
                    continue
                missing = set(keys) - set(data.get(sec, {}))
                self.assertEqual(missing, set(), f"{lang}.{sec} missing keys: {missing}")

    def test_format_fields_match_english(self):
        en = self._raw("en")
        for lang in web_i18n.LANGS:
            if lang == "en":
                continue
            data = self._raw(lang)
            for sec, keys in en.items():
                if not isinstance(keys, dict):
                    continue
                for k, v in keys.items():
                    other = data.get(sec, {}).get(k)
                    if not isinstance(v, str) or not other:
                        continue          # missing keys are caught above; runtime falls back to en
                    self.assertEqual(self._fields(v), self._fields(other),
                                     f"{lang}.{sec}.{k}: format fields differ")

    def test_every_page_renders_without_placeholders(self):
        import re
        for page in web_i18n.PAGES:
            for lang in web_i18n.LANGS:
                html = web_i18n.render(page, lang)
                left = re.findall(r"{{[A-Za-z_]+}}", html)
                self.assertEqual(left, [], f"{page}/{lang} leftover placeholders: {set(left)}")


class TestPrintHandout(unittest.TestCase):
    """The results screen must ship the scoped print-handout CSS + toggle so a printed plan is a clean
    one-page action sheet, not a raw dump of the whole page (form/nav/dark-on-dark cards)."""

    def _home(self, lang="en"):
        return web_i18n.render("home", lang)

    def test_print_css_and_toggle_present(self):
        html = self._home()
        self.assertIn("@media print", html)                 # a print stylesheet exists at all
        self.assertIn("printing-result", html)              # scoped so the blank form print is untouched
        self.assertIn("addEventListener(\"beforeprint\"", html)   # flips into handout mode
        self.assertIn("addEventListener(\"afterprint\"", html)    # restores the on-screen state

    def test_handout_flattens_dark_hero_for_no_background_print(self):
        # The dark hero/step cards must be forced to a light background so white text isn't invisible
        # when the browser's "print background graphics" is off.
        html = self._home()
        self.assertIn("body.printing-result .hero", html)
        self.assertIn("body.printing-result .rcard-h .n", html)

    def test_handout_present_in_every_language(self):
        # Reuses translated strings (zero new i18n keys), so the handout must exist in all langs + RTL.
        for lang in web_i18n.LANGS:
            self.assertIn("printing-result", self._home(lang), f"{lang}: print handout missing")


class TestHospitalPagesI18n(unittest.TestCase):
    """The localized per-hospital SEO pages (T3.1) must render in every language with the right <html
    lang>/dir and no unfilled {token}s (a dropped placeholder would print '{name}' to a patient)."""
    import re as _re
    if _WEB not in sys.path:
        sys.path.insert(0, _WEB)
    import hospital_pages as _hp

    def _row(self):
        r = make_row(); r["hospital"] = "Alpha Regional Medical Center"; r["city"] = "Fresno"
        r["county"] = "Fresno"; r["oshpdid"] = "12345"
        r["charity_policy_url"] = "https://example.org/policy.pdf"
        r["charity_effective_date"] = "05/13/2025"
        return r

    def test_renders_all_langs_no_tokens(self):
        idx, _ = self._hp.build_index([self._row()])
        slug = next(iter(idx))
        for lang, (_, direction) in web_i18n.LANGS.items():
            html = self._hp.render_hospital(idx[slug], slug, idx, lang)
            self.assertIn(f'lang="{lang}"', html)
            self.assertIn(f'dir="{direction}"', html)
            left = self._re.findall(r"\{(?:name|pct|date|county)\}", html)
            self.assertEqual(left, [], f"{lang}: unfilled tokens {set(left)}")
            self.assertIn(f'hreflang="{lang}"', html)         # reciprocal alternates present

    def test_directory_renders_all_langs(self):
        idx, _ = self._hp.build_index([self._row()])
        for lang in web_i18n.LANGS:
            html = self._hp.render_directory(idx, lang)
            self.assertIn(f'lang="{lang}"', html)
            self.assertNotIn("{county}", html)

    def test_faq_jsonld_english_only(self):     # avoid an English FAQ on a translated page
        idx, _ = self._hp.build_index([self._row()])
        slug = next(iter(idx))
        self.assertIn("FAQPage", self._hp.render_hospital(idx[slug], slug, idx, "en"))
        self.assertNotIn("FAQPage", self._hp.render_hospital(idx[slug], slug, idx, "es"))

    def test_jsonld_is_valid_json(self):        # a double-encoded URL once broke BreadcrumbList silently
        import json, re
        idx, _ = self._hp.build_index([self._row()])
        slug = next(iter(idx))
        for lang in web_i18n.LANGS:
            html = self._hp.render_hospital(idx[slug], slug, idx, lang)
            blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
            self.assertTrue(blocks, f"{lang}: no JSON-LD emitted")
            for b in blocks:
                json.loads(b)                # raises on the malformed double-quoted URL regression


class TestOgImages(unittest.TestCase):
    """Per-page OG share cards (T3.4): pages must reference a card that actually exists on disk, and
    the generated set must cover every hospital + county slug (a missing PNG = a broken social preview)."""
    if _WEB not in sys.path:
        sys.path.insert(0, _WEB)
    import hospital_pages as _hp
    _OG = os.path.join(_WEB, "og")

    def test_hospital_og_points_to_existing_card(self):
        ds = navigator.load_dataset()[0]
        idx, _ = self._hp.build_index(ds["rows"])
        slug = next(iter(idx))
        html = self._hp.render_hospital(idx[slug], slug, idx, "en")
        self.assertIn(f'og:image" content="{self._hp.BASE}/og/hospital/{slug}.png"', html)
        self.assertTrue(os.path.isfile(os.path.join(self._OG, "hospital", slug + ".png")))

    def test_county_og_points_to_existing_card(self):
        ds = navigator.load_dataset()[0]
        idx, _ = self._hp.build_index(ds["rows"])
        cslug, county = next(iter(self._hp.county_index(idx).items()))
        html = self._hp.render_county(county, idx, "en")
        self.assertIn(f'og:image" content="{self._hp.BASE}/og/county/{cslug}.png"', html)
        self.assertTrue(os.path.isfile(os.path.join(self._OG, "county", cslug + ".png")))

    def test_every_hospital_and_county_slug_has_a_card(self):
        ds = navigator.load_dataset()[0]
        idx, _ = self._hp.build_index(ds["rows"])
        missing = [s for s in idx if not os.path.isfile(os.path.join(self._OG, "hospital", s + ".png"))]
        missing += ["county:" + c for c in self._hp.county_index(idx)
                    if not os.path.isfile(os.path.join(self._OG, "county", c + ".png"))]
        self.assertEqual(missing, [], f"missing OG cards (re-run scripts/gen_og_images.py): {missing[:5]}")

    def test_guide_cards_exist(self):
        # Per-guide OG cards: each guide shares with its OWN title card; render must point at it.
        for slug in web_i18n.GUIDES:
            self.assertTrue(os.path.isfile(os.path.join(self._OG, "guide", slug + ".png")),
                            f"missing guide OG card (re-run scripts/gen_og_images.py): {slug}")
            self.assertIn(f"/og/guide/{slug}.png", web_i18n.render_guide(slug, "en"))
        self.assertTrue(os.path.isfile(os.path.join(self._OG, "guide.png")))  # shared fallback kept


class TestQrCodes(unittest.TestCase):
    """Print-handout QR codes (scripts/gen_qr_codes.py): every hospital/county/guide print view
    references a QR that exists on disk, the server serves it as SVG, and the static handler is
    contained to web/qr/ (a crafted ../ can't escape to another file)."""
    if _WEB not in sys.path:
        sys.path.insert(0, _WEB)
    import hospital_pages as _hp
    _QR = os.path.join(_WEB, "qr")

    def test_every_slug_has_a_qr(self):
        ds = navigator.load_dataset()[0]
        idx, _ = self._hp.build_index(ds["rows"])
        missing = [f"hospital/{s}" for s in idx
                   if not os.path.isfile(os.path.join(self._QR, "hospital", s + ".svg"))]
        missing += [f"county/{c}" for c in self._hp.county_index(idx)
                    if not os.path.isfile(os.path.join(self._QR, "county", c + ".svg"))]
        missing += [f"guide/{g}" for g in web_i18n.GUIDES
                    if not os.path.isfile(os.path.join(self._QR, "guide", g + ".svg"))]
        self.assertEqual(missing, [], f"missing QR codes (re-run scripts/gen_qr_codes.py): {missing[:5]}")

    def test_pages_reference_their_qr(self):
        ds = navigator.load_dataset()[0]
        idx, _ = self._hp.build_index(ds["rows"])
        slug = next(iter(idx))
        self.assertIn(f"/qr/hospital/{slug}.svg", self._hp.render_hospital(idx[slug], slug, idx, "en"))
        cslug, county = next(iter(self._hp.county_index(idx).items()))
        self.assertIn(f"/qr/county/{cslug}.svg", self._hp.render_county(county, idx, "en"))
        g = next(iter(web_i18n.GUIDES))
        self.assertIn(f"/qr/guide/{g}.svg", web_i18n.render_guide(g, "en"))

    def test_server_serves_qr_and_blocks_traversal(self):
        import http.server
        import threading
        import server as _srv
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            slug = next(iter(self._hp.build_index(navigator.load_dataset()[0]["rows"])[0]))
            status, ctype, body = self._raw(port, f"/qr/hospital/{slug}.svg")
            self.assertEqual(status, 200)
            self.assertIn("image/svg+xml", ctype)
            self.assertIn(b"<svg", body)
            self.assertEqual(self._raw(port, "/qr/does-not-exist.svg")[0], 404)
            # ../favicon.svg is a REAL file OUTSIDE web/qr/ — containment must refuse to serve it.
            self.assertEqual(self._raw(port, "/qr/../favicon.svg")[0], 404,
                             "path traversal escaped web/qr/!")
        finally:
            httpd.shutdown()

    @staticmethod
    def _raw(port, path):
        # Raw socket so the literal '..' in the request line isn't normalized by an HTTP client.
        import socket
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        status = int(lines[0].split()[1])
        ctype = next((ln.split(":", 1)[1].strip() for ln in lines[1:]
                      if ln.lower().startswith("content-type")), "")
        return status, ctype, body


class TestCountyHubs(unittest.TestCase):
    """Per-county hub pages (T3.3): localized in every language, valid JSON-LD, correct <html lang>/dir,
    and they list every hospital in the county (the additive crawl surface + internal links)."""
    import re as _re
    if _WEB not in sys.path:
        sys.path.insert(0, _WEB)
    import hospital_pages as _hp

    def _rows(self):
        a = make_row(); a["hospital"] = "Alpha Regional Medical Center"; a["city"] = "Fresno"
        a["county"] = "Fresno"; a["oshpdid"] = "1"
        b = make_row(); b["hospital"] = "Beta Community Hospital"; b["city"] = "Clovis"
        b["county"] = "Fresno"; b["oshpdid"] = "2"
        return [a, b]

    def test_county_index_and_paths(self):
        idx, _ = self._hp.build_index(self._rows())
        ci = self._hp.county_index(idx)
        self.assertEqual(set(ci), {"fresno"})
        self.assertEqual(ci["fresno"], "Fresno")
        self.assertEqual(len(self._hp.county_paths(idx)), len(ci) * len(web_i18n.LANGS))

    def test_renders_all_langs_lists_hospitals(self):
        idx, _ = self._hp.build_index(self._rows())
        for lang, (_, direction) in web_i18n.LANGS.items():
            html = self._hp.render_county("Fresno", idx, lang)
            self.assertIn(f'lang="{lang}"', html)
            self.assertIn(f'dir="{direction}"', html)
            self.assertEqual(self._re.findall(r"\{county\}", html), [], f"{lang}: unfilled {{county}}")
            self.assertIn(f'hreflang="{lang}"', html)
            self.assertIn("Alpha Regional Medical Center", html)   # every hospital in the county is linked
            self.assertIn("Beta Community Hospital", html)

    def test_jsonld_is_valid_json(self):
        import json, re
        idx, _ = self._hp.build_index(self._rows())
        for lang in web_i18n.LANGS:
            html = self._hp.render_county("Fresno", idx, lang)
            blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
            self.assertTrue(blocks, f"{lang}: no JSON-LD emitted")
            for b in blocks:
                json.loads(b)


class TestGuides(unittest.TestCase):
    """Evergreen explainer guides (T3.3): render in every language with no unfilled {{placeholder}},
    valid JSON-LD, reciprocal hreflang, and an unknown slug is a clean miss (404-able)."""
    import re as _re

    def test_renders_all_langs_no_placeholders(self):
        for slug in web_i18n.GUIDES:
            for lang, (_, direction) in web_i18n.LANGS.items():
                html = web_i18n.render_guide(slug, lang)
                self.assertIsNotNone(html, f"{slug}/{lang} returned None")
                self.assertIn(f'lang="{lang}"', html)
                self.assertIn(f'dir="{direction}"', html)
                left = self._re.findall(r"\{\{[A-Za-z_]+\}\}", html)
                self.assertEqual(left, [], f"{slug}/{lang} leftover placeholders: {set(left)}")
                self.assertIn(f'hreflang="{lang}"', html)

    def test_jsonld_is_valid_json(self):
        import json, re
        for slug in web_i18n.GUIDES:
            for lang in web_i18n.LANGS:
                html = web_i18n.render_guide(slug, lang)
                blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
                self.assertTrue(blocks, f"{slug}/{lang}: no JSON-LD emitted")
                for b in blocks:
                    json.loads(b)

    def test_unknown_guide_returns_none(self):
        self.assertIsNone(web_i18n.render_guide("does-not-exist", "en"))

    def test_guide_paths_count(self):
        self.assertEqual(len(web_i18n.guide_paths()), len(web_i18n.GUIDES) * len(web_i18n.LANGS))


class TestSupportPage(unittest.TestCase):
    """The /support page (config-driven giving). The Donate button appears ONLY when a giving URL is
    live (SUPPORT_URL); until then the page shows the honest 'giving opens soon' note + non-monetary
    ways to help. Every language renders, and the page is in the sitemap + reachable over HTTP."""

    def test_support_is_a_registered_page(self):
        self.assertIn("support", web_i18n.PAGES)

    def test_renders_all_langs_no_placeholders(self):
        import re
        for lang in web_i18n.LANGS:
            html = web_i18n.render("support", lang)
            self.assertIn(f'lang="{lang}"', html)
            self.assertEqual(re.findall(r"{{[A-Za-z_]+}}", html), [],
                             f"support/{lang} has leftover placeholders")

    def test_donate_button_hidden_when_no_url(self):
        # Default config: no giving path live -> no Donate button, honest 'soon' note instead.
        original = web_i18n.SUPPORT_URL
        web_i18n.SUPPORT_URL = ""
        try:
            html = web_i18n.render("support", "en")
            self.assertNotIn(">Donate</a>", html)
            self.assertIn(web_i18n.strings("en", "support")["give_soon"], html)
            self.assertNotIn('href=""', html)          # never emit an empty-target button
        finally:
            web_i18n.SUPPORT_URL = original

    def test_donate_button_shown_when_url_set(self):
        original = web_i18n.SUPPORT_URL
        web_i18n.SUPPORT_URL = "https://example.org/give"
        try:
            html = web_i18n.render("support", "en")
            self.assertIn('href="https://example.org/give"', html)
            self.assertIn(">Donate</a>", html)
            self.assertIn('rel="noopener"', html)       # external giving link is hardened
            self.assertNotIn(web_i18n.strings("en", "support")["give_soon"], html)
        finally:
            web_i18n.SUPPORT_URL = original

    def test_english_has_no_machine_translation_banner(self):
        self.assertNotIn(web_i18n.strings("en", "support")["mt_note"],
                         web_i18n.render("support", "en"))

    def test_translated_page_shows_banner_and_translated_copy(self):
        # es is fully translated -> the 'machine-assisted, under review' banner should show, and the
        # copy must be the Spanish h1 (not English fallback).
        html = web_i18n.render("support", "es")
        self.assertIn(web_i18n.strings("es", "support")["h1"], html)
        self.assertIn(web_i18n.strings("es", "common")["mt_note"], html)

    def test_support_in_sitemap_every_language(self):
        paths = web_i18n.sitemap_paths()
        for lang in web_i18n.LANGS:
            self.assertIn(web_i18n.url(lang, "support"), paths)

    def test_footer_links_to_support(self):
        # Every localized chrome page (and a guide) should expose the Support footer link.
        label = web_i18n.strings("en", "common")["f_support"]          # "Support"
        for page in ("home", "about", "faq", "privacy", "landing"):
            self.assertIn(">%s<" % label, web_i18n.render(page, "en"),
                          f"{page} footer missing Support link")
        self.assertIn("/support", web_i18n.render_guide(next(iter(web_i18n.GUIDES)), "en"))

    def test_server_routes_support_en_and_localized(self):
        import http.server, threading, socket
        if _WEB not in sys.path:
            sys.path.insert(0, _WEB)
        import server as _srv
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def fetch(path):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            s.close()
            head, _, body = buf.partition(b"\r\n\r\n")
            status = int(head.decode("latin1").split("\r\n")[0].split()[1])
            return status, body

        try:
            st, body = fetch("/support")
            self.assertEqual(st, 200)
            self.assertIn(b"Support this work", body)
            st_es, body_es = fetch("/es/support")
            self.assertEqual(st_es, 200)
            self.assertIn(web_i18n.strings("es", "support")["h1"].encode("utf-8"), body_es)
        finally:
            httpd.shutdown()


class TestHelpResources(unittest.TestCase):
    """The 'where to get help' routing (resources.py + navigator.help_leads). Which door shows is a
    pure function of the patient's signals; every door is a real https destination; labels + heading
    localize in all 10 languages; and the routes ride on BOTH the web struct and the CLI text so they
    can never drift."""

    import resources as _res

    def _ids(self, pct, insurance, in_collections):
        return [r["id"] for r in self._res.help_resources(pct, insurance, in_collections)]

    def test_routing_by_patient_signals(self):
        self.assertEqual(self._ids(90, "uninsured", False), ["medical", "clinic"])
        self.assertEqual(self._ids(160, "uninsured", False), ["medical", "coveredca", "clinic"])
        self.assertEqual(self._ids(300, "uninsured", False), ["coveredca", "clinic"])
        self.assertEqual(self._ids(300, "uninsured", True), ["coveredca", "clinic", "legalaid"])

    def test_insured_not_in_collections_gets_no_routes(self):
        # An insured patient who isn't being chased for a bill shouldn't see a link dump.
        self.assertEqual(self._ids(90, "aetna", False), [])

    def test_insured_in_collections_gets_legal_aid_only(self):
        self.assertEqual(self._ids(90, "aetna", True), ["legalaid"])

    def test_blank_and_selfpay_count_as_uninsured(self):
        for label in ("", None, "none", "self-pay", "UNINSURED"):
            self.assertIn("clinic", self._ids(90, label, False), f"{label!r} should route to a clinic")

    def test_every_destination_is_https_and_labeled(self):
        for r in self._res.RESOURCES:
            self.assertTrue(r["url"].startswith("https://"), f"{r['id']} url not https")
            self.assertIn(r["label"], MESSAGES["en"], f"{r['id']} label key missing from catalog")
            if r["phone"]:
                self.assertRegex(r["phone"], r"[0-9]", f"{r['id']} phone has no digits")

    def test_labels_and_heading_translated_in_every_language(self):
        keys = ["res_heading"] + [r["label"] for r in self._res.RESOURCES]
        for lang in navigator_langs():
            for k in keys:
                self.assertIn(k, MESSAGES[lang], f"{lang} catalog missing {k} (would fall back to English)")
                self.assertTrue(MESSAGES[lang][k].strip(), f"{lang}.{k} is empty")

    def test_struct_carries_resolved_routes(self):
        intake = {"first_name": "Maria", "household_size": 3, "annual_income": 20000,
                  "insurance": "uninsured", "in_collections": True}
        g = navigator.build_generic_plan_struct(intake, lang="en")
        self.assertTrue(g["resources"], "generic plan should route an uninsured patient somewhere")
        self.assertTrue(g["res_heading"])
        labels = [x["label"] for x in g["resources"]]
        self.assertTrue(any("Medi-Cal" in l for l in labels))
        self.assertTrue(any("legal help" in l for l in labels))
        for x in g["resources"]:
            self.assertTrue(x["url"].startswith("https://"))
            self.assertIn("label", x)

    def test_cli_text_includes_routes(self):
        # Web/CLI parity: the same routes ride on the text plan too (url + staffed phone line).
        DS, _ = navigator.load_dataset()
        intake = {"first_name": "J", "household_size": 1, "annual_income": 12000,
                  "insurance": "uninsured", "in_collections": True}
        _, _, text = navigator.build_plan(intake, DS["rows"][0], lang="en")
        self.assertIn("benefitscal.com", text)
        self.assertIn("(888) 804-3536", text)


def navigator_langs():
    """The languages the plan catalog claims to support (en/es inline + the 8 loaded from web i18n)."""
    return list(MESSAGES.keys())


class TestEmbedWidget(unittest.TestCase):
    """The embeddable iframe widget (/embed + /embed.js). The embed render must strip chrome and be
    framable by any partner site, while the normal homepage stays clickjacking-protected and byte-for-
    byte unchanged. Served over the real handler so the header overrides are exercised."""

    def _server(self):
        import http.server, threading
        if _WEB not in sys.path:
            sys.path.insert(0, _WEB)
        import server as _srv
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd, httpd.server_address[1]

    @staticmethod
    def _get(port, path):
        import socket
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        lines = head.decode("latin1").split("\r\n")
        status = int(lines[0].split()[1])
        hd = {ln.split(":", 1)[0].lower(): ln.split(":", 1)[1].strip() for ln in lines[1:] if ":" in ln}
        return status, hd, body

    def test_normal_home_unchanged_by_embed_hooks(self):
        html = web_i18n.render("home", "en")
        for tok in ("{{BODY_CLASS}}", "{{EMBED_HEAD}}", "{{EMBED_SCRIPT}}"):
            self.assertNotIn(tok, html, f"{tok} left unfilled on normal home")
        self.assertIn('<body class="">', html)
        self.assertNotIn("noindex", html)           # the homepage must stay indexable
        self.assertNotIn("parent.postMessage", html)

    def test_embed_render_strips_chrome_and_isolates(self):
        e = web_i18n.render("home", "en", embed=True)
        self.assertIn('<body class="embed">', e)
        self.assertIn("noindex", e)                  # embed must NOT be indexed (homepage is canonical)
        self.assertIn("parent.postMessage", e)       # auto-resize reporter present
        self.assertIn('class="attribution"', e)      # growth loop back to the full site

    def test_embed_localizes(self):
        es = web_i18n.render("home", "es", embed=True)
        self.assertIn('lang="es"', es)
        self.assertIn('<body class="embed">', es)

    def test_home_is_clickjacking_protected(self):
        httpd, port = self._server()
        try:
            st, hd, _ = self._get(port, "/")
            self.assertEqual(st, 200)
            self.assertEqual(hd.get("x-frame-options"), "DENY")
            self.assertIn("frame-ancestors 'none'", hd.get("content-security-policy", ""))
        finally:
            httpd.shutdown()

    def test_embed_is_framable_anywhere(self):
        httpd, port = self._server()
        try:
            for path in ("/embed", "/es/embed"):
                st, hd, body = self._get(port, path)
                self.assertEqual(st, 200, path)
                self.assertNotIn("x-frame-options", hd, f"{path} still blocks framing")
                self.assertIn("frame-ancestors *", hd.get("content-security-policy", ""), path)
                self.assertIn(b'class="embed"', body)
        finally:
            httpd.shutdown()

    def test_embed_js_served_as_script(self):
        httpd, port = self._server()
        try:
            st, hd, body = self._get(port, "/embed.js")
            self.assertEqual(st, 200)
            self.assertIn("javascript", hd.get("content-type", ""))
            self.assertIn(b"iframe", body)
            self.assertIn(b"/embed", body)
            self.assertIn(b"postMessage", body)      # host-side resize listener
        finally:
            httpd.shutdown()


class TestPartnersPage(unittest.TestCase):
    """The /for-partners page: the discoverable home for the embed snippet + a live preview. Renders in
    every language, is in the sitemap, reachable over HTTP, and links back into the footer nav."""

    def test_registered_and_rendered(self):
        self.assertIn("for-partners", web_i18n.PAGES)
        import re
        for lang in web_i18n.LANGS:
            html = web_i18n.render("for-partners", lang)
            self.assertIn(f'lang="{lang}"', html)
            self.assertEqual(re.findall(r"{{[A-Za-z_]+}}", html), [], f"for-partners/{lang} leftovers")

    def test_carries_snippet_and_live_preview(self):
        html = web_i18n.render("for-partners", "en")
        self.assertIn("embed.js", html)                 # the copy-paste snippet
        self.assertIn("cobijo-widget", html)            # the live preview mount
        self.assertIn("Put free medical-bill help", html)

    def test_snippet_localizes_data_lang(self):
        self.assertIn('data-lang="es"', web_i18n.render("for-partners", "es"))

    def test_in_sitemap_every_language_but_not_embed(self):
        sm = web_i18n.sitemap_paths()
        for lang in web_i18n.LANGS:
            self.assertIn(web_i18n.url(lang, "for-partners"), sm)
        self.assertFalse(any("embed" in u for u in sm), "the noindex /embed must stay out of the sitemap")

    def test_footer_link_present_across_pages(self):
        label = web_i18n.strings("en", "common")["f_partners"]
        for page in ("home", "about", "faq", "privacy", "landing", "support"):
            self.assertIn(">%s<" % label, web_i18n.render(page, "en"), f"{page} footer missing partners link")
        self.assertIn("/for-partners", web_i18n.render_guide(next(iter(web_i18n.GUIDES)), "en"))

    def test_server_routes_localized(self):
        import http.server, threading, socket
        if _WEB not in sys.path:
            sys.path.insert(0, _WEB)
        import server as _srv
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.Handler)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()

        def fetch(path):
            s = socket.create_connection(("127.0.0.1", port), timeout=5)
            s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
            s.close()
            return int(buf.split(b"\r\n", 1)[0].split()[1]), buf

        try:
            st, body = fetch("/for-partners")
            self.assertEqual(st, 200)
            self.assertIn(b"embed.js", body)
            self.assertEqual(fetch("/es/for-partners")[0], 200)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
