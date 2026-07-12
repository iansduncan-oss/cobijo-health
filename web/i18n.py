#!/usr/bin/env python3
"""
Tiny i18n engine for Cobijo Health — per-language URLs, server-rendered.

English is the source of truth and the fallback for any missing key. Non-English pages
live at /<lang>/<page> (e.g. /es/about, /zh/); English stays at the root (/about, /).

  strings(lang, page)   -> merged {common + page} dict for that language (en fallback)
  render(page, lang)    -> full HTML: fills {{KEY}} placeholders + <html lang/dir>,
                           hreflang alternates + self canonical, and the language switcher
  PAGES                 -> the pages this engine owns (templates/<page>.html must exist)

Design goals: no deps (stdlib json only), never crash a request (missing lang/key -> en),
and emit Google-correct hreflang + canonical so every language is independently indexable.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
I18N_DIR = os.path.join(HERE, "i18n")
TPL_DIR = os.path.join(HERE, "templates")
SITE = "https://cobijohealth.org"
DEFAULT = "en"

# code -> (endonym shown in the switcher, text direction)
LANGS = {
    "en": ("English", "ltr"),
    "es": ("Español", "ltr"),
    "zh": ("中文", "ltr"),
    "vi": ("Tiếng Việt", "ltr"),
    "tl": ("Tagalog", "ltr"),
    "ko": ("한국어", "ltr"),
    "hy": ("Հայերեն", "ltr"),
    "fa": ("فارسی", "rtl"),
    "ar": ("العربية", "rtl"),
    "ru": ("Русский", "ltr"),
}
PAGES = ("home", "landing", "about", "privacy", "faq")   # pages this engine serves (home = the tool, at /)
SECTION = {"home": "tool"}             # page -> i18n section name when they differ

# Organization schema (MedicalOrganization + NGO) for the homepage/landing — the structured data that
# actually helps per 2026 research (FAQ/SearchAction rich results are deprecated). English is standard.
ORG_JSONLD = '<script type="application/ld+json">' + json.dumps({
    "@context": "https://schema.org",
    "@type": ["MedicalOrganization", "NGO"],
    "name": "Cobijo Health",
    "url": "https://cobijohealth.org/",
    "logo": "https://cobijohealth.org/icon-512.png",
    "description": ("Free, multilingual tool that helps California patients get financial help for "
                    "medical bills — hospital charity care and Medi-Cal / Covered California screening."),
    "email": "hello@cobijohealth.org",
    "foundingDate": "2026",
    "nonprofitStatus": "Nonprofit501c3",
    "areaServed": {"@type": "State", "name": "California"},
    "contactPoint": {"@type": "ContactPoint", "email": "hello@cobijohealth.org",
                     "contactType": "customer support",
                     "availableLanguage": ["en", "es", "zh", "vi", "tl", "ko", "hy", "fa", "ar", "ru"]},
    "sameAs": ["https://github.com/iansduncan-oss/cobijo-health"],
}, ensure_ascii=False) + "</script>"

_cache = {}


def _load_lang(lang):
    if lang not in _cache:
        path = os.path.join(I18N_DIR, f"{lang}.json")
        try:
            _cache[lang] = json.load(open(path, encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            _cache[lang] = {}
    return _cache[lang]


def _section(lang, sec):
    """One section's strings for a language, English-filled."""
    en = _load_lang("en")
    out = dict(en.get(sec, {}))
    if lang != "en":
        for k, v in _load_lang(lang).get(sec, {}).items():
            if v:
                out[k] = v
    return out


def strings(lang, page):
    """Merged {common + page-section} strings; English fills any gap so a page never renders blank."""
    sec = SECTION.get(page, page)
    return {**_section(lang, "common"), **_section(lang, sec)}


def _path(lang, page):
    seg = "" if page in ("", "home") else page
    if lang == DEFAULT:
        return f"/{seg}" if seg else "/"
    return f"/{lang}/{seg}" if seg else f"/{lang}/"


def url(lang, page):
    return SITE + _path(lang, page)


def _head_links(page, cur):
    """Reciprocal hreflang alternates + x-default + a self-referential canonical."""
    out = [f'<link rel="alternate" hreflang="{c}" href="{url(c, page)}">' for c in LANGS]
    out.append(f'<link rel="alternate" hreflang="x-default" href="{url(DEFAULT, page)}">')
    out.append(f'<link rel="canonical" href="{url(cur, page)}">')
    return "\n".join(out)


def _lang_switch(page, cur):
    opts = []
    for code, (name, _) in LANGS.items():
        sel = " selected" if code == cur else ""
        opts.append(f'<option value="{_path(code, page)}"{sel}>{name}</option>')
    return ('<select class="langsel" aria-label="Language" '
            'onchange="location.href=this.value">' + "".join(opts) + "</select>")


def render(page, lang):
    if lang not in LANGS:
        lang = DEFAULT
    tpl = open(os.path.join(TPL_DIR, f"{page}.html"), encoding="utf-8").read()
    tool_json = json.dumps(_section(lang, "tool"), ensure_ascii=False).replace("</", "<\\/")
    html = (tpl.replace("{{LANG}}", lang)
               .replace("{{DIR}}", LANGS[lang][1])
               .replace("{{HEAD_LINKS}}", _head_links(page, lang))
               .replace("{{LANG_SWITCH}}", _lang_switch(page, lang))
               .replace("{{ORG_JSONLD}}", ORG_JSONLD)
               .replace("{{TOOL_STRINGS_JSON}}", tool_json))
    s = strings(lang, page)
    if lang == DEFAULT:
        s = {**s, "mt_note": ""}      # English is authoritative — no "machine-translated" banner
    for k, v in s.items():
        html = html.replace("{{%s}}" % k, v)
    return html


def hospital_strings(lang):
    """The 'hospital' section (per-hospital SEO pages + directory), English-filled. Consumed by
    hospital_pages.py so those pages localize the same way the template pages do."""
    return _section(lang, "hospital")


def sitemap_paths():
    """All (path) URLs this engine owns, every language — for the sitemap."""
    return [url(lang, page) for page in PAGES for lang in LANGS]
