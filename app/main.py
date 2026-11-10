from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
import time
from typing import Optional

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from . import ai, auth, config, logs, nightly
from .dates import is_iso, normalize as normalize_date
from .db import STATUSES, Document, Job, NightlyRun, Persona, User, get_db, init_db, now
from .docx_export import to_docx
from .pdf_export import to_pdf
from .scrape import ScrapeError, fetch_job, looks_like_url, normalize_url

logs.setup()
log = logs.log
app = FastAPI(title="Job Hacker", docs_url="/api/docs", openapi_url="/api/openapi.json")
init_db()


@app.on_event("startup")
def _start_background():
    log.info("app started (data in %s)", config.DATA_DIR)
    nightly.start_scheduler()

OPEN_PATHS = {"/api/login", "/api/session", "/api/health", "/api/logout"}


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN_PATHS and not auth.is_authed(request):
        return JSONResponse({"detail": "Log in to continue."}, status_code=401)
    if not path.startswith("/api/"):
        return await call_next(request)
    t0 = time.monotonic()
    try:
        resp = await call_next(request)
    except Exception:
        log.exception("%s %s crashed", request.method, path)
        return JSONResponse({"detail": "Something went wrong. Details are in today's log file (Settings → Log files)."},
                            status_code=500)
    ms = int((time.monotonic() - t0) * 1000)
    # Changes, errors and slow calls are worth a line; routine reads aren't.
    if request.method != "GET" or resp.status_code >= 400 or ms > 5000:
        log.info("%s %s -> %s (%d ms)", request.method, path, resp.status_code, ms)
    return resp


# ---------- helpers ----------

def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    """Active profile: cookie for the browser, X-User header (name or id) for automations."""
    want = request.headers.get("x-user") or request.cookies.get("jh_user")
    u = None
    if want:
        u = db.get(User, int(want)) if want.isdigit() else db.query(User).filter(User.name == want).first()
    return u or db.query(User).order_by(User.id).first()


def own_job(job_id: int, user: User, db: Session) -> Job:
    j = db.get(Job, job_id)
    if not j or j.user_id != user.id:
        raise HTTPException(404, "That job isn't in this profile.")
    return j


def find_dupe(db: Session, user: User, url: str, exclude_id: int | None = None) -> Job | None:
    if not url:
        return None
    q = db.query(Job).filter(Job.user_id == user.id, Job.url == url)
    if exclude_id:
        q = q.filter(Job.id != exclude_id)
    return q.first()


def default_persona(db: Session, user: User, persona_id: int | None = None) -> Persona | None:
    q = db.query(Persona).filter(Persona.user_id == user.id)
    if persona_id:
        p = q.filter(Persona.id == persona_id).first()
        if p:
            return p
    return q.filter(Persona.is_default.is_(True)).first() or q.order_by(Persona.id).first()


def iso(d: dt.datetime | None) -> str | None:
    return d.isoformat() + "Z" if d else None


def job_out(j: Job, full: bool = True) -> dict:
    d = {
        "id": j.id, "url": j.url, "title": j.title, "company": j.company, "location": j.location,
        "salary": j.salary, "employment_type": j.employment_type, "posted_date": j.posted_date,
        "source": j.source, "status": j.status, "tags": j.tags, "fit_score": j.fit_score,
        "queued": bool(j.queued), "reposted_at": iso(j.reposted_at), "first_posted": j.first_posted or "",
        "archived_at": iso(j.archived_at),
        "applied_at": iso(j.applied_at), "created_at": iso(j.created_at), "updated_at": iso(j.updated_at),
        "has_description": bool(j.description and j.description.strip()),
    }
    if full:
        d.update(
            description=j.description, notes=j.notes,
            fit=json.loads(j.fit_json) if j.fit_json else None,
            documents=[doc_out(x) for x in j.documents],
        )
    return d


def doc_out(d: Document) -> dict:
    return {"id": d.id, "job_id": d.job_id, "kind": d.kind, "content": d.content,
            "persona_name": d.persona_name, "engine": d.engine or "", "nightly": bool(d.nightly),
            "created_at": iso(d.created_at)}


def persona_out(p: Persona) -> dict:
    return {"id": p.id, "name": p.name, "resume_text": p.resume_text, "is_default": p.is_default}


# ---------- schemas ----------

class LoginIn(BaseModel):
    password: str


class JobIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    url: Optional[str] = ""
    company: str = ""
    location: str = ""
    salary: str = ""
    employment_type: str = ""
    posted_date: str = ""
    description: str = ""
    source: str = "manual"
    status: str = "saved"
    tags: str = ""
    notes: str = ""


