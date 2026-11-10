import io
import json

import pytest
from fastapi.testclient import TestClient  # noqa: E402

from app import scrape  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def c():
    client = TestClient(app)
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"password": "secret"}).status_code == 200
    return client


def test_session(c):
    s = c.get("/api/session").json()
    assert s["logged_in"] and s["auth_required"] and not s["ai_enabled"]
    assert s["user"]["name"] == "Me"


def test_frontend_served():
    client = TestClient(app)
    assert "Job Hacker" in client.get("/").text
    assert "Job Hacker" in client.get("/job/3").text  # SPA fallback
    assert client.get("/assets/app.js").status_code == 200
    assert client.get("/manifest.webmanifest").json()["share_target"]


def test_job_crud_and_dupes(c):
    r = c.post("/api/jobs", json={"title": "Scrum Master", "company": "DBS",
                                   "url": "https://sg.linkedin.com/jobs/view/scrum-1234567?trk=x"})
    assert r.status_code == 201
    j = r.json()
    assert j["url"] == "https://www.linkedin.com/jobs/view/1234567/"
    assert j["status"] == "saved" and not j["has_description"]

    # Same link again fills the gap instead of duplicating
    r2 = c.post("/api/jobs", json={"title": "Scrum Master", "url": "https://www.linkedin.com/jobs/view/1234567",
                                    "description": "Lead teams"})
    assert r2.status_code == 200 and r2.json()["duplicate"] and r2.json()["description"] == "Lead teams"
    assert len(c.get("/api/jobs").json()) == 1

    # extract/url short-circuits on dupes without fetching
    e = c.post("/api/extract/url", json={"url": "linkedin.com/jobs/view/1234567"}).json()
    assert e["existing_id"] == j["id"]

    p = c.patch(f"/api/jobs/{j['id']}", json={"status": "applied", "notes": "called"}).json()
    assert p["status"] == "applied" and p["applied_at"] and p["notes"] == "called"
    assert c.patch(f"/api/jobs/{j['id']}", json={"status": "bogus"}).status_code == 400
    assert c.patch(f"/api/jobs/{j['id']}", json={"url": ""}).json()["url"] is None

    # job without link
    assert c.post("/api/jobs", json={"title": "No link job"}).status_code == 201
    assert c.post("/api/jobs", json={"title": "No link job 2", "url": ""}).status_code == 201

    assert c.delete(f"/api/jobs/{j['id']}").status_code == 200
    assert c.get(f"/api/jobs/{j['id']}").status_code == 404


def test_api_token_and_x_user():
    client = TestClient(app)
    r = client.post("/api/jobs", headers={"X-API-Key": "tok"}, json={"title": "From n8n", "url": "https://x.com/j/1"})
    assert r.status_code == 201
    assert client.post("/api/jobs", headers={"X-API-Key": "wrong"}, json={"title": "x"}).status_code == 401


def test_extract_url_failure_is_manual(c, monkeypatch):
    def boom(url, **kw):
        raise scrape.ScrapeError("That site blocks automatic reading.")
    monkeypatch.setattr("app.main.fetch_job", boom)
    r = c.post("/api/extract/url", json={"url": "https://blocked.example/job/9"})
    assert r.status_code == 422 and r.json()["needs_manual"]


def test_extract_url_success(c, monkeypatch):
    monkeypatch.setattr("app.main.fetch_job", lambda u, **kw: ({"title": "Agile Coach", "company": "Grab", "location": "",
                                                          "salary": "", "employment_type": "", "posted_date": "",
                                                          "description": "x" * 300}, "structured data"))
    r = c.post("/api/extract/url", json={"url": "https://careers.grab.com/job/5?utm_source=a"}).json()
    assert r["draft"]["title"] == "Agile Coach" and r["draft"]["url"] == "https://careers.grab.com/job/5"


def test_extract_text_without_ai(c):
    r = c.post("/api/extract/text", json={"text": "Delivery Lead\nGovTech\nWe need someone to lead delivery across teams."}).json()
    assert r["method"] == "basic" and r["draft"]["title"] == "Delivery Lead"
    assert c.post("/api/extract/text", json={"text": "short"}).status_code == 400


