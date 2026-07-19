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
import json
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


class TestStatutoryPlanStruct(unittest.TestCase):
    """The statute-driven plan renders the same struct shape as build_plan_struct, deriving eligibility
    from the law (no per-hospital FAP), and the letter cites the state's own act (CA stays byte-identical)."""

    def _intake(self, income, **kw):
        base = {"first_name": "there", "full_name": "Maria Lopez", "household_size": 2,
                "annual_income": income, "insurance": "uninsured", "in_collections": True}
        base.update(kw)
        return base

    def _il_row(self):
        return {"hospital": "ADVOCATE CHRIST", "state": "IL", "hospital_type": "Acute Care Hospitals",
                "phone": "(708) 555-0100"}

    def test_struct_shape_and_law_cited(self):
        p = navigator.build_statutory_plan_struct(self._intake(45000), self._il_row(), "en")
        for k in ("fpl_pct", "tier", "headline", "hospital", "charity", "debt", "closing"):
            self.assertIn(k, p)
        self.assertTrue(p["statutory"])
        self.assertEqual(p["tier"], "discount")                 # ~225% FPL, in 200–600 band
        self.assertIn("210 ILCS 89", p["charity"]["message"])
        self.assertNotRegex(p["charity"]["message"], r"\{[a-z_]+\}")   # no unfilled tokens
        self.assertEqual(p["benefits"], [])                     # CA-only screening omitted off-CA
        self.assertEqual(p["hospital"]["phone"], "(708) 555-0100")

    def test_tiers_pick_the_right_message(self):
        free = navigator.build_statutory_plan_struct(self._intake(18000), self._il_row(), "en")
        over = navigator.build_statutory_plan_struct(self._intake(130000), self._il_row(), "en")
        self.assertEqual(free["tier"], "free")
        self.assertEqual(over["tier"], "over")
        self.assertNotEqual(free["charity"]["message"], over["charity"]["message"])

    def test_letter_cites_state_law_ca_byte_identical(self):
        ca = navigator.generate_letter(self._intake(30000), {"hospital": "ADVENTIST HEALTH"}, 180, "free")
        self.assertIn("California's Hospital Fair Pricing Act and", ca)   # unchanged
        il = navigator.generate_letter(self._intake(30000), self._il_row(), 180, "free")
        self.assertIn("210 ILCS 89", il)
        self.assertNotIn("California's Hospital Fair Pricing Act", il)


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


class TestLetterReferenceLaw(unittest.TestCase):
    """The translated reference letter (letter_reference) parameterizes the governing law via a {law}
    token: CA stays byte-identical (short act name kept in English inside the translation), a statute-
    driven state cites its own act. Guards against a regression that would either hardcode CA back in or
    leave a {law} token unfilled on a non-EN page."""

    _CA_LAW = "California's Hospital Fair Pricing Act"
    _IL_LAW = "210 ILCS 89"
    _LANGS = ["en", "es", "zh", "ar", "ru", "vi", "ko", "fa", "hy", "tl"]

    def _intake(self, income=18000):
        return {"first_name": "Maria", "full_name": "Maria Lopez", "household_size": 4,
                "annual_income": income, "insurance": "uninsured", "in_collections": True}

    def test_ca_reference_still_cites_ca_act_in_every_language(self):
        ca_row = {"hospital": "ADVENTIST HEALTH", "state": "CA"}
        for lang in self._LANGS:
            body = navigator.letter_reference(self._intake(), ca_row, 75.0, "free", lang)["body"]
            self.assertIn(self._CA_LAW, body, f"{lang}: CA reference dropped the CA act name")
            self.assertNotRegex(body, r"\{law\}", f"{lang}: unfilled {{law}} token")

    def test_il_reference_cites_il_act_not_ca(self):
        il_row = {"hospital": "ADVOCATE CHRIST", "state": "IL", "hospital_type": "Acute Care Hospitals"}
        for lang in self._LANGS:
            body = navigator.letter_reference(self._intake(), il_row, 75.0, "free", lang)["body"]
            self.assertIn(self._IL_LAW, body, f"{lang}: IL reference didn't cite the IL act")
            self.assertNotIn(self._CA_LAW, body, f"{lang}: IL reference wrongly cited the CA act")
            self.assertNotRegex(body, r"\{law\}", f"{lang}: unfilled {{law}} token")


