"""Everything that talks to an AI model. Two engines:

  claude  Anthropic API (ANTHROPIC_API_KEY)
  local   Ollama on the Mac mini (OLLAMA_URL, OLLAMA_MODEL), e.g. qwen3:14b

Every feature takes an optional engine; None means the default engine.
All features degrade gracefully when neither is available — the UI hides them."""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from . import config

log = logging.getLogger("jobhacker.ai")
ENGINES = ("claude", "local")
_client = None
_local_cache: dict = {"at": 0.0, "status": None}


class AIError(Exception):
    pass


# ---------- availability ----------

def local_status(force: bool = False) -> dict:
    """{'ok': bool, 'model': str, 'reason': str}. Cached for a minute so page
    loads don't wait on Ollama."""
    if not config.OLLAMA_URL:
        return {"ok": False, "model": config.OLLAMA_MODEL, "reason": "OLLAMA_URL isn't set."}
    if not force and _local_cache["status"] and time.time() - _local_cache["at"] < 60:
        return _local_cache["status"]
    st = {"ok": False, "model": config.OLLAMA_MODEL, "reason": ""}
    try:
        r = httpx.get(config.OLLAMA_URL.rstrip("/") + "/api/tags", timeout=httpx.Timeout(2.0))
        names = {m.get("name", "") for m in r.json().get("models", [])}
        want = config.OLLAMA_MODEL
        if want in names or f"{want}:latest" in names or any(n.split(":")[0] == want for n in names):
            st["ok"] = True
        else:
            st["reason"] = f"Ollama is running but doesn't have {want}. Run: ollama pull {want}"
    except Exception as e:
        st["reason"] = f"Couldn't reach Ollama at {config.OLLAMA_URL} ({e.__class__.__name__})."
    _local_cache.update(at=time.time(), status=st)
    return st


def available(engine: str) -> bool:
    if engine == "claude":
        return bool(config.ANTHROPIC_API_KEY)
    if engine == "local":
        return local_status()["ok"]
    return False


def default_engine() -> str | None:
    pref = config.DEFAULT_ENGINE if config.DEFAULT_ENGINE in ENGINES else "claude"
    for e in (pref, *[x for x in ENGINES if x != pref]):
        if available(e):
            return e
    return None


def enabled(engine: str | None = None) -> bool:
    return available(engine) if engine else default_engine() is not None


def engines() -> dict:
    loc = local_status()
    return {
        "claude": {"available": bool(config.ANTHROPIC_API_KEY), "label": "Claude",
                   "model": config.CLAUDE_MODEL,
                   "reason": "" if config.ANTHROPIC_API_KEY else "ANTHROPIC_API_KEY isn't set."},
        "local": {"available": loc["ok"], "label": "Local", "model": config.OLLAMA_MODEL, "reason": loc["reason"]},
        "default": default_engine(),
    }


def resolve(engine: str | None) -> str:
    """Pick the engine for a request, with a clear error if it can't be used."""
    if engine in (None, "", "default"):
        e = default_engine()
        if not e:
            raise AIError("AI features are off. Add ANTHROPIC_API_KEY, or set up Ollama, in your .env.")
        return e
    if engine not in ENGINES:
        raise AIError(f"Unknown AI engine: {engine}")
    if not available(engine):
        if engine == "local":
            raise AIError("The local model isn't available. " + local_status(force=True)["reason"])
        raise AIError("Claude isn't set up. Add ANTHROPIC_API_KEY to your .env.")
    return engine


def label(engine: str) -> str:
    return f"Claude ({config.CLAUDE_MODEL})" if engine == "claude" else f"Local ({config.OLLAMA_MODEL})"


# ---------- calls ----------

