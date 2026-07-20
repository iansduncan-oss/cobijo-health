#!/usr/bin/env python3
"""Unit coverage for the pure, non-medical utility helpers in web/hospital_pages.py.

These are template/URL/formatting primitives — no eligibility, benefit, or legal logic — so they
are safe to pin directly (goals.md "NOW" list). Locking their behavior guards the SEO/render layer
against a silent regression (e.g. a slug collision or a money formatter that starts leaking "None").

Run (from repo root):  python3 -m unittest tests.test_page_helpers
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
for p in (_ROOT, _WEB):
    if p not in sys.path:
        sys.path.insert(0, p)

import hospital_pages as hp


class TestSlugify(unittest.TestCase):
    def test_normal_name(self):
        self.assertEqual(hp.slugify("Bridgton Hospital"), "bridgton-hospital")

    def test_apostrophes_and_hyphens_collapse_to_single_separator(self):
        # every run of non-[a-z0-9] becomes ONE hyphen: "st. mary's-west" -> "st-mary-s-west"
        self.assertEqual(hp.slugify("St. Mary's-West"), "st-mary-s-west")

    def test_empty_and_none(self):
        self.assertEqual(hp.slugify(""), "")
        self.assertEqual(hp.slugify(None), "")

    def test_numbers_preserved(self):
        self.assertEqual(hp.slugify("Hospital 21"), "hospital-21")

    def test_leading_trailing_separators_stripped(self):
        self.assertEqual(hp.slugify("  Hospital!  "), "hospital")


class TestMoney(unittest.TestCase):
    def test_thousands_separator(self):
        self.assertEqual(hp._money(30000), "$30,000")

    def test_zero(self):
        self.assertEqual(hp._money(0), "$0")

    def test_none_returns_none(self):
        self.assertIsNone(hp._money(None))

    def test_non_numeric_string_returns_none(self):
        self.assertIsNone(hp._money("abc"))

    def test_numeric_string_is_formatted(self):
        # covers the try/except: a digit string parses via int()
        self.assertEqual(hp._money("1500"), "$1,500")


class TestEscape(unittest.TestCase):
    def test_escapes_markup_and_quotes(self):
        self.assertEqual(hp._e('<a href="x">Tom & Jerry</a>'),
                         "&lt;a href=&quot;x&quot;&gt;Tom &amp; Jerry&lt;/a&gt;")

    def test_coerces_non_str(self):
        self.assertEqual(hp._e(5), "5")
        self.assertEqual(hp._e(None), "None")


class TestFill(unittest.TestCase):
    def test_single_token(self):
        self.assertEqual(hp._fill("Hello {name}", name="Ana"), "Hello Ana")

    def test_multiple_tokens(self):
        self.assertEqual(hp._fill("{a} and {b}", a="x", b="y"), "x and y")

    def test_absent_token_passes_through(self):
        self.assertEqual(hp._fill("Hi {x}", y="z"), "Hi {x}")

    def test_stray_brace_not_mangled(self):
        self.assertEqual(hp._fill("a { b {n}", n="1"), "a { b 1")


class TestUrlq(unittest.TestCase):
    def test_plain_string_unchanged(self):
        self.assertEqual(hp._urlq("abc"), "abc")

    def test_space_encoded(self):
        self.assertEqual(hp._urlq("a b"), "a%20b")

    def test_slash_is_safe_but_ampersand_encoded(self):
        # urllib quote defaults to safe="/", so "/" passes through while "&" is percent-encoded
        self.assertEqual(hp._urlq("a/b&c"), "a/b%26c")


class TestTitle(unittest.TestCase):
    def test_title_cases_lowercase_name(self):
        # documents current str.title() behavior for a simple lowercase hospital name
        self.assertEqual(hp._title("bridgton hospital"), "Bridgton Hospital")


if __name__ == "__main__":
    unittest.main()