def test_ai_endpoints_report_clearly_when_off(c):
    j = c.post("/api/jobs", json={"title": "T", "description": "Some description"}).json()
    r = c.post(f"/api/jobs/{j['id']}/fit")
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    r = c.post(f"/api/jobs/{j['id']}/documents", json={"kind": "resume"})
    assert r.status_code == 400  # no resume yet


def test_ai_fit_and_docs_with_stub(c, monkeypatch):
    from app import ai
    monkeypatch.setattr(ai, "available", lambda e: e == "claude")
    monkeypatch.setattr(ai, "_ask", lambda system, user, max_tokens=0, **kw:
                        '{"score": 8, "verdict": "Good", "strengths": ["a"], "gaps": ["b"], "keywords": ["k"]}'
                        if "Score how well" in system else "TAILORED TEXT")
    c.post("/api/personas", json={"name": "Main", "resume_text": "My resume"})
    j = c.post("/api/jobs", json={"title": "AI job", "description": "Desc"}).json()
    f = c.post(f"/api/jobs/{j['id']}/fit").json()
    assert f["fit_score"] == 8 and f["fit"]["verdict"] == "Good"
    d = c.post(f"/api/jobs/{j['id']}/documents", json={"kind": "cover_letter"}).json()
    assert d["content"] == "Dear Hiring Manager,\n\nTAILORED TEXT\n\nKind regards," and d["persona_name"] == "Main"
    assert c.patch(f"/api/documents/{d['id']}", json={"content": "edited"}).json()["content"] == "edited"
    r = c.get(f"/api/documents/{d['id']}/docx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = c.get(f"/api/documents/{d['id']}/pdf")
    assert r.status_code == 200 and r.content[:5] == b"%PDF-" and r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].endswith('.pdf"')


def test_personas_default_logic(c):
    ps = c.get("/api/personas").json()
    assert sum(p["is_default"] for p in ps) == 1
    second = c.post("/api/personas", json={"name": "PO", "resume_text": "x"}).json()
    assert not second["is_default"]
    c.post(f"/api/personas/{second['id']}/default")
    assert [p["name"] for p in c.get("/api/personas").json() if p["is_default"]] == ["PO"]
    c.delete(f"/api/personas/{second['id']}")
    assert sum(p["is_default"] for p in c.get("/api/personas").json()) == 1


def test_csv_import(c):
    csv_text = ("Job URL,Job title,Company Name,Location,Published At,Status\n"
                "https://www.linkedin.com/jobs/view/1111111111,Scrum Master,Acme,Singapore,2026-04-01,Not started\n"
                "https://www.linkedin.com/jobs/view/2222222222,Agile Coach,Globex,Remote,2026-04-15,In progress\n"
                "https://www.linkedin.com/jobs/view/2222222222,Agile Coach,Globex,Remote,2026-04-15,In progress\n"
                ",,,,,\n")
    r = c.post("/api/import/csv", files={"file": ("jobs.csv", csv_text, "text/csv")}).json()
    assert r == {"rows": 4, "added": 2, "duplicates": 1, "reposted": 0, "skipped": 1, "dead": 0}
    statuses = {j["title"]: j["status"] for j in c.get("/api/jobs").json()}
    assert statuses["Agile Coach"] == "applied" and statuses["Scrum Master"] == "saved"


def test_export_import_roundtrip(c):
    backup = c.get("/api/export").content
    data = json.loads(backup)
    assert data["format"] == "job-hacker" and data["profiles"][0]["jobs"]
    # importing the same backup again adds nothing new
    r = c.post("/api/import/backup", files={"file": ("b.json", io.BytesIO(backup), "application/json")}).json()
    assert r["jobs"] == 0 and r["duplicates"] > 0
    # a different profile name creates a profile
    data["profiles"][0]["name"] = "Wife"
    r = c.post("/api/import/backup", files={"file": ("b.json", json.dumps(data), "application/json")}).json()
    assert r["profiles"] == 1 and r["jobs"] > 0
    assert c.post("/api/import/backup", files={"file": ("x.json", "{}", "application/json")}).status_code == 400


