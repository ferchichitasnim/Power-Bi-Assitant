#!/usr/bin/env python3
"""ReportLab PDF renderer for Power BI technical documentation."""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Callable

from reportlab.lib import colors
from reportlab.lib.colors import HexColor, white
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

# ── Colors (reference palette) ──
PRIMARY = HexColor("#1B2A4A")
SECONDARY = HexColor("#2D5AA0")
ACCENT = HexColor("#3B82F6")
LIGHT_BG = HexColor("#F1F5F9")
BORDER = HexColor("#E2E8F0")
TEXT_DARK = HexColor("#1E293B")
TEXT_MED = HexColor("#475569")
TEXT_LIGHT = HexColor("#94A3B8")
SUCCESS = HexColor("#10B981")
WARNING = HexColor("#F59E0B")
DANGER = HexColor("#EF4444")
ROW_ALT = HexColor("#F8FAFC")

W, H = A4

styles = {
    "section_title": ParagraphStyle(
        "section_title",
        fontName="Helvetica-Bold",
        fontSize=18,
        textColor=PRIMARY,
        leading=24,
        spaceBefore=20,
        spaceAfter=8,
    ),
    "section_num": ParagraphStyle(
        "section_num",
        fontName="Helvetica-Bold",
        fontSize=11,
        textColor=ACCENT,
        leading=14,
        spaceBefore=0,
        spaceAfter=2,
    ),
    "subsection": ParagraphStyle(
        "subsection",
        fontName="Helvetica-Bold",
        fontSize=13,
        textColor=SECONDARY,
        leading=18,
        spaceBefore=14,
        spaceAfter=6,
    ),
    "body": ParagraphStyle(
        "body",
        fontName="Helvetica",
        fontSize=9.5,
        textColor=TEXT_DARK,
        leading=14,
        spaceAfter=6,
    ),
    "body_small": ParagraphStyle(
        "body_small",
        fontName="Helvetica",
        fontSize=8.5,
        textColor=TEXT_MED,
        leading=12,
        spaceAfter=4,
    ),
    "caption": ParagraphStyle(
        "caption",
        fontName="Helvetica-Oblique",
        fontSize=8,
        textColor=TEXT_LIGHT,
        leading=11,
        spaceAfter=8,
    ),
    "kpi_value": ParagraphStyle(
        "kpi_value",
        fontName="Helvetica-Bold",
        fontSize=22,
        textColor=PRIMARY,
        leading=26,
        alignment=TA_CENTER,
    ),
    "kpi_label": ParagraphStyle(
        "kpi_label",
        fontName="Helvetica",
        fontSize=8,
        textColor=TEXT_MED,
        leading=11,
        alignment=TA_CENTER,
    ),
    "table_header": ParagraphStyle(
        "table_header",
        fontName="Helvetica-Bold",
        fontSize=8,
        textColor=white,
        leading=12,
    ),
    "table_cell": ParagraphStyle(
        "table_cell",
        fontName="Helvetica",
        fontSize=8,
        textColor=TEXT_DARK,
        leading=11,
    ),
    "table_cell_code": ParagraphStyle(
        "table_cell_code",
        fontName="Courier",
        fontSize=7,
        textColor=TEXT_DARK,
        leading=10,
    ),
}


class DocTemplate(BaseDocTemplate):
    def __init__(self, buffer: io.BytesIO, header_left: str, generated_at: str, **kw: Any):
        super().__init__(buffer, **kw)
        self.header_left = header_left
        self.generated_at = generated_at
        frame = Frame(20 * mm, 18 * mm, W - 40 * mm, H - 36 * mm, id="main")
        self.addPageTemplates([PageTemplate(id="content", frames=frame, onPage=self._draw_page_elements)])

    def _draw_page_elements(self, canvas_obj: Any, doc: Any) -> None:
        canvas_obj.saveState()
        canvas_obj.setStrokeColor(ACCENT)
        canvas_obj.setLineWidth(2)
        canvas_obj.line(20 * mm, H - 14 * mm, W - 20 * mm, H - 14 * mm)

        canvas_obj.setFont("Helvetica", 7)
        canvas_obj.setFillColor(TEXT_LIGHT)
        canvas_obj.drawString(20 * mm, H - 12 * mm, self.header_left)
        canvas_obj.drawRightString(W - 20 * mm, H - 12 * mm, "Power BI Assistant")

        canvas_obj.setStrokeColor(BORDER)
        canvas_obj.setLineWidth(0.5)
        canvas_obj.line(20 * mm, 14 * mm, W - 20 * mm, 14 * mm)
        canvas_obj.setFont("Helvetica", 7)
        canvas_obj.setFillColor(TEXT_LIGHT)
        canvas_obj.drawString(
            20 * mm,
            10 * mm,
            f"Généré le {self.generated_at} — Confidentiel",
        )
        canvas_obj.drawRightString(W - 20 * mm, 10 * mm, f"Page {doc.page}")
        canvas_obj.restoreState()