class TestServerILPlan(unittest.TestCase):
    """End-to-end wiring (T4.1 Phase 2 increment 2b): an IL patient reaches the statute-driven plan via
    the `il=<ccn>` hint the IL SEO pages send, gets a correct law-based answer + a letter citing the IL
    act, and IL is deliberately kept OUT of the CA autocomplete so a cross-state name can't misresolve."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        web = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
        if web not in sys.path:
            sys.path.insert(0, web)
        import server as _srv
        cls.srv = _srv
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        # A real metro (acute-care) IL CCN from the loaded roster.
        cls.ccn = next(c for c, r in _srv.IL_BY_CCN.items()
                       if r.get("hospital_type") == "Acute Care Hospitals")

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _post(self, payload):
        import socket
        body = json.dumps(payload).encode()
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"POST /plan HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                  b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" % (len(body), body))
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, resp = buf.partition(b"\r\n\r\n")
        status = int(head.decode("latin1").split("\r\n")[0].split()[1])
        return status, (json.loads(resp) if resp else None)

    def test_il_ccn_resolves_to_statutory_plan(self):
        st, d = self._post({"st": "IL", "sid": self.ccn, "income": 18000, "household": 4,
                            "insurance": "uninsured", "lang": "en", "full_name": "Maria Lopez"})
        self.assertEqual(st, 200)
        self.assertEqual(d["tier"], "free")                        # 18k / 4 is well under 200% FPL
        self.assertTrue(d["result"]["statutory"])
        self.assertIn("210 ILCS 89", d["result"]["charity"]["message"])
        self.assertIn("210 ILCS 89", d["letter"])                  # the English letter cites the IL act
        self.assertIsNone(d["plan"])                               # CLI text unused by the web UI

    def test_il_plan_localizes_and_reference_cites_il_law(self):
        st, d = self._post({"st": "IL", "sid": self.ccn, "income": 18000, "household": 4,
                            "insurance": "uninsured", "lang": "es", "full_name": "Maria Lopez"})
        self.assertEqual(st, 200)
        self.assertIn("210 ILCS 89", d["result"]["charity"]["message"])
        self.assertTrue(d["letter_ref"], "a non-EN IL plan should carry a translated reference letter")
        self.assertIn("210 ILCS 89", d["letter_ref"]["body"])

    def test_invalid_il_ccn_is_404(self):
        st, _ = self._post({"st": "IL", "sid": "000000", "income": 18000, "household": 4})
        self.assertEqual(st, 404)

    def test_statutory_plan_carries_min_note_and_resources(self):
        # M2: the free/discount plan reuses the page's "legal minimum" caveat (so the floor isn't mistaken
        # for the hospital's actual policy). H3: the plan routes to real doors, not a resources=[] dead end.
        st, d = self._post({"st": "IL", "sid": self.ccn, "income": 18000, "household": 4,
                            "insurance": "uninsured", "in_collections": True, "lang": "en"})
        self.assertEqual(st, 200)
        note = d["result"]["charity"].get("min_note")
        self.assertTrue(note and "legal minimum" in note.lower(), "M2 caveat missing from free-tier plan")
        ids = [r["url"] for r in d["result"]["resources"]]
        self.assertTrue(any("abe.illinois.gov" in u for u in ids), "H3 IL coverage door missing")
        self.assertTrue(any("illinoislegalaid.org" in u for u in ids), "H3 IL legal-aid door missing")

    def test_over_tier_plan_omits_min_note(self):
        # The "legal minimum" caveat only makes sense where a guarantee was asserted; an over-ceiling
        # patient (no guaranteed tier) shouldn't be told about a minimum that didn't apply to them.
        st, d = self._post({"st": "IL", "sid": self.ccn, "income": 900000, "household": 1,
                            "insurance": "uninsured", "lang": "en"})
        self.assertEqual(st, 200)
        self.assertEqual(d["result"]["tier"], "over")
        self.assertIsNone(d["result"]["charity"].get("min_note"))

    def test_il_hospitals_stay_out_of_ca_autocomplete(self):
        # Launch decision: IL reachable only via its own pages, so the CA datalist must not carry it.
        il_names = {r["hospital"].title() for r in self.srv.IL_BY_CCN.values()}
        self.assertTrue(self.srv.IL_BY_CCN, "IL roster failed to load")
        self.assertFalse(il_names & set(self.srv.HOSPITALS),
                         "an IL hospital leaked into the CA autocomplete")

    def test_healthz_counts_ca_only(self):
        # IL is a separate statute-driven roster; it must not inflate the CA readiness count.
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"GET /healthz HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        payload = json.loads(buf.partition(b"\r\n\r\n")[2])
        self.assertEqual(payload["hospitals"], len(self.srv.DS["rows"]))       # CA count only
        self.assertGreater(len(self.srv.IL_BY_CCN), 0)                          # IL loaded separately
        self.assertLess(payload["hospitals"], len(self.srv.DS["rows"]) + len(self.srv.IL_BY_CCN))


class TestILSeoPages(unittest.TestCase):
    """The statute-driven IL SEO pages (T4.1 Phase 2 increment 2c): cite IL law + the statutory bands
    (rural-adjusted), route into the tool via ?il=<ccn>, live under the /il/ namespace, and leave every
    CA URL byte-identical. Rendered directly (no server)."""
    import hospital_pages as _hp

    def _il_rows(self):
        return [
            {"hospital": "ADVOCATE CHRIST HOSPITAL", "ccn": "140208", "city": "OAK LAWN",
             "county": "Cook", "state": "IL", "phone": "(708) 684-8000",
             "hospital_type": "Acute Care Hospitals", "status": "statutory", "policy": None},
            {"hospital": "ABRAHAM LINCOLN MEMORIAL HOSPITAL", "ccn": "141322", "city": "LINCOLN",
             "county": "Logan", "state": "IL", "phone": "(217) 732-2161",
             "hospital_type": "Critical Access Hospitals", "status": "statutory", "policy": None},
        ]

    def _idx(self):
        return self._hp.build_index(self._il_rows())[0]

    def test_metro_hospital_cites_law_bands_cta_no_unfilled_tokens(self):
        idx = self._idx()
        slug = next(s for s, r in idx.items() if r["ccn"] == "140208")
        for lang in ("en", "es", "zh", "ar"):
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            self.assertIn("210 ILCS 89", html, f"{lang}: IL act not cited")
            self.assertIn("?st=IL", html, f"{lang}: tool CTA missing the state code")
            self.assertIn("sid=140208", html, f"{lang}: tool CTA missing the CCN")
            self.assertIn(self._hp._url(lang, slug, "hospital", "IL"), html, f"{lang}: /il/ canonical missing")
            self.assertIn("200", html, f"{lang}: metro free band (200% FPL) missing")
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on the page")

    def test_rural_hospital_uses_lower_bands(self):
        idx = self._idx()
        slug = next(s for s, r in idx.items() if r["ccn"] == "141322")
        html = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("125", html)                          # rural free band
        self.assertIn("300", html)                          # rural discount band
        self.assertNotRegex(html, r"\{[a-z_]+\}")

    def test_county_and_directory_render(self):
        idx = self._idx()
        county = self._hp.render_statutory_county("Cook", idx, "en", "IL")
        self.assertIn("210 ILCS 89", county)
        self.assertIn("Advocate Christ", county)            # the county's hospital is listed
        self.assertNotRegex(county, r"\{[a-z_]+\}")
        directory = self._hp.render_statutory_directory(idx, "en", "IL")
        self.assertIn("Illinois", directory)
        self.assertIn("Cook", directory)
        self.assertNotRegex(directory, r"\{[a-z_]+\}")

    def test_url_helpers_state_aware_ca_byte_identical(self):
        # CA (the default) stays exactly as before; IL gets the /il/ namespace.
        self.assertEqual(self._hp._path("en", "x", "hospital", "CA"), "/hospital/x")
        self.assertEqual(self._hp._path("es", "x", "hospital", "CA"), "/es/hospital/x")
        self.assertEqual(self._hp._path("en", None, "hospital", "CA"), "/california-hospitals")
        self.assertEqual(self._hp._path("en", "x", "hospital", "IL"), "/il/hospital/x")
        self.assertEqual(self._hp._path("es", "cook", "county", "IL"), "/es/il/hospitals/cook")
        self.assertEqual(self._hp._path("en", None, "hospital", "IL"), "/il/illinois-hospitals")

    def test_sitemap_paths_are_il_namespaced(self):
        idx = self._idx()
        hp = self._hp.statutory_hospital_paths(idx, "IL")
        self.assertTrue(all("/il/" in u for u in hp))
        self.assertTrue(any(u.endswith("/il/illinois-hospitals") for u in hp))
        cp = self._hp.statutory_county_paths(idx, "IL")
        self.assertTrue(all("/il/hospitals/" in u for u in cp))


class TestStatesHub(unittest.TestCase):
    """The /find multi-state hub ('choose your state, then your hospital'): lists each state linking to
    its directory, shows live counts, self-canonicalizes at /find, no unfilled tokens in any language."""
    import hospital_pages as _hp

    def _states(self):
        return [{"name": "California", "code": "CA", "count": 466},
                {"name": "Illinois", "code": "IL", "count": 189}]

    def test_hub_lists_states_linking_to_their_directories(self):
        for lang in ("en", "es", "ar"):
            html = self._hp.render_states_hub(self._states(), lang)
            self.assertIn(f'href="{self._hp._path(lang, None, "hospital", "CA")}"', html)   # CA directory
            self.assertIn(f'href="{self._hp._path(lang, None, "hospital", "IL")}"', html)   # IL directory
            self.assertIn("466", html)
            self.assertIn("189", html)
            self.assertIn(self._hp._find_path(lang), html)                                   # self /find canonical
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on the hub")

    def test_find_paths_cover_all_langs(self):
        paths = self._hp.find_paths()
        self.assertEqual(len(paths), len(self._hp.i18n.LANGS))
        self.assertTrue(any(p.endswith("/find") for p in paths))            # en at /find
        self.assertTrue(any(p.endswith("/es/find") for p in paths))         # localized


class TestSitemapIndex(unittest.TestCase):
    """/sitemap.xml is now a sitemap INDEX pointing at per-state child sitemaps; IL URLs live in
    /sitemap-il.xml, CA in /sitemap-ca.xml. Guards the national-scale structure."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading
        web = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
        if web not in sys.path:
            sys.path.insert(0, web)
        import server as _srv
        cls.srv = _srv
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _srv.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path):
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        buf = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        head, _, body = buf.partition(b"\r\n\r\n")
        return int(head.decode("latin1").split("\r\n")[0].split()[1]), body.decode("utf-8")

    def test_index_lists_children(self):
        st, body = self._get("/sitemap.xml")
        self.assertEqual(st, 200)
        self.assertIn("<sitemapindex", body)
        for child in ("sitemap-pages.xml", "sitemap-ca.xml", "sitemap-il.xml", "sitemap-ny.xml"):
            self.assertIn(child, body)

    def test_il_child_has_namespaced_urls(self):
        st, body = self._get("/sitemap-il.xml")
        self.assertEqual(st, 200)
        self.assertIn("<urlset", body)
        self.assertIn("/il/hospital/", body)
        self.assertIn("/il/illinois-hospitals", body)

    def test_ca_child_still_has_ca_urls(self):
        st, body = self._get("/sitemap-ca.xml")
        self.assertEqual(st, 200)
        self.assertIn("/california-hospitals", body)
        self.assertNotIn("/il/", body)                      # CA sitemap must not carry IL URLs


