"""Overnight run: for each profile that has it turned on, work through the jobs
the user put on tonight's list (Job.queued) and make what the settings ask for:
a tailored resume, a fit check, a cover letter. Nothing is made for jobs that
aren't on the list. A job comes off the list once everything for it worked;
anything that failed stays on for the next night.
Runs inside the app on a background thread — no cron needed.

Only one run happens at a time. A run can be stopped from Settings; it finishes
the item it's on and then stops.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
import time
from zoneinfo import ZoneInfo

from . import ai, config
from .db import Document, Job, NightlyRun, NightlySetting, Persona, SessionLocal, User, now

log = logging.getLogger("jobhacker.nightly")
TZ = ZoneInfo(config.TIMEZONE)

_lock = threading.Lock()
_stop = threading.Event()
_started = False


def is_running() -> bool:
    return _lock.locked()


def request_stop():
    if is_running():
        _stop.set()


def settings_for(db, user_id: int) -> NightlySetting:
    s = db.get(NightlySetting, user_id)
    if not s:
        s = NightlySetting(user_id=user_id)
        db.add(s)
        db.commit()
    return s


def _persona(db, user_id: int) -> Persona | None:
    q = db.query(Persona).filter(Persona.user_id == user_id)
    return q.filter(Persona.is_default.is_(True)).first() or q.order_by(Persona.id).first()


def plan(db, user_id: int, s: NightlySetting) -> list[tuple[Job, str]]:
    """The (job, task) pairs a run would do right now: only jobs on tonight's list.
    A fit check is skipped if the job already has a score; a resume or cover
    letter is always made, since the job was picked on purpose."""
    jobs = (db.query(Job)
            .filter(Job.user_id == user_id, Job.queued.is_(True), Job.archived_at.is_(None))
            .order_by(Job.updated_at.desc()).all())
    tasks = []
    for j in jobs:
        if not (j.description or "").strip():
            continue
        if s.do_fit and j.fit_score is None:
            tasks.append((j, "fit"))
        if s.do_resume:
            tasks.append((j, "resume"))
        if s.do_cover:
            tasks.append((j, "cover_letter"))
    # cap by number of distinct jobs, not tasks
    limit = s.max_jobs or config.NIGHTLY_MAX_JOBS
    keep, seen = [], []
    for j, t in tasks:
        if j.id not in seen:
            if len(seen) >= limit:
                break
            seen.append(j.id)
        keep.append((j, t))
    return keep


def summary(db, user_id: int, s: NightlySetting) -> dict:
    p = plan(db, user_id, s)
    return {
        "queued": db.query(Job).filter(Job.user_id == user_id, Job.queued.is_(True), Job.archived_at.is_(None)).count(),
        # picked jobs a run could work on (they have a description), and the per-run limit
        "ready": db.query(Job).filter(Job.user_id == user_id, Job.queued.is_(True), Job.archived_at.is_(None),
                                      Job.description != "").count(),
        "limit": s.max_jobs or config.NIGHTLY_MAX_JOBS,
        "stop_at": s.stop_at or "",
        "jobs": len({j.id for j, _ in p}),
        "resume": sum(t == "resume" for _, t in p),
        "fit": sum(t == "fit" for _, t in p),
        "cover_letter": sum(t == "cover_letter" for _, t in p),
    }


TASK_LABEL = {"resume": "Tailored resume", "fit": "Fit check", "cover_letter": "Cover letter"}


def _deadline(stop_at: str | None) -> dt.datetime | None:
    """The next time it's stop_at (HH:MM, local), or None when there's no limit."""
    if not stop_at:
        return None
    try:
        h, m = (int(x) for x in stop_at.split(":"))
    except ValueError:
        return None
    now_l = dt.datetime.now(ZoneInfo(config.TIMEZONE))
    d = now_l.replace(hour=h, minute=m, second=0, microsecond=0)
    return d if d > now_l else d + dt.timedelta(days=1)


def run_for_user(user_id: int, trigger: str = "schedule") -> int | None:
    """Do the work. Returns the run id, or None if another run is in progress."""
    if not _lock.acquire(blocking=False):
        return None
    _stop.clear()
    try:
        with SessionLocal() as db:
            s = settings_for(db, user_id)
            user = db.get(User, user_id)
            run = NightlyRun(user_id=user_id, trigger=trigger, engine="")
            db.add(run)
            db.commit()
            entries: list[dict] = []

            def save(status=None):
                run.log = json.dumps(entries)
                if status:
                    run.status = status
                    run.finished_at = now()
                    log.info("overnight run %s (%s) %s: %s made, %s failed of %s%s", run.id, trigger, status, run.made,
                             run.failed, run.total, "" if status != "failed" or not entries else
                             ": " + entries[-1].get("note", ""))
                db.commit()

            ai.use_claude_model(s.claude_model or None)  # this run's thread uses the overnight model
            try:
                engine = ai.resolve(s.engine)
            except ai.AIError as e:
                entries.append({"ok": False, "note": str(e)})
                save("failed")
                return run.id
            run.engine = ai.label(engine)
            persona = _persona(db, user_id)
            if not persona or not persona.resume_text.strip():
                entries.append({"ok": False, "note": "Add your base resume in Settings first."})
                save("failed")
                return run.id

            tasks = plan(db, user_id, s)
            run.total = len(tasks)
            log.info("overnight run %s (%s) started: %d task(s) with %s", run.id, trigger, len(tasks), run.engine)
            save()
            failed_jobs: set[int] = set()
            remaining = {}
            for job, _ in tasks:
                remaining[job.id] = remaining.get(job.id, 0) + 1
            deadline = _deadline(s.stop_at)
            for job, task in tasks:
                if _stop.is_set():
                    save("stopped")
                    return run.id
                if deadline and dt.datetime.now(ZoneInfo(config.TIMEZONE)) >= deadline:
                    log.info("overnight run %s reached the %s stop time with %d task(s) left", run.id, s.stop_at,
                             len(tasks) - len(entries))
                    entries.append({"ok": True, "note": f"Stopped at {s.stop_at}, as set in Settings. The rest stay on tonight's list."})
                    save("stopped")
                    return run.id
                entry = {"job_id": job.id, "title": job.title, "company": job.company, "task": task}
                t0 = time.time()
                try:
                    if task == "fit":
                        res = ai.fit(persona.resume_text, user.preferences, job, engine=engine)
                        res["engine"] = ai.label(engine)
                        job.fit_score = res["score"]
                        job.fit_json = json.dumps(res)
                        entry["note"] = f"Scored {res['score']}/10"
                    else:
                        fn = ai.tailor_resume if task == "resume" else ai.cover_letter
                        text = fn(persona.resume_text, job, "", engine=engine)
                        log.info("overnight %s for job %s: %d words, ends %r", task, job.id, len(text.split()), text.strip()[-60:])
                        db.add(Document(job_id=job.id, kind=task, content=text, persona_name=persona.name,
                                        engine=ai.label(engine), nightly=True))
                    entry["ok"] = True
                    run.made += 1
                except Exception as e:  # never let one job kill the run
                    db.rollback()  # drop this task's half-made changes, then record the failure
                    if not isinstance(e, ai.AIError):
                        log.exception("nightly task failed")
                    entry.update(ok=False, note=str(e) if isinstance(e, ai.AIError) else f"{e.__class__.__name__}: {e}")
                    run.failed += 1
                entry["seconds"] = round(time.time() - t0)
                log.info("overnight %s for job %s (%s): %s in %ss", task, job.id, (job.title or "")[:60],
                         "done" if entry.get("ok") else "failed: " + entry.get("note", ""), entry["seconds"])
                entries.append(entry)
                if not entry.get("ok"):
                    failed_jobs.add(job.id)
                remaining[job.id] -= 1
                if remaining[job.id] == 0 and job.id not in failed_jobs:
                    db.get(Job, job.id).queued = False  # all done for this job: take it off the list
                save()
            save("done")
            return run.id
    finally:
        _lock.release()


def start_manual(user_id: int) -> bool:
    if is_running():
        return False
    threading.Thread(target=run_for_user, args=(user_id, "manual"), daemon=True, name="nightly-manual").start()
    return True


def _loop():
    while True:
        try:
            local = dt.datetime.now(TZ)
            today, hhmm = local.date().isoformat(), local.strftime("%H:%M")
            with SessionLocal() as db:
                due = [s.user_id for s in db.query(NightlySetting).filter(NightlySetting.enabled.is_(True))
                       if s.last_run_date != today and s.run_time <= hhmm
                       and _minutes(hhmm) - _minutes(s.run_time) < 60]  # catch up if the app was restarting, not later
                for uid in due:
                    st = db.get(NightlySetting, uid)
                    st.last_run_date = today
                db.commit()
            for uid in due:
                log.info("overnight run for user %s", uid)
                run_for_user(uid, "schedule")
        except Exception:
            log.exception("scheduler loop error")
        time.sleep(20)


def _minutes(hhmm: str) -> int:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return -10_000


def start_scheduler():
    global _started
    if _started or not config.NIGHTLY_SCHEDULER:
        return
    _started = True
    threading.Thread(target=_loop, daemon=True, name="nightly-scheduler").start()
