"""Turn an approved text/Markdown deliverable into a clean PDF for member chats.

Pure, local and deterministic: converts the already-verified artifact bytes
(never regenerates content). Non-text artifacts pass through unchanged.
"""
from __future__ import annotations

import html
import io
import re
from pathlib import Path

TEXT_SUFFIXES = {".md", ".markdown", ".txt"}


def needs_pdf(name: str) -> bool:
    return Path(str(name)).suffix.lower() in TEXT_SUFFIXES


def pdf_name(name: str) -> str:
    stem = Path(str(name)).stem or "document"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.") + ".pdf"


def _inline(text: str) -> str:
    s = html.escape(text, quote=False)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", s)
    s = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<link href='\2' color='#1f4e8c'>\1</link>", s)
    return s


def markdown_to_pdf(raw: bytes, title_hint: str = "") -> bytes:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    base = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=base["BodyText"], fontName="Helvetica", fontSize=10.5, leading=15, spaceAfter=6)
    h = {1: ParagraphStyle("h1", parent=base["Heading1"], fontName="Helvetica-Bold", fontSize=19, leading=23, spaceAfter=10),
         2: ParagraphStyle("h2", parent=base["Heading2"], fontName="Helvetica-Bold", fontSize=14, leading=18, spaceBefore=10, spaceAfter=6),
         3: ParagraphStyle("h3", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=12, leading=15, spaceBefore=8, spaceAfter=4)}
    story, bullets, para = [], [], []

    def flush_para():
        if para:
            story.append(Paragraph(_inline(" ".join(para)), body)); para.clear()

    def flush_bullets():
        if bullets:
            story.append(ListFlowable([ListItem(Paragraph(_inline(b), body), leftIndent=12) for b in bullets],
                                      bulletType="bullet", start="•", leftIndent=14))
            bullets.clear()

    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        b = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", s)
        if not s:
            flush_para(); flush_bullets(); continue
        if re.fullmatch(r"[-*_]{3,}", s):
            flush_para(); flush_bullets(); story.append(Spacer(1, 8)); continue
        if m:
            flush_para(); flush_bullets()
            story.append(Paragraph(_inline(m.group(2)), h[min(len(m.group(1)), 3)])); continue
        if b:
            flush_para(); bullets.append(b.group(1)); continue
        if bullets and line.startswith((" ", "\t")):
            bullets[-1] += " " + s; continue
        flush_bullets(); para.append(s)
    flush_para(); flush_bullets()
    if not story:
        story.append(Paragraph(html.escape(title_hint or "Document"), h[1]))
    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=LETTER, leftMargin=0.9 * inch, rightMargin=0.9 * inch,
                      topMargin=0.85 * inch, bottomMargin=0.85 * inch,
                      title=title_hint or "Document", author="Margot").build(story)
    return out.getvalue()


def member_file(name: str, data: bytes, title_hint: str = "") -> tuple[str, bytes]:
    """(filename, bytes) to hand a member. Falls back to the original on error."""
    if not needs_pdf(name):
        return name, data
    try:
        return pdf_name(name), markdown_to_pdf(data, title_hint)
    except Exception:
        return name, data