class JobPatch(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    salary: Optional[str] = None
    employment_type: Optional[str] = None
    posted_date: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    tags: Optional[str] = None
    notes: Optional[str] = None
    queued: Optional[bool] = None
    archived: Optional[bool] = None


class StageIn(BaseModel):
    job_ids: list[int]
    status: str


class ArchiveIn(BaseModel):
    job_ids: list[int]
    archived: bool = True


class QueueIn(BaseModel):
    job_ids: list[int]
    queued: bool = True


class UrlIn(BaseModel):
    url: str
    engine: Optional[str] = None  # claude | local | None (default)


class TextIn(BaseModel):
    text: str
    url: Optional[str] = ""
    engine: Optional[str] = None


class GenIn(BaseModel):
    kind: str  # resume | cover_letter
    persona_id: Optional[int] = None
    instructions: str = ""
    engine: Optional[str] = None


class EngineIn(BaseModel):
    engine: Optional[str] = None


class NightlyIn(BaseModel):
    enabled: Optional[bool] = None
    run_time: Optional[str] = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    engine: Optional[str] = None
    do_resume: Optional[bool] = None
    do_fit: Optional[bool] = None
    do_cover: Optional[bool] = None


class DocPatch(BaseModel):
    content: str


class PersonaIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    resume_text: str = ""


class PersonaPatch(BaseModel):
    name: Optional[str] = None
    resume_text: Optional[str] = None


class UserIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class UserPatch(BaseModel):
    name: Optional[str] = None
    preferences: Optional[str] = None


# ---------- session ----------

@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/session")
def session_info(request: Request, db: Session = Depends(get_db)):
    authed = auth.is_authed(request)
    out = {"auth_required": bool(config.APP_PASSWORD), "logged_in": authed, "ai_enabled": ai.enabled()}
    if authed:
        out["engines"] = ai.engines()
        u = current_user(request, db)
        out["user"] = {"id": u.id, "name": u.name, "preferences": u.preferences}
        out["users"] = [{"id": x.id, "name": x.name} for x in db.query(User).order_by(User.id)]
        out["statuses"] = STATUSES
    return out


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response):
    if not config.APP_PASSWORD:
        return {"ok": True}
    if not auth.check_password(body.password):
        raise HTTPException(401, "That password didn't match.")
    response.set_cookie(auth.COOKIE, auth.make_token(), max_age=config.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=auth.cookie_secure(request))
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}


# ---------- profiles ----------

@app.post("/api/users")
def create_user(body: UserIn, response: Response, db: Session = Depends(get_db)):
    name = body.name.strip()
    if db.query(User).filter(func.lower(User.name) == name.lower()).first():
        raise HTTPException(409, f"There's already a profile called {name}.")
    u = User(name=name)
    db.add(u)
    db.commit()
    response.set_cookie("jh_user", str(u.id), max_age=10 * 365 * 86400, samesite="lax")
    return {"id": u.id, "name": u.name}


@app.post("/api/users/{user_id}/select")
def select_user(user_id: int, response: Response, db: Session = Depends(get_db)):
    if not db.get(User, user_id):
        raise HTTPException(404, "Profile not found.")
    response.set_cookie("jh_user", str(user_id), max_age=10 * 365 * 86400, samesite="lax")
    return {"ok": True}


@app.patch("/api/me")
def update_me(body: UserPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.name is not None and body.name.strip():
        clash = db.query(User).filter(func.lower(User.name) == body.name.strip().lower(), User.id != user.id).first()
        if clash:
            raise HTTPException(409, "Another profile already uses that name.")
        user.name = body.name.strip()
    if body.preferences is not None:
        user.preferences = body.preferences
    db.commit()
    return {"id": user.id, "name": user.name, "preferences": user.preferences}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, response: Response, db: Session = Depends(get_db)):
    if db.query(User).count() <= 1:
        raise HTTPException(400, "You need at least one profile.")
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404, "Profile not found.")
    db.delete(u)
    db.commit()
    response.delete_cookie("jh_user")
    return {"ok": True}


# ---------- personas (base resumes) ----------

