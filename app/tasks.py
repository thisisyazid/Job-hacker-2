"""Resumes and cover letters made on the server, so the browser can move on.

A request is checked straight away (job description, base resume, AI engine), then queued.
One worker thread makes the documents one at a time: a local model handles one job at a
time best, and Claude requests are quick anyway. The queue lives in memory, so a restart
of the server drops tasks that hadn't finished; finished documents are saved to the
database as usual."""
from __future__ import annotations

import datetime as dt
import itertools
import logging
import queue
import threading
import time

from . import ai

log = logging.getLogger("jobhacker")
KINDS = {"resume": "Tailored resume", "cover_letter": "Cover letter"}
KEEP = 50  # finished tasks kept for the status bar

_q: queue.Queue[int] = queue.Queue()
_tasks: dict[int, dict] = {}
_ids = itertools.count(1)
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _iso(t: float | None) -> str | None:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat().replace("+00:00", "Z") if t else None


def _public(t: dict) -> dict:
    return {k: (_iso(v) if k in ("queued_at", "started_at", "finished_at") else v)
            for k, v in t.items() if not k.startswith("_")}


def submit(user_id: int, job, kind: str, persona_id: int | None, instructions: str, engine: str) -> dict:
    """Queue a document. Raises nothing: everything that can fail fast was checked by the caller."""
    global _worker
    with _lock:
        tid = next(_ids)
        _tasks[tid] = {
            "id": tid, "user_id": user_id, "job_id": job.id, "title": job.title or "", "company": job.company or "",
            "kind": kind, "label": KINDS.get(kind, kind), "engine": ai.label(engine), "status": "queued",
            "error": "", "document_id": None, "queued_at": time.time(), "started_at": None, "finished_at": None,
            "_persona_id": persona_id, "_instructions": instructions, "_engine": engine,
            "_claude_model": ai.claude_model(), "_local_model": ai.local_model(),
        }
        _prune()
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work, name="documents", daemon=True)
            _worker.start()
    _q.put(tid)
    log.info("queued %s for job %s (%s) by %s: task %s", kind, job.id, (job.title or "")[:60], ai.label(engine), tid)
    return _public(_tasks[tid])


def _prune() -> None:
    done = [t for t in _tasks.values() if t["status"] in ("done", "failed", "cancelled")]
    for t in sorted(done, key=lambda x: x["finished_at"] or 0)[:-KEEP]:
        _tasks.pop(t["id"], None)


def list_for(user_id: int) -> list[dict]:
    with _lock:
        mine = [t for t in _tasks.values() if t["user_id"] == user_id]
    return [_public(t) for t in sorted(mine, key=lambda t: t["id"], reverse=True)]


def cancel(user_id: int, tid: int) -> bool:
    """Cancel a task that hasn't started yet."""
    with _lock:
        t = _tasks.get(tid)
        if not t or t["user_id"] != user_id or t["status"] != "queued":
            return False
        t.update(status="cancelled", finished_at=time.time())
    log.info("cancelled task %s (%s for job %s)", tid, t["kind"], t["job_id"])
    return True


def _work() -> None:
    while True:
        tid = _q.get()
        with _lock:
            t = _tasks.get(tid)
            if not t or t["status"] != "queued":
                continue
            t.update(status="running", started_at=time.time())
        try:
            t["document_id"] = _make(t)
            t["status"] = "done"
        except Exception as e:  # never let one task stop the worker
            t["status"] = "failed"
            t["error"] = str(e) if isinstance(e, ai.AIError) else f"{e.__class__.__name__}: {e}"
            if not isinstance(e, ai.AIError):
                log.exception("background %s for job %s failed", t["kind"], t["job_id"])
        t["finished_at"] = time.time()
        log.info("task %s (%s for job %s): %s in %ds", tid, t["kind"], t["job_id"],
                 "done" if t["status"] == "done" else "failed: " + t["error"], t["finished_at"] - t["started_at"])


def _make(t: dict) -> int:
    from .db import Document, Job, Persona, SessionLocal
    with SessionLocal() as db:
        job = db.get(Job, t["job_id"])
        if not job:
            raise ai.AIError("The job was deleted before its turn came.")
        q = db.query(Persona).filter(Persona.user_id == t["user_id"])
        p = (q.filter(Persona.id == t["_persona_id"]).first() if t["_persona_id"] else None) \
            or q.filter(Persona.is_default.is_(True)).first() or q.order_by(Persona.id).first()
        if not p or not p.resume_text.strip():
            raise ai.AIError("Add your base resume in Settings first.")
        ai.use_claude_model(t["_claude_model"])
        ai.use_local_model(t["_local_model"])
        fn = ai.tailor_resume if t["kind"] == "resume" else ai.cover_letter
        text = fn(p.resume_text, job, t["_instructions"], engine=t["_engine"])
        d = Document(job_id=job.id, kind=t["kind"], content=text, persona_name=p.name, engine=ai.label(t["_engine"]))
        db.add(d)
        db.commit()
        log.info("saved %s for job %s by %s (background): %d words", t["kind"], job.id, d.engine, len(text.split()))
        return d.id
