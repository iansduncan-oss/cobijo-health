#!/usr/bin/env python3
"""Unit coverage for the pure URL/path helpers in web/i18n.py.

_path/url build the localized route + absolute URL for a page; guide_path/guide_url do the same for
a guide slug. They drive every hreflang alternate, canonical, and sitemap URL, so pinning them guards
against a silent SEO regression (e.g. a default-language page suddenly emitting "/en/" instead of "/").
Pure string builders — no eligibility or legal logic. (Distinct from hospital_pages._path.)

Run (from repo root):  python3 -m unittest tests.test_i18n_paths
"""
import os
import sys
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
for p in (_ROOT, _WEB):
    if p not in sys.path:
        sys.path.insert(0, p)

import i18n


class TestI18nPath(unittest.TestCase):
    def test_default_lang_home_is_root(self):
        self.assertEqual(i18n._path(i18n.DEFAULT, "home"), "/")

    def test_default_lang_empty_page_is_root(self):
        self.assertEqual(i18n._path(i18n.DEFAULT, ""), "/")

    def test_default_lang_named_page_unprefixed(self):
        self.assertEqual(i18n._path(i18n.DEFAULT, "about"), "/about")

    def test_non_default_lang_named_page_prefixed(self):
        self.assertEqual(i18n._path("es", "about"), "/es/about")

    def test_non_default_lang_home_is_lang_root(self):
        # home/"" collapse to the bare language root, not "/es/home"
        self.assertEqual(i18n._path("es", "home"), "/es/")
        self.assertEqual(i18n._path("es", ""), "/es/")

    def test_url_prepends_site(self):
        self.assertEqual(i18n.url("es", "about"), i18n.SITE + "/es/about")
        self.assertEqual(i18n.url(i18n.DEFAULT, "home"), i18n.SITE + "/")


class TestGuidePath(unittest.TestCase):
    def test_default_lang_guide_unprefixed(self):
        self.assertEqual(i18n.guide_path(i18n.DEFAULT, "hospital-bill-errors-california"),
                         "/guides/hospital-bill-errors-california")

    def test_non_default_lang_guide_prefixed(self):
        self.assertEqual(i18n.guide_path("es", "hospital-bill-errors-california"),
                         "/es/guides/hospital-bill-errors-california")

    def test_guide_url_prepends_site(self):
        self.assertEqual(i18n.guide_url("es", "some-slug"), i18n.SITE + "/es/guides/some-slug")


if __name__ == "__main__":
    unittest.main()