def test_profiles(c):
    users = c.get("/api/session").json()["users"]
    wife = next(u for u in users if u["name"] == "Wife")
    me_jobs = len(c.get("/api/jobs").json())
    c.post(f"/api/users/{wife['id']}/select")
    assert c.get("/api/session").json()["user"]["name"] == "Wife"
    wife_jobs = c.get("/api/jobs").json()
    other = c.post("/api/users", json={"name": "Temp"}).json()
    assert c.get("/api/jobs").json() == []  # new profile is active and empty
    assert c.post("/api/users", json={"name": "temp"}).status_code == 409
    c.delete(f"/api/users/{other['id']}")
    c.post(f"/api/users/{wife['id']}/select")
    assert len(c.get("/api/jobs").json()) == len(wife_jobs)
    assert me_jobs > 0


def test_bulk_from_url_and_refresh(c, monkeypatch):
    def fake(url, **kw):
        if "blocked" in url:
            raise scrape.ScrapeError("That site blocks automatic reading.")
        return ({"title": "Bulk Coach", "company": "OCBC", "location": "Singapore", "salary": "S$10k",
                 "employment_type": "", "posted_date": "", "description": "Full description"}, "structured data")
    monkeypatch.setattr("app.main.fetch_job", fake)

    r = c.post("/api/jobs/from-url", json={"url": "https://careers.ocbc.com/job/1?utm_source=x"}).json()
    assert r["status"] == "added" and r["job"]["title"] == "Bulk Coach" and r["job"]["has_description"]
    assert c.post("/api/jobs/from-url", json={"url": "careers.ocbc.com/job/1"}).json()["status"] == "duplicate"
    f = c.post("/api/jobs/from-url", json={"url": "https://blocked.example/j"}).json()
    assert f["status"] == "failed" and "blocks" in f["reason"]
    assert c.post("/api/jobs/from-url", json={"url": ""}).json()["status"] == "failed"

    # refresh fills only empty fields
    j = c.post("/api/jobs", json={"title": "From CSV", "company": "Keep Me", "url": "https://x.example/csv/9"}).json()
    r = c.post(f"/api/jobs/{j['id']}/refresh").json()
    assert r["status"] == "updated" and set(r["filled"]) == {"location", "salary", "description"}
    full = c.get(f"/api/jobs/{j['id']}").json()
    assert full["company"] == "Keep Me" and full["description"] == "Full description"
    assert c.post(f"/api/jobs/{j['id']}/refresh").json()["status"] == "unchanged"
    nolink = c.post("/api/jobs", json={"title": "No link here"}).json()
    assert c.post(f"/api/jobs/{nolink['id']}/refresh").json()["status"] == "failed"



def test_engine_choice_local_vs_claude(c, monkeypatch):
    from app import ai
    calls = []
    monkeypatch.setattr(ai, "available", lambda e: True)
    monkeypatch.setattr(ai, "_ask_claude", lambda s, u, m: calls.append("claude") or "CLAUDE TEXT")
    monkeypatch.setattr(ai, "_ask_local", lambda s, u, m, j: calls.append("local") or "LOCAL TEXT")
    j = c.post("/api/jobs", json={"title": "Engine job", "description": "Desc"}).json()
    d1 = c.post(f"/api/jobs/{j['id']}/documents", json={"kind": "cover_letter", "engine": "local"}).json()
    d2 = c.post(f"/api/jobs/{j['id']}/documents", json={"kind": "cover_letter", "engine": "claude"}).json()
    assert calls == ["local", "claude"]
    assert "LOCAL TEXT" in d1["content"] and d1["engine"].startswith("Local")
    assert d2["engine"].startswith("Claude")
    assert c.post(f"/api/jobs/{j['id']}/documents", json={"kind": "resume", "engine": "bogus"}).status_code == 400


def test_engine_unavailable_is_clear(c, monkeypatch):
    from app import ai
    monkeypatch.setattr(ai, "available", lambda e: e == "claude")
    monkeypatch.setattr(ai, "local_status", lambda force=False: {"ok": False, "model": "qwen3:14b", "reason": "Couldn't reach Ollama."})
    j = c.post("/api/jobs", json={"title": "Engine job 2", "description": "Desc"}).json()
    r = c.post(f"/api/jobs/{j['id']}/fit", json={"engine": "local"})
    assert r.status_code == 400 and "Ollama" in r.json()["detail"]