class TestNewYorkStatutory(unittest.TestCase):
    """New York (3rd state) exercises the generalized statutory model: statewide bands (NO rural
    distinction) and a charge cap on the Medicaid rate (NO % -of-income cap) — so the income-cap clause
    is omitted from both the page and the plan, and the rural note never shows even for a CAH."""
    import hospital_pages as _hp

    def _ny(self, rural=False):
        return {"hospital": "MOUNT SINAI HOSPITAL", "ccn": "330024", "city": "NEW YORK",
                "county": "New York", "state": "NY", "phone": "(212) 555-0100",
                "hospital_type": "Critical Access Hospitals" if rural else "Acute Care Hospitals",
                "status": "statutory", "policy": None}

    def test_state_rules_ny_pinned(self):
        ny = state_rules.rules_for("NY")
        self.assertEqual((ny.statutory_free_pct, ny.statutory_discount_pct), (200, 400))
        self.assertIsNone(ny.income_cap_pct)                 # charge cap is % of Medicaid rate, not income
        self.assertFalse(ny.has_rural_bands)                 # statewide bands
        self.assertTrue(state_rules.rules_for("IL").has_rural_bands)   # IL contrast

    def test_ny_page_omits_income_cap_and_rural_note(self):
        idx = self._hp.build_index([self._ny(), self._ny(rural=True)])[0]
        for slug, row in idx.items():
            for lang in ("en", "es", "zh"):
                html = self._hp.render_statutory_hospital(row, slug, idx, lang)
                self.assertIn("2807-k", html)                            # NY act cited
                self.assertIn("200", html)                               # free band
                self.assertIn("400", html)                               # discount band
                self.assertIn("?st=NY", html)                            # generic CTA
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token")
                if lang == "en":
                    self.assertNotIn("yearly family income", html)       # no income-cap sentence
                    self.assertNotIn("Critical Access", html)            # no rural-lower-limits note

    def test_ny_page_surfaces_immigration_protection(self):
        self.assertTrue(state_rules.rules_for("NY").immigration_excluded)
        self.assertFalse(state_rules.rules_for("IL").immigration_excluded)
        idx = self._hp.build_index([self._ny()])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "ar"):
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on NY page")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("immigration status", en.lower())          # the reassurance is shown
        self.assertIn("New York", en)                            # {state} filled

    def test_il_page_has_no_immigration_note(self):
        # IL law doesn't carry the explicit exclusion, so the note must NOT appear (no false claim).
        idx = self._hp.build_index([{"hospital": "ADVOCATE CHRIST", "ccn": "140208", "city": "OAK LAWN",
                                     "county": "Cook", "state": "IL", "phone": "(708) 555-0100",
                                     "hospital_type": "Acute Care Hospitals", "status": "statutory",
                                     "policy": None}])[0]
        slug = next(iter(idx))
        self.assertNotIn("immigration status", self._hp.render_statutory_hospital(idx[slug], slug, idx, "en").lower())

    def test_ny_discount_plan_drops_cap_clause(self):
        import navigator
        intake = {"first_name": "there", "full_name": "A B", "household_size": 2,
                  "annual_income": 55000, "insurance": "uninsured", "in_collections": False}
        p = navigator.build_statutory_plan_struct(intake, self._ny(), "en")   # ~340% FPL -> discount
        self.assertEqual(p["tier"], "discount")
        msg = p["charity"]["message"]
        self.assertIn("2807-k", msg)
        self.assertNotIn("None", msg)                        # no "None%" from a null cap
        self.assertNotIn("yearly income", msg)               # cap clause dropped

    def test_il_still_cites_income_cap(self):
        # Regression: IL keeps its % -of-income cap (the cap-bearing path still works).
        idx = self._hp.build_index([{"hospital": "ADVOCATE CHRIST", "ccn": "140208", "city": "OAK LAWN",
                                     "county": "Cook", "state": "IL", "phone": "(708) 555-0100",
                                     "hospital_type": "Acute Care Hospitals", "status": "statutory",
                                     "policy": None}])[0]
        slug = next(iter(idx))
        html = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("yearly family income", html)          # IL income-cap sentence present


