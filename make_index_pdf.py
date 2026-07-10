#!/usr/bin/env python3
"""Render index_current.json as a clean paginated PDF table for the HCAI CPRA request."""
import json
import os
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer)

rows = json.load(open("data/index_current.json"))
rows.sort(key=lambda r: (r.get("county", ""), r.get("post_title", "")))
os.makedirs("output", exist_ok=True)

styles = getSampleStyleSheet()
title = ParagraphStyle("t", parent=styles["Title"], fontSize=15, spaceAfter=4)
sub = ParagraphStyle("s", parent=styles["Normal"], fontSize=9, textColor=colors.grey, spaceAfter=10)
cell = ParagraphStyle("c", parent=styles["Normal"], fontSize=8, leading=9)

doc = SimpleDocTemplate("output/hcai-hospital-index.pdf", pagesize=letter,
                        leftMargin=0.6*inch, rightMargin=0.6*inch,
                        topMargin=0.6*inch, bottomMargin=0.6*inch,
                        title="HCAI Hospital Fair Pricing Policy Index (469 hospitals)",
                        author="Cobijo Health")

story = [Paragraph("Hospital Fair Pricing Policy Lookup — Facility Index", title),
         Paragraph("469 California hospitals currently listed in HCAI's Hospital Fair Pricing "
                   "Policy Lookup. Scope reference attachment to Cobijo Health's Public Records "
                   "Act request. Sorted by county, then facility name.", sub)]

header = ["#", "Facility", "City", "ZIP", "County"]
data = [header]
for i, r in enumerate(rows, 1):
    data.append([str(i),
                 Paragraph(r.get("post_title", ""), cell),
                 Paragraph((r.get("city") or "").title(), cell),
                 r.get("zip", ""),
                 Paragraph(r.get("county", ""), cell)])

tbl = Table(data, colWidths=[0.35*inch, 3.6*inch, 1.5*inch, 0.7*inch, 1.05*inch], repeatRows=1)
tbl.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f6f54")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("FONTSIZE", (0, 0), (-1, 0), 9),
    ("FONTSIZE", (0, 1), (-1, -1), 8),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#eef4f1")]),
    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cccccc")),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ("TOPPADDING", (0, 0), (-1, -1), 2),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
]))
story.append(tbl)
doc.build(story)
print("wrote hcai-hospital-index.pdf")