def test_local_strips_thinking(monkeypatch):
    from app import ai, config
    monkeypatch.setattr(config, "OLLAMA_URL", "http://ollama.test:11434")

    class R:
        status_code = 200
        text = ""
        def raise_for_status(self): pass
        def json(self): return {"message": {"content": "<think>hmm</think>\n{\"score\": 7}"}}
    sent = {}
    monkeypatch.setattr(ai.httpx, "post", lambda url, json, timeout: sent.update(json) or R())
    out = ai._ask_local("sys", "user", 100, True)
    assert out == '{"score": 7}' and sent["format"] == "json" and sent["think"] is False
    assert sent["options"]["num_ctx"] >= 8192


def test_nightly_settings_and_run(c, monkeypatch):
    from app import ai, nightly
    from app.db import SessionLocal
    monkeypatch.setattr(ai, "available", lambda e: True)
    monkeypatch.setattr(ai, "_ask", lambda system, user, max_tokens=0, engine=None, json_mode=False:
                        '{"score": 6, "verdict": "OK", "strengths": [], "gaps": [], "keywords": []}'
                        if json_mode else "# Me\nx\n\n## Professional Experience\n### A | 2020 - 2021\n- y\n")
    me = c.get("/api/session").json()["user"]
    s = c.get("/api/nightly").json()
    assert s["settings"]["enabled"] is False and s["settings"]["run_time"] == "01:00"
    assert c.patch("/api/nightly", json={"run_time": "25:00"}).status_code == 422
    s = c.patch("/api/nightly", json={"enabled": True, "engine": "local", "do_fit": True}).json()
    assert s["settings"]["enabled"] and s["settings"]["engine"] == "local"
    if not c.get("/api/personas").json():
        c.post("/api/personas", json={"name": "Main", "resume_text": "My resume"})
    j = c.post("/api/jobs", json={"title": "Night job", "description": "Needs a resume", "url": "https://n.example/1"}).json()
    other = c.post("/api/jobs", json={"title": "Not picked", "description": "Leave me alone"}).json()
    bare = c.post("/api/jobs", json={"title": "No description"}).json()

    # nothing is made unless it's on the list
    assert c.get("/api/nightly").json()["pending"]["jobs"] == 0
    r = c.post("/api/nightly/queue", json={"job_ids": [j["id"], bare["id"]], "queued": True}).json()
    assert r["changed"] == 1 and r["pending"]["queued"] == 1 and r["pending"]["resume"] == 1
    assert c.patch(f"/api/jobs/{bare['id']}", json={"queued": True}).status_code == 400
    assert c.get(f"/api/jobs/{j['id']}").json()["queued"] is True

    run_id = nightly.run_for_user(me["id"], "manual")  # synchronous for the test
    runs = c.get("/api/nightly").json()["runs"]
    assert runs[0]["id"] == run_id and runs[0]["status"] == "done" and runs[0]["made"] == 2
    full = c.get(f"/api/jobs/{j['id']}").json()
    assert full["fit_score"] == 6 and full["queued"] is False  # done, so off the list
    doc = next(d for d in full["documents"] if d["kind"] == "resume")
    assert doc["nightly"] and doc["engine"].startswith("Local")
    assert next(x for x in c.get("/api/jobs").json() if x["id"] == j["id"])["has_resume"]
    assert c.get(f"/api/jobs/{other['id']}").json()["documents"] == []

    # list is empty now, so a second run makes nothing
    nightly.run_for_user(me["id"], "manual")
    assert sum(d["kind"] == "resume" for d in c.get(f"/api/jobs/{j['id']}").json()["documents"]) == 1

    # a failure keeps the job on the list for next time
    c.patch(f"/api/jobs/{other['id']}", json={"queued": True})
    monkeypatch.setattr(ai, "_ask", lambda *a, **k: (_ for _ in ()).throw(ai.AIError("model down")))
    nightly.run_for_user(me["id"], "manual")
    last = c.get("/api/nightly").json()["runs"][0]
    assert last["failed"] >= 1 and c.get(f"/api/jobs/{other['id']}").json()["queued"] is True
    c.post("/api/nightly/queue", json={"job_ids": [other["id"]], "queued": False})
    c.patch("/api/nightly", json={"enabled": False})


