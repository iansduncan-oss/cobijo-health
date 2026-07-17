#!/usr/bin/env python3
"""Programmatic SEO: one content-rich page per California hospital, generated from the dataset.

Each page answers the searches patients actually make ("<hospital> financial assistance",
"<hospital> charity care income limits") with the hospital's real rules: the free-care income
table by household size, discount thresholds, required documents, the official application/policy
PDFs, a phone + call script, and a CTA into the interactive tool. Server-rendered HTML (no JS
needed to read it) + JSON-LD so it's crawlable and rich-result eligible.

Localized (T3.1): the hospital DATA is language-neutral, but the boilerplate (headings, lead, table
labels, CTA, footer) is translated via the i18n "hospital" section, so /hospital/<slug> (English) and
/<lang>/hospital/<slug> each carry reciprocal hreflang + a language switcher and are independently
indexable. Share/copy/print affordances (T3.2) turn a page into an offline handout for clinics/social
workers; Plausible events (T3.5) measure the SEO -> tool funnel.

Public API:
  build_index(rows) -> (slug->row dict, oshpdid->slug dict)
  render_hospital(row, slug, index, lang="en") -> html str
  render_directory(index, lang="en") -> html str
  hospital_paths(index) -> [absolute URL, ...] for every hospital × language (sitemap)
  BASE = "https://cobijohealth.org"
"""
import html
import re

import i18n
import state_rules

BASE = "https://cobijohealth.org"


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def build_index(rows):
    """slug -> row (unique slugs; disambiguate collisions by city then oshpdid). Also oshpdid -> slug."""
    slug_to_row, oshpdid_to_slug = {}, {}
    for r in rows:
        base = slugify(r["hospital"])
        slug = base
        if slug in slug_to_row:
            slug = slugify(r["hospital"] + "-" + (r.get("city") or ""))
        if slug in slug_to_row:
            slug = base + "-" + str(r.get("oshpdid") or "")
        slug_to_row[slug] = r
        if r.get("oshpdid"):
            oshpdid_to_slug[str(r["oshpdid"])] = slug
    return slug_to_row, oshpdid_to_slug


def _e(s):
    return html.escape(str(s), quote=True)


def _title(name):
    return name.title()


def _money(v):
    try:
        return "${:,}".format(int(v))
    except (ValueError, TypeError):
        return None


def _fill(s, **kw):
    """Fill {token} placeholders by literal replace — robust against a stray brace in a translation."""
    for k, v in kw.items():
        s = s.replace("{" + k + "}", str(v))
    return s


# --- localized URL + hreflang + switcher helpers ------------------------------------------------- #
# A statute-driven state (T4.1 Phase 2) lives under an /<state>/ namespace (e.g. /il/hospital/<slug>),
# so its pages never collide with California's, which stays at the un-prefixed root (byte-identical).
_DIR_SLUG = {"CA": "california-hospitals", "IL": "illinois-hospitals"}


def _path(lang, slug=None, kind="hospital", state="CA"):
    """Relative path per language. kind='hospital' + slug -> a hospital page; kind='hospital' + no slug
    -> the statewide directory; kind='county' + slug -> a per-county hub. A non-CA state prefixes the
    whole path with /<state>/ (CA is un-prefixed so every existing CA URL is unchanged)."""
    if kind == "county":
        seg = f"hospitals/{slug}"
    elif slug:
        seg = f"hospital/{slug}"
    else:
        seg = _DIR_SLUG.get(state, "california-hospitals")
    if state != "CA":
        seg = f"{state.lower()}/{seg}"
    return f"/{seg}" if lang == "en" else f"/{lang}/{seg}"


def _url(lang, slug=None, kind="hospital", state="CA"):
    return BASE + _path(lang, slug, kind, state)


def _head_links(cur, slug=None, kind="hospital", state="CA"):
    """Reciprocal hreflang alternates + x-default + a self canonical, mirroring web/i18n.py."""
    out = [f'<link rel="alternate" hreflang="{c}" href="{_url(c, slug, kind, state)}">' for c in i18n.LANGS]
    out.append(f'<link rel="alternate" hreflang="x-default" href="{_url("en", slug, kind, state)}">')
    out.append(f'<link rel="canonical" href="{_url(cur, slug, kind, state)}">')
    return "\n".join(out)


def _lang_switch(cur, slug=None, kind="hospital", state="CA"):
    opts = "".join(f'<option value="{_path(code, slug, kind, state)}"{" selected" if code == cur else ""}>{name}</option>'
                   for code, (name, _) in i18n.LANGS.items())
    return ('<select class="langsel" aria-label="Language" onchange="location.href=this.value">'
            + opts + "</select>")