def _escape_xml(text: str) -> str:
    return (
        str(text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _para(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_escape_xml(text).replace("\n", "<br/>"), style)


def _make_section_header(story: list[Any], num: str, title: str) -> None:
    story.append(_para(num, styles["section_num"]))
    story.append(_para(title, styles["section_title"]))
    story.append(HRFlowable(width="100%", thickness=1, color=ACCENT, spaceAfter=4 * mm))


def _make_data_table(
    headers: list[str],
    rows: list[list[str]],
    col_widths: list[float] | None = None,
    code_cols: set[int] | None = None,
) -> Table:
    code_cols = code_cols or set()
    header_row = [_para(h, styles["table_header"]) for h in headers]
    data_rows: list[list[Paragraph]] = []
    for row in rows:
        cells: list[Paragraph] = []
        for i, cell in enumerate(row):
            if i in code_cols:
                cells.append(_para(str(cell), styles["table_cell_code"]))
            else:
                cells.append(_para(str(cell), styles["table_cell"]))
        data_rows.append(cells)

    all_data = [header_row] + data_rows
    if col_widths is None:
        col_widths = [(W - 40 * mm) / len(headers)] * len(headers)

    table = Table(all_data, colWidths=col_widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), PRIMARY),
                ("TEXTCOLOR", (0, 0), (-1, 0), white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("FONTSIZE", (0, 0), (-1, 0), 8),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("GRID", (0, 0), (-1, -1), 0.5, BORDER),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [white, ROW_ALT]),
            ]
        )
    )
    return table


def _warning_box(text: str) -> Table:
    warn_style = ParagraphStyle(
        "warn",
        fontName="Helvetica",
        fontSize=8.5,
        textColor=HexColor("#92400E"),
        leading=12,
    )
    content = Paragraph(f"<b>⚠ Attention :</b> {_escape_xml(text)}", warn_style)
    box = Table([[content]], colWidths=[W - 40 * mm])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#FEF3C7")),
                ("BOX", (0, 0), (-1, -1), 1, WARNING),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    return box


def _info_box(text: str) -> Table:
    info_style = ParagraphStyle(
        "info",
        fontName="Helvetica",
        fontSize=8.5,
        textColor=HexColor("#1E40AF"),
        leading=12,
    )
    content = Paragraph(f"<b>ℹ Impact du RLS :</b> {_escape_xml(text)}", info_style)
    box = Table([[content]], colWidths=[W - 40 * mm])
    box.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), HexColor("#EFF6FF")),
                ("BOX", (0, 0), (-1, -1), 1, HexColor("#93C5FD")),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )
    return box


def _strength_row(text: str) -> Table:
    check_style = ParagraphStyle("ck", fontName="Helvetica-Bold", fontSize=10, textColor=SUCCESS)
    row = Table(
        [[Paragraph("✓", check_style), _para(text, styles["body"])]],
        colWidths=[8 * mm, W - 48 * mm],
    )
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 0)]))
    return row


def _issue_row(severity: str, issue: str) -> Table:
    severity = severity or "Faible"
    if severity == "Élevé":
        bg = DANGER
    elif severity == "Moyen":
        bg = WARNING
    else:
        bg = TEXT_LIGHT

    sev_style = ParagraphStyle("sev", fontName="Helvetica-Bold", fontSize=7.5, textColor=white, leading=10)
    badge = Table([[Paragraph(severity, sev_style)]], colWidths=[16 * mm], rowHeights=[5 * mm])
    badge.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 1),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ]
        )
    )
    row = Table([[badge, _para(issue, styles["body_small"])]], colWidths=[20 * mm, W - 60 * mm])
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 0)]))
    return row


