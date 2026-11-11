"""Helpers for the resume Markdown the AI produces (see ai.RESUME_SYSTEM)."""
from __future__ import annotations

import re

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
PRESENT = re.compile(r"present|current|now|today", re.I)
DATE_TOKEN = re.compile(r"(?:([A-Za-z]{3,9})\.?\s+)?(\d{4})")


def _point(text: str, is_end: bool) -> tuple[int, int] | None:
    if PRESENT.search(text):
        return (9999, 12)
    m = DATE_TOKEN.search(text)
    if not m:
        return None
    month = MONTHS.get((m.group(1) or "")[:3].lower(), 12 if is_end else 1)
    return (int(m.group(2)), month)


def role_dates(heading: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """'Citibank | June 2022 – October 2023' → ((2023,10),(2022,6)) as (end, start)."""
    parts = re.split(r"\s+[–—-]\s+|\s+to\s+", heading)
    if len(parts) < 2:
        one = _point(heading, True)
        return (one, one) if one else None
    start = _point(parts[-2], False)
    end = _point(parts[-1], True)
    if not end:
        return None
    return (end, start or end)


ROLE_LINE = re.compile(
    r"(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2}\s*(?:[–—-]|to)\s*(?:(?:[A-Za-z]{3,9}\.?\s+)?(?:19|20)\d{2}|present|current|now|today)",
    re.I)


def base_roles(resume: str) -> list[str]:
    """Lines of a resume that look like a role: they hold a date range such as
    "Jan 2024 – Present" or "2014 - 2021". Works on pasted plain text as well as Markdown."""
    out = []
    for raw in resume.splitlines():
        line = raw.strip().lstrip("#").strip().strip("*").strip()
        if line and len(line) <= 200 and ROLE_LINE.search(line) and line not in out:
            out.append(line)
    return out


def _span(text: str) -> tuple[int, int] | None:
    """(end year, start year) of a role line; 'Present' counts as 9999."""
    m = ROLE_LINE.search(text)
    d = role_dates(m.group(0)) if m else None
    return (d[0][0], d[1][0]) if d else None


def missing_roles(base: list[str], md: str) -> list[str]:
    """Roles from the base resume whose dates appear nowhere in the tailored resume.
    Matching on years means a reworded company name or month still counts as present."""
    have = {sp for l in md.splitlines() for sp in [_span(l)] if sp}
    return [r for r in base if (_span(r) or None) and _span(r) not in have]


def sort_experience(md: str) -> str:
    """Guarantee newest-first role order in the Professional Experience section,
    whatever the model did. Roles whose dates can't be read keep their position
    relative to each other at the end."""
    lines = md.splitlines()
    try:
        start = next(i for i, l in enumerate(lines)
                     if re.match(r"^##\s+.*(experience|employment|work history)", l, re.I))
    except StopIteration:
        return md
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"^##\s", lines[i])), len(lines))
    body = lines[start + 1:end]

    intro, blocks, cur = [], [], None
    for l in body:
        if l.startswith("### "):
            cur = [l]
            blocks.append(cur)
        elif cur is None:
            intro.append(l)
        else:
            cur.append(l)
    if len(blocks) < 2:
        return md

    def key(item):
        idx, block = item
        d = role_dates(block[0])
        return (0, (-d[0][0], -d[0][1], -d[1][0], -d[1][1]), idx) if d else (1, (0, 0, 0, 0), idx)

    ordered = [b for _, b in sorted(enumerate(blocks), key=key)]
    # keep exactly one blank line between roles
    flat = []
    for b in ordered:
        while b and not b[-1].strip():
            b = b[:-1]
        flat.extend(b + [""])
    return "\n".join(lines[:start + 1] + intro + flat + lines[end:]).rstrip() + "\n"


def is_markdown(text: str) -> bool:
    return bool(re.search(r"^#{1,3}\s", text, re.M))


def to_plain(md: str) -> str:
    """Markdown → clean plain text for pasting into application forms."""
    out = []
    for l in md.splitlines():
        if l.startswith("# "):
            out.append(l[2:].strip())
        elif l.startswith("## "):
            out.extend(["", l[3:].strip().upper()])
        elif l.startswith("### "):
            out.extend(["", l[4:].strip()])
        elif re.match(r"^\s*[-*]\s+", l):
            out.append("• " + re.sub(r"^\s*[-*]\s+", "", l))
        else:
            out.append(l)
    text = "\n".join(out)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
