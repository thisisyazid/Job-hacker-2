import re

from app.docx_export import to_docx
from app.pdf_export import to_pdf
from app.resume_format import role_dates, sort_experience, to_plain

MD = """# Jane Doe
jane@x.com | Singapore
Scrum Master | Agile Coach

## Professional Summary
Summary here.

## Professional Experience
### Citibank | June 2022 – October 2023
**Scrum Master**
- Did things

### Acme | Jan 2024 – Present
**Agile Coach**
- Now things

### SIA | 2014 – 2021
**Product Owner**
- Old things

## Certifications & Education
- **Certifications:** CSM
"""


def test_role_dates():
    assert role_dates("Citibank | June 2022 – October 2023") == ((2023, 10), (2022, 6))
    assert role_dates("Acme | Jan 2024 – Present")[0] == (9999, 12)
    assert role_dates("SIA | 2014 - 2021") == ((2021, 12), (2014, 1))
    assert role_dates("No dates here") is None


def test_sort_experience_newest_first_and_keeps_other_sections():
    out = sort_experience(MD)
    roles = [l for l in out.splitlines() if l.startswith("### ")]
    assert [r.split(" |")[0] for r in roles] == ["### Acme", "### Citibank", "### SIA"]
    assert out.index("## Professional Summary") < out.index("## Professional Experience") < out.index("## Certifications")
    assert "- Now things" in out and "**Scrum Master**" in out
    assert sort_experience("no headings at all") == "no headings at all"


def test_plain_and_docx():
    plain = to_plain(MD)
    assert "PROFESSIONAL EXPERIENCE" in plain and "• Did things" in plain and "**" not in plain and "#" not in plain
    assert to_docx(sort_experience(MD), "resume")[:2] == b"PK"
    assert to_docx("Dear team,\n\nI'd love to help.\n\nBest,\nJane", "cover_letter")[:2] == b"PK"


def test_posted_date_normalize():
    import datetime as dt
    from app.dates import normalize
    ref = dt.date(2026, 11, 5)
    assert normalize("2026-04-27T00:00:00.000Z") == "2026-04-27"
    assert normalize("2026-04-27") == "2026-04-27"
    assert normalize("27 Apr 2026") == "2026-04-27"
    assert normalize("Apr 27, 2026") == "2026-04-27"
    assert normalize("27/04/2026") == "2026-04-27"
    assert normalize("3 days ago", ref) == "2026-11-02"
    assert normalize("2 weeks ago", ref) == "2026-10-22"
    assert normalize("a month ago", ref) == "2026-10-06"
    assert normalize("5 hours ago", ref) == "2026-11-05"
    assert normalize("Reposted 1 day ago", ref) == "2026-11-04"
    assert normalize("") == "" and normalize("sometime") == "sometime"


def test_pdf_export():
    resume = to_pdf(sort_experience(MD), "resume")
    assert resume[:5] == b"%PDF-" and b"+Carlito" in resume
    letter = "Dear team,\n\nWhat I bring:\n- **Delivery** \u2013 on time\n- Coaching \u201cteams\u201d\n\nBest,\nJane"
    assert to_pdf(letter, "cover_letter")[:5] == b"%PDF-"
    long = "# A\nx@y.z\nHead\n\n## Experience\n" + "".join(f"### Co {i} | 2010 \u2013 2011\n- did things\n" * 3 for i in range(40))
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", to_pdf(long, "resume"))) >= 2
