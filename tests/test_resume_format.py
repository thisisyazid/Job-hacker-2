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


def test_every_base_role_is_kept(monkeypatch):
    from app import ai
    from app.resume_format import base_roles, missing_roles
    base = """Jane Tan
jane@x.com | Singapore

EXPERIENCE
Agile Coach, Booking.com — Jan 2024 – Present
- Coached 6 teams
Scrum Master, Citibank | June 2022 – October 2023
- Ran PI planning
Product Owner, SIA (2014 - 2021)

EDUCATION
BSc Information Systems, NUS, 2010 – 2014"""
    roles = base_roles(base)
    assert len(roles) == 4 and roles[0].startswith("Agile Coach, Booking.com")
    dropped = "# Jane Tan\n\n## Professional Experience\n### Citibank | June 2022 – October 2023\n- x\n### SIA | 2014 – 2021\n- y\n\n## Certifications & Education\n- **Education:** BSc, NUS, 2010 – 2014\n"
    assert missing_roles(roles, dropped) == [roles[0]]
    fixed = dropped.replace("## Professional Experience\n", "## Professional Experience\n### Booking.com | January 2024 – Present\n- z\n")
    assert missing_roles(roles, fixed) == []

    prompts = []
    answers = iter([dropped, fixed])
    monkeypatch.setattr(ai, "_ask", lambda system, user, **k: prompts.append(user) or next(answers))
    out = ai.tailor_resume(base, {"title": "PM", "description": "d"}, engine="local")
    assert "Booking.com" in out and out.index("Booking.com") < out.index("Citibank")
    assert "ROLES IN THE BASE RESUME" in prompts[0] and "Booking.com" in prompts[0]
    assert len(prompts) == 2 and "left out these roles" in prompts[1] and "Booking.com" in prompts[1]

    # nothing missing: one call only
    prompts.clear(); answers = iter([fixed])
    ai.tailor_resume(base, {"title": "PM", "description": "d"}, engine="local")
    assert len(prompts) == 1


def test_tailored_resume_keeps_extra_sections(monkeypatch):
    from app import ai
    from app.resume_format import extra_sections, missing_sections
    base = """Jane Tan
jane@example.com | Singapore
SCRUM MASTER AGILE COACH
PROFESSIONAL SUMMARY
Scrum Master who also builds software.
INDEPENDENT PROJECTS — AI-ASSISTED PRODUCT BUILDING
Job Hacker Free AI job-search workbench
- Built a job-search app.
PROFESSIONAL EXPERIENCE
Scrum Master, Citibank | June 2022 – October 2023
- Ran PI planning
CERTIFICATIONS & EDUCATION
- CSM"""
    assert extra_sections(base) == ["INDEPENDENT PROJECTS"]  # headline line at the top isn't a section
    without = "# Jane Tan\n\n## Professional Summary\nx\n\n## Professional Experience\n### Citibank | June 2022 – October 2023\n- x\n"
    with_p = without.replace("## Professional Experience", "## Independent Projects\n### Job Hacker\n- y\n\n## Professional Experience")
    assert missing_sections(["INDEPENDENT PROJECTS"], without) == ["INDEPENDENT PROJECTS"]
    assert missing_sections(["INDEPENDENT PROJECTS"], with_p) == []

    prompts = []
    answers = iter([without, with_p])
    monkeypatch.setattr(ai, "_ask", lambda system, user, **k: prompts.append(user) or next(answers))
    out = ai.tailor_resume(base, {"title": "PM", "description": "d"}, engine="local")
    assert "## Independent Projects" in out
    assert "OTHER SECTIONS IN THE BASE RESUME" in prompts[0] and "INDEPENDENT PROJECTS" in prompts[0]
    assert len(prompts) == 2 and "left out these sections" in prompts[1]
    assert "KEEP EVERY SECTION" in ai.RESUME_SYSTEM


def test_resume_prompt_uses_fit_keywords(monkeypatch):
    import json
    from app import ai
    prompts = []
    monkeypatch.setattr(ai, "_ask", lambda system, user, **k: prompts.append((system, user)) or "# Jane\n\n## Professional Summary\nx")
    job = {"title": "Business Analyst", "description": "d",
           "fit_json": json.dumps({"keywords": ["BPMN", "stakeholder management"], "gaps": ["No SQL shown"]})}
    ai.tailor_resume("Jane Tan\nScrum Master", job, engine="local")
    system, user = prompts[0]
    assert "KEYWORDS FROM THE POSTING" in user and "BPMN, stakeholder management" in user
    assert "GAPS THE FIT CHECK FOUND" in user and "No SQL shown" in user
    assert "exact job title" in system and "No stock phrases" in system
    # no fit check yet: no keyword block
    prompts.clear()
    ai.tailor_resume("Jane Tan\nScrum Master", {"title": "BA", "description": "d"}, engine="local")
    assert "KEYWORDS FROM THE POSTING" not in prompts[0][1]
    assert ai.fit_hints({"fit": {"keywords": ["a", " ", "b"]}}) == {"keywords": ["a", "b"], "gaps": []}