def test_csv_published_at_becomes_posted_date(c):
    csv_text = ("Company Name,Job URL,Job title,Location,Published At,Status\n"
                "Acme,https://sg.linkedin.com/jobs/view/dated-role-5550001,Dated Role,Singapore,2026-04-27T00:00:00.000Z,Abandon\n")
    assert c.post("/api/import/csv", files={"file": ("n.csv", csv_text, "text/csv")}).json()["added"] == 1
    j = next(x for x in c.get("/api/jobs").json() if x["title"] == "Dated Role")
    assert j["posted_date"] == "2026-04-27" and j["status"] == "closed"
    r = c.post("/api/jobs", json={"title": "Relative", "posted_date": "3 days ago"}).json()
    assert len(r["posted_date"]) == 10 and r["posted_date"][4] == "-"


def test_old_posted_dates_are_fixed_on_startup():
    from sqlalchemy import text
    from app import db
    with db.engine.begin() as conn:
        uid = conn.execute(text("SELECT id FROM users LIMIT 1")).scalar()
        conn.execute(text("INSERT INTO jobs (user_id, title, company, location, salary, employment_type, posted_date, "
                          "description, source, status, tags, notes, fit_json, queued, created_at, updated_at) VALUES "
                          "(:u, 'Legacy', '', '', '', '', '2025-12-01T00:00:00.000Z', '', 'csv', 'saved', '', '', '', 0, "
                          "'2026-01-01 00:00:00', '2026-01-02 00:00:00')"), {"u": uid})
    db._fix_posted_dates()
    with db.engine.begin() as conn:
        row = conn.execute(text("SELECT posted_date, updated_at FROM jobs WHERE title = 'Legacy'")).one()
    assert row[0] == "2025-12-01" and str(row[1]).startswith("2026-01-02")


def test_taken_down_jobs_are_rejected_and_cleaned(c):
    dead = "https://www.linkedin.com/jobs/view/4456058972/?trackingId=wNmgq%3D%3D&refId=vpd&eBP=CwEAAAG"
    csv_text = ("Company Name,Job URL,Job title,Location,Published At,Status\n"
                f"{dead},{dead},{dead},Singapore,2026-08-24T00:00:00.000Z,In progress\n"
                "Real Co,https://example.com/jobs/77,Real Scrum Master,Singapore,2026-08-24,Not started\n")
    r = c.post("/api/import/csv", files={"file": ("d.csv", csv_text, "text/csv")}).json()
    assert r["added"] == 1 and r["dead"] == 1
    assert c.post("/api/jobs", json={"title": dead}).status_code == 422
    ok = c.post("/api/jobs", json={"title": "Fine title", "company": "https://www.linkedin.com/company/x"}).json()
    assert ok["company"] == ""
    assert c.patch(f"/api/jobs/{ok['id']}", json={"title": "www.example.com/job/1"}).status_code == 422

    # an old dead job already in the database gets removed by the cleanup
    from sqlalchemy import text
    from app import db
    with db.engine.begin() as conn:
        uid = c.get("/api/session").json()["user"]["id"]
        conn.execute(text("INSERT INTO jobs (user_id, title, company, location, salary, employment_type, posted_date, "
                          "description, source, status, tags, notes, fit_json, queued, created_at, updated_at) VALUES "
                          "(:u, :t, :t, '', '', '', '', '', 'csv', 'applied', '', '', '', 0, '2026-01-01', '2026-01-01')"),
                     {"u": uid, "t": dead + "&x=1"})
    out = c.post("/api/cleanup/dead-links").json()
    assert out["removed"] >= 1
    assert not any(j["title"].startswith("https://") for j in c.get("/api/jobs").json())


def test_looks_like_url():
    from app.scrape import looks_like_url as L
    assert L("https://www.linkedin.com/jobs/view/1/?trackingId=x") and L("%2Fm%3D&eBP=CwEA") and L("www.dbs.com/careers")
    assert not L("Senior Scrum Master") and not L("Node.js Developer") and not L("ASP.NET Lead") and not L("")


def test_daily_log_file(c):
    csv_text = "Job title,Job URL\nLog Test Role,https://jobs.example.com/log-test-1\nLog Test Role,https://jobs.example.com/log-test-1\n"
    r = c.post("/api/import/csv", files={"file": ("log.csv", csv_text, "text/csv")}).json()
    assert r["added"] == 1 and r["duplicates"] == 1
    files = c.get("/api/logs").json()["files"]
    assert files and files[0]["name"].startswith("jobhacker-")
    text = c.get(f"/api/logs/{files[0]['name']}").text
    assert "CSV import done: 2 rows, 1 added, 1 already saved" in text
    assert "line 3: 'Log Test Role'" in text and "-> already saved as job" in text
    assert c.get("/api/logs/..%2Fjobhacker.db").status_code == 404
    assert c.get("/api/logs/secrets.txt").status_code == 404