_CSS = """
:root{--ink:#1a2e28;--sand:#f6f1e7;--card:#fffdf8;--teal:#1f6f54;--teal-d:#155540;--gold:#c79a3c;--line:#e3dccb;--muted:#5c6b63}
*{box-sizing:border-box}body{margin:0;background:var(--sand);color:var(--ink);font:17px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:800px;margin:0 auto;padding:0 20px 80px}
a{color:var(--teal-d)}
header{padding:22px 0;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.brand{font-size:22px;font-weight:800;text-decoration:none;color:var(--ink)}.brand span{color:var(--teal)}
.hnav{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.langsel{border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink);padding:8px 12px;min-height:40px;font:inherit;font-weight:600;cursor:pointer}
.crumb{font-size:13.5px;color:var(--muted);margin:6px 0 2px}.crumb a{color:var(--muted)}
h1{font-size:29px;line-height:1.2;margin:6px 0 6px}
.loc{color:var(--muted);font-size:15px;margin:0 0 18px}
.lead{font-size:18px;margin:0 0 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px 24px;margin:16px 0;box-shadow:0 12px 32px -24px rgba(31,111,84,.4)}
h2{font-size:21px;color:var(--teal-d);margin:2px 0 10px}
table{width:100%;border-collapse:collapse;font-size:15.5px;margin:8px 0}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:.3px}
td.amt{font-weight:800;color:var(--teal-d);white-space:nowrap}
ul{padding-left:20px}li{margin:5px 0}
[dir=rtl] ul{padding-left:0;padding-right:20px}
[dir=rtl] th,[dir=rtl] td{text-align:right}
.callbtn{display:inline-flex;align-items:center;gap:8px;background:var(--teal);color:#fff;text-decoration:none;font-weight:800;font-size:18px;border-radius:10px;padding:12px 18px;min-height:44px}
.script{font-size:14.5px;margin:10px 0 0}
.linkrow{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 2px}
.linkbtn{display:inline-flex;align-items:center;gap:7px;text-decoration:none;font-weight:700;font-size:14.5px;border-radius:10px;padding:11px 15px;min-height:44px;background:var(--teal);color:#fff;border:1.5px solid var(--teal);cursor:pointer;font-family:inherit}
.linkbtn.alt{background:#fff;color:var(--teal-d);border-color:var(--line)}
.cta{background:linear-gradient(135deg,#1f6f54,#15503d);color:#fff;text-align:center}
.cta h2{color:#fff}.cta p{opacity:.95;margin:0 0 16px}
.cta a{display:inline-block;background:#fff;color:var(--teal-d);font-weight:800;text-decoration:none;border-radius:12px;padding:14px 26px;font-size:17px}
.chip{display:inline-block;background:#fff4e0;border:1px solid #e8d6a8;color:#7a5c15;border-radius:999px;padding:5px 12px;font-size:12.5px;font-weight:600;margin-bottom:8px}
.note{font-size:13.5px;color:var(--muted)}
.nearby a{display:inline-block;margin:3px 10px 3px 0}
.share{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.copied{color:var(--teal-d);font-weight:700;font-size:14px}
.print-qr{display:none}
footer{color:var(--muted);font-size:13.5px;margin-top:30px;border-top:1px solid var(--line);padding-top:18px}
footer a{color:var(--muted)}
@media print{
  .hnav,.cta,.share,.sharewrap,.nearby,header a[href="/"]{display:none!important}
  body{background:#fff}.card{box-shadow:none;border-color:#ccc;break-inside:avoid}
  a[href]:after{content:""}
  .print-qr{display:flex;gap:16px;align-items:center;margin:18px 0;padding:14px 16px;border:1px solid #ccc;border-radius:12px}
  .print-qr img{width:132px;height:132px;flex:none}
  .print-qr strong{font-size:15px}.print-qr span{color:#444;font-size:13.5px}
}
"""


def _og_image(kind, slug):
    """Per-page share card generated by scripts/gen_og_images.py (language-neutral). Hospital + county
    pages get a unique card; anything else falls back to the site-wide og-image.png."""
    if kind == "hospital" and slug:
        return f"{BASE}/og/hospital/{slug}.png"
    if kind == "county" and slug:
        return f"{BASE}/og/county/{slug}.png"
    return f"{BASE}/og-image.png"


def _print_qr(kind, slug, caption):
    """Print-only QR (hidden on screen via .print-qr; @media print reveals it). Encodes the page's
    English canonical URL so a paper handout posted on a clinic wall taps back to the site.
    Codes are pre-generated by scripts/gen_qr_codes.py and served from /qr/."""
    return (f'<div class="print-qr"><img src="/qr/{kind}/{slug}.svg" width="132" height="132" alt="">'
            f'<div><strong>{_e(caption)}</strong><br><span>cobijohealth.org</span></div></div>')


