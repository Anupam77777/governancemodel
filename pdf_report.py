"""
pdf_report.py
Renders the report dict from report.py into a polished, colorful PDF.

Design system:
  - Cover header band with title + scope + generated time
  - Executive summary with color-coded status pills (green / amber / red)
  - Icon-prefixed section headers with colored underline bands
  - Severity-aware tables (impact High/Medium/Low colored)
  - Footer with page numbers + confidentiality note
Errors / unavailable data are shown clearly rather than hidden.
"""
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Flowable,
)

# ---- Palette -------------------------------------------------------------
AZURE      = colors.HexColor("#0078d4")
AZURE_DK   = colors.HexColor("#004578")
AZURE_LT   = colors.HexColor("#e9f2fb")
TEAL       = colors.HexColor("#0099bc")
RED        = colors.HexColor("#c0273c")
RED_LT     = colors.HexColor("#fbe9ec")
GREEN      = colors.HexColor("#0f7b34")
GREEN_LT   = colors.HexColor("#e7f4ec")
AMBER      = colors.HexColor("#b5750a")
AMBER_LT   = colors.HexColor("#fdf2e0")
GREY       = colors.HexColor("#5b6b7c")
GREY_LT    = colors.HexColor("#f1f4f8")
INK        = colors.HexColor("#1b2733")
WHITE      = colors.white
ROW_ALT    = colors.HexColor("#f4f8fc")
BORDER     = colors.HexColor("#d2dded")



def _hx(c):
    """ReportLab color -> #RRGGBB string for <font color=...> tags."""
    return "#" + c.hexval()[2:]

def _styles():
    ss = getSampleStyleSheet()
    ss.add(ParagraphStyle("Cover",  parent=ss["Title"], textColor=WHITE, fontSize=26,
                          leading=30, spaceAfter=2))
    ss.add(ParagraphStyle("CoverSub", parent=ss["Normal"], textColor=colors.HexColor("#dbeafe"),
                          fontSize=11, leading=15))
    ss.add(ParagraphStyle("Sub",    parent=ss["Normal"], textColor=GREY, fontSize=9, spaceAfter=1))
    ss.add(ParagraphStyle("Sect",   parent=ss["Heading2"], textColor=AZURE_DK, fontSize=14,
                          spaceBefore=4, spaceAfter=2, leading=17))
    ss.add(ParagraphStyle("Body",   parent=ss["Normal"], fontSize=9.5, leading=13, textColor=INK))
    ss.add(ParagraphStyle("Good",   parent=ss["Normal"], fontSize=10, textColor=GREEN))
    ss.add(ParagraphStyle("Bad",    parent=ss["Normal"], fontSize=10, textColor=RED))
    ss.add(ParagraphStyle("Cell",   parent=ss["Normal"], fontSize=8, leading=10, textColor=INK))
    ss.add(ParagraphStyle("CellW",  parent=ss["Normal"], fontSize=8.5, leading=11, textColor=WHITE))
    ss.add(ParagraphStyle("Pill",   parent=ss["Normal"], fontSize=8.5, leading=10, alignment=1))
    return ss


# ---- Custom flowables ----------------------------------------------------
class HeaderBand(Flowable):
    """Full-width gradient-ish header band drawn with two rects."""
    def __init__(self, title, lines, width, height=70):
        super().__init__()
        self.title = title
        self.lines = lines
        self.width = width
        self.height = height

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        # base band
        c.setFillColor(AZURE_DK)
        c.rect(0, 0, self.width, self.height, fill=1, stroke=0)
        # lighter overlay strip on top portion
        c.setFillColor(AZURE)
        c.rect(0, self.height * 0.42, self.width, self.height * 0.58, fill=1, stroke=0)
        # accent bar
        c.setFillColor(TEAL)
        c.rect(0, 0, self.width, 4, fill=1, stroke=0)
        # cloud glyph (simple)
        c.setFillColor(colors.Color(1, 1, 1, alpha=0.18))
        c.circle(self.width - 38, self.height - 26, 14, fill=1, stroke=0)
        c.circle(self.width - 56, self.height - 30, 10, fill=1, stroke=0)
        c.circle(self.width - 22, self.height - 30, 10, fill=1, stroke=0)
        c.rect(self.width - 60, self.height - 42, 42, 12, fill=1, stroke=0)
        # title
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(16, self.height - 32, self.title)
        c.setFont("Helvetica", 9.5)
        c.setFillColor(colors.HexColor("#dbeafe"))
        y = self.height - 50
        for ln in self.lines:
            c.drawString(16, y, ln)
            y -= 12


