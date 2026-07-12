#!/usr/bin/env python3
"""
Shared constants for Cobijo Health — the numbers that must stay identical everywhere they're
used, so they can't silently drift between the extractor (extract_llm.py), the navigator
(navigator.py), and the PolicyEngine bridge (policyengine.py).

Before this module the 2026 FPL table lived (byte-identical) in TWO files and the PolicyEngine
year was hardcoded to 2026 in THREE places — a January update could touch one and miss another.
Now they live here once.
"""
import datetime

# 2026 HHS Federal Poverty Guidelines (48 states + DC), annual USD. Effective 1/13/2026.
# Verified 2026-07 against ASPE: https://aspe.hhs.gov/topics/poverty-economic-mobility/poverty-guidelines
# Re-verify each January when new guidelines publish — every eligibility % divides by these.
FPL = {1: 15960, 2: 21640, 3: 27320, 4: 33000, 5: 38680, 6: 44360, 7: 50040, 8: 55720}
FPL_EACH_ADDITIONAL = 5680

# The eligibility year PolicyEngine models. Derived from today's date so the benefit call never
# silently models a stale year come January (the FPL table above is a separate, manual January
# refresh — PolicyEngine carries its own year-specific Medi-Cal / ACA rules internally).
BENEFIT_YEAR = datetime.date.today().year