def _head(title, desc, canonical, lang, slug=None, jsonld="", kind="hospital", state="CA"):
    d = i18n.LANGS.get(lang, ("", "ltr"))[1]
    og = _og_image(kind, slug)
    return f"""<!DOCTYPE html><html lang="{lang}" dir="{d}"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<meta name="description" content="{_e(desc)}">
{_head_links(lang, slug, kind, state)}
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/apple-touch-icon.png"><meta name="theme-color" content="#1f6f54">
<meta property="og:type" content="article"><meta property="og:title" content="{_e(title)}">
<meta property="og:description" content="{_e(desc)}"><meta property="og:url" content="{_e(canonical)}">
<meta property="og:image" content="{og}"><meta property="og:image:width" content="1200"><meta property="og:image:height" content="630"><meta name="twitter:card" content="summary_large_image">
<script defer data-domain="cobijohealth.org" src="https://analytics.aviontechs.com/js/script.outbound-links.js"></script>
<script>window.plausible=window.plausible||function(){{(window.plausible.q=window.plausible.q||[]).push(arguments)}};</script>
{jsonld}
<style>{_CSS}</style></head><body><div class="wrap">"""


def _header(lang, nav_cta, slug=None, kind="hospital", state="CA"):
    return (f'<header><a class="brand" href="{"/" if lang == "en" else "/" + lang + "/"}">Cobijo<span>Health</span></a>'
            f'<div class="hnav"><a href="{"/" if lang == "en" else "/" + lang + "/"}" style="font-weight:700;text-decoration:none">{_e(nav_cta)}</a>'
            f'{_lang_switch(lang, slug, kind, state)}</div></header>')


def _foot(S, lang, foot_note=None):
    p = (lambda seg: (f"/{seg}" if lang == "en" else f"/{lang}/{seg}"))
    home = "/" if lang == "en" else f"/{lang}/"
    # About/FAQ/Privacy are short nav labels the "hospital" section doesn't carry — pull them from the
    # localized common/page catalog so the footer isn't half-English on a translated page.
    common = i18n.strings(lang, "about")
    # h_foot's data-source line is CA-specific; a statute-driven page carries a state-generic s_foot in its
    # merged strings (CA pages don't, so they stay byte-identical on h_foot).
    note = foot_note or S.get("s_foot") or S["h_foot"]
    return (f'<footer><p><a href="{home}">{_e(S["h_check_bill"])}</a> · '
            f'<a href="{p("about")}">{_e(common.get("f_about", "About"))}</a> · '
            f'<a href="{p("faq")}">{_e(common.get("f_faq", "FAQ"))}</a> · '
            f'<a href="{p("privacy")}">{_e(common.get("f_priv", "Privacy"))}</a> · '
            f'<a href="{_path(lang)}">{_e(S["h_dir"])}</a></p>'
            f'<p>{_e(note)}</p></footer></div></body></html>')


