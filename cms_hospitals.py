#!/usr/bin/env python3
"""CMS hospital roster -> per-state statute-driven dataset (T4.1 Phase 2 "thin national").

Nationally there is NO HCAI-equivalent structured FAP database. But for STATUTE-DRIVEN states (whose law
sets the charity-care eligibility thresholds directly — see state_rules.STATES), a correct patient answer
needs only the hospital LIST + the statutory thresholds, NOT a per-hospital FAP PDF. The authoritative,
free, machine-readable hospital list is the CMS Provider Data Catalog "Hospital General Information"
dataset (xubh-q36u): every US hospital with name, address, county, ownership, and CCN.

This module resolves that CSV (via the DKAN metastore, so it survives the file-hash rotating), filters to
one state, drops federal VA hospitals (not subject to state law + their own system), and normalizes each
row to the app's dataset shape with `status="statutory"` and `policy=None` — the navigator derives the
plan from state_rules, not an extracted policy.

Usage:
  python3 cms_hospitals.py --state IL --out data/dataset_il.json
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.request

METASTORE = ("https://data.cms.gov/provider-data/api/1/metastore/schemas/dataset/items/"
             "xubh-q36u?show-reference-ids")

# Federal hospitals aren't subject to a state's uninsured-discount law and run their own assistance.
_EXCLUDE_OWNERSHIP = {"Veterans Health Administration", "Department of Defense"}


def resolve_csv_url():
    """The CSV downloadURL from CMS's DKAN metastore (hash in the path rotates, so never hardcode it)."""
    with urllib.request.urlopen(METASTORE, timeout=60) as r:
        meta = json.load(r)
    for dist in meta.get("distribution", []):
        url = (dist.get("data") or dist).get("downloadURL")
        if url and url.lower().endswith(".csv"):
            return url
    raise RuntimeError("no CSV distribution found for xubh-q36u")


def fetch_rows(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        text = r.read().decode("utf-8-sig", "replace")
    return list(csv.DictReader(io.StringIO(text)))


def normalize_cms_row(r, state):
    """One CMS row -> the app's dataset shape. `policy=None` + `status="statutory"` mark a statute-driven
    row: the plan comes from state_rules, not a per-hospital FAP. Unit-testable in isolation (no network)."""
    def g(*keys):
        for k in keys:
            v = (r.get(k) or "").strip()
            if v:
                return v
        return ""
    county = g("County/Parish", "County Name").title() or None
    return {
        "hospital": g("Facility Name").upper(),          # match the CA rows' uppercase convention
        "ccn": g("Facility ID", "CCN"),                  # CMS Certification Number = the stable id (no oshpdid)
        "city": g("City/Town", "City").upper(),
        "county": county,
        "zip": g("ZIP Code", "ZIP"),
        "state": state,
        "phone": g("Telephone Number") or None,
        "ownership": g("Hospital Ownership") or None,
        "hospital_type": g("Hospital Type") or None,     # "Critical Access Hospitals" -> rural tier (Phase 2.x)
        "status": "statutory",
        "policy": None,
    }


def build(state, out_path):
    url = resolve_csv_url()
    rows = fetch_rows(url)
    kept = [normalize_cms_row(r, state) for r in rows
            if (r.get("State") or "").strip().upper() == state.upper()
            and (r.get("Hospital Ownership") or "").strip() not in _EXCLUDE_OWNERSHIP
            and (r.get("Facility Name") or "").strip()]
    kept.sort(key=lambda x: x["hospital"])
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(kept, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="IL")
    ap.add_argument("--out", default="data/dataset_il.json")
    a = ap.parse_args()
    kept = build(a.state, a.out)
    print(f"{a.state}: wrote {len(kept)} hospitals -> {a.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