@pytest.mark.parametrize("name,data,title", [
    ("excel", "Job title,Company Name,Job URL\nCafé Lead,Société,https://csv.example/e1\n".encode("cp1252"), "Café Lead"),
    ("semicolon", b"Job title;Company Name;Job URL\nSemi Role;Acme;https://csv.example/e2\n", "Semi Role"),
    ("tabs", b"Title\tCompany\tURL\nTab Role\tGamma\thttps://csv.example/e3\n", "Tab Role"),
    ("notion", b"Name,Company,URL\nNotion Role,Beta,https://csv.example/e4\n", "Notion Role"),
])
def test_csv_as_people_save_it(c, name, data, title):
    r = c.post("/api/import/csv", files={"file": (f"{name}.csv", data, "text/csv")}).json()
    assert r["added"] == 1, r
    assert any(j["title"] == title for j in c.get("/api/jobs").json())


def test_csv_bad_files_say_why(c):
    def err(data):
        r = c.post("/api/import/csv", files={"file": ("x.csv", data, "text/csv")})
        assert r.status_code == 400
        return r.json()["detail"]
    assert "Excel file" in err(b"PK\x03\x04rest")
    assert "empty" in err(b"  \n")
    assert "Couldn't find a job title column" in err(b"Company,URL\nAcme,https://a.example\n")
    assert "no jobs" in err(b"Job title,URL\n")


def test_csv_repost_updates_posted_date(c):
    first = "Job title,Job URL,Published At\nRepost Role,https://jobs.example.com/repost-1,2026-09-03T00:00:00.000Z\n"
    assert c.post("/api/import/csv", files={"file": ("a.csv", first, "text/csv")}).json()["added"] == 1
    job = next(j for j in c.get("/api/jobs").json() if j["title"] == "Repost Role")
    assert job["posted_date"] == "2026-09-03"
    # the same link twice more: the newest date wins, whatever order the rows are in
    later = ("Job title,Job URL,Published At\n"
             "Repost Role,https://jobs.example.com/repost-1?utm_source=x,2026-11-06T00:00:00.000Z\n"
             "Repost Role,https://jobs.example.com/repost-1,2026-10-01T00:00:00.000Z\n")
    r = c.post("/api/import/csv", files={"file": ("b.csv", later, "text/csv")}).json()
    assert r["added"] == 0 and r["duplicates"] == 2 and r["reposted"] == 1
    j = c.get(f"/api/jobs/{job['id']}").json()
    assert j["posted_date"] == "2026-11-06" and j["first_posted"] == "2026-09-03" and j["reposted_at"]
    backup = json.loads(c.get("/api/export").content)
    saved = next(x for p in backup["profiles"] for x in p["jobs"] if x["title"] == "Repost Role")
    assert saved["first_posted"] == "2026-09-03" and saved["reposted_at"]
    # an older date never moves it back
    older = "Job title,Job URL,Published At\nRepost Role,https://jobs.example.com/repost-1,2026-08-01\n"
    assert c.post("/api/import/csv", files={"file": ("c.csv", older, "text/csv")}).json()["reposted"] == 0
    assert c.get(f"/api/jobs/{job['id']}").json()["posted_date"] == "2026-11-06"
    # a new job listed twice in one file is saved once, with the newer date
    twice = ("Job title,Job URL,Published At\nTwice Role,https://jobs.example.com/twice,2026-10-01\n"
             "Twice Role,https://jobs.example.com/twice,2026-10-20\n")
    r = c.post("/api/import/csv", files={"file": ("d.csv", twice, "text/csv")}).json()
    assert r["added"] == 1 and r["reposted"] == 0
    twice_job = next(j for j in c.get("/api/jobs").json() if j["title"] == "Twice Role")
    assert twice_job["posted_date"] == "2026-10-20" and not twice_job["reposted_at"]