class TestMarylandWashingtonStatutory(unittest.TestCase):
    """Maryland (4th) + Washington (5th) states. MD mirrors NY (statewide, no income cap, immigration
    EXCLUDED). WA mirrors MD's bands but immigration is NOT excluded (the bar is agency guidance, not
    statute text — the note must not appear). Both prove the generalized engine adds a state with just a
    state_rules row + roster: they route, render, and resolve a plan with no per-state page code."""
    import hospital_pages as _hp

    def _row(self, state, name, ccn, city, county, rural=False):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": state,
                "phone": "(555) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Critical Access Hospitals" if rural else "Acute Care Hospitals"}

    def test_md_and_wa_pinned_from_statute(self):
        md, wa = state_rules.rules_for("MD"), state_rules.rules_for("WA")
        for r in (md, wa):
            self.assertEqual((r.statutory_free_pct, r.statutory_discount_pct), (200, 300))
            self.assertIsNone(r.income_cap_pct)          # no % -of-income collection cap in the model
            self.assertFalse(r.has_rural_bands)          # statewide bands (WA's CAHs get the "other" floor)
        self.assertTrue(md.immigration_excluded)         # §19-214.1 bars citizenship/immigration status
        self.assertFalse(wa.immigration_excluded)        # WA bar is AG/DOH guidance, not statute text

    def test_md_page_cites_law_and_shows_immigration_note(self):
        idx = self._hp.build_index([self._row("MD", "JOHNS HOPKINS HOSPITAL", "210009", "BALTIMORE",
                                              "Baltimore City")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            self.assertIn("19-214.1", html)                          # MD act cited
            self.assertIn("?st=MD", html)                            # generic CTA
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on MD page")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("immigration status", en.lower())              # reassurance shown
        self.assertIn("Maryland", en)                                # {state} filled

    def test_wa_page_has_no_immigration_note(self):
        # WA statute text doesn't carry the exclusion (only agency guidance) -> the note must NOT appear.
        for rural in (False, True):
            idx = self._hp.build_index([self._row("WA", "HARBORVIEW MEDICAL CENTER", "500001", "SEATTLE",
                                                  "King", rural=rural)])[0]
            slug = next(iter(idx))
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
            self.assertIn("70.170.060", html)                        # WA act cited
            self.assertNotIn("immigration status", html.lower())     # no fabricated claim
            self.assertNotIn("Critical Access", html)                # WA has no rural-lower-limits note

    def test_md_and_wa_free_plan_cites_law(self):
        import navigator
        intake = {"first_name": "there", "full_name": "A B", "household_size": 4,
                  "annual_income": 18000, "insurance": "uninsured", "in_collections": True}
        for state, law in (("MD", "19-214.1"), ("WA", "70.170.060")):
            p = navigator.build_statutory_plan_struct(intake, self._row(state, "X HOSPITAL", "999", "C", "D"), "en")
            self.assertEqual(p["tier"], "free")                      # ~90% FPL -> free
            self.assertIn(law, p["charity"]["message"])

    def test_registry_discovers_md_and_wa(self):
        import server
        self.assertEqual(set(server.STATUTORY_STATES) >= {"IL", "NY", "MD", "WA"}, True)
        self.assertGreater(len(server.STATUTORY_STATES["MD"]["hospitals"]), 0)
        self.assertGreater(len(server.STATUTORY_STATES["WA"]["hospitals"]), 0)

    def test_statutory_page_qr_is_namespaced_and_og_is_safe(self):
        # L1: the print handout QR must be namespaced under /qr/<ns>/ (so a slug shared with a CA hospital
        # can't collide) and the OG card must fall back to the site-wide image — NOT the CA-only per-
        # hospital path, which for a statutory slug would 404 or show the wrong hospital's card.
        idx = self._hp.build_index([self._row("MD", "X HOSPITAL", "210009", "BALTIMORE", "Baltimore City")])[0]
        slug = next(iter(idx))
        html = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn(f"/qr/md/hospital/{slug}.svg", html)       # namespaced print-QR
        self.assertIn("/og-image.png", html)                     # safe site-wide OG
        self.assertNotIn("/og/hospital/", html)                  # never the CA-only per-hospital card


class TestNewJerseyStatutory(unittest.TestCase):
    """New Jersey (6th state). Shape mirrors WA: statewide 200/300 bands, no income cap, no rural tier,
    immigration NOT surfaced (NJ Charity Care is residency+income+asset-based; the statute doesn't bar
    immigration status 'in terms' the way NY §2807-k(9-a) does, so the reassurance must not appear).
    Proves the generalized engine adds NJ with only a state_rules row + roster — routes, renders, resolves."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "NJ",
                "phone": "(555) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_nj_pinned_from_statute(self):
        nj = state_rules.rules_for("NJ")
        self.assertEqual((nj.statutory_free_pct, nj.statutory_discount_pct), (200, 300))
        self.assertIsNone(nj.income_cap_pct)             # asset test, not a % -of-income cap -> not modeled
        self.assertFalse(nj.has_rural_bands)             # statewide program, no rural/CAH distinction
        self.assertFalse(nj.immigration_excluded)        # not barred in statute text -> no reassurance note
        self.assertTrue(nj.is_statutory)

    def test_nj_page_cites_law_and_has_no_immigration_note(self):
        idx = self._hp.build_index([self._row("HACKENSACK UNIVERSITY MEDICAL CENTER", "310002",
                                              "HACKENSACK", "Bergen")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            self.assertIn("26:2H-18.60", html)                       # NJ act cited
            self.assertIn("?st=NJ", html)                            # generic CTA
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on NJ page")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("New Jersey", en)                              # {state} filled
        self.assertNotIn("immigration status", en.lower())          # no fabricated statutory claim
        self.assertNotIn("Critical Access", en)                     # no rural-lower-limits note
        self.assertIn(f"/qr/nj/hospital/{slug}.svg", en)            # namespaced print-QR
        self.assertIn("/og-image.png", en)                          # safe site-wide OG card
        self.assertNotIn("/og/hospital/", en)                       # never the CA-only per-hospital card

    def test_nj_free_plan_cites_law(self):
        import navigator
        intake = {"first_name": "there", "full_name": "A B", "household_size": 4,
                  "annual_income": 18000, "insurance": "uninsured", "in_collections": True}
        p = navigator.build_statutory_plan_struct(intake, self._row("X HOSPITAL", "999", "C", "D"), "en")
        self.assertEqual(p["tier"], "free")                          # ~90% FPL -> free
        self.assertIn("26:2H-18.60", p["charity"]["message"])

    def test_registry_discovers_nj(self):
        import server
        self.assertIn("NJ", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["NJ"]["hospitals"]), 0)


class TestColoradoStatutory(unittest.TestCase):
    """Colorado (7th state) — the first DISCOUNT-ONLY statutory state. CO Hospital Discounted Care
    (HB21-1198) sets NO free tier: every eligible patient ≤250% FPL gets capped-rate discounted care, not
    free care. So statutory_free_pct=None and both renderers must use the discount-only text (never print
    'FREE care ... at or below None%'). Proves the engine handles a no-free-tier statute honestly."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "CO",
                "phone": "(555) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_co_pinned_discount_only(self):
        co = state_rules.rules_for("CO")
        self.assertIsNone(co.statutory_free_pct)         # no statutory free tier
        self.assertEqual(co.statutory_discount_pct, 250)
        self.assertIsNone(co.income_cap_pct)             # monthly-payment cap, not an annual %-of-income cap
        self.assertFalse(co.has_rural_bands)
        self.assertFalse(co.immigration_excluded)
        self.assertTrue(co.is_statutory)

    def test_co_page_discount_only_no_free_tier_leak(self):
        idx = self._hp.build_index([self._row("DENVER HEALTH MEDICAL CENTER", "060052", "DENVER", "Denver")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            self.assertIn("HB21-1198", html)                         # CO law cited
            self.assertIn("?st=CO", html)                            # generic CTA
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on CO page")
            self.assertNotIn("None%", html)                          # the free_pct=None leak must NOT appear
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Colorado", en)                                # {state} filled
        self.assertIn("discounted care", en.lower())                 # discount-only framing present
        self.assertNotIn("must provide free care", en.lower())       # no false free-tier claim
        self.assertNotIn("immigration status", en.lower())           # not barred in statute -> no note
        self.assertIn(f"/qr/co/hospital/{slug}.svg", en)             # namespaced print-QR
        self.assertIn("/og-image.png", en)                           # safe site-wide OG
        self.assertNotIn("/og/hospital/", en)

    def test_co_plan_low_income_is_discount_not_free(self):
        import navigator
        # ~90% FPL: a free-tier state would say 'free'; CO (no free tier) must say 'discount', cite the law,
        # and never render 'None%'.
        intake = {"first_name": "there", "full_name": "A B", "household_size": 4,
                  "annual_income": 18000, "insurance": "uninsured", "in_collections": True}
        p = navigator.build_statutory_plan_struct(intake, self._row("X HOSPITAL", "060052", "C", "D"), "en")
        self.assertEqual(p["tier"], "discount")                      # NOT 'free' — CO has no free tier
        self.assertIn("HB21-1198", p["charity"]["message"])
        self.assertNotIn("None", p["charity"]["message"])            # no {free_pct}=None leak
        # Above 250% FPL -> over tier.
        over = {**intake, "annual_income": 90000}
        po = navigator.build_statutory_plan_struct(over, self._row("X HOSPITAL", "060052", "C", "D"), "en")
        self.assertEqual(po["tier"], "over")

    def test_registry_discovers_co(self):
        import server
        self.assertIn("CO", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["CO"]["hospitals"]), 0)

    def test_co_county_directory_lead_no_free_tier_leak(self):
        # Regression guard: the county hub, the statewide directory, and every page LEAD must use the
        # discount-only text for CO — never the free-tier string that prints "free care ... at or below
        # None%" (the bug the free-tier states' shared strings would produce for a None free_pct).
        idx = self._hp.build_index([self._row("DENVER HEALTH MEDICAL CENTER", "060052", "DENVER", "Denver")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "ar"):
            county = self._hp.render_statutory_county("Denver", idx, lang, "CO")
            directory = self._hp.render_statutory_directory(idx, lang, "CO")
            hospital = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            for html, where in ((county, "county"), (directory, "directory"), (hospital, "hospital")):
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
                self.assertIn("HB21-1198", html, f"{lang} {where}: CO law cited")
        # English body text must never assert a statutory FREE tier for CO (discount-only law).
        for html, where in ((self._hp.render_statutory_county("Denver", idx, "en", "CO"), "county"),
                            (self._hp.render_statutory_directory(idx, "en", "CO"), "directory"),
                            (self._hp.render_statutory_hospital(idx[slug], slug, idx, "en"), "hospital")):
            self.assertNotIn("must provide free care", html.lower(), f"{where}: false free-tier claim")
            self.assertIn("discounted care", html.lower(), f"{where}: discount-only framing present")
            # incl. the <meta> description / snippet: CO law guarantees a discount, not "free or discounted".
            self.assertNotIn("free or discounted", html.lower(), f"{where}: meta/body overstates free")


class TestMaineStatutory(unittest.TestCase):
    """Maine (10th state) — the first FREE-ONLY statutory shape. 22 M.R.S. §1716-A (as rewritten by PL 2025
    c. 488, eff. 2026-07-01) mandates 100% FREE care ≤200% FPL and sets NO statutory %-discount tier — instead
    a payment plan capped at 4% of monthly income for patients up to 400% FPL. So statutory_free_pct=200,
    statutory_discount_pct=None: the tier logic returns 'free' ≤200 and 'over' above (never a bogus 'discount
    ... at or below None%'), and renderers must use the free-only text. The 200–400% band is surfaced as a
    payment-cap note. STALE-LAW GUARD: pin 200 (not the old 150) so a silent reversion is caught."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "ME",
                "phone": "(207) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_me_pinned_free_only(self):
        me = state_rules.rules_for("ME")
        self.assertEqual(me.statutory_free_pct, 200)      # free tier ≤200% FPL (c. 488 raised it from 150)
        self.assertIsNone(me.statutory_discount_pct)      # NO statutory %-discount tier — free-only shape
        self.assertIsNone(me.income_cap_pct)              # affordability is a payment cap, not an income cap
        self.assertEqual((me.payment_cap_pct, me.payment_cap_ceiling_pct), (4, 400))
        self.assertFalse(me.has_rural_bands)
        self.assertFalse(me.immigration_excluded)         # statute is silent on immigration -> no reassurance
        self.assertTrue(me.is_statutory)                  # free-only must still register as statute-driven

    def test_me_page_free_only_no_discount_tier_leak(self):
        idx = self._hp.build_index([self._row("MAINE MEDICAL CENTER", "200024", "PORTLAND", "Cumberland")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            html = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            self.assertIn("22 M.R.S. §1716-A", html)                  # ME law cited
            self.assertIn("?st=ME", html)                            # generic CTA
            self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on ME page")
            self.assertNotIn("None%", html)                          # the discount_pct=None leak must NOT appear
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Maine", en)                                   # {state} filled
        self.assertIn("free care", en.lower())                       # free-only framing present
        self.assertNotIn("discounted care", en.lower())              # no false statutory-discount claim
        self.assertNotIn("free or discounted", en.lower())           # meta/body must not overstate a discount tier
        self.assertNotIn("immigration status", en.lower())           # not barred in statute -> no note
        self.assertIn(f"/qr/me/hospital/{slug}.svg", en)             # namespaced print-QR
        self.assertIn("/og-image.png", en)                           # safe site-wide OG
        self.assertNotIn("/og/hospital/", en)
        # The 200–400% affordability band is surfaced as a payment-cap note on the page.
        self.assertIn("4%", en)
        self.assertIn("400%", en)

    def test_me_plan_free_then_payment_cap_then_over(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4,
                "insurance": "uninsured", "in_collections": True}
        row = self._row("X HOSPITAL", "200024", "C", "D")
        # ~75% FPL -> FREE (a free-only state answers 'free', never 'discount ... None%').
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 24000}, row, "en")
        self.assertEqual(free["tier"], "free")
        self.assertIn("22 M.R.S. §1716-A", free["charity"]["message"])
        self.assertNotIn("None", free["charity"]["message"])
        # ~242% FPL -> OVER the free floor but within the 400% cap -> over tier + payment-cap note, no None% leak.
        cap = navigator.build_statutory_plan_struct({**base, "annual_income": 80000}, row, "en")
        self.assertEqual(cap["tier"], "over")
        self.assertNotIn("None%", cap["charity"]["message"])          # over-tier free-only variant cites 200%, not None
        self.assertIn("200%", cap["charity"]["message"])              # names the free floor, not a discount ceiling
        self.assertIn("4% of your monthly income", cap["charity"]["message"])   # payment-cap protection appended
        # ~435% FPL -> above the 400% cap -> over tier, NO payment-cap note.
        over = navigator.build_statutory_plan_struct({**base, "annual_income": 140000}, row, "en")
        self.assertEqual(over["tier"], "over")
        self.assertNotIn("4% of your monthly income", over["charity"]["message"])

    def test_registry_discovers_me(self):
        import server
        self.assertIn("ME", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["ME"]["hospitals"]), 0)

    def test_me_county_directory_lead_no_discount_tier_leak(self):
        # Regression guard: the county hub, the statewide directory, and every page LEAD must use the
        # free-only text for ME — never a discount-tier string that would print "discounted care ... at or
        # below None%" for a null discount_pct.
        idx = self._hp.build_index([self._row("MAINE MEDICAL CENTER", "200024", "PORTLAND", "Cumberland")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "ar"):
            county = self._hp.render_statutory_county("Cumberland", idx, lang, "ME")
            directory = self._hp.render_statutory_directory(idx, lang, "ME")
            hospital = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            for html, where in ((county, "county"), (directory, "directory"), (hospital, "hospital")):
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
                self.assertIn("22 M.R.S. §1716-A", html, f"{lang} {where}: ME law cited")
        for html, where in ((self._hp.render_statutory_county("Cumberland", idx, "en", "ME"), "county"),
                            (self._hp.render_statutory_directory(idx, "en", "ME"), "directory"),
                            (self._hp.render_statutory_hospital(idx[slug], slug, idx, "en"), "hospital")):
            self.assertNotIn("discounted care", html.lower(), f"{where}: false discount-tier claim")
            self.assertIn("free care", html.lower(), f"{where}: free-only framing present")
            self.assertNotIn("free or discounted", html.lower(), f"{where}: meta/body overstates discount")


class TestRhodeIslandStatutory(unittest.TestCase):
    """Rhode Island (9th state). Clean free+discount drop-in, same shape as NJ/MD/WA (free ≤200% / discount
    ≤300%, 216-RICR-40-10-23). Reuses the existing free-tier strings on every surface — a state added with
    just a state_rules row + roster, no i18n."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "RI",
                "phone": "(555) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_ri_pinned_from_statute(self):
        r = state_rules.rules_for("RI")
        self.assertEqual((r.statutory_free_pct, r.statutory_discount_pct), (200, 300))
        self.assertIsNone(r.income_cap_pct)
        self.assertFalse(r.immigration_excluded)
        self.assertTrue(r.is_statutory)

    def test_ri_surfaces_cite_law_no_leak(self):
        idx = self._hp.build_index([self._row("RHODE ISLAND HOSPITAL", "410001", "PROVIDENCE",
                                              "Providence")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            h = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            c = self._hp.render_statutory_county("Providence", idx, lang, "RI")
            d = self._hp.render_statutory_directory(idx, lang, "RI")
            for html, where in ((h, "hospital"), (c, "county"), (d, "directory")):
                self.assertIn("216-RICR-40-10-23", html, f"{lang} {where}: RI reg cited")
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Rhode Island", en)
        self.assertIn("free care", en.lower())                       # RI has a free tier
        self.assertIn(f"/qr/ri/hospital/{slug}.svg", en)

    def test_ri_plan_free_and_discount(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4, "insurance": "uninsured",
                "in_collections": True}
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 18000},
                                                     self._row("X HOSPITAL", "410001", "C", "D"), "en")
        self.assertEqual(free["tier"], "free")
        self.assertIn("216-RICR-40-10-23", free["charity"]["message"])
        disc = navigator.build_statutory_plan_struct({**base, "annual_income": 80000},   # ~242% FPL -> discount
                                                     self._row("X HOSPITAL", "410001", "C", "D"), "en")
        self.assertEqual(disc["tier"], "discount")

    def test_registry_discovers_ri(self):
        import server
        self.assertIn("RI", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["RI"]["hospitals"]), 0)


class TestOregonStatutory(unittest.TestCase):
    """Oregon (8th state). Free+discount shape like NJ/MD/WA but with a HIGHER discount ceiling (400% vs
    300%): free ≤200% FPL, sliding discount to 400% (ORS 442.614 / HB 3076). Because OR has a real free
    tier it reuses the existing free-tier strings on every surface (no discount-only variant). Confirms the
    2026 HB 4040 change (presumptive-screening $ threshold) did NOT move the modeled FPL bands."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "OR",
                "phone": "(555) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_or_pinned_from_statute(self):
        o = state_rules.rules_for("OR")
        self.assertEqual((o.statutory_free_pct, o.statutory_discount_pct), (200, 400))
        self.assertIsNone(o.income_cap_pct)
        self.assertFalse(o.has_rural_bands)
        self.assertFalse(o.immigration_excluded)
        self.assertTrue(o.is_statutory)

    def test_or_page_and_county_cite_law_no_leak(self):
        idx = self._hp.build_index([self._row("OREGON HEALTH SCIENCE UNIV HOSP", "380001", "PORTLAND",
                                              "Multnomah")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            h = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            c = self._hp.render_statutory_county("Multnomah", idx, lang, "OR")
            d = self._hp.render_statutory_directory(idx, lang, "OR")
            for html, where in ((h, "hospital"), (c, "county"), (d, "directory")):
                self.assertIn("442.614", html, f"{lang} {where}: OR law cited")
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Oregon", en)
        self.assertIn("free care", en.lower())                       # OR HAS a free tier -> correct here
        self.assertIn("?st=OR", en)
        self.assertIn(f"/qr/or/hospital/{slug}.svg", en)

    def test_or_plan_free_and_discount_tiers(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4, "insurance": "uninsured",
                "in_collections": True}
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 18000},
                                                     self._row("X HOSPITAL", "380001", "C", "D"), "en")
        self.assertEqual(free["tier"], "free")                       # ~90% FPL -> free
        self.assertIn("442.614", free["charity"]["message"])
        # ~330% FPL: discount under OR's 400% ceiling, but would be 'over' under NJ/MD/WA's 300% ceiling —
        # this is the OR-specific band the state exists to cover, so the income must actually exceed 300%.
        disc = navigator.build_statutory_plan_struct({**base, "annual_income": 109000},   # ~330% FPL
                                                     self._row("X HOSPITAL", "380001", "C", "D"), "en")
        self.assertEqual(disc["tier"], "discount")
        # Above OR's 400% ceiling -> over (guards against the discount ceiling being wrongly lowered).
        over = navigator.build_statutory_plan_struct({**base, "annual_income": 140000},   # ~424% FPL
                                                     self._row("X HOSPITAL", "380001", "C", "D"), "en")
        self.assertEqual(over["tier"], "over")

    def test_registry_discovers_or(self):
        import server
        self.assertIn("OR", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["OR"]["hospitals"]), 0)


class TestMassachusettsStatutory(unittest.TestCase):
    """Massachusetts (11th state). Clean free+discount drop-in via the Health Safety Net (M.G.L. c.118E;
    101 CMR 613.00) — but with a LOWER free floor than the 200%-floor states: free ≤150% FPL, partial/
    deductible 150–300% FPL. Reuses the existing free-tier strings (no i18n). The distinguishing test is the
    150–200% band: MA answers 'discount' there, where a 200-floor state (NJ/MD/WA/RI/OR) would answer 'free' —
    so a silent revert of the free floor to 200 is caught."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "MA",
                "phone": "(555) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_ma_pinned_from_statute(self):
        m = state_rules.rules_for("MA")
        self.assertEqual((m.statutory_free_pct, m.statutory_discount_pct), (150, 300))   # HSN: free 150 / partial 300
        self.assertIsNone(m.income_cap_pct)              # sliding deductible, not a %-of-income collection cap
        self.assertFalse(m.has_rural_bands)
        self.assertTrue(m.immigration_excluded)          # 101 CMR 613.08 non-exclusion (user-approved) -> reassurance note
        self.assertTrue(m.is_statutory)

    def test_ma_surfaces_cite_law_no_leak(self):
        idx = self._hp.build_index([self._row("MASSACHUSETTS GENERAL HOSPITAL", "220071", "BOSTON",
                                              "Suffolk")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            h = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            c = self._hp.render_statutory_county("Suffolk", idx, lang, "MA")
            d = self._hp.render_statutory_directory(idx, lang, "MA")
            for html, where in ((h, "hospital"), (c, "county"), (d, "directory")):
                self.assertIn("101 CMR 613.00", html, f"{lang} {where}: MA law cited")
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Massachusetts", en)
        self.assertIn("free care", en.lower())                       # MA HAS a free tier
        self.assertIn("immigration status", en.lower())              # 101 CMR 613.08 non-exclusion reassurance renders
        self.assertIn("?st=MA", en)
        self.assertIn(f"/qr/ma/hospital/{slug}.svg", en)

    def test_ma_plan_free_discount_boundary_at_150(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4, "insurance": "uninsured",
                "in_collections": True}
        row = self._row("X HOSPITAL", "220071", "C", "D")
        # ~73% FPL -> free.
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 24000}, row, "en")
        self.assertEqual(free["tier"], "free")
        self.assertIn("101 CMR 613.00", free["charity"]["message"])
        # ~182% FPL: DISCOUNT under MA's 150% free floor — but a 200-floor state would call this 'free'. This is
        # the MA-specific band; asserting 'discount' here guards the lower free floor against a silent revert.
        disc = navigator.build_statutory_plan_struct({**base, "annual_income": 60000}, row, "en")
        self.assertEqual(disc["tier"], "discount")
        # Above MA's 300% ceiling -> over.
        over = navigator.build_statutory_plan_struct({**base, "annual_income": 110000}, row, "en")
        self.assertEqual(over["tier"], "over")

    def test_registry_discovers_ma(self):
        import server
        self.assertIn("MA", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["MA"]["hospitals"]), 0)


class TestOhioStatutory(unittest.TestCase):
    """Ohio (12th state) — the SECOND free-only statutory shape (after ME). HCAP (Ohio Rev. Code §5168.14)
    guarantees FREE basic medically-necessary care to Ohio residents (non-Medicaid) ≤100% FPL, with NO
    statutory discount tier. statutory_free_pct=100, statutory_discount_pct=None — reuses the ME free-only
    i18n (no discount-tier leak, no 'None%'). No payment-cap band (unlike ME). Immigration silent -> no note."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "OH",
                "phone": "(614) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_oh_pinned_free_only(self):
        o = state_rules.rules_for("OH")
        self.assertEqual(o.statutory_free_pct, 100)       # HCAP: free basic care ≤100% FPL
        self.assertIsNone(o.statutory_discount_pct)       # no statutory discount tier — free-only
        self.assertIsNone(o.income_cap_pct)
        self.assertIsNone(o.payment_cap_pct)              # no 200–400% payment-cap band (unlike ME)
        self.assertFalse(o.immigration_excluded)          # statute silent on immigration -> no reassurance
        self.assertTrue(o.is_statutory)

    def test_oh_surfaces_free_only_no_leak(self):
        idx = self._hp.build_index([self._row("OHIO STATE UNIVERSITY HOSPITAL", "360085", "COLUMBUS",
                                              "Franklin")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "ar"):
            h = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            c = self._hp.render_statutory_county("Franklin", idx, lang, "OH")
            d = self._hp.render_statutory_directory(idx, lang, "OH")
            for html, where in ((h, "hospital"), (c, "county"), (d, "directory")):
                self.assertIn("5168.14", html, f"{lang} {where}: OH law cited")
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Ohio", en)
        self.assertIn("free care", en.lower())
        self.assertNotIn("discounted care", en.lower())              # free-only: no false discount-tier claim
        self.assertNotIn("immigration status", en.lower())           # silent statute -> no note
        self.assertIn("?st=OH", en)
        self.assertIn(f"/qr/oh/hospital/{slug}.svg", en)

    def test_oh_plan_free_then_over(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4, "insurance": "uninsured",
                "in_collections": True}
        row = self._row("X HOSPITAL", "360085", "C", "D")
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 20000}, row, "en")  # ~60% FPL
        self.assertEqual(free["tier"], "free")
        self.assertIn("5168.14", free["charity"]["message"])
        self.assertNotIn("None", free["charity"]["message"])
        over = navigator.build_statutory_plan_struct({**base, "annual_income": 45000}, row, "en")  # ~136% FPL
        self.assertEqual(over["tier"], "over")                       # above 100% -> over (no discount tier)
        self.assertNotIn("None%", over["charity"]["message"])
        self.assertIn("100%", over["charity"]["message"])            # free-only over cites the 100% floor, not None

    def test_registry_discovers_oh(self):
        import server
        self.assertIn("OH", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["OH"]["hospitals"]), 0)


class TestVermontStatutory(unittest.TestCase):
    """Vermont (13th state). Free+discount via 18 V.S.A. §9482 (Act 119 of 2022) with the MOST GENEROUS free
    floor in the model: free ≤250% FPL, ≥40% discount 250–400%. immigration_excluded=True — §9483 explicitly
    protects undocumented immigrants (an in-terms non-exclusion), so the translated reassurance surfaces. The
    distinguishing test is the 200–250% band: VT answers 'free' there, where every 200-floor state answers
    'discount' — guards the 250 free floor against a silent revert."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "VT",
                "phone": "(802) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_vt_pinned_from_statute(self):
        v = state_rules.rules_for("VT")
        self.assertEqual((v.statutory_free_pct, v.statutory_discount_pct), (250, 400))   # §9482: free 250 / disc 400
        self.assertIsNone(v.income_cap_pct)
        self.assertFalse(v.has_rural_bands)
        self.assertTrue(v.immigration_excluded)           # §9483 explicit undocumented protection -> note
        self.assertTrue(v.is_statutory)

    def test_vt_surfaces_cite_law_no_leak(self):
        idx = self._hp.build_index([self._row("UNIVERSITY OF VERMONT MEDICAL CENTER", "470003", "BURLINGTON",
                                              "Chittenden")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            h = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            c = self._hp.render_statutory_county("Chittenden", idx, lang, "VT")
            d = self._hp.render_statutory_directory(idx, lang, "VT")
            for html, where in ((h, "hospital"), (c, "county"), (d, "directory")):
                self.assertIn("9482", html, f"{lang} {where}: VT law cited")
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
        en = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        self.assertIn("Vermont", en)
        self.assertIn("free care", en.lower())                       # VT HAS a free tier
        self.assertIn("immigration status", en.lower())              # §9483 non-exclusion reassurance renders
        self.assertIn(f"/qr/vt/hospital/{slug}.svg", en)

    def test_vt_plan_free_floor_at_250(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4, "insurance": "uninsured",
                "in_collections": True}
        row = self._row("X HOSPITAL", "470003", "C", "D")
        # ~230% FPL: FREE under VT's 250 floor — but a 200-floor state would call this 'discount'. Guards the floor.
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 76000}, row, "en")
        self.assertEqual(free["tier"], "free")
        self.assertIn("9482", free["charity"]["message"])
        disc = navigator.build_statutory_plan_struct({**base, "annual_income": 109000}, row, "en")  # ~330% FPL
        self.assertEqual(disc["tier"], "discount")
        over = navigator.build_statutory_plan_struct({**base, "annual_income": 150000}, row, "en")  # ~454% FPL
        self.assertEqual(over["tier"], "over")

    def test_registry_discovers_vt(self):
        import server
        self.assertIn("VT", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["VT"]["hospitals"]), 0)


class TestNorthCarolinaStatutory(unittest.TestCase):
    """North Carolina (14th state) — the FIRST program-authority state. Free ≤200% FPL / discount ≤300% comes
    from a binding statewide PROGRAM (the Medicaid HASP charity-care condition all 99 acute hospitals accepted),
    NOT a statute — so authority_is_program=True and the pages must say "North Carolina's ... program", NEVER
    "North Carolina law" (which would misstate the authority). Same free+discount bands as MD; the honesty test
    is the framing: 'program' present, '{state} law' absent."""
    import hospital_pages as _hp

    def _row(self, name, ccn, city, county):
        return {"hospital": name, "ccn": ccn, "city": city, "county": county, "state": "NC",
                "phone": "(919) 555-0100", "status": "statutory", "policy": None,
                "hospital_type": "Acute Care Hospitals"}

    def test_nc_pinned_program_authority(self):
        n = state_rules.rules_for("NC")
        self.assertEqual((n.statutory_free_pct, n.statutory_discount_pct), (200, 300))
        self.assertTrue(n.authority_is_program)          # binding Medicaid program, not a statute
        self.assertIsNone(n.income_cap_pct)
        self.assertFalse(n.immigration_excluded)          # immigration-neutral but no in-terms non-exclusion
        self.assertTrue(n.is_statutory)

    def test_nc_surfaces_say_program_not_law(self):
        idx = self._hp.build_index([self._row("DUKE UNIVERSITY HOSPITAL", "340030", "DURHAM", "Durham")])[0]
        slug = next(iter(idx))
        for lang in ("en", "es", "zh"):
            h = self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
            c = self._hp.render_statutory_county("Durham", idx, lang, "NC")
            d = self._hp.render_statutory_directory(idx, lang, "NC")
            for html, where in ((h, "hospital"), (c, "county"), (d, "directory")):
                self.assertIn("Medical Debt Relief Program", html, f"{lang} {where}: program named")
                self.assertNotIn("None%", html, f"{lang} {where}: None% leak")
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang} {where}: unfilled token")
        en_h = self._hp.render_statutory_hospital(idx[slug], slug, idx, "en")
        en_c = self._hp.render_statutory_county("Durham", idx, "en", "NC")
        for html, where in ((en_h, "hospital"), (en_c, "county")):
            self.assertIn("program", html.lower(), f"{where}: program framing present")
            self.assertNotIn("north carolina law", html.lower(), f"{where}: must NOT call the program 'law'")
            self.assertIn("free care", html.lower(), f"{where}: free tier present")
        self.assertIn("?st=NC", en_h)
        self.assertIn(f"/qr/nc/hospital/{slug}.svg", en_h)

    def test_nc_hub_card_says_program(self):
        hub = self._hp.render_states_hub([{"name": "North Carolina", "code": "NC", "count": 99}], "en")
        self.assertIn("statewide program", hub)
        self.assertNotIn("under state law", hub)          # the NC card must not say "under state law"

    def test_nc_plan_free_discount_over_cite_program(self):
        import navigator
        base = {"first_name": "there", "full_name": "A B", "household_size": 4, "insurance": "uninsured",
                "in_collections": True}
        row = self._row("X HOSPITAL", "340030", "C", "D")
        free = navigator.build_statutory_plan_struct({**base, "annual_income": 24000}, row, "en")   # ~73% FPL
        self.assertEqual(free["tier"], "free")
        self.assertIn("Medical Debt Relief Program", free["charity"]["message"])
        self.assertNotIn("North Carolina law", free["charity"]["message"])
        disc = navigator.build_statutory_plan_struct({**base, "annual_income": 80000}, row, "en")   # ~242% FPL
        self.assertEqual(disc["tier"], "discount")
        over = navigator.build_statutory_plan_struct({**base, "annual_income": 110000}, row, "en")  # ~333% FPL
        self.assertEqual(over["tier"], "over")

    def test_registry_discovers_nc(self):
        import server
        self.assertIn("NC", server.STATUTORY_STATES)
        self.assertGreater(len(server.STATUTORY_STATES["NC"]["hospitals"]), 0)


