"""PDF export, laid out like the Word export: resumes in the app's Markdown
structure get the resume layout, anything else (cover letters, older
plain-text documents) is set as clean paragraphs. Uses Carlito, a free font
with the same metrics as Calibri, bundled in app/fonts (SIL Open Font License)."""
from __future__ import annotations

import re
from pathlib import Path

from fpdf import FPDF

from .resume_format import is_markdown

FONT_DIR = Path(__file__).resolve().parent / "fonts"
FONT = "Carlito"
ACCENT = (0x1E, 0x3A, 0x8A)
MUTED = (0x44, 0x4F, 0x5A)
INK = (0x11, 0x18, 0x27)
RULE = (0x9A, 0xA5, 0xB1)
BULLET = re.compile(r"^\s*[-*•▪]\s+")
PT = 0.3528  # mm per point


def _lh(size: float) -> float:
    return size * PT * 1.22


class _Doc(FPDF):
    def __init__(self, margin_mm: float):
        super().__init__(format="A4", unit="mm")
        self.set_margins(margin_mm, margin_mm - 3, margin_mm)
        self.set_auto_page_break(True, margin_mm - 3)
        for style, name in (("", "Regular"), ("B", "Bold"), ("I", "Italic"), ("BI", "Bold")):
            self.add_font(FONT, style, str(FONT_DIR / f"Carlito-{name}.ttf"))
        self.set_title("")
        self.set_creator("Job Hacker")
        self.add_page()

    def room(self, mm: float) -> None:
        """Start a new page if less than `mm` is left, so a heading stays with what follows."""
        if self.get_y() + mm > self.page_break_trigger:
            self.add_page()

    def runs(self, text: str, size: float, color=INK, bold=False, italic=False) -> None:
        """Flowing text at the current position; **bold** spans become bold."""
        for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text)):
            if not part:
                continue
            self.set_font(FONT, ("B" if bold or i % 2 else "") + ("I" if italic else ""), size)
            self.set_text_color(*color)
            self.write(_lh(size), part)

    def para(self, text: str, size: float, after_pt: float, **kw) -> None:
        self.set_x(self.l_margin)
        self.runs(text, size, **kw)
        self.ln(_lh(size))
        self.ln(after_pt * PT)

    def centred(self, text: str, size: float, after_pt: float, color=INK, bold=False) -> None:
        self.set_font(FONT, "B" if bold else "", size)
        self.set_text_color(*color)
        self.set_x(self.l_margin)
        self.multi_cell(0, _lh(size), _plain(text), align="C", new_x="LMARGIN", new_y="NEXT")
        self.ln(after_pt * PT)

    def bullet(self, text: str, size: float) -> None:
        indent, hang = 5.5, 4.0
        self.set_x(self.l_margin + indent - hang)
        self.set_font(FONT, "", size)
        self.set_text_color(*INK)
        self.cell(hang, _lh(size), "•")
        keep = self.l_margin
        self.set_left_margin(keep + indent)
        self.set_x(keep + indent)
        self.runs(text, size)
        self.ln(_lh(size))
        self.set_left_margin(keep)
        self.ln(2 * PT)


def _plain(s: str) -> str:
    return re.sub(r"\*\*(.*?)\*\*", r"\1", s).strip()


def _resume(md: str) -> _Doc:
    pdf = _Doc(18)
    header = 0
    for raw in md.splitlines():
        line = raw.rstrip().replace("\t", " ")
        if not line.strip():
            continue
        if line.startswith("# "):
            pdf.centred(line[2:], 20, 2, color=ACCENT, bold=True)
            header = 1
        elif line.startswith("## "):
            header = 0
            pdf.ln(8 * PT)
            pdf.room(_lh(11) + 3 * _lh(10.5))
            pdf.set_x(pdf.l_margin)
            pdf.runs(_plain(line[3:]).upper(), 11, color=ACCENT, bold=True)
            pdf.ln(_lh(11) + 0.6)
            pdf.set_draw_color(*RULE)
            pdf.set_line_width(0.25)
            pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
            pdf.ln(4 * PT)
        elif line.startswith("### "):
            # "Company | dates": company left, dates right-aligned
            text = line[4:].strip()
            left, _, right = text.rpartition(" | ") if " | " in text else (text, "", "")
            pdf.ln(6 * PT)
            pdf.room(_lh(11) + 2 * _lh(10.5))
            y = pdf.get_y()
            pdf.set_font(FONT, "", 10)
            date_w = pdf.get_string_width(right.strip()) + 1 if right else 0
            pdf.set_font(FONT, "B", 11)
            pdf.set_text_color(*INK)
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(pdf.epw - date_w - (2 if right else 0), _lh(11), _plain(left), align="L",
                           new_x="LMARGIN", new_y="NEXT")
            if right:
                after = pdf.get_y()
                pdf.set_xy(pdf.w - pdf.r_margin - date_w, y)
                pdf.set_font(FONT, "", 10)
                pdf.set_text_color(*MUTED)
                pdf.cell(date_w, _lh(11), right.strip(), align="R")
                pdf.set_y(after)
        elif BULLET.match(line):
            pdf.bullet(BULLET.sub("", line), 10.5)
        elif header and header < 3:
            # contact line, then headline line, centred under the name
            pdf.centred(line, 10 if header == 1 else 10.5, 1 if header == 1 else 4,
                        color=MUTED if header == 1 else INK, bold=header == 2)
            header += 1
        elif re.match(r"^\*\*.+\*\*$", line.strip()):
            # role title under a ### company heading
            pdf.para(line.strip().strip("*"), 10.5, 2, color=MUTED, italic=True)
        else:
            pdf.para(line.strip(), 10.5, 3)
    return pdf


def _letter(text: str) -> _Doc:
    pdf = _Doc(24)
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [_plain(l) for l in block.splitlines() if l.strip()]
        if not lines:
            continue
        # Plain lines stay together as one paragraph; "- " lines become bullets,
        # also when they follow an intro line in the same block.
        text: list[str] = []

        def flush():
            if text:
                pdf.set_font(FONT, "", 11)
                pdf.set_text_color(*INK)
                pdf.set_x(pdf.l_margin)
                pdf.multi_cell(0, _lh(11), "\n".join(text), align="L", new_x="LMARGIN", new_y="NEXT")
                pdf.ln(2 * PT)
                text.clear()

        for l in lines:
            if BULLET.match(l):
                flush()
                pdf.bullet(BULLET.sub("", l), 11)
            else:
                text.append(re.sub(r"^#+\s+", "", l))
        flush()
        pdf.ln(8 * PT)
    return pdf


def to_pdf(text: str, kind: str = "resume") -> bytes:
    pdf = _resume(text) if (kind == "resume" and is_markdown(text)) else _letter(text)
    return bytes(pdf.output())