def test_fill_in_respects_no_ai(c, monkeypatch):
    from app import main
    seen = {}

    def fake_fetch(url, engine=None, allow_ai=True):
        seen["allow_ai"] = allow_ai
        return {"title": "X", "description": "Filled description"}, "structured data"
    monkeypatch.setattr(main, "fetch_job", fake_fetch)
    j = c.post("/api/jobs", json={"title": "Fill Me", "url": "https://fill.example.com/1"}).json()
    r = c.post(f"/api/jobs/{j['id']}/refresh", json={"engine": "none"}).json()
    assert seen["allow_ai"] is False and r["status"] == "updated" and r["method"] == "structured data"
    assert "description" in r["filled"]
    c.post(f"/api/jobs/{j['id']}/refresh", json={"engine": "claude"})
    assert seen["allow_ai"] is True



def test_cover_letter_always_reads_as_a_letter():
    from app import ai
    resume = "# Yazid bin Abd Rahim\nyazid@example.com | Singapore\nAgile Coach\n\n## Summary\nStuff."
    assert ai.candidate_name(resume) == "Yazid bin Abd Rahim"
    assert ai.candidate_name("My resume") == "" and ai.candidate_name("jane@x.com | 9123 4567") == ""
    # what a local model sometimes returns: chatter, Markdown, no greeting, no sign-off
    raw = "Here is your cover letter:\n\n**Hyundai's** mission caught my eye.\n\nI led teams at Citibank."
    out = ai.finish_letter(raw, "Yazid bin Abd Rahim")
    assert out.startswith("Dear Hiring Manager,\n\nHyundai's mission") and "**" not in out
    assert out.endswith("Kind regards,\nYazid bin Abd Rahim")
    # a proper letter is left as it is
    good = "Dear Ms Lee,\n\nBody.\n\nBest regards,\nYazid bin Abd Rahim"
    assert ai.finish_letter(good, "Yazid bin Abd Rahim") == good
    # sign-off without the name gets the name
    assert ai.finish_letter("Dear team,\n\nBody.\n\nSincerely,", "Yazid").endswith("Sincerely,\nYazid")


def test_archive_jobs(c):
    a = c.post("/api/jobs", json={"title": "Old Role", "description": "Desc", "status": "closed"}).json()
    b = c.post("/api/jobs", json={"title": "Old Role 2", "description": "Desc", "status": "closed"}).json()
    c.post("/api/nightly/queue", json={"job_ids": [a["id"]], "queued": True})
    # archiving keeps the stage, takes the job off tonight's list
    before = c.get(f"/api/jobs/{a['id']}").json()["updated_at"]
    j = c.patch(f"/api/jobs/{a['id']}", json={"archived": True}).json()
    assert j["archived_at"] and j["status"] == "closed" and not j["queued"]
    assert j["updated_at"] == before  # archiving doesn't count as a change
    assert c.patch(f"/api/jobs/{a['id']}", json={"queued": True}).status_code == 400
    assert c.post("/api/nightly/queue", json={"job_ids": [a["id"]], "queued": True}).json()["changed"] == 0
    # bulk, and it's idempotent
    assert c.post("/api/jobs/archive", json={"job_ids": [a["id"], b["id"]], "archived": True}).json()["changed"] == 1
    listed = {x["id"]: x for x in c.get("/api/jobs").json()}
    assert listed[b["id"]]["archived_at"]
    # kept in backups
    backup = json.loads(c.get("/api/export").content)
    assert next(x for p in backup["profiles"] for x in p["jobs"] if x["title"] == "Old Role 2")["archived_at"]
    # unarchive
    assert c.patch(f"/api/jobs/{b['id']}", json={"archived": False}).json()["archived_at"] is None


def test_bulk_stage(c):
    a = c.post("/api/jobs", json={"title": "Bulk A"}).json()
    b = c.post("/api/jobs", json={"title": "Bulk B", "status": "applied"}).json()
    r = c.post("/api/jobs/stage", json={"job_ids": [a["id"], b["id"]], "status": "applied"}).json()
    assert r["changed"] == 1 and all(j["status"] == "applied" for j in r["jobs"])
    assert next(j for j in r["jobs"] if j["id"] == a["id"])["applied_at"]
    assert c.post("/api/jobs/stage", json={"job_ids": [a["id"]], "status": "nope"}).status_code == 400