class TestStatutoryProtectionNotes(unittest.TestCase):
    """H1/H2/M3: statute-backed extras a patient can act on, each gated by a state_rules flag so a claim
    only shows where the law says so — NY's named Medicaid-rate cap (H1) + no-lawsuit/no-foreclosure
    protection (H2), IL's 60-day apply deadline (M3). Translated in all 10 languages (token-parity)."""
    import hospital_pages as _hp
    import i18n as _i18n

    def _row(self, state, ccn):
        return {"hospital": "X HOSPITAL", "ccn": ccn, "city": "C", "county": "D", "state": state,
                "phone": "(1) 2", "status": "statutory", "policy": None, "hospital_type": "Acute Care Hospitals"}

    def test_flags_pinned(self):
        ny, il = state_rules.rules_for("NY"), state_rules.rules_for("IL")
        self.assertTrue(ny.bars_debt_lawsuits and ny.names_medicaid_cap)
        self.assertEqual(il.apply_deadline_days, 60)
        for code in ("MD", "WA", "CA", "IL"):                     # only NY names a Medicaid-rate cap
            self.assertFalse(state_rules.rules_for(code).names_medicaid_cap)
        for code in ("MD", "WA", "CA", "NY"):                     # only IL models an apply deadline
            self.assertIsNone(state_rules.rules_for(code).apply_deadline_days)

    def test_ny_page_shows_cap_and_debt_il_shows_deadline(self):
        def page(state, ccn, lang):
            idx = self._hp.build_index([self._row(state, ccn)])[0]
            slug = next(iter(idx))
            return self._hp.render_statutory_hospital(idx[slug], slug, idx, lang)
        ny_en, il_en = page("NY", "330024", "en"), page("IL", "140208", "en")
        self.assertIn("share of the Medicaid", ny_en)             # H1 cap note
        self.assertIn("foreclosure", ny_en)                      # H2 protection note
        self.assertNotIn("within 60 days", ny_en)                # IL-only deadline absent
        self.assertIn("within 60 days", il_en)                   # M3 deadline note
        self.assertNotIn("share of the Medicaid", il_en)         # NY-only cap absent
        for state, ccn in (("MD", "210009"), ("WA", "500001")):  # neither flag set -> no notes
            h = page(state, ccn, "en")
            self.assertNotIn("share of the Medicaid", h)
            self.assertNotIn("within 60 days", h)
        # every language renders the gated notes with no unfilled {token}
        for lang in self._i18n.LANGS:
            for html in (page("NY", "330024", lang), page("IL", "140208", lang)):
                self.assertNotRegex(html, r"\{[a-z_]+\}", f"{lang}: unfilled token on a statutory page")

    def test_new_keys_present_and_nonempty_in_every_language(self):
        for lang in self._i18n.LANGS:
            S = self._i18n.statutory_strings(lang)
            for k in ("s_medicaid_cap", "s_debt_protection", "s_apply_deadline"):
                self.assertTrue(S.get(k, "").strip(), f"{lang}.statutory.{k} empty")
            # res_coverage rides the plan catalog (messages.t) — present + generic (not 'Medi-Cal')
            import messages
            self.assertIn("res_coverage", messages.MESSAGES.get(lang, messages.MESSAGES["en"]))


if __name__ == "__main__":
    unittest.main()
