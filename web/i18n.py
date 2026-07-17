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

# The one lever for the /support page's Donate button. Empty = no financial-giving path is live yet
# (Cobijo is a 501(c)(3) *in formation*; a fiscal sponsor must be confirmed before we can solicit
# tax-deductible gifts). While empty, /support shows an honest "giving opens soon" note and the
# non-monetary ways to help. Set this to the donation URL — fiscal-sponsor page, GitHub Sponsors, or
# a Stripe payment link — and the button appears. That is the ONLY change needed to turn giving on.
SUPPORT_URL = ""

# --- Embeddable widget (embed.js loads /embed in an iframe on partner sites) ---------------------- #
# The embed page must NOT be indexed (the homepage is the canonical tool) and must tell its host page
# how tall it is so the iframe can auto-size with no inner scrollbar. Injected only when embed=True.
EMBED_HEAD = '<meta name="robots" content="noindex">'
EMBED_SCRIPT = (
    "<script>\n"
    "(function(){\n"
    "  function post(){\n"
    "    var h=Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);\n"
    "    try{ parent.postMessage({cobijo:'height', height:h}, '*'); }catch(_){}\n"
    "  }\n"
    "  window.addEventListener('load', post);\n"
    "  window.addEventListener('resize', post);\n"
    "  if(window.ResizeObserver){ new ResizeObserver(post).observe(document.body); }\n"
    "  else { setInterval(post, 500); }\n"
    "})();\n"
    "</script>"
)

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
PAGES = ("home", "landing", "about", "privacy", "faq", "support", "for-partners")   # pages this engine serves (home = the tool, at /)
SECTION = {"home": "tool", "for-partners": "partners"}   # page -> i18n section name when they differ

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


def render(page, lang, embed=False):
    """Render a page. `embed=True` (only meaningful for `home`) serves the tool as a chromeless iframe
    widget: the same template with the nav/footer stripped (body.embed CSS), a noindex tag, and a
    postMessage height reporter so the host page can auto-size the iframe. The embed hooks are empty
    on every normal render, so the live homepage is byte-for-byte unchanged."""
    if lang not in LANGS:
        lang = DEFAULT
    tpl = open(os.path.join(TPL_DIR, f"{page}.html"), encoding="utf-8").read()
    tool_json = json.dumps(_section(lang, "tool"), ensure_ascii=False).replace("</", "<\\/")
    html = (tpl.replace("{{LANG}}", lang)
               .replace("{{DIR}}", LANGS[lang][1])
               .replace("{{HEAD_LINKS}}", _head_links(page, lang))
               .replace("{{LANG_SWITCH}}", _lang_switch(page, lang))
               .replace("{{ORG_JSONLD}}", ORG_JSONLD)
               .replace("{{TOOL_STRINGS_JSON}}", tool_json)
               .replace("{{BODY_CLASS}}", "embed" if embed else "")
               .replace("{{EMBED_HEAD}}", EMBED_HEAD if embed else "")
               .replace("{{EMBED_SCRIPT}}", EMBED_SCRIPT if embed else ""))
    s = strings(lang, page)
    if page == "support":
        # Config-driven Donate button: rendered only when a giving URL is live (see SUPPORT_URL).
        # Until then the page shows an honest "giving opens soon" note from the copy itself.
        cta = (f'<p><a class="cta" href="{SUPPORT_URL}" rel="noopener" target="_blank">'
               f'{s.get("give_cta", "Donate")}</a></p>') if SUPPORT_URL else \
              f'<p class="soon">{s.get("give_soon", "")}</p>'
        html = html.replace("{{DONATE_CTA}}", cta)
    # English is authoritative (no banner); /support is English-only until its 9-language fan-out lands,
    # so suppress the "machine-translated" banner there too rather than claim a translation that isn't.
    if lang == DEFAULT or (page == "support" and not _load_lang(lang).get("support")):
        s = {**s, "mt_note": ""}
    for k, v in s.items():
        html = html.replace("{{%s}}" % k, v)
    return html


def hospital_strings(lang):
    """The 'hospital' section (per-hospital SEO pages + directory), English-filled. Consumed by
    hospital_pages.py so those pages localize the same way the template pages do."""
    return _section(lang, "hospital")


def county_strings(lang):
    """The 'county' section (per-county hub pages), English-filled. hospital_pages merges this over
    hospital_strings so the hubs reuse the same shared chrome (h_home, h_dir, h_foot, …)."""
    return _section(lang, "county")


def statutory_strings(lang):
    """The 'statutory' section (T4.1 Phase 2): state-law prose for statute-driven states (IL), merged over
    hospital/county strings. State-generic via {state}/{law}/{free_pct}/{discount_pct}/{cap} tokens."""
    return _section(lang, "statutory")


# --- Evergreen explainer guides (T3.3): /guides/<slug> + /<lang>/guides/<slug> ------------------- #
# slug -> i18n section. One shared template (templates/guide.html); each guide's copy is its section.
GUIDES = {
    "cant-afford-hospital-bill-california": "guide_afford",
    "how-to-apply-for-charity-care": "guide_apply",
    "medical-bill-in-collections": "guide_collections",
    "undocumented-immigrant-hospital-bill-california": "guide_undoc",
    "how-to-negotiate-a-hospital-bill-california": "guide_negotiate",
    "emergency-room-bill-no-insurance-california": "guide_er",
    "surprise-medical-bill-california": "guide_surprise",
    "hospital-presumptive-eligibility-california": "guide_pe",
    "medi-cal-vs-charity-care-california": "guide_medical",
    "hospital-bill-errors-california": "guide_errors",
    "medical-debt-credit-report-california": "guide_credit",
    "hospital-payment-plan-rights-california": "guide_payplan",
}