@app.get("/api/personas")
def list_personas(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return [persona_out(p) for p in db.query(Persona).filter(Persona.user_id == user.id).order_by(Persona.id)]


@app.post("/api/personas")
def create_persona(body: PersonaIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    first = not db.query(Persona).filter(Persona.user_id == user.id).first()
    p = Persona(user_id=user.id, name=body.name.strip(), resume_text=body.resume_text, is_default=first)
    db.add(p)
    db.commit()
    return persona_out(p)


def _own_persona(pid: int, user: User, db: Session) -> Persona:
    p = db.get(Persona, pid)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Resume not found.")
    return p


@app.patch("/api/personas/{pid}")
def update_persona(pid: int, body: PersonaPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_persona(pid, user, db)
    if body.name is not None and body.name.strip():
        p.name = body.name.strip()
    if body.resume_text is not None:
        p.resume_text = body.resume_text
    db.commit()
    return persona_out(p)


@app.post("/api/personas/{pid}/default")
def make_default(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_persona(pid, user, db)
    db.query(Persona).filter(Persona.user_id == user.id).update({Persona.is_default: False})
    p.is_default = True
    db.commit()
    return persona_out(p)


@app.delete("/api/personas/{pid}")
def delete_persona(pid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    p = _own_persona(pid, user, db)
    was_default = p.is_default
    db.delete(p)
    db.commit()
    if was_default:
        nxt = db.query(Persona).filter(Persona.user_id == user.id).order_by(Persona.id).first()
        if nxt:
            nxt.is_default = True
            db.commit()
    return {"ok": True}


# ---------- capture ----------

@app.post("/api/extract/url")
def extract_url(body: UrlIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    url = normalize_url(body.url)
    if not url:
        raise HTTPException(400, "Paste a link to a job posting.")
    dupe = find_dupe(db, user, url)
    if dupe:
        log.info("read link %s: already saved as job %s (%s, %s)", url, dupe.id, logs.short(dupe.title), dupe.status)
        return {"existing_id": dupe.id, "title": dupe.title, "company": dupe.company}
    try:
        draft, method = fetch_job(body.url, engine=_engine_or_none(body.engine))
    except ScrapeError as e:
        log.info("read link %s: failed: %s", body.url, e)
        return JSONResponse({"needs_manual": True, "reason": str(e), "url": url}, status_code=422)
    draft = _clean_draft(draft)
    log.info("read link %s: found with %s: %s", url, method, logs.short(draft.get("title")))
    draft["url"] = url
    return {"draft": draft, "method": method}


@app.post("/api/extract/text")
def extract_text(body: TextIn):
    text = body.text.strip()
    if len(text) < 40:
        raise HTTPException(400, "Paste the whole job post, including the description.")
    url = normalize_url(body.url or "")
    if ai.enabled():
        try:
            eng = ai.resolve(body.engine)
            d = ai.extract_job(text[:30000], engine=eng)
            d["url"] = url
            return {"draft": d, "method": "AI", "engine": ai.label(eng)}
        except ai.AIError as e:
            fallback_reason = str(e)
            log.warning("read pasted text: AI failed: %s", e)
    else:
        fallback_reason = "AI is off, so only the description was filled in."
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return {
        "draft": {"title": lines[0][:200] if lines else "", "company": "", "location": "", "salary": "",
                  "employment_type": "", "posted_date": "", "description": text, "url": url},
        "method": "basic", "note": fallback_reason,
    }


# ---------- jobs ----------

@app.get("/api/jobs")
def list_jobs(user: User = Depends(current_user), db: Session = Depends(get_db)):
    jobs = db.query(Job).filter(Job.user_id == user.id).order_by(Job.updated_at.desc()).all()
    with_resume = {r[0] for r in db.query(Document.job_id).join(Job)
                   .filter(Job.user_id == user.id, Document.kind == "resume").distinct()}
    return [{**job_out(j, full=False), "has_resume": j.id in with_resume} for j in jobs]


@app.post("/api/jobs")
def create_job(body: JobIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Also the endpoint for n8n / Shortcuts (send X-API-Key). If a job with the
    same link exists, empty fields on it are filled in and it's returned with 200."""
    if looks_like_url(body.title):
        raise HTTPException(422, DEAD_LINK)
    url = normalize_url(body.url or "")
    data = body.model_dump()
    data["url"] = url or None
    if looks_like_url(data.get("company")):
        data["company"] = ""
    if data["status"] not in STATUSES:
        data["status"] = "saved"
    dupe = find_dupe(db, user, url)
    if dupe:
        changed = False
        for k in ("company", "location", "salary", "employment_type", "posted_date", "description"):
            if data.get(k) and not getattr(dupe, k):
                setattr(dupe, k, data[k])
                changed = True
        if changed:
            db.commit()
        return JSONResponse({**job_out(dupe), "duplicate": True}, status_code=200)
    j = Job(user_id=user.id, **data)
    if j.status in ("applied", "interviewing", "offer"):
        j.applied_at = now()
    db.add(j)
    db.commit()
    db.refresh(j)
    return JSONResponse(job_out(j), status_code=201)


DETAIL_FIELDS = ("company", "location", "salary", "employment_type", "posted_date", "description")
# Title for a job imported with only a link; filling it in replaces it with the real title.
PENDING_TITLE = "(title not read yet)"
DEAD_LINK = "The title is just a link, which usually means the job was taken down. Not saved."


def _clean_draft(d: dict) -> dict:
    """Drop link-shaped values a scraper or the AI picked up as title/company."""
    for k in ("title", "company"):
        if looks_like_url(d.get(k)):
            d[k] = ""
    return d


@app.post("/api/jobs/from-url")
def job_from_url(body: UrlIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Read a link and save it in one step (used by bulk add). Always 200 so a
    batch can carry on: check "status" — added | duplicate | failed."""
    url = normalize_url(body.url)
    if not url:
        return {"status": "failed", "reason": "That isn't a link.", "url": body.url}
    dupe = find_dupe(db, user, url)
    if dupe:
        log.info("bulk link %s: already saved as job %s (%s, %s)", url, dupe.id, logs.short(dupe.title), dupe.status)
        return {"status": "duplicate", "url": url, "job": job_out(dupe, full=False)}
    try:
        draft, method = fetch_job(body.url, engine=_engine_or_none(body.engine))
    except ScrapeError as e:
        log.info("bulk link %s: failed: %s", body.url, e)
        return {"status": "failed", "reason": str(e), "url": url}
    draft = _clean_draft(draft)
    if not draft.get("title"):
        log.info("bulk link %s: no title on the page", url)
        return {"status": "failed", "reason": "No job title on that page. The job has probably been taken down.", "url": url}
    j = Job(user_id=user.id, url=url, source="bulk", title=draft["title"][:300],
            **{k: draft.get(k) or "" for k in DETAIL_FIELDS})
    db.add(j)
    db.commit()
    db.refresh(j)
    log.info("bulk link %s: saved as job %s with %s: %s", url, j.id, method, logs.short(j.title))
    return {"status": "added", "url": url, "method": method, "job": job_out(j, full=False)}


@app.post("/api/jobs/{job_id}/refresh")
def refresh_job(job_id: int, body: Optional[EngineIn] = None, user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """Re-read a saved job's link and fill only the fields that are empty.
    Never overwrites anything already there. Always 200: status updated | unchanged | failed."""
    j = own_job(job_id, user, db)
    if not j.url:
        return {"status": "failed", "reason": "This job has no link to read."}
    want = body.engine if body else None  # "none" = don't use AI even as a last resort
    try:
        draft, method = fetch_job(j.url, engine=_engine_or_none(want), allow_ai=want != "none")
    except ScrapeError as e:
        log.info("fill in job %s (%s): failed%s: %s", j.id, j.url, " (AI not allowed)" if want == "none" else "", e)
        return {"status": "failed", "reason": str(e), "ai_allowed": want != "none"}
    draft = _clean_draft(draft)
    filled = [k for k in DETAIL_FIELDS if draft.get(k) and not (getattr(j, k) or "").strip()]
    if j.title == PENDING_TITLE and draft.get("title"):
        filled.insert(0, "title")
        draft["title"] = draft["title"][:300]
    for k in filled:
        setattr(j, k, draft[k])
    log.info("fill in job %s (%s) with %s: %s", j.id, j.url, method, ", ".join(filled) if filled else "nothing new found")
    if filled:
        db.commit()
        db.refresh(j)
    return {"status": "updated" if filled else "unchanged", "filled": filled, "method": method,
            "job": job_out(j, full=False)}


@app.post("/api/cleanup/dead-links")
def cleanup_dead_links(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Delete this profile's jobs whose title is just a link (taken-down postings),
    and blank out company fields that are links."""
    removed = fixed = 0
    for j in db.query(Job).filter(Job.user_id == user.id).all():
        if looks_like_url(j.title):
            log.info("removed taken-down job %s: %s", j.id, j.url or logs.short(j.title))
            db.delete(j)
            removed += 1
        elif looks_like_url(j.company):
            j.company = ""
            fixed += 1
    db.commit()
    return {"removed": removed, "fixed": fixed}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return job_out(own_job(job_id, user, db))


@app.patch("/api/jobs/{job_id}")
def update_job(job_id: int, body: JobPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    j = own_job(job_id, user, db)
    data = body.model_dump(exclude_unset=True)
    if "url" in data:
        u = normalize_url(data["url"] or "")
        if u and find_dupe(db, user, u, exclude_id=j.id):
            raise HTTPException(409, "Another saved job already has that link.")
        data["url"] = u or None
    if "status" in data:
        if data["status"] not in STATUSES:
            raise HTTPException(400, f"Status must be one of: {', '.join(STATUSES)}")
        if data["status"] in ("applied", "interviewing", "offer") and not j.applied_at:
            j.applied_at = now()
    if "title" in data and not (data["title"] or "").strip():
        raise HTTPException(400, "A job needs a title.")
    if "title" in data and looks_like_url(data["title"]):
        raise HTTPException(422, "Give the job a real title, not a link.")
    if "archived" in data:
        _set_archived(j, bool(data.pop("archived")))
    if "queued" in data:
        if data["queued"] and j.archived_at:
            raise HTTPException(400, "Unarchive this job first.")
        if data["queued"] and not (j.description or "").strip():
            raise HTTPException(400, "Add the job description first, so there's something to tailor to.")
        j.queued = bool(data.pop("queued"))
    for k, v in data.items():
        setattr(j, k, v if (v is not None or k == "url") else "")
    db.commit()
    db.refresh(j)
    return job_out(j)


def _set_archived(j: Job, archived: bool) -> bool:
    """Archive or unarchive a job. Archiving also takes it off tonight's list. Returns True if it changed."""
    if bool(j.archived_at) == archived:
        return False
    j.archived_at = now() if archived else None
    if archived:
        j.queued = False
    flag_modified(j, "updated_at")  # keep "Updated" as it was: archiving isn't a change to the job itself
    log.info("%s job %s: %r at %r", "archived" if archived else "unarchived", j.id, logs.short(j.title, 60),
             logs.short(j.company, 40))
    return True


@app.post("/api/jobs/stage")
def set_stage(body: StageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Move several jobs to one stage. Moving to Applied (or later) records the applied date if it's not set."""
    if body.status not in STATUSES:
        raise HTTPException(400, f"Status must be one of: {', '.join(STATUSES)}")
    jobs = db.query(Job).filter(Job.user_id == user.id, Job.id.in_(body.job_ids)).all() if body.job_ids else []
    changed = 0
    for j in jobs:
        if j.status == body.status:
            continue
        j.status = body.status
        if body.status in ("applied", "interviewing", "offer") and not j.applied_at:
            j.applied_at = now()
        changed += 1
    db.commit()
    log.info("moved %d job(s) to %s", changed, body.status)
    return {"changed": changed, "jobs": [job_out(j, full=False) for j in jobs]}


@app.post("/api/jobs/archive")
def archive_jobs(body: ArchiveIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Archive (or unarchive) several jobs at once."""
    jobs = db.query(Job).filter(Job.user_id == user.id, Job.id.in_(body.job_ids)).all() if body.job_ids else []
    changed = sum(_set_archived(j, body.archived) for j in jobs)
    db.commit()
    return {"changed": changed}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    j = own_job(job_id, user, db)
    log.info("deleted job %s: %r at %r %s", j.id, logs.short(j.title, 60), logs.short(j.company, 40), j.url or "")
    db.delete(j)
    db.commit()
    return {"ok": True}


# ---------- AI on a job ----------

def _engine_or_400(engine: str | None) -> str:
    try:
        return ai.resolve(engine)
    except ai.AIError as e:
        raise HTTPException(400, str(e))


def _engine_or_none(engine: str | None) -> str | None:
    """For link reading, where AI is only a fallback: use the chosen engine if
    it's usable, otherwise quietly fall back to the default."""
    return engine if engine and ai.available(engine) else None


# ---------- overnight runs ----------

def _nightly_out(db: Session, user: User) -> dict:
    s = nightly.settings_for(db, user.id)
    runs = db.query(NightlyRun).filter(NightlyRun.user_id == user.id).order_by(NightlyRun.id.desc()).limit(5).all()
    return {
        "settings": {k: getattr(s, k) for k in ("enabled", "run_time", "engine", "do_resume", "do_fit", "do_cover")},
        "timezone": config.TIMEZONE,
        "running": nightly.is_running(),
        "pending": nightly.summary(db, user.id, s),
        "runs": [{
            "id": r.id, "trigger": r.trigger, "engine": r.engine, "status": r.status, "made": r.made,
            "failed": r.failed, "total": r.total, "log": json.loads(r.log or "[]"),
            "started_at": iso(r.started_at), "finished_at": iso(r.finished_at),
        } for r in runs],
    }


@app.get("/api/nightly")
def get_nightly(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return _nightly_out(db, user)


@app.patch("/api/nightly")
def update_nightly(body: NightlyIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s = nightly.settings_for(db, user.id)
    data = body.model_dump(exclude_unset=True)
    if "engine" in data and data["engine"] not in ai.ENGINES:
        raise HTTPException(400, "engine must be claude or local")
    for k, v in data.items():
        if v is not None:
            setattr(s, k, v)
    if "run_time" in data:
        s.last_run_date = ""  # a new time should be able to run today
    db.commit()
    return _nightly_out(db, user)


@app.post("/api/nightly/run")
def run_nightly_now(user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not nightly.start_manual(user.id):
        raise HTTPException(409, "A run is already going.")
    return {"started": True}


@app.post("/api/nightly/queue")
def queue_jobs(body: QueueIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Put several jobs on (or take them off) tonight's list. Jobs without a
    description are skipped when adding."""
    jobs = db.query(Job).filter(Job.user_id == user.id, Job.id.in_(body.job_ids)).all()
    changed = 0
    for j in jobs:
        if body.queued and (not (j.description or "").strip() or j.archived_at):
            continue
        if bool(j.queued) != body.queued:
            j.queued = body.queued
            changed += 1
    db.commit()
    return {"changed": changed, **_nightly_out(db, user)}


@app.post("/api/nightly/stop")
def stop_nightly():
    nightly.request_stop()
    return {"ok": True}

@app.post("/api/jobs/{job_id}/fit")
def check_fit(job_id: int, body: Optional[EngineIn] = None, user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    j = own_job(job_id, user, db)
    if not (j.description or "").strip():
        raise HTTPException(400, "Add the job description first, so there's something to compare against.")
    p = default_persona(db, user)
    eng = _engine_or_400(body.engine if body else None)
    try:
        result = ai.fit(p.resume_text if p else "", user.preferences, j, engine=eng)
    except ai.AIError as e:
        raise HTTPException(502, str(e))
    result["engine"] = ai.label(eng)
    j.fit_score = result["score"]
    j.fit_json = json.dumps(result)
    db.commit()
    db.refresh(j)
    return job_out(j)


@app.post("/api/jobs/{job_id}/documents")
def generate_document(job_id: int, body: GenIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    j = own_job(job_id, user, db)
    if body.kind not in ("resume", "cover_letter"):
        raise HTTPException(400, "kind must be resume or cover_letter")
    if not (j.description or "").strip():
        raise HTTPException(400, "Add the job description first, so it can be tailored.")
    p = default_persona(db, user, body.persona_id)
    if not p or not p.resume_text.strip():
        raise HTTPException(400, "Add your base resume in Settings first.")
    eng = _engine_or_400(body.engine)
    try:
        fn = ai.tailor_resume if body.kind == "resume" else ai.cover_letter
        text = fn(p.resume_text, j, body.instructions, engine=eng)
    except ai.AIError as e:
        raise HTTPException(502, str(e))
    d = Document(job_id=j.id, kind=body.kind, content=text, persona_name=p.name, engine=ai.label(eng))
    db.add(d)
    db.commit()
    return doc_out(d)


def _own_doc(doc_id: int, user: User, db: Session) -> Document:
    d = db.get(Document, doc_id)
    if not d or d.job.user_id != user.id:
        raise HTTPException(404, "Document not found.")
    return d


@app.patch("/api/documents/{doc_id}")
def edit_document(doc_id: int, body: DocPatch, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = _own_doc(doc_id, user, db)
    d.content = body.content
    db.commit()
    return doc_out(d)


@app.delete("/api/documents/{doc_id}")
def delete_document(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    db.delete(_own_doc(doc_id, user, db))
    db.commit()
    return {"ok": True}


def _export_text(d: Document, user: User, db: Session) -> str:
    """Cover letters get a greeting and sign-off if they're missing (older or local-model letters)."""
    if d.kind != "cover_letter":
        return d.content
    p = (db.query(Persona).filter(Persona.user_id == user.id, Persona.name == d.persona_name).first()
         if d.persona_name else None) or default_persona(db, user)
    return ai.finish_letter(d.content, ai.candidate_name(p.resume_text) if p else "")


def _doc_slug(d: Document, user: User) -> str:
    j = d.job
    return re.sub(r"[^A-Za-z0-9]+", "-", f"{user.name} {d.kind.replace('_', ' ')} {j.company or ''} {j.title}").strip("-")[:90]


@app.get("/api/documents/{doc_id}/pdf")
def download_pdf(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = _own_doc(doc_id, user, db)
    try:
        data = to_pdf(_export_text(d, user, db), d.kind)
    except Exception:
        log.exception("PDF export failed for document %s", d.id)
        raise HTTPException(500, "Couldn't make the PDF. Download the Word file instead; details are in today's log.")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{_doc_slug(d, user)}.pdf"'})


@app.get("/api/documents/{doc_id}/docx")
def download_docx(doc_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    d = _own_doc(doc_id, user, db)
    slug = _doc_slug(d, user)
    return Response(
        to_docx(_export_text(d, user, db), d.kind),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{slug}.docx"'},
    )


# ---------- import / export ----------

OLD_STATUS = {"not started": "saved", "in progress": "applied", "abandon": "closed", "abandoned": "closed",
              "applied": "applied", "interviewing": "interviewing", "offer": "offer", "closed": "closed",
              "rejected": "closed", "saved": "saved"}


def _map_status(s: str | None) -> str:
    return OLD_STATUS.get((s or "").strip().lower(), "saved")


TITLE_COLS = ("job title", "title", "name", "job name", "position", "role", "job")
COMPANY_COLS = ("company name", "company", "employer", "organisation", "organization", "hiring organization")
URL_COLS = ("job url", "url", "link", "job link", "posting url", "job posting url", "apply url")


def _csv_fail(filename: str | None, why: str):
    log.warning("CSV import of %r failed: %s", filename, why)
    raise HTTPException(400, why)


def _read_csv(raw: bytes, filename: str | None) -> list[dict]:
    """Reads the CSV the way people actually save it: UTF-8 from Notion or
    Google Sheets, Windows encoding from Excel, commas, semicolons or tabs."""
    if raw[:2] == b"PK" or raw[:4] == b"\xd0\xcf\x11\xe0":
        _csv_fail(filename, "That's an Excel file, not a CSV. In Excel use File → Save As → CSV UTF-8, then import that file.")
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text.strip():
        _csv_fail(filename, "That file is empty.")
    head = text[:5000]
    try:
        delim = csv.Sniffer().sniff(head.split("\n", 1)[0], delimiters=",;\t|").delimiter
    except csv.Error:
        delim = ","
    rows = list(csv.DictReader(io.StringIO(text), delimiter=delim))
    cols = [k.strip().lower() for k in (rows[0].keys() if rows else csv.DictReader(io.StringIO(text), delimiter=delim).fieldnames or []) if k]
    log.info("CSV import: file %r read as %s, separated by %r, %d rows, columns: %s",
             filename, enc, delim, len(rows), ", ".join(cols) or "none")
    if not any(c in cols for c in TITLE_COLS + URL_COLS):
        _csv_fail(filename, f"Couldn't find a job title or link column. The file has: {', '.join(cols) or 'no columns'}. "
                            "Name the column with the job titles “Job title” (or the links “Job URL”) and import again.")
    if not rows:
        _csv_fail(filename, "That file has column names but no jobs under them.")
    return rows


@app.post("/api/import/csv")
async def import_csv(file: UploadFile = File(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = _read_csv(await file.read(), file.filename)

    def col(row, *names):
        low = {k.strip().lower(): v for k, v in row.items() if k}
        for n in names:
            if low.get(n):
                return low[n].strip()
        return ""

    log.info("CSV import into profile %r: %d rows", user.name, len(rows))
    POSTED = ("published at", "posted", "posted date", "date posted", "date")

    # A job that's reposted keeps its link, so it can appear more than once.
    # Find the newest posted date for each link across the whole file.
    today = now()
    newest: dict[str, str] = {}
    for row in rows:
        u = normalize_url(col(row, *URL_COLS))
        d = normalize_date(col(row, *POSTED), today)
        if u and is_iso(d) and d > newest.get(u, ""):
            newest[u] = d

    added = dupes = invalid = dead = redated = 0
    to_fill: list[int] = []  # new jobs with a link but no description, to read from their postings next
    redated_ids: set[int] = set()
    for n, row in enumerate(rows, start=2):  # line 1 is the header
        raw_url = col(row, *URL_COLS)
        url = normalize_url(raw_url)
        title = col(row, *TITLE_COLS)
        company = col(row, *COMPANY_COLS)
        what = f"line {n}: {logs.short(title, 60)!r} at {logs.short(company, 40)!r} {url or '(no link)'}"
        if not title and not url:
            invalid += 1
            log.info("  %s -> skipped, no job title and no link", what)
            continue
        if not title:
            title = PENDING_TITLE  # only a link: the title is read from the posting when it's filled in
        if looks_like_url(title):
            dead += 1
            log.info("  %s -> left out, title is a link (posting taken down)", what)
            continue
        dupe = find_dupe(db, user, url)
        if dupe:
            dupes += 1
            latest = newest.get(url, "")
            old = dupe.posted_date or ""
            if latest and (not is_iso(old) or latest > old) and dupe.id not in redated_ids:
                dupe.posted_date = latest
                if old and not dupe.first_posted:
                    dupe.first_posted = old
                dupe.reposted_at = now()
                redated_ids.add(dupe.id)
                redated += 1
                log.info("  %s -> already saved as job %s (%r, stage %s); reposted, posted date %s -> %s", what,
                         dupe.id, logs.short(dupe.title, 60), dupe.status, old or "(none)", latest)
            else:
                log.info("  %s -> already saved as job %s (%r, stage %s, posted %s)", what, dupe.id,
                         logs.short(dupe.title, 60), dupe.status, old or "(none)")
            continue
        job = Job(user_id=user.id, url=url or None, title=title,
                   company="" if looks_like_url(company) else company, location=col(row, "location"),
                   posted_date=newest.get(url) or col(row, *POSTED),
                   description=col(row, "description", "job description"),
                   status=_map_status(col(row, "status")), source="csv")
        db.add(job)
        db.flush()
        added += 1
        if job.url and not (job.description or "").strip():
            to_fill.append(job.id)
        log.info("  %s -> added as job %s (stage %s)", what, job.id, job.status)
    db.commit()
    log.info("CSV import done: %d rows, %d added, %d already saved (%d of them reposted, date updated), "
             "%d without a title, %d taken down", len(rows), added, dupes, redated, invalid, dead)
    return {"rows": len(rows), "added": added, "duplicates": dupes, "reposted": redated, "skipped": invalid, "dead": dead,
            "to_fill": to_fill}


@app.get("/api/export")
def export_all(db: Session = Depends(get_db)):
    out = {"format": "job-hacker", "version": 2, "exported_at": iso(now()), "profiles": []}
    for u in db.query(User).order_by(User.id):
        out["profiles"].append({
            "name": u.name, "preferences": u.preferences,
            "personas": [{"name": p.name, "resume_text": p.resume_text, "is_default": p.is_default} for p in u.personas],
            "jobs": [{
                **{k: getattr(j, k) for k in ("url", "title", "company", "location", "salary", "employment_type",
                                               "posted_date", "description", "source", "status", "tags", "notes",
                                               "fit_score", "fit_json", "first_posted")},
                "applied_at": iso(j.applied_at), "created_at": iso(j.created_at), "reposted_at": iso(j.reposted_at),
                "archived_at": iso(j.archived_at),
                "documents": [{"kind": d.kind, "content": d.content, "persona_name": d.persona_name,
                               "engine": d.engine or "",
                               "created_at": iso(d.created_at)} for d in j.documents],
            } for j in u.jobs],
        })
    stamp = dt.date.today().isoformat()
    return Response(json.dumps(out, indent=1), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="job-hacker-backup-{stamp}.json"'})


def _parse_dt(s) -> dt.datetime | None:
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s).replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        return None


@app.post("/api/import/backup")
async def import_backup(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Restores a backup from this app, or the file made by scripts/export_old_tracker.py.
    Merges by profile name; skips jobs whose link is already saved."""
    try:
        data = json.loads((await file.read()).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "That isn't a Job Hacker backup file.")
    if data.get("format") != "job-hacker":
        raise HTTPException(400, "That isn't a Job Hacker backup file.")

    summary = {"profiles": 0, "jobs": 0, "duplicates": 0, "resumes": 0, "documents": 0}
    log.info("backup import: file %r, %d profile(s)", file.filename, len(data.get("profiles", [])))
    for prof in data.get("profiles", []):
        name = (prof.get("name") or "Me").strip()
        u = db.query(User).filter(func.lower(User.name) == name.lower()).first()
        if not u:
            # Fresh install: adopt the empty default "Me" profile instead of adding a second one.
            only = db.query(User).all()
            if len(only) == 1 and only[0].name == "Me" and not only[0].jobs and not only[0].personas:
                u = only[0]
                u.name = name
            else:
                u = User(name=name)
                db.add(u)
            db.flush()
            summary["profiles"] += 1
        if prof.get("preferences") and not u.preferences:
            u.preferences = prof["preferences"]

        existing_p = {p.name.lower() for p in u.personas}
        has_default = any(p.is_default for p in u.personas)
        for p in prof.get("personas", []):
            if p.get("name", "").lower() in existing_p:
                continue
            make_def = bool(p.get("is_default")) and not has_default
            has_default = has_default or make_def
            db.add(Persona(user_id=u.id, name=p.get("name") or "Resume", resume_text=p.get("resume_text") or "",
                           is_default=make_def))
            summary["resumes"] += 1

        for j in prof.get("jobs", []):
            url = normalize_url(j.get("url") or "")
            if not (j.get("title") or "").strip() or looks_like_url(j.get("title")):
                summary["dead"] = summary.get("dead", 0) + 1
                continue
            same_linkless = not url and db.query(Job).filter(
                Job.user_id == u.id, Job.url.is_(None), Job.title == j["title"][:300],
                Job.company == (j.get("company") or "")).first()
            dupe = find_dupe(db, u, url) or same_linkless
            if dupe:
                summary["duplicates"] += 1
                log.info("  %r at %r %s -> already saved as job %s (stage %s)", logs.short(j.get("title"), 60),
                         logs.short(j.get("company"), 40), url or "(no link)", dupe.id, dupe.status)
                continue
            job = Job(
                user_id=u.id, url=url or None, title=j["title"][:300],
                **{k: (j.get(k) or "") for k in ("company", "location", "salary", "employment_type", "posted_date",
                                                  "description", "tags", "notes", "fit_json")},
                source=j.get("source") or "import", status=_map_status(j.get("status")),
                fit_score=j.get("fit_score"), applied_at=_parse_dt(j.get("applied_at")),
                reposted_at=_parse_dt(j.get("reposted_at")), first_posted=j.get("first_posted") or "",
                archived_at=_parse_dt(j.get("archived_at")),
                created_at=_parse_dt(j.get("created_at")) or now(),
            )
            db.add(job)
            db.flush()
            summary["jobs"] += 1
            for d in j.get("documents", []):
                db.add(Document(job_id=job.id, kind=d.get("kind") or "resume", content=d.get("content") or "",
                                persona_name=d.get("persona_name") or "", engine=d.get("engine") or "",
                                created_at=_parse_dt(d.get("created_at")) or now()))
                summary["documents"] += 1
    db.commit()
    log.info("backup import done: %s", summary)
    return summary


# ---------- log files ----------

@app.get("/api/logs")
def list_logs():
    return {"folder": str(logs.LOG_DIR), "keep_days": logs.KEEP_DAYS, "files": logs.files()}


@app.get("/api/logs/{name}")
def get_log(name: str):
    p = logs.path_for(name)
    if not p:
        raise HTTPException(404, "No log file with that name.")
    return FileResponse(p, media_type="text/plain; charset=utf-8", filename=name)


# ---------- frontend ----------

@app.get("/sw.js", include_in_schema=False)
def sw():
    return FileResponse(config.STATIC_DIR / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    return FileResponse(config.STATIC_DIR / "manifest.webmanifest", media_type="application/manifest+json")


app.mount("/assets", StaticFiles(directory=config.STATIC_DIR), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