def _recommendation_row(num: str, title: str, desc: str) -> Table:
    rec_title = Paragraph(
        f"<b>{_escape_xml(title)}</b>",
        ParagraphStyle("rt", fontName="Helvetica-Bold", fontSize=9, textColor=TEXT_DARK, leading=13),
    )
    rec_desc = _para(desc, styles["body_small"])
    num_style = ParagraphStyle("rn", fontName="Helvetica-Bold", fontSize=11, textColor=white, alignment=TA_CENTER)
    num_cell = Table([[Paragraph(num, num_style)]], colWidths=[8 * mm], rowHeights=[8 * mm])
    num_cell.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), ACCENT),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    content_cell = Table([[rec_title], [rec_desc]], colWidths=[W - 56 * mm])
    content_cell.setStyle(
        TableStyle(
            [
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    row = Table([[num_cell, content_cell]], colWidths=[12 * mm, W - 52 * mm])
    row.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (0, -1), 0)]))
    return row


def _build_cover(story: list[Any], doc: dict[str, Any]) -> None:
    filename = doc["filename"]
    generated_at = doc["generated_at"]
    model_name = doc["model_name"]
    data_source_label = doc.get("data_source_label") or "Non détectée"
    kpis = doc["kpis"]

    story.append(Spacer(1, 40 * mm))

    tag_style = ParagraphStyle("tag", fontName="Helvetica-Bold", fontSize=8, textColor=ACCENT, leading=10)
    tag = Table(
        [[Paragraph("DOCUMENTATION TECHNIQUE", tag_style)]],
        colWidths=[W - 40 * mm],
        rowHeights=[14 * mm],
    )
    tag.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(tag)

    story.append(_para(filename, styles["section_title"]))
    title_style = ParagraphStyle(
        "big_title",
        fontName="Helvetica-Bold",
        fontSize=26,
        textColor=PRIMARY,
        leading=32,
        spaceAfter=8,
    )
    story.append(Paragraph("Documentation du Modèle<br/>Power BI", title_style))
    story.append(Spacer(1, 4 * mm))
    story.append(HRFlowable(width="30%", thickness=3, color=ACCENT, spaceAfter=8 * mm))

    meta_data = [
        ["Outil", "Power BI Assistant"],
        ["Date de génération", generated_at],
        ["Modèle LLM", model_name],
        ["Source de données", data_source_label],
    ]
    meta_rows = []
    for label, value in meta_data:
        meta_rows.append(
            [
                Paragraph(f"<b>{_escape_xml(label)}</b>", ParagraphStyle("ml", fontName="Helvetica-Bold", fontSize=9, textColor=TEXT_MED, leading=12)),
                Paragraph(_escape_xml(value), ParagraphStyle("mv", fontName="Helvetica", fontSize=9, textColor=TEXT_DARK, leading=12)),
            ]
        )
    meta_table = Table(meta_rows, colWidths=[45 * mm, 100 * mm], rowHeights=[8 * mm] * 4)
    meta_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, -1), 0),
                ("LINEBELOW", (0, 0), (-1, -2), 0.5, BORDER),
            ]
        )
    )
    story.append(meta_table)
    story.append(Spacer(1, 20 * mm))

    kpi_cells = [
        [
            _para(str(kpis.get("tables", 0)), styles["kpi_value"]),
            _para(str(kpis.get("measures", 0)), styles["kpi_value"]),
            _para(str(kpis.get("calculated_columns", 0)), styles["kpi_value"]),
            _para(str(kpis.get("relationships", 0)), styles["kpi_value"]),
        ],
        [
            _para("Tables", styles["kpi_label"]),
            _para("Mesures DAX", styles["kpi_label"]),
            _para("Colonnes Calculées", styles["kpi_label"]),
            _para("Relations", styles["kpi_label"]),
        ],
    ]
    kpi_table = Table(kpi_cells, colWidths=[(W - 40 * mm) / 4] * 4, rowHeights=[12 * mm, 6 * mm])
    kpi_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("BOX", (0, 0), (-1, -1), 1, BORDER),
                ("LINEBEFORE", (1, 0), (1, -1), 0.5, BORDER),
                ("LINEBEFORE", (2, 0), (2, -1), 0.5, BORDER),
                ("LINEBEFORE", (3, 0), (3, -1), 0.5, BORDER),
                ("BACKGROUND", (0, 0), (-1, -1), LIGHT_BG),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
            ]
        )
    )
    story.append(kpi_table)
    story.append(PageBreak())