def _income_table(ceilings, verb, S):
    rows = []
    for size in range(1, 9):
        v = _money((ceilings or {}).get(str(size)))
        if not v:
            continue
        ppl = S["h_person"] if size == 1 else S["h_people"]
        rows.append(f'<tr><td>{size} {_e(ppl)}</td><td class="amt">{v}<span class="note">{_e(S["h_per_yr"])}</span></td>'
                    f'<td>{_e(verb)}</td></tr>')
    if not rows:
        return ""
    return (f'<table><thead><tr><th>{_e(S["h_tbl_size"])}</th><th>{_e(S["h_tbl_income"])}</th>'
            f'<th>{_e(S["h_tbl_qualify"])}</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


def render_hospital(row, slug, index, lang="en"):
    S = i18n.hospital_strings(lang)
    name = _title(row["hospital"])
    city = (row.get("city") or "").title()
    county = row.get("county")
    canonical = _url(lang, slug)
    pol = row.get("policy") or {}
    free = pol.get("free_care") or {}
    disc = pol.get("discount_payment") or {}
    free_pct = free.get("fpl_ceiling_pct")
    disc_pct = disc.get("fpl_ceiling_pct")
    phone = (pol.get("contact") or {}).get("phone")
    state = row.get("state", "CA")     # per-row state (all current rows are CA); prose below stays CA-specific (Phase 3)
    loc = ", ".join([p for p in [city, (county + " County") if county else None, state] if p])

    city_suffix = f" ({city}, {state})" if city else ""
    title = _fill(S["h_h1"], name=name).replace(" — ", " ") + city_suffix + " | Cobijo Health"
    desc = _fill(S["h_meta"], name=name + (f", {city}" if city else ""))

    # --- JSON-LD: BreadcrumbList (all langs) + FAQPage (English only; FAQ rich results are deprecated,
    #     and an English FAQ on a translated page is a content/lang mismatch we'd rather avoid). ---
    home_url = BASE + ("/" if lang == "en" else f"/{lang}/")
    breadcrumb = (
        '{"@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":1,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":2,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":3,"name":%s,"item":"%s"}]}'
        % (_json(S["h_home"]), home_url, _json(S["h_dir"]), _url(lang), _json(name), canonical))
    graph = [breadcrumb]
    if lang == "en":
        fam4 = _money((row.get("free_care_income_ceiling_by_household") or {}).get("4"))
        faqs = []
        if fam4 and free_pct:
            faqs.append((f"Who qualifies for free care at {name}?",
                         f"Patients at or below {free_pct}% of the Federal Poverty Level generally qualify for "
                         f"free (charity) care. For a household of four that is about {fam4} per year or less."))
        faqs.append((f"Does {name} offer financial assistance?",
                     f"Yes. Under California's Hospital Fair Pricing Act, {name} offers free or discounted care to "
                     f"eligible low-income and uninsured patients. Cobijo Health can check if you qualify in about a minute."))
        if phone:
            faqs.append((f"How do I apply for financial assistance at {name}?",
                         f"Call the hospital's financial-assistance office at {phone}, or download the application "
                         f"form and submit it with proof of income. You can apply even after a bill is sent or in collections."))
        faq_json = ",".join('{"@type":"Question","name":%s,"acceptedAnswer":{"@type":"Answer","text":%s}}'
                            % (_json(q), _json(a)) for q, a in faqs)
        graph.append('{"@type":"FAQPage","mainEntity":[' + faq_json + ']}')
    jsonld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
              + ",".join(graph) + ']}</script>')

    out = [_head(title, desc, canonical, lang, slug, jsonld)]
    out.append(_header(lang, S["h_nav_cta"], slug))
    dir_path = _path(lang)
    home = "/" if lang == "en" else f"/{lang}/"
    out.append(f'<p class="crumb"><a href="{home}">{_e(S["h_home"])}</a> › '
               f'<a href="{dir_path}">{_e(S["h_dir"])}</a> › {_e(name)}</p>')
    if row.get("needs_review"):
        out.append(f'<span class="chip">{_e(S["h_verify"])}</span>')
    out.append(f'<h1>{_e(_fill(S["h_h1"], name=name))}</h1>')
    if loc:
        out.append(f'<p class="loc">{_e(loc)}</p>')
    out.append(f'<p class="lead">{_e(_fill(S["h_lead"], name=name))}</p>')

    free_tbl = _income_table(row.get("free_care_income_ceiling_by_household"), S["h_free_verb"], S)
    if free_tbl:
        out.append(f'<div class="card"><h2>{_e(S["h_free_h"])}</h2>')
        if free_pct:
            out.append(f'<p>{_e(_fill(S["h_free_p"], name=name, pct=int(free_pct)))}</p>')
        out.append(free_tbl)
        out.append('</div>')

    disc_tbl = _income_table(row.get("discount_income_ceiling_by_household"), S["h_disc_verb"], S)
    if disc_tbl:
        out.append(f'<div class="card"><h2>{_e(S["h_disc_h"])}</h2>')
        if disc_pct:
            out.append(f'<p>{_e(_fill(S["h_disc_p"], pct=int(disc_pct)))}</p>')
        out.append(disc_tbl)
        out.append('</div>')

    docs = (pol.get("eligibility_process") or {}).get("documentation_required") or []
    if docs:
        out.append(f'<div class="card"><h2>{_e(S["h_docs_h"])}</h2><ul>')
        for d in docs[:6]:
            out.append(f"<li>{_e(d)}</li>")
        out.append('</ul></div>')

    out.append(f'<div class="card"><h2>{_e(S["h_apply_h"])}</h2>')
    if phone:
        out.append(f'<a class="callbtn" href="tel:{_e(re.sub(r"[^0-9+]", "", phone))}" '
                   f'onclick="plausible(\'hospital_call_clicked\')">📞 {_e(phone)}</a>'
                   f'<p class="script">{_e(S["h_call_script"])}</p>')
    links = []
    if _is_url(row.get("application_url")):
        links.append(f'<a class="linkbtn" href="{_e(row["application_url"])}" target="_blank" rel="noopener">📝 {_e(S["h_get_app"])}</a>')
    if _is_url(row.get("charity_policy_url")):
        links.append(f'<a class="linkbtn alt" href="{_e(row["charity_policy_url"])}" target="_blank" rel="noopener">📄 {_e(S["h_read_policy"])}</a>')
    if links:
        out.append('<div class="linkrow">' + "".join(links) + '</div>')
    if row.get("charity_effective_date"):
        out.append(f'<p class="note" style="margin-top:12px">{_e(_fill(S["h_effective"], date=row["charity_effective_date"]))}</p>')
    out.append('</div>')

    # CTA into the tool. Pass oshpdid so a shared hospital name (Stanford ×2, UCI-Fountain Valley ×2)
    # resolves to THIS exact campus. The tool carries the chosen language via the /<lang>/ prefix.
    tool_home = "/" if lang == "en" else f"/{lang}/"
    cta_q = f'{tool_home}?hospital={_urlq(row["hospital"].title())}'
    if row.get("oshpdid"):
        cta_q += f'&h={_urlq(str(row["oshpdid"]))}'
    out.append(f'<div class="card cta"><h2>{_e(S["h_cta_h"])}</h2>'
               f'<p>{_e(S["h_cta_p"])}</p>'
               f'<a href="{_e(cta_q)}" onclick="plausible(\'hospital_cta_clicked\')">{_e(_fill(S["h_cta_btn"], name=name))}</a></div>')

    # Share / print (T3.2) — an offline handout channel for clinics & social workers.
    out.append(f'<div class="card sharewrap"><h2>{_e(S["h_share_h"])}</h2><div class="share">'
               f'<button type="button" class="linkbtn" onclick="cobShare()">🔗 {_e(S["h_share"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="cobCopy(this)">📋 {_e(S["h_copy"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="window.print()">🖨 {_e(S["h_print"])}</button>'
               f'<span class="copied" id="copied"></span></div></div>')
    out.append(_print_qr("hospital", slug, S["h_share_h"]))

    # internal linking: other hospitals in the same county
    if county:
        same = [(s, r) for s, r in index.items() if r.get("county") == county and s != slug][:10]
        if same:
            # Heading links up to the county hub — gives every hospital page a crawl path to /hospitals/<county>.
            out.append(f'<div class="card nearby"><h2>'
                       f'<a href="{_path(lang, slugify(county), "county")}">{_e(_fill(S["h_nearby"], county=county))}</a>'
                       f'</h2><p class="nearby">'
                       + "".join(f'<a href="{_path(lang, s)}">{_e(_title(r["hospital"]))}</a>' for s, r in same)
                       + '</p></div>')

    out.append(_foot(S, lang))
    out.append(_share_js(_e(S["h_copied"])))
    return "".join(out)