class SectionHeader(Flowable):
    """Icon chip + title + colored underline."""
    def __init__(self, icon, title, width, color=AZURE):
        super().__init__()
        self.icon = icon
        self.title = title
        self.width = width
        self.color = color
        self.height = 24

    def wrap(self, aw, ah):
        return self.width, self.height

    def draw(self):
        c = self.canv
        # chip
        c.setFillColor(self.color)
        c.roundRect(0, 2, 20, 18, 4, fill=1, stroke=0)
        c.setFillColor(WHITE)
        c.setFont("Helvetica-Bold", 11)
        c.drawCentredString(10, 6, self.icon)
        # title
        c.setFillColor(AZURE_DK)
        c.setFont("Helvetica-Bold", 14)
        c.drawString(28, 6, self.title)
        # underline
        c.setStrokeColor(BORDER)
        c.setLineWidth(1)
        c.line(0, 0, self.width, 0)
        c.setStrokeColor(self.color)
        c.setLineWidth(2)
        c.line(0, 0, 70, 0)


def _pill(text, kind):
    """Return a single-cell Table styled as a colored status pill."""
    cmap = {"good": (GREEN_LT, GREEN), "bad": (RED_LT, RED),
            "warn": (AMBER_LT, AMBER), "info": (AZURE_LT, AZURE_DK),
            "na": (GREY_LT, GREY)}
    bg, fg = cmap.get(kind, cmap["info"])
    p = Paragraph(f'<font color="{_hx(fg)}"><b>{text}</b></font>',
                  ParagraphStyle("p", fontSize=8.5, leading=10, alignment=1))
    t = Table([[p]], colWidths=[None])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bg),
        ("BOX", (0, 0), (-1, -1), 0.5, fg),
        ("ROUNDEDCORNERS", [4, 4, 4, 4]),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _impact_color(impact):
    i = str(impact).lower()
    if "high" in i:
        return RED
    if "medium" in i:
        return AMBER
    if "low" in i:
        return GREEN
    return GREY


def _section_error(block, S):
    return [_pill("DATA UNAVAILABLE", "na"),
            Spacer(1, 3),
            Paragraph(f"{block.get('note','Data unavailable.')}", S["Bad"]),
            Paragraph(str(block.get("error", ""))[:300], S["Sub"]),
            Spacer(1, 6)]


def _md_to_flowables(md_text, S):
    """
    Lightweight Markdown -> ReportLab flowables for AI-generated text.
    Supports ### / #### headers, - and * bullets, **bold**, `code`, and
    paragraphs. Not a full Markdown engine — just enough for our prompts.
    """
    import re as _re
    flow = []
    lines = md_text.split("\n")
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flow.append(Spacer(1, 4))
            continue
        # inline formatting
        def inline(t):
            t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            t = _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
            t = _re.sub(r"`(.+?)`", r'<font face="Courier">\1</font>', t)
            return t
        if line.startswith("#### "):
            flow.append(Paragraph(f"<b>{inline(line[5:])}</b>", S["Body"]))
        elif line.startswith("### "):
            flow.append(Spacer(1, 2))
            flow.append(Paragraph(f'<font color="{_hx(AZURE_DK)}"><b>{inline(line[4:])}</b></font>',
                                  ParagraphStyle("aih", parent=S["Body"], fontSize=11, leading=14)))
        elif line.startswith("## "):
            flow.append(Paragraph(f'<font color="{_hx(AZURE_DK)}"><b>{inline(line[3:])}</b></font>',
                                  ParagraphStyle("aih2", parent=S["Body"], fontSize=12, leading=15)))
        elif _re.match(r"^\s*[-*]\s+", line):
            txt = _re.sub(r"^\s*[-*]\s+", "", line)
            flow.append(Paragraph(f"• {inline(txt)}",
                                  ParagraphStyle("aib", parent=S["Body"], leftIndent=10)))
        elif _re.match(r"^\s*\d+\.\s+", line):
            flow.append(Paragraph(inline(line.strip()),
                                  ParagraphStyle("ain", parent=S["Body"], leftIndent=10)))
        else:
            flow.append(Paragraph(inline(line), S["Body"]))
    return flow