def _build_toc(story: list[Any], sections: list[dict[str, str]]) -> None:
    _make_section_header(story, "01", "Table des Matières")
    story.append(Spacer(1, 4 * mm))

    for item in sections:
        num = item["num"]
        title = item["title"]
        desc = item["desc"]
        row = Table(
            [
                [
                    Paragraph(
                        f"<b>{num}</b>",
                        ParagraphStyle("tn", fontName="Helvetica-Bold", fontSize=11, textColor=ACCENT, leading=14),
                    ),
                    Paragraph(
                        f"<b>{_escape_xml(title)}</b><br/><font size=8 color=\"#{TEXT_MED.hexval()[2:]}\">{_escape_xml(desc)}</font>",
                        ParagraphStyle("td", fontName="Helvetica", fontSize=10, textColor=TEXT_DARK, leading=15),
                    ),
                ]
            ],
            colWidths=[12 * mm, W - 52 * mm],
            rowHeights=[14 * mm],
        )
        row.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("LINEBELOW", (0, 0), (-1, -1), 0.5, BORDER),
                    ("LEFTPADDING", (0, 0), (0, -1), 0),
                ]
            )
        )
        story.append(row)

    story.append(PageBreak())


def build_reportlab_pdf(doc: dict[str, Any]) -> bytes:
    """Build PDF bytes from a prepared documentation dict."""
    buffer = io.BytesIO()
    generated_at = doc.get("generated_at") or datetime.now().strftime("%d/%m/%Y %H:%M")
    header_left = f"{doc.get('filename', 'model.pbix')} — Documentation Technique"

    template = DocTemplate(
        buffer,
        header_left=header_left,
        generated_at=generated_at,
        pagesize=A4,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
    )

    story: list[Any] = []
    _build_cover(story, doc)

    toc_sections = doc.get("toc_sections") or []
    if toc_sections:
        _build_toc(story, toc_sections)

    for section in doc.get("content_sections") or []:
        builder: Callable[[list[Any], dict[str, Any]], None] = section["builder"]
        builder(story, section.get("data") or {})

    template.build(story)
    return buffer.getvalue()


# ── Section builders ──