def _share_js(copied_label):
    return ("<script>"
            "function cobCopy(btn){var u=location.href;var d=function(){var c=document.getElementById('copied');"
            "if(c)c.textContent=' " + copied_label + "';plausible('hospital_link_copied');};"
            "if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(u).then(d).catch(function(){});}}"
            "function cobShare(){var u=location.href,t=document.title;"
            "if(navigator.share){navigator.share({title:t,url:u}).then(function(){plausible('hospital_shared');}).catch(function(){});}"
            "else{cobCopy();}}"
            "</script>")


def render_directory(index, lang="en"):
    S = i18n.hospital_strings(lang)
    canonical = _url(lang)
    out = [_head(S["h_dir_title"], S["h_dir_meta"], canonical, lang, None)]
    out.append(_header(lang, S["h_nav_cta"], None))
    home = "/" if lang == "en" else f"/{lang}/"
    out.append(f'<p class="crumb"><a href="{home}">{_e(S["h_home"])}</a> › {_e(S["h_dir"])}</p>')
    out.append(f'<h1>{_e(S["h_dir_h1"])}</h1>')
    out.append(f'<p class="lead">{_e(S["h_dir_lead"])}</p>')
    by_county = {}
    for slug, r in index.items():
        by_county.setdefault(r.get("county") or "Other", []).append((slug, r))
    for county in sorted(by_county):
        hosps = sorted(by_county[county], key=lambda x: x[1]["hospital"])
        label = _fill(S["h_dir_county"], county=county) if "h_dir_county" in S else county + " County"
        # Link the heading to the county hub (a real page) unless it's the catch-all "Other" bucket.
        head = (f'<a href="{_path(lang, slugify(county), "county")}">{_e(label)}</a>'
                if county != "Other" else _e(label))
        out.append(f'<div class="card"><h2>{head}</h2><p class="nearby">'
                   + "".join(f'<a href="{_path(lang, s)}">{_e(_title(r["hospital"]))}</a>' for s, r in hosps)
                   + '</p></div>')
    out.append(_foot(S, lang))
    return "".join(out)


def county_index(index):
    """slug -> canonical county name, from the hospital rows (one entry per county)."""
    out = {}
    for r in index.values():
        c = r.get("county")
        if c:
            out.setdefault(slugify(c), c)
    return out