def _table(headers, rows, S, col_widths=None, header_color=AZURE,
           impact_col=None):
    data = [[Paragraph(f"<b>{h}</b>", S["CellW"]) for h in headers]]
    for r in rows:
        cells = []
        for ci, c in enumerate(r):
            if impact_col is not None and ci == impact_col:
                col = _impact_color(c)
                cells.append(Paragraph(
                    f'<font color="{_hx(col)}"><b>{c}</b></font>', S["Cell"]))
            else:
                cells.append(Paragraph(str(c), S["Cell"]))
        data.append(cells)
    t = Table(data, colWidths=col_widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), header_color),
        ("LINEBELOW", (0, 0), (-1, 0), 0, header_color),
        ("GRID", (0, 0), (-1, -1), 0.4, BORDER),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, ROW_ALT]),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


# ---- Page furniture (footer) ---------------------------------------------
def _footer(canvas, doc):
    canvas.saveState()
    w, h = A4
    canvas.setStrokeColor(BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(18*mm, 12*mm, w - 18*mm, 12*mm)
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(GREY)
    canvas.drawString(18*mm, 8*mm, "Azure Governance Bot — Confidential")
    canvas.drawRightString(w - 18*mm, 8*mm, f"Page {doc.page}")
    canvas.restoreState()


# ---- Main render ---------------------------------------------------------
def render_pdf(report, out_path):
    S = _styles()
    doc = SimpleDocTemplate(out_path, pagesize=A4,
                            leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=14*mm, bottomMargin=18*mm)
    content_w = doc.width
    E = []
    m = report["meta"]

    # ---- Cover header band
    E.append(HeaderBand(
        "Azure Governance Report",
        [f"Subscription:  {m['subscription_name']}",
         f"Scope:  {m['scope']}",
         f"Subscription ID:  {m['subscription_id']}",
         f"Generated:  {m['generated_utc']}"],
        content_w, height=92))
    E.append(Spacer(1, 12))

    # ---- Executive summary with pills
    E.append(SectionHeader("◆", "Summary of Observations", content_w, AZURE))
    E.append(Spacer(1, 6))

    summary = []  # (label, pill_text, pill_kind)

    b = report["backup"]
    if b["status"] == "ok":
        d = b["data"]
        if d["all_backed_up"]:
            summary.append(("All servers backed up", "YES — all protected", "good"))
        else:
            summary.append(("All servers backed up",
                            f"NO — {d['unprotected_count']}/{d['total_vms']} unprotected", "bad"))
    else:
        summary.append(("All servers backed up", "DATA UNAVAILABLE", "na"))

    n = report["nsg"]
    if n["status"] == "ok":
        d = n["data"]
        summary.append(("NSG open to internet",
                        f"YES — {d['internet_open_count']} rule(s)" if d["has_internet_exposure"]
                        else "No exposure", "bad" if d["has_internet_exposure"] else "good"))
        summary.append(("Broad any-any NSG rules",
                        f"YES — {d['any_any_count']} rule(s)" if d["has_any_any"]
                        else "None", "bad" if d["has_any_any"] else "good"))
    else:
        summary.append(("NSG open to internet", "DATA UNAVAILABLE", "na"))
        summary.append(("Broad any-any NSG rules", "DATA UNAVAILABLE", "na"))

    h = report["health"]
    if h["status"] == "ok":
        d = h["data"]
        summary.append(("Planned maintenance (3 mo)",
                        f"{d['planned_count']} event(s)" if d['planned_count'] else "None found",
                        "warn" if d['planned_count'] else "good"))
        summary.append(("Service retirements (3 mo)",
                        f"{d['retirement_count']} notice(s)" if d['retirement_count'] else "None found",
                        "warn" if d['retirement_count'] else "good"))
    else:
        summary.append(("Planned maintenance (3 mo)", "DATA UNAVAILABLE", "na"))
        summary.append(("Service retirements (3 mo)", "DATA UNAVAILABLE", "na"))

    a = report["advisor"]
    if a["status"] == "ok":
        d = a["data"]
        summary.append(("Advisor — security",
                        f"{d['security_count']} finding(s)",
                        "bad" if d['security_count'] else "good"))
        summary.append(("Advisor — reliability",
                        f"{d['reliability_count']} finding(s)",
                        "warn" if d['reliability_count'] else "good"))
        summary.append(("Advisor — performance",
                        f"{d['performance_count']} finding(s)",
                        "warn" if d['performance_count'] else "good"))
    else:
        summary.append(("Advisor recommendations", "DATA UNAVAILABLE", "na"))

    cost = report["cost"]
    if cost["status"] == "ok":
        d = cost["data"]
        summary.append(("Cost last month (amortized)",
                        f"{d['total']:,.2f} {d['currency']}", "info"))
    else:
        summary.append(("Cost last month", "DATA UNAVAILABLE", "na"))

    stor = report["storage"]
    if stor["status"] == "ok":
        d = stor["data"]
        with_shares = sum(1 for x in d["accounts"] if x["has_file_shares"])
        summary.append(("Storage accounts",
                        f"{d['count']} ({with_shares} w/ file shares)", "info"))
    else:
        summary.append(("Storage accounts", "DATA UNAVAILABLE", "na"))

    inv = report["inventory"]
    if inv["status"] == "ok":
        summary.append(("Total resources in scope", str(inv["data"]["total"]), "info"))

    # Build a 2-column-per-row grid of label + pill
    grid_rows = []
    for label, text, kind in summary:
        grid_rows.append([Paragraph(f"<b>{label}</b>", S["Cell"]), _pill(text, kind)])
    summ_tbl = Table(grid_rows, colWidths=[content_w*0.46, content_w*0.54])
    summ_tbl.setStyle(TableStyle([
        ("ROWBACKGROUNDS", (0, 0), (-1, -1), [WHITE, GREY_LT]),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (0, -1), 8),
    ]))
    E.append(summ_tbl)
    E.append(Spacer(1, 12))

    # ---- 1. Backup
    E.append(SectionHeader("B", "1. Backup Coverage", content_w, GREEN))
    E.append(Spacer(1, 6))
    if b["status"] == "ok":
        d = b["data"]
        E.append(Paragraph(
            f"Vaults scanned: <b>{d['vault_count']}</b> &nbsp;•&nbsp; VMs: <b>{d['total_vms']}</b> "
            f"&nbsp;•&nbsp; Protected: <b>{d['protected_count']}</b> "
            f"&nbsp;•&nbsp; Unprotected: <b>{d['unprotected_count']}</b>", S["Body"]))
        if d["unprotected"]:
            E.append(Spacer(1, 5))
            E.append(Paragraph("Unprotected VMs:", S["Bad"]))
            E.append(_table(["VM Name"], [[v] for v in d["unprotected"]], S,
                            [content_w*0.6], header_color=RED))
    else:
        E += _section_error(b, S)
    E.append(Spacer(1, 12))

    # ---- 2. Patching
    E.append(SectionHeader("P", "2. Patch Assessment (Update Manager)", content_w, TEAL))
    E.append(Spacer(1, 6))
    p = report["patching"]
    if p["status"] == "ok":
        rows = [[v["name"], v.get("last_assessed", ""),
                 v.get("critical_and_security", "-"), v.get("other", "-"),
                 v.get("assessment_status", "")[:55]] for v in p["data"]["vms"]]
        if rows:
            E.append(_table(["VM", "Last Assessed", "Crit+Sec", "Other", "Status"],
                            rows, S, [content_w*0.2, content_w*0.22, content_w*0.11,
                                      content_w*0.1, content_w*0.37], header_color=TEAL))
        else:
            E.append(Paragraph("No VMs found in scope.", S["Body"]))
    else:
        E += _section_error(p, S)
    E.append(Spacer(1, 12))

    # ---- 3 & 4. NSG
    E.append(SectionHeader("N", "3. NSG Internet Exposure", content_w, RED))
    E.append(Spacer(1, 6))
    if n["status"] == "ok":
        d = n["data"]
        if d["internet_open"]:
            rows = [[r["nsg"], r["rg"], r["rule"], r["protocol"], str(r["ports"]), str(r["priority"])]
                    for r in d["internet_open"]]
            E.append(_table(["NSG", "RG", "Rule", "Proto", "Ports", "Prio"], rows, S,
                            [content_w*0.2, content_w*0.18, content_w*0.2,
                             content_w*0.1, content_w*0.18, content_w*0.08], header_color=RED))
        else:
            E.append(Paragraph("✓ No inbound allow rules open to Internet/Any.", S["Good"]))
        E.append(Spacer(1, 10))
        E.append(SectionHeader("!", "4. Broad Any-Any Rules", content_w, AMBER))
        E.append(Spacer(1, 6))
        if d["any_any"]:
            rows = [[r["nsg"], r["rg"], r["rule"], r["protocol"], str(r["priority"])]
                    for r in d["any_any"]]
            E.append(_table(["NSG", "RG", "Rule", "Proto", "Prio"], rows, S,
                            [content_w*0.24, content_w*0.2, content_w*0.24,
                             content_w*0.14, content_w*0.1], header_color=AMBER))
        else:
            E.append(Paragraph("✓ No any-source/any-dest/any-port allow rules.", S["Good"]))
    else:
        E += _section_error(n, S)

    # ---- 5. Inventory
    E.append(PageBreak())
    E.append(SectionHeader("I", "5. Inventory Report", content_w, AZURE))
    E.append(Spacer(1, 6))
    if inv["status"] == "ok":
        d = inv["data"]
        E.append(Paragraph(f"Total resources: <b>{d['total']}</b>", S["Body"]))
        E.append(Spacer(1, 5))
        E.append(_table(["Resource Type", "Count"],
                        [[t, str(c)] for t, c in d["by_type"]], S,
                        [content_w*0.8, content_w*0.2]))
    else:
        E += _section_error(inv, S)
    E.append(Spacer(1, 12))

    # ---- 6. Planned maintenance
    E.append(SectionHeader("M", "6. Planned Maintenance (next 3 months)", content_w, AMBER))
    E.append(Spacer(1, 6))
    if h["status"] == "ok":
        d = h["data"]
        if d["planned_maintenance"]:
            rows = [[x["title"][:55], x["impact_start"][:16], x["status"]]
                    for x in d["planned_maintenance"]]
            E.append(_table(["Title", "Impact Start", "Status"], rows, S,
                            [content_w*0.56, content_w*0.26, content_w*0.18],
                            header_color=AMBER))
        else:
            E.append(Paragraph("✓ No planned maintenance events found.", S["Good"]))
        E.append(Spacer(1, 10))
        E.append(SectionHeader("R", "7. Service Retirements (next 3 months)", content_w, RED))
        E.append(Spacer(1, 6))
        if d["retirements"]:
            rows = [[x["title"][:60], x["impact_start"][:16]] for x in d["retirements"]]
            E.append(_table(["Notice", "Date"], rows, S,
                            [content_w*0.74, content_w*0.26], header_color=RED))
        else:
            E.append(Paragraph("✓ No retirement notices found.", S["Good"]))
        E.append(Spacer(1, 4))
        E.append(Paragraph(d["note"], S["Sub"]))
    else:
        E += _section_error(h, S)
    E.append(Spacer(1, 12))

    # ---- 8. Advisor
    E.append(SectionHeader("A", "8. Azure Advisor Recommendations", content_w, AZURE))
    E.append(Spacer(1, 6))
    if a["status"] == "ok":
        d = a["data"]

        def advisor_block(title, recs, color):
            out = [Paragraph(f'<font color="{_hx(color)}"><b>{title}</b></font>', S["Body"])]
            if recs:
                rows = [[r["problem"][:68], r["impact"], r["impacted_resource"][:26]] for r in recs]
                out.append(_table(["Problem", "Impact", "Resource"], rows, S,
                                  [content_w*0.58, content_w*0.16, content_w*0.26],
                                  header_color=color, impact_col=1))
            else:
                out.append(Paragraph("✓ None.", S["Good"]))
            out.append(Spacer(1, 6))
            return out

        E += advisor_block("Security", d["security"], RED)
        E += advisor_block("Reliability", d["reliability"], AMBER)
        E += advisor_block("Performance", d["performance"], TEAL)
    else:
        E += _section_error(a, S)

    # ---- 9. Cost
    E.append(PageBreak())
    E.append(SectionHeader("$", "9. Cost per Resource — Last Month (Amortized)", content_w, GREEN))
    E.append(Spacer(1, 6))
    if cost["status"] == "ok":
        d = cost["data"]
        E.append(Paragraph(
            f"Total: <b>{d['total']:,.2f} {d['currency']}</b> across {d['count']} resources.",
            S["Body"]))
        E.append(Spacer(1, 5))
        if d["rows"]:
            rows = [[r["resource"][:40], r["type"][:34], f"{r['cost']:,.2f}"] for r in d["rows"]]
            E.append(_table(["Resource", "Type", f"Cost ({d['currency']})"], rows, S,
                            [content_w*0.34, content_w*0.46, content_w*0.2],
                            header_color=GREEN))
        else:
            E.append(Paragraph("No cost records returned for the period.", S["Body"]))
    else:
        E += _section_error(cost, S)
    E.append(Spacer(1, 12))

    # ---- 10. Storage
    E.append(SectionHeader("S", "10. Storage Accounts & File Shares", content_w, AZURE))
    E.append(Spacer(1, 6))
    if stor["status"] == "ok":
        d = stor["data"]
        if d["accounts"]:
            rows = []
            for acct in d["accounts"]:
                if acct["has_file_shares"]:
                    fs = f"Yes ({acct['share_count']})"
                    bk = f"{acct['shares_protected']}/{acct['share_count']} backed up"
                else:
                    fs, bk = "No", "-"
                rows.append([acct["name"][:30], acct["rg"][:22], acct["kind"], fs, bk])
            E.append(_table(["Storage Account", "RG", "Kind", "File Shares", "Backup"],
                            rows, S, [content_w*0.24, content_w*0.2, content_w*0.16,
                                      content_w*0.16, content_w*0.24]))
            unprotected = []
            for acct in d["accounts"]:
                for s in acct["shares"]:
                    if not s["backed_up"]:
                        unprotected.append([acct["name"][:30], s["name"][:40]])
            if unprotected:
                E.append(Spacer(1, 5))
                E.append(Paragraph("File shares without backup:", S["Bad"]))
                E.append(_table(["Storage Account", "File Share"], unprotected, S,
                                [content_w*0.45, content_w*0.55], header_color=RED))
        else:
            E.append(Paragraph("No storage accounts in scope.", S["Body"]))
    else:
        E += _section_error(stor, S)

    # ---- 11. Azure Policy Compliance & Exemptions
    E.append(PageBreak())
    E.append(SectionHeader("P", "11. Azure Policy Compliance & Exemptions", content_w, AZURE))
    E.append(Spacer(1, 6))
    pol = report.get("policy")
    if pol and pol.get("status") == "ok":
        d = pol["data"]
        comp = d.get("compliance")
        if comp:
            pct = comp.get("compliance_pct")
            E.append(Paragraph(
                f"Compliance: <b>{pct if pct is not None else '—'}%</b> "
                f"({comp.get('compliant_resources',0)} compliant / "
                f"{comp.get('evaluated_resources',0)} evaluated). "
                f"Non-compliant resources: <b>{comp.get('non_compliant_resources',0)}</b>, "
                f"across <b>{comp.get('non_compliant_policies',0)}</b> policies.", S["Body"]))
        else:
            E.append(Paragraph("Compliance summary unavailable "
                               f"({d.get('compliance_error','no data')}).", S["Body"]))
        E.append(Spacer(1, 6))

        # Top non-compliant policies
        ncp = d.get("noncompliant_policies", [])
        if ncp:
            E.append(Paragraph("<b>Top non-compliant policies</b>", S["Body"]))
            rows = [[p["policy"][:60], str(p["noncompliant_resources"]), p.get("effect","")[:18]]
                    for p in ncp]
            E.append(_table(["Policy", "Non-compliant", "Effect"], rows, S,
                            [content_w*0.62, content_w*0.2, content_w*0.18]))
            E.append(Spacer(1, 8))

        # Exemptions
        exs = d.get("exemptions", [])
        E.append(Paragraph(f"<b>Policy exemptions ({d.get('exemption_count',0)})</b>", S["Body"]))
        if exs:
            rows = []
            for e in exs:
                rows.append([e["display_name"][:34], e["category"], e["expires_on"][:16],
                             e["policy_assignment_id"][:24]])
            E.append(_table(["Exemption", "Type", "Expires", "Assignment"], rows, S,
                            [content_w*0.3, content_w*0.16, content_w*0.2, content_w*0.34],
                            header_color=AMBER))
        else:
            E.append(Paragraph("No policy exemptions found in scope.", S["Good"]))
    else:
        E += _section_error(pol or {"note": "Policy data unavailable."}, S)

    # ---- 12. AI Policy & Exemption Risk Analysis
    aip = report.get("ai_policy")
    if aip:
        E.append(Spacer(1, 10))
        E.append(SectionHeader("AI", "12. AI Policy & Exemption Risk Analysis", content_w, RED))
        E.append(Spacer(1, 3))
        E.append(Paragraph("Generated by Claude from the policy compliance and "
                           "exemption data above. Review before acting.", S["Sub"]))
        E.append(Spacer(1, 6))
        if aip.get("status") == "ok" and aip.get("text"):
            E += _md_to_flowables(aip["text"], S)
        else:
            E += _section_error(aip, S)

    # ---- 13. AI Remediation Plan
    air = report.get("ai_remediation")
    if air:
        E.append(PageBreak())
        E.append(SectionHeader("AI", "13. AI Remediation Plan", content_w, TEAL))
        E.append(Spacer(1, 3))
        E.append(Paragraph("Generated by Claude from the findings above. Review before acting.",
                           S["Sub"]))
        E.append(Spacer(1, 6))
        if air.get("status") == "ok" and air.get("text"):
            E += _md_to_flowables(air["text"], S)
        else:
            E += _section_error(air, S)

    # ---- 12. AI Cost Optimization
    aic = report.get("ai_cost")
    if aic:
        E.append(Spacer(1, 10))
        E.append(SectionHeader("AI", "14. AI Cost Optimization", content_w, GREEN))
        E.append(Spacer(1, 3))
        E.append(Paragraph("Generated by Claude from cost, inventory, and Advisor data. "
                           "Treat suggestions marked 'verify' as leads, not facts.", S["Sub"]))
        E.append(Spacer(1, 6))
        if aic.get("status") == "ok" and aic.get("text"):
            E += _md_to_flowables(aic["text"], S)
        else:
            E += _section_error(aic, S)

    doc.build(E, onFirstPage=_footer, onLaterPages=_footer)
    return out_path