def section_overview(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Vue d'Ensemble du Modèle")
    for para in data.get("paragraphs") or []:
        story.append(Paragraph(para, styles["body"]))
    story.append(PageBreak())


def section_sources(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Sources de Données")
    if data.get("intro"):
        story.append(Paragraph(data["intro"], styles["body"]))
    story.append(Spacer(1, 3 * mm))
    rows = data.get("rows") or []
    if rows:
        story.append(
            _make_data_table(
                ["Type", "Serveur", "Base de données", "Mode", "Statut"],
                rows,
                col_widths=[25 * mm, 30 * mm, 35 * mm, 35 * mm, 25 * mm],
            )
        )
    story.append(Spacer(1, 3 * mm))
    if data.get("caption"):
        story.append(_para(data["caption"], styles["caption"]))
    story.append(PageBreak())


def section_tables(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Tables et Schéma")
    if data.get("intro"):
        story.append(Paragraph(data["intro"], styles["body"]))
    story.append(Spacer(1, 3 * mm))

    fact_tables = data.get("fact_tables") or []
    if fact_tables:
        story.append(_para("Tables de Faits", styles["subsection"]))
        story.append(
            _make_data_table(
                ["Table", "Colonnes", "Rôle Métier", "Colonnes Principales"],
                fact_tables,
                col_widths=[28 * mm, 14 * mm, 30 * mm, 78 * mm],
            )
        )
        story.append(Spacer(1, 4 * mm))

    dim_tables = data.get("dim_tables") or []
    if dim_tables:
        story.append(_para("Tables de Dimensions", styles["subsection"]))
        story.append(
            _make_data_table(
                ["Table", "Colonnes", "Description"],
                dim_tables,
                col_widths=[32 * mm, 14 * mm, 104 * mm],
            )
        )

    if data.get("caption"):
        story.append(Spacer(1, 3 * mm))
        story.append(_para(data["caption"], styles["caption"]))
    story.append(PageBreak())


def section_relationships(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Relations")
    if data.get("intro"):
        story.append(Paragraph(data["intro"], styles["body"]))
    story.append(Spacer(1, 3 * mm))
    rows = data.get("rows") or []
    if rows:
        cw = [38 * mm, 38 * mm, 12 * mm, 14 * mm, 10 * mm, 38 * mm]
        story.append(
            _make_data_table(
                ["De", "Vers", "Card.", "Filtre", "Actif", "Remarque"],
                rows,
                col_widths=cw,
            )
        )
    if data.get("warning"):
        story.append(Spacer(1, 3 * mm))
        story.append(_warning_box(data["warning"]))
    story.append(PageBreak())


def section_measures(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Mesures DAX")
    if data.get("intro"):
        story.append(Paragraph(data["intro"], styles["body"]))
    story.append(Spacer(1, 3 * mm))

    for domain, measures in (data.get("groups") or {}).items():
        if not measures:
            continue
        story.append(_para(domain, styles["subsection"]))
        rows = [[m.get("name", ""), m.get("business_desc", ""), m.get("dax_logic", "")] for m in measures]
        story.append(
            _make_data_table(
                ["Mesure", "Description Métier", "Logique DAX"],
                rows,
                col_widths=[35 * mm, 60 * mm, 55 * mm],
                code_cols={2},
            )
        )
        story.append(Spacer(1, 4 * mm))
    story.append(PageBreak())


def section_calculated_columns(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Colonnes Calculées DAX")
    if data.get("intro"):
        story.append(Paragraph(data["intro"], styles["body"]))
    story.append(Spacer(1, 3 * mm))
    rows = data.get("rows") or []
    if rows:
        story.append(
            _make_data_table(
                ["Table", "Colonne", "Expression Simplifiée", "Description"],
                rows,
                col_widths=[30 * mm, 24 * mm, 50 * mm, 46 * mm],
                code_cols={2},
            )
        )
    story.append(PageBreak())


def section_rls(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Sécurité — Row-Level Security")
    if data.get("intro"):
        story.append(Paragraph(data["intro"], styles["body"]))
    story.append(Spacer(1, 3 * mm))
    rows = data.get("rows") or []
    if rows:
        story.append(
            _make_data_table(
                ["Rôle", "Table Filtrée", "Expression DAX", "Description"],
                rows,
                col_widths=[20 * mm, 32 * mm, 55 * mm, 43 * mm],
                code_cols={2},
            )
        )
    if data.get("info"):
        story.append(Spacer(1, 4 * mm))
        story.append(_info_box(data["info"]))
    story.append(PageBreak())


def section_audit(story: list[Any], data: dict[str, Any]) -> None:
    _make_section_header(story, data["num"], "Audit et Recommandations")

    story.append(_para("Points Forts", styles["subsection"]))
    for strength in data.get("strengths") or []:
        story.append(_strength_row(strength))

    story.append(Spacer(1, 4 * mm))
    story.append(_para("Points de Vigilance", styles["subsection"]))
    for severity, issue in data.get("issues") or []:
        story.append(_issue_row(severity, issue))
        story.append(Spacer(1, 1.5 * mm))

    story.append(Spacer(1, 6 * mm))
    story.append(_para("Recommandations Prioritaires", styles["subsection"]))
    for num, title, desc in data.get("recommendations") or []:
        story.append(_recommendation_row(str(num), title, desc))
        story.append(Spacer(1, 2 * mm))


SECTION_BUILDERS = {
    "overview": section_overview,
    "sources": section_sources,
    "tables": section_tables,
    "relationships": section_relationships,
    "measures": section_measures,
    "calculated_columns": section_calculated_columns,
    "rls": section_rls,
    "audit": section_audit,
}