def render_county(county, index, lang="en"):
    """A per-county hub (/hospitals/<county>): the CA Fair Pricing Act intro, every hospital in the
    county linked to its page, a CTA into the tool, and cross-links to the guides + statewide directory.
    Localized like the hospital pages (hreflang/canonical/switcher, RTL). Additive high-intent SEO."""
    S = {**i18n.hospital_strings(lang), **i18n.county_strings(lang)}
    cslug = slugify(county)
    canonical = _url(lang, cslug, "county")
    hosps = sorted(((s, r) for s, r in index.items() if r.get("county") == county),
                   key=lambda x: x[1]["hospital"])
    home = "/" if lang == "en" else f"/{lang}/"
    dir_path = _path(lang)                     # statewide directory
    h1 = _fill(S["c_h1"], county=county)
    title = h1 + " | Cobijo Health"
    desc = _fill(S["c_meta"], county=county)

    breadcrumb = (
        '{"@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":1,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":2,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":3,"name":%s,"item":"%s"}]}'
        % (_json(S["h_home"]), BASE + home, _json(S["h_dir"]), _url(lang), _json(h1), canonical))
    jsonld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
              + breadcrumb + ']}</script>')

    out = [_head(title, desc, canonical, lang, cslug, jsonld, "county")]
    out.append(_header(lang, S["h_nav_cta"], cslug, "county"))
    out.append(f'<p class="crumb"><a href="{home}">{_e(S["h_home"])}</a> › '
               f'<a href="{dir_path}">{_e(S["h_dir"])}</a> › {_e(h1)}</p>')
    out.append(f'<h1>{_e(h1)}</h1>')
    out.append(f'<p class="lead">{_e(_fill(S["c_lead"], county=county))}</p>')

    out.append(f'<div class="card"><h2>{_e(_fill(S["c_law_h"], county=county))}</h2>'
               f'<p>{_e(_fill(S["c_law_p"], county=county))}</p></div>')

    out.append(f'<div class="card nearby"><h2>{_e(_fill(S["c_list_h"], county=county))} ({len(hosps)})</h2><p class="nearby">'
               + "".join(f'<a href="{_path(lang, s)}">{_e(_title(r["hospital"]))}</a>' for s, r in hosps)
               + '</p></div>')

    out.append(f'<div class="card cta"><h2>{_e(S["c_cta_h"])}</h2><p>{_e(S["c_cta_p"])}</p>'
               f'<a href="{home}" onclick="plausible(\'county_cta_clicked\')">{_e(S["c_cta_btn"])}</a></div>')

    out.append(f'<div class="card nearby"><h2>{_e(S["c_guides_h"])}</h2><p class="nearby">'
               + "".join(f'<a href="{i18n.guide_path(lang, g)}">{_e(i18n.guide_nav_label(lang, g))}</a>'
                         for g in i18n.GUIDES)
               + f'<a href="{dir_path}">{_e(S["c_dir_link"])}</a></p></div>')

    # Share / copy / print — makes the print handout (below) discoverable, like the hospital pages.
    # Reuses the already-translated h_* strings (S includes hospital_strings), so no new i18n keys.
    out.append(f'<div class="card sharewrap"><h2>{_e(S["h_share_h"])}</h2><div class="share">'
               f'<button type="button" class="linkbtn" onclick="cobShare()">🔗 {_e(S["h_share"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="cobCopy(this)">📋 {_e(S["h_copy"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="window.print()">🖨 {_e(S["h_print"])}</button>'
               f'<span class="copied" id="copied"></span></div></div>')
    out.append(_print_qr("county", cslug, S["h_share_h"]))
    out.append(_foot(S, lang))
    out.append(_share_js(_e(S["h_copied"])))
    return "".join(out)


def county_paths(index):
    """Absolute URLs for the sitemap — every county × every language."""
    return [_url(lang, cslug, "county") for cslug in county_index(index) for lang in i18n.LANGS]


def hospital_paths(index):
    """Absolute URLs for the sitemap — every hospital × every language + the directory × every language."""
    urls = [_url(lang, s) for s in index for lang in i18n.LANGS]
    urls += [_url(lang) for lang in i18n.LANGS]
    return urls


# --- Statute-driven state pages (T4.1 Phase 2 increment 2c) -------------------------------------- #
# For a statute-driven state (IL), eligibility comes from the LAW (state_rules), not a per-hospital FAP.
# So instead of CA's per-hospital income tables, these pages cite the statutory bands + an honest "this is
# the legal minimum, the hospital may be more generous — apply to find out" caveat, and route into the
# tool via ?il=<ccn>. They reuse ALL the shared chrome (_head/_header/_foot/share/print/JSON-LD helpers).

def _statutory_bands(row):
    """(rules, state_code, state_name, rural, free_pct, discount_pct, cap, law) for a statute row.
    Rural/Critical-Access hospitals use the lower statutory bands."""
    rules = state_rules.rules_for(row.get("state"))
    rural = row.get("hospital_type") == "Critical Access Hospitals"
    return (rules, rules.code, rules.name, rural,
            rules.free_pct_for(rural), rules.discount_pct_for(rural), rules.income_cap_pct, rules.fap_law)