def guide_path(lang, slug):
    return f"/guides/{slug}" if lang == DEFAULT else f"/{lang}/guides/{slug}"


def guide_url(lang, slug):
    return SITE + guide_path(lang, slug)


def guide_nav_label(lang, slug):
    """The short cross-link label for a guide (its section's nav_label), English-filled."""
    return _section(lang, GUIDES[slug]).get("nav_label", slug)


def _guide_head_links(cur, slug):
    out = [f'<link rel="alternate" hreflang="{c}" href="{guide_url(c, slug)}">' for c in LANGS]
    out.append(f'<link rel="alternate" hreflang="x-default" href="{guide_url(DEFAULT, slug)}">')
    out.append(f'<link rel="canonical" href="{guide_url(cur, slug)}">')
    return "\n".join(out)


def _guide_lang_switch(cur, slug):
    opts = "".join(f'<option value="{guide_path(code, slug)}"{" selected" if code == cur else ""}>{name}</option>'
                   for code, (name, _) in LANGS.items())
    return ('<select class="langsel" aria-label="Language" '
            'onchange="location.href=this.value">' + opts + "</select>")


def render_guide(slug, lang):
    """Render one evergreen guide. Reuses the localized `common` chrome + `about` disclaimer/footer,
    with the guide's own section (title/h1/lead/body) winning on collisions. JSON-LD is built via
    json.dumps so every block is valid by construction (no hand-quoted URLs)."""
    if lang not in LANGS:
        lang = DEFAULT
    if slug not in GUIDES:
        return None
    sec = GUIDES[slug]
    # common (brand/nav/footer) + about (disc_card + already-translated footer nav) + county (the
    # "guides that can help" heading) + the guide copy (wins on any collision).
    s = {**_section(lang, "common"), **_section(lang, "about"), **_section(lang, "hospital"),
         **_section(lang, "county"), **_section(lang, sec)}
    canonical = guide_url(lang, slug)
    home = "/" if lang == DEFAULT else f"/{lang}/"
    p = lambda seg: (f"/{seg}" if lang == DEFAULT else f"/{lang}/{seg}")  # noqa: E731

    # steps/rights list — skip any empty item so a guide can carry fewer than 5.
    items = "".join(f"<li>{s[k]}</li>" for k in ("li1", "li2", "li3", "li4", "li5") if s.get(k))

    # related-guides cross-links (the other guides), localized labels.
    rel = "".join(f'<a href="{guide_path(lang, g)}">{guide_nav_label(lang, g)}</a>'
                  for g in GUIDES if g != slug)

    graph = [
        {"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": s.get("brand_a", "Cobijo") + s.get("brand_b", "Health"),
             "item": SITE + home},
            {"@type": "ListItem", "position": 2, "name": s["h1"], "item": canonical}]},
        {"@type": "Article", "headline": s["h1"], "description": s["meta"], "inLanguage": lang,
         "mainEntityOfPage": canonical,
         "publisher": {"@type": ["MedicalOrganization", "NGO"], "name": "Cobijo Health",
                       "url": "https://cobijohealth.org/"}},
    ]
    jsonld = ('<script type="application/ld+json">'
              + json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False).replace("</", "<\\/")
              + "</script>")

    # Print-only QR (scripts/gen_qr_codes.py) — turns the printed guide handout back into a tap.
    # Caption reuses the already-translated `h_share_h` (from the merged hospital section) so this
    # adds no new i18n keys. Shown only under @media print (see guide.html).
    print_qr = (f'<div class="print-qr"><img src="/qr/guide/{slug}.svg" width="132" height="132" alt="">'
                f'<div><strong>{s.get("h_share_h", "")}</strong><br><span>cobijohealth.org</span></div></div>')

    tpl = open(os.path.join(TPL_DIR, "guide.html"), encoding="utf-8").read()
    repl = {
        "{{LANG}}": lang, "{{DIR}}": LANGS[lang][1],
        "{{HEAD_LINKS}}": _guide_head_links(lang, slug),
        "{{LANG_SWITCH}}": _guide_lang_switch(lang, slug),
        "{{JSONLD}}": jsonld,
        "{{HOME}}": home,
        "{{P_ABOUT}}": p("about"), "{{P_FAQ}}": p("faq"), "{{P_PRIV}}": p("privacy"),
        "{{P_SUPPORT}}": p("support"), "{{P_PARTNERS}}": p("for-partners"),
        "{{P_DIR}}": "/california-hospitals" if lang == DEFAULT else f"/{lang}/california-hospitals",
        "{{LIST}}": items, "{{RELATED}}": rel,
        "{{OG_IMAGE}}": f"{SITE}/og/guide/{slug}.png",
        "{{PRINT_QR}}": print_qr,
    }
    if lang == DEFAULT:
        s = {**s, "mt_note": ""}
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    for k, v in s.items():
        tpl = tpl.replace("{{%s}}" % k, v)
    return tpl


def guide_paths():
    return [guide_url(lang, slug) for slug in GUIDES for lang in LANGS]


def sitemap_paths():
    """All (path) URLs this engine owns, every language — for the sitemap."""
    return ([url(lang, page) for page in PAGES for lang in LANGS] + guide_paths())
