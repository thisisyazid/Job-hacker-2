"""Word export. Resumes in the app's Markdown structure get a proper resume
layout; anything else (cover letters, older plain-text documents) is set as
clean paragraphs."""
import io
import re

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

from .resume_format import is_markdown

ACCENT = RGBColor(0x1E, 0x3A, 0x8A)
MUTED = RGBColor(0x44, 0x4F, 0x5A)
FONT = "Calibri"


def _base_doc(margin_cm: float = 1.8) -> Document:
    doc = Document()
    for s in doc.sections:
        s.top_margin = s.bottom_margin = Cm(margin_cm - 0.3)
        s.left_margin = s.right_margin = Cm(margin_cm)
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    st.font.size = Pt(10.5)
    pf = st.paragraph_format
    pf.space_before = Pt(0)
    pf.space_after = Pt(3)
    pf.line_spacing = 1.08
    return doc


def _runs(p, text: str, size=None, color=None, bold=None, italic=None):
    """Add text to a paragraph, turning **bold** spans into bold runs."""
    for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
        if not part:
            continue
        r = p.add_run(part)
        r.bold = True if i % 2 else bold
        if italic:
            r.italic = True
        if size:
            r.font.size = Pt(size)
        if color is not None:
            r.font.color.rgb = color
    return p


def _bottom_border(p):
    pPr = p._p.get_or_add_pPr()
    bdr = OxmlElement("w:pBdr")
    b = OxmlElement("w:bottom")
    for k, v in (("w:val", "single"), ("w:sz", "6"), ("w:space", "1"), ("w:color", "9AA5B1")):
        b.set(qn(k), v)
    bdr.append(b)
    pPr.append(bdr)


def _bullet(doc, text):
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.space_after = Pt(2)
    p.paragraph_format.left_indent = Cm(0.55)
    p.paragraph_format.first_line_indent = Cm(-0.4)
    _runs(p, text)


def _resume(md: str) -> Document:
    doc = _base_doc()
    width = doc.sections[0].page_width - doc.sections[0].left_margin - doc.sections[0].right_margin
    header_lines = 0
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.startswith("# "):
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(2)
            _runs(p, line[2:].strip(), size=20, bold=True, color=ACCENT)
            header_lines = 1
        elif line.startswith("## "):
            header_lines = 0
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(10)
            p.paragraph_format.space_after = Pt(4)
            p.paragraph_format.keep_with_next = True
            _runs(p, line[3:].strip().upper(), size=11, bold=True, color=ACCENT)
            _bottom_border(p)
        elif line.startswith("### "):
            # "Company | dates" → company left, dates right-aligned
            text = line[4:].strip()
            left, _, right = text.rpartition(" | ") if " | " in text else (text, "", "")
            p = doc.add_paragraph()
            p.paragraph_format.space_before = Pt(7)
            p.paragraph_format.space_after = Pt(0)
            p.paragraph_format.keep_with_next = True
            p.paragraph_format.tab_stops.add_tab_stop(width, WD_TAB_ALIGNMENT.RIGHT)
            _runs(p, left.strip(), size=11, bold=True)
            if right:
                p.add_run("\t")
                _runs(p, right.strip(), size=10, color=MUTED)
        elif re.match(r"^\s*[-*•▪]\s+", line):
            _bullet(doc, re.sub(r"^\s*[-*•▪]\s+", "", line))
        elif header_lines and header_lines < 3:
            # contact line, then headline line, centred under the name
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(1 if header_lines == 1 else 4)
            _runs(p, line.strip(), size=10 if header_lines == 1 else 10.5,
                  color=MUTED if header_lines == 1 else None, bold=header_lines == 2 or None)
            header_lines += 1
        elif re.match(r"^\*\*.+\*\*$", line.strip()):
            # role title line under a ### company heading
            p = doc.add_paragraph()
            p.paragraph_format.space_after = Pt(2)
            p.paragraph_format.keep_with_next = True
            _runs(p, line.strip().strip("*"), italic=True, color=MUTED)
        else:
            p = doc.add_paragraph()
            _runs(p, line.strip())
    return doc


def _letter(text: str) -> Document:
    doc = _base_doc(margin_cm=2.4)
    doc.styles["Normal"].font.size = Pt(11)
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [re.sub(r"\*\*(.*?)\*\*", r"\1", l).strip() for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        # Plain lines stay together as one paragraph; "- " lines become bullets,
        # also when they follow an intro line in the same block.
        p = None
        for l in lines:
            if re.match(r"^[-*•]\s+", l):
                _bullet(doc, re.sub(r"^[-*•]\s+", "", l))
                p = None
                continue
            if p is None:
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(10)
            else:
                p.add_run().add_break()
            p.add_run(re.sub(r"^#+\s+", "", l))
    return doc


def to_docx(text: str, kind: str = "resume") -> bytes:
    doc = _resume(text) if (kind == "resume" and is_markdown(text)) else _letter(text)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