def render_statutory_hospital(row, slug, index, lang="en"):
    """A per-hospital page for a statute-driven state. Same chrome as the CA hospital page, but the body
    cites the state law's guaranteed bands (honest legal floor) rather than an extracted per-hospital policy."""
    S = {**i18n.hospital_strings(lang), **i18n.statutory_strings(lang)}
    _, state, state_name, rural, free_pct, disc_pct, cap, law = _statutory_bands(row)
    name = _title(row["hospital"])
    city = (row.get("city") or "").title()
    county = row.get("county")
    phone = row.get("phone")
    canonical = _url(lang, slug, "hospital", state)
    loc = ", ".join([p for p in [city, (county + " County") if county else None, state] if p])
    title = _fill(S["h_h1"], name=name).replace(" — ", " ") + (f" ({city}, {state})" if city else "") + " | Cobijo Health"
    desc = _fill(S["s_meta"], name=name + (f", {city}" if city else ""), state=state_name)

    home_url = BASE + ("/" if lang == "en" else f"/{lang}/")
    breadcrumb = (
        '{"@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":1,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":2,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":3,"name":%s,"item":"%s"}]}'
        % (_json(S["h_home"]), home_url, _json(_fill(S["s_dir"], state=state_name)),
           _url(lang, None, "hospital", state), _json(name), canonical))
    org = ('{"@type":"MedicalOrganization","name":%s,"areaServed":%s%s}'
           % (_json(name), _json(state_name),
              (',"telephone":' + _json(phone)) if phone else ""))
    jsonld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
              + breadcrumb + "," + org + ']}</script>')

    out = [_head(title, desc, canonical, lang, slug, jsonld, "hospital", state)]
    out.append(_header(lang, S["h_nav_cta"], slug, "hospital", state))
    dir_path = _path(lang, None, "hospital", state)
    home = "/" if lang == "en" else f"/{lang}/"
    out.append(f'<p class="crumb"><a href="{home}">{_e(S["h_home"])}</a> › '
               f'<a href="{dir_path}">{_e(_fill(S["s_dir"], state=state_name))}</a> › {_e(name)}</p>')
    out.append(f'<h1>{_e(_fill(S["h_h1"], name=name))}</h1>')
    if loc:
        out.append(f'<p class="loc">{_e(loc)}</p>')
    out.append(f'<p class="lead">{_e(_fill(S["s_h_lead"], name=name, state=state_name))}</p>')

    # What the law guarantees — the statutory bands (rural-adjusted), + an honest "legal minimum" caveat.
    out.append(f'<div class="card"><h2>{_e(_fill(S["s_law_h"], state=state_name))}</h2>'
               f'<p>{_e(_fill(S["s_law_p"], name=name, law=law, free_pct=free_pct, discount_pct=disc_pct, cap=cap))}</p>')
    if rural:
        out.append(f'<p>{_e(_fill(S["s_rural_note"], name=name, state=state_name, free_pct=free_pct, discount_pct=disc_pct))}</p>')
    out.append(f'<p class="note">{_e(_fill(S["s_minimum_note"], name=name))}</p></div>')

    # How to apply — reuse the CA chrome (phone + call script).
    out.append(f'<div class="card"><h2>{_e(S["h_apply_h"])}</h2>')
    if phone:
        out.append(f'<a class="callbtn" href="tel:{_e(re.sub(r"[^0-9+]", "", phone))}" '
                   f'onclick="plausible(\'hospital_call_clicked\')">📞 {_e(phone)}</a>'
                   f'<p class="script">{_e(S["h_call_script"])}</p>')
    out.append('</div>')

    # CTA into the tool — carry the CCN via ?il= so the plan is derived from state law for THIS hospital.
    tool_home = "/" if lang == "en" else f"/{lang}/"
    cta_q = f'{tool_home}?il={_urlq(str(row.get("ccn") or ""))}&hospital={_urlq(name)}'
    out.append(f'<div class="card cta"><h2>{_e(S["h_cta_h"])}</h2><p>{_e(S["h_cta_p"])}</p>'
               f'<a href="{_e(cta_q)}" onclick="plausible(\'hospital_cta_clicked\')">{_e(_fill(S["h_cta_btn"], name=name))}</a></div>')

    out.append(f'<div class="card sharewrap"><h2>{_e(S["h_share_h"])}</h2><div class="share">'
               f'<button type="button" class="linkbtn" onclick="cobShare()">🔗 {_e(S["h_share"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="cobCopy(this)">📋 {_e(S["h_copy"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="window.print()">🖨 {_e(S["h_print"])}</button>'
               f'<span class="copied" id="copied"></span></div></div>')

    if county:
        same = [(s, r) for s, r in index.items() if r.get("county") == county and s != slug][:10]
        if same:
            out.append(f'<div class="card nearby"><h2>'
                       f'<a href="{_path(lang, slugify(county), "county", state)}">{_e(_fill(S["h_nearby"], county=county))}</a>'
                       f'</h2><p class="nearby">'
                       + "".join(f'<a href="{_path(lang, s, "hospital", state)}">{_e(_title(r["hospital"]))}</a>' for s, r in same)
                       + '</p></div>')

    out.append(_foot(S, lang))
    out.append(_share_js(_e(S["h_copied"])))
    return "".join(out)