def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _ask_claude(system: str, user: str, max_tokens: int) -> str:
    try:
        resp = _get_client().messages.create(
            model=config.CLAUDE_MODEL, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:  # network, auth, rate limit…
        raise AIError(f"Claude request failed: {e.__class__.__name__}: {e}") from e
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()


def _ask_local(system: str, user: str, max_tokens: int, json_mode: bool) -> str:
    body = {
        "model": config.OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,  # qwen3 and other reasoning models: answer directly
        "keep_alive": "5m",
        "options": {"num_ctx": config.OLLAMA_NUM_CTX, "num_predict": max_tokens, "temperature": 0.4},
    }
    if json_mode:
        body["format"] = "json"
    url = config.OLLAMA_URL.rstrip("/") + "/api/chat"
    timeout = httpx.Timeout(config.OLLAMA_TIMEOUT, connect=5.0)
    try:
        r = httpx.post(url, json=body, timeout=timeout)
        if r.status_code == 400 and "think" in r.text.lower():  # older Ollama without the think option
            body.pop("think")
            r = httpx.post(url, json=body, timeout=timeout)
        r.raise_for_status()
        text = (r.json().get("message") or {}).get("content", "")
    except httpx.TimeoutException as e:
        raise AIError(f"The local model took longer than {config.OLLAMA_TIMEOUT}s and was stopped.") from e
    except Exception as e:
        raise AIError(f"Local model request failed: {e.__class__.__name__}: {e}") from e
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if not text:
        raise AIError("The local model returned an empty answer. Try again, or use Claude.")
    return text


def _ask(system: str, user: str, max_tokens: int = 2000, engine: str | None = None, json_mode: bool = False) -> str:
    e = resolve(engine)
    t0 = time.monotonic()
    try:
        out = _ask_local(system, user, max_tokens, json_mode) if e == "local" else _ask_claude(system, user, max_tokens)
    except AIError as err:
        log.warning("AI call (%s) failed after %.1fs: %s", label(e), time.monotonic() - t0, err)
        raise
    log.info("AI call (%s): %.1fs, %d chars in, %d chars out", label(e), time.monotonic() - t0, len(user), len(out))
    return out


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise AIError("The AI didn't return the expected format. Try again.")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise AIError("The AI didn't return the expected format. Try again.") from e


def _job_block(job) -> str:
    g = (lambda k: getattr(job, k, None) if not isinstance(job, dict) else job.get(k))
    return (
        f"Job title: {g('title') or 'n/a'}\n"
        f"Company: {g('company') or 'n/a'}\n"
        f"Location: {g('location') or 'n/a'}\n"
        f"Salary: {g('salary') or 'n/a'}\n\n"
        f"Job description:\n{g('description') or '(no description saved)'}\n"
    )


# ---------- extraction ----------

EXTRACT_SYSTEM = """You extract job posting details from messy text copied off a web page.
Return ONLY a JSON object with these string keys:
title, company, location, salary, employment_type, posted_date, description.
Rules:
- Use "" for anything not stated. Never guess a company or salary.
- posted_date: YYYY-MM-DD if a date is given, otherwise the text as written (e.g. "3 days ago"), else "".
- description: the full job description (responsibilities, requirements, benefits, about the company)
  as clean plain text with "- " bullets and blank lines between sections. Drop navigation, cookie
  banners, "similar jobs", sign-in prompts and other page chrome. Do not summarise or shorten it."""


def extract_job(text: str, hint_title: str = "", engine: str | None = None) -> dict:
    user = (f"Page title: {hint_title}\n\n" if hint_title else "") + f"TEXT:\n{text}"
    data = _json(_ask(EXTRACT_SYSTEM, user, max_tokens=4000, engine=engine, json_mode=True))
    return {k: str(data.get(k) or "").strip() for k in
            ("title", "company", "location", "salary", "employment_type", "posted_date", "description")}


# ---------- fit score ----------

FIT_SYSTEM = """You are a frank career advisor. Score how well a candidate fits a job, 1–10.
10 = they meet essentially every requirement and it matches what they want.
7 = solid match worth applying, a few gaps. 4 = stretch. 1 = clearly wrong role.
If anything in the candidate's stated dealbreakers applies, score 2 or lower and say which.
Return ONLY JSON:
{"score": <int 1-10>,
 "verdict": "<one plain sentence, max 25 words>",
 "strengths": ["<max 4 short points, each tied to real resume evidence>"],
 "gaps": ["<max 4 short points: missing requirements or concerns>"],
 "keywords": ["<up to 8 terms from the posting worth mirroring in the resume>"]}"""


def fit(resume: str, preferences: str, job, engine: str | None = None) -> dict:
    user = (
        f"CANDIDATE RESUME:\n{resume or '(none provided)'}\n\n"
        f"WHAT THE CANDIDATE WANTS / DEALBREAKERS:\n{preferences or '(not specified)'}\n\n"
        f"JOB:\n{_job_block(job)}"
    )
    d = _json(_ask(FIT_SYSTEM, user, max_tokens=1200, engine=engine, json_mode=True))
    try:
        d["score"] = max(1, min(10, int(d.get("score", 0))))
    except (TypeError, ValueError):
        raise AIError("Claude didn't return a score. Try again.")
    for k in ("strengths", "gaps", "keywords"):
        d[k] = [str(x) for x in (d.get(k) or [])][:8]
    d["verdict"] = str(d.get("verdict") or "")
    return d


# ---------- documents ----------

RESUME_SYSTEM = """You are an expert resume writer. You get a candidate's base resume and a target
job. Produce a tailored resume in the exact Markdown structure below — nothing else.

# Full Name
email | phone | city | website
Headline 1 | Headline 2 | Headline 3

## Professional Summary
One paragraph, 3–5 sentences, written for this role.

## Core Capabilities
- **Group name:** item, item, item

## Professional Experience
### Company | Month YYYY – Month YYYY
**Job Title — Team or Department**
- Achievement bullet
- Achievement bullet

## Certifications & Education
- **Certifications:** ...
- **Education:** ...

## Tools & Technology
Comma-separated list.

Hard rules:
- ROLE ORDER IS FIXED: list every role in reverse chronological order (most recent first),
  with the same companies, titles and dates as the base resume. Never move a role up or down
  because it is more relevant, and never drop a role. Older roles may be shortened to 1–2 bullets.
- Within a role you MAY reorder, reword and trim bullets to mirror the job's language and
  priorities. You MAY rewrite the summary, headlines and capability groups for this role.
- Never invent employers, titles, dates, certifications, tools or metrics.
- Write past roles in past tense and the current role (end date "Present") in present tense.
- Use only the markers shown: #, ##, ###, "- " bullets and **bold**. No tables, no horizontal
  rules, no decorative symbols (❖ ▪ • ★ etc.) — remove any that appear in the base resume.
- Contact and headline lines separate items with " | ".
- Keep it to about two pages; cut the least relevant bullets first.
- Output ONLY the resume."""

COVER_SYSTEM = """You are an expert cover letter writer. You get a candidate's resume and a target
job. Write a specific cover letter under 350 words as clean plain text — no Markdown.

Rules:
- Open with something specific to this role or company, not "I am writing to express my interest".
- Reference concrete details from the posting (team, product, requirements).
- Use only real experience from the resume; never fabricate.
- Plain, confident, warm tone. Short paragraphs.
- Output ONLY the letter (greeting to sign-off)."""


def tailor_resume(resume: str, job, extra: str = "", engine: str | None = None) -> str:
    if not resume.strip():
        raise AIError("Add your base resume in Settings first.")
    user = f"BASE RESUME:\n{resume}\n\nTARGET JOB:\n{_job_block(job)}"
    if extra.strip():
        user += f"\n\nEXTRA INSTRUCTIONS FROM THE CANDIDATE:\n{extra}"
    from .resume_format import sort_experience
    out = _ask(RESUME_SYSTEM, user, max_tokens=4000, engine=engine)
    out = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", out.strip())
    return sort_experience(out)


def cover_letter(resume: str, job, extra: str = "", engine: str | None = None) -> str:
    if not resume.strip():
        raise AIError("Add your base resume in Settings first.")
    user = f"RESUME:\n{resume}\n\nTARGET JOB:\n{_job_block(job)}"
    if extra.strip():
        user += f"\n\nEXTRA INSTRUCTIONS FROM THE CANDIDATE:\n{extra}"
    out = _ask(COVER_SYSTEM, user, max_tokens=1500, engine=engine)
    return re.sub(r"^```(?:\w+)?\s*|\s*```$", "", out.strip())