def render_statutory_county(county, index, lang="en", state="IL"):
    """A per-county hub for a statute-driven state: the state law intro + every hospital in the county."""
    S = {**i18n.hospital_strings(lang), **i18n.county_strings(lang), **i18n.statutory_strings(lang)}
    rules = state_rules.rules_for(state)
    state_name, law = rules.name, rules.fap_law
    free_pct, disc_pct = rules.free_pct_for(False), rules.discount_pct_for(False)   # metro bands at county level
    cslug = slugify(county)
    canonical = _url(lang, cslug, "county", state)
    hosps = sorted(((s, r) for s, r in index.items() if r.get("county") == county),
                   key=lambda x: x[1]["hospital"])
    home = "/" if lang == "en" else f"/{lang}/"
    dir_path = _path(lang, None, "hospital", state)
    h1 = _fill(S["s_c_h1"], county=county, state=state_name)
    title = h1 + " | Cobijo Health"
    desc = _fill(S["s_c_meta"], county=county, state=state_name)

    breadcrumb = (
        '{"@type":"BreadcrumbList","itemListElement":['
        '{"@type":"ListItem","position":1,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":2,"name":%s,"item":"%s"},'
        '{"@type":"ListItem","position":3,"name":%s,"item":"%s"}]}'
        % (_json(S["h_home"]), BASE + home, _json(_fill(S["s_dir"], state=state_name)),
           _url(lang, None, "hospital", state), _json(h1), canonical))
    jsonld = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":['
              + breadcrumb + ']}</script>')

    out = [_head(title, desc, canonical, lang, cslug, jsonld, "county", state)]
    out.append(_header(lang, S["h_nav_cta"], cslug, "county", state))
    out.append(f'<p class="crumb"><a href="{home}">{_e(S["h_home"])}</a> › '
               f'<a href="{dir_path}">{_e(_fill(S["s_dir"], state=state_name))}</a> › {_e(h1)}</p>')
    out.append(f'<h1>{_e(h1)}</h1>')
    out.append(f'<p class="lead">{_e(_fill(S["s_c_lead"], county=county, state=state_name))}</p>')

    out.append(f'<div class="card"><h2>{_e(_fill(S["c_law_h"], county=county))}</h2>'
               f'<p>{_e(_fill(S["s_c_law_p"], county=county, law=law, free_pct=free_pct, discount_pct=disc_pct))}</p></div>')

    out.append(f'<div class="card nearby"><h2>{_e(_fill(S["c_list_h"], county=county))} ({len(hosps)})</h2><p class="nearby">'
               + "".join(f'<a href="{_path(lang, s, "hospital", state)}">{_e(_title(r["hospital"]))}</a>' for s, r in hosps)
               + '</p></div>')

    out.append(f'<div class="card cta"><h2>{_e(S["c_cta_h"])}</h2><p>{_e(S["c_cta_p"])}</p>'
               f'<a href="{home}" onclick="plausible(\'county_cta_clicked\')">{_e(S["c_cta_btn"])}</a></div>')

    out.append(f'<div class="card sharewrap"><h2>{_e(S["h_share_h"])}</h2><div class="share">'
               f'<button type="button" class="linkbtn" onclick="cobShare()">🔗 {_e(S["h_share"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="cobCopy(this)">📋 {_e(S["h_copy"])}</button>'
               f'<button type="button" class="linkbtn alt" onclick="window.print()">🖨 {_e(S["h_print"])}</button>'
               f'<span class="copied" id="copied"></span></div></div>')
    out.append(_foot(S, lang))
    out.append(_share_js(_e(S["h_copied"])))
    return "".join(out)


def render_statutory_directory(index, lang="en", state="IL"):
    """The statewide directory for a statute-driven state, grouped by county (links to the county hubs)."""
    S = {**i18n.hospital_strings(lang), **i18n.statutory_strings(lang)}
    rules = state_rules.rules_for(state)
    state_name, law = rules.name, rules.fap_law
    canonical = _url(lang, None, "hospital", state)
    out = [_head(_fill(S["s_dir_title"], state=state_name), _fill(S["s_dir_meta"], state=state_name),
                 canonical, lang, None, "", "hospital", state)]
    out.append(_header(lang, S["h_nav_cta"], None, "hospital", state))
    home = "/" if lang == "en" else f"/{lang}/"
    out.append(f'<p class="crumb"><a href="{home}">{_e(S["h_home"])}</a> › {_e(_fill(S["s_dir"], state=state_name))}</p>')
    out.append(f'<h1>{_e(_fill(S["s_dir_h1"], state=state_name))}</h1>')
    out.append(f'<p class="lead">{_e(_fill(S["s_dir_lead"], state=state_name, law=law))}</p>')
    by_county = {}
    for slug, r in index.items():
        by_county.setdefault(r.get("county") or "Other", []).append((slug, r))
    for county in sorted(by_county):
        hosps = sorted(by_county[county], key=lambda x: x[1]["hospital"])
        label = county + " County"
        head = (f'<a href="{_path(lang, slugify(county), "county", state)}">{_e(label)}</a>'
                if county != "Other" else _e(label))
        out.append(f'<div class="card"><h2>{head}</h2><p class="nearby">'
                   + "".join(f'<a href="{_path(lang, s, "hospital", state)}">{_e(_title(r["hospital"]))}</a>' for s, r in hosps)
                   + '</p></div>')
    out.append(_foot(S, lang))
    return "".join(out)


def statutory_hospital_paths(index, state="IL"):
    """Absolute URLs for the sitemap — every statute-driven hospital × language + the directory × language."""
    urls = [_url(lang, s, "hospital", state) for s in index for lang in i18n.LANGS]
    urls += [_url(lang, None, "hospital", state) for lang in i18n.LANGS]
    return urls


def statutory_county_paths(index, state="IL"):
    return [_url(lang, cslug, "county", state) for cslug in county_index(index) for lang in i18n.LANGS]


# --- tiny helpers (stdlib only) ---
def _json(s):
    import json
    return json.dumps(str(s), ensure_ascii=False)


def _urlq(s):
    from urllib.parse import quote
    return quote(s)


def _is_url(u):
    return isinstance(u, str) and u.startswith(("http://", "https://"))
