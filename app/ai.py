"""Everything that talks to an AI model. Two engines:

  claude  Anthropic API (ANTHROPIC_API_KEY)
  local   Ollama on the Mac mini (OLLAMA_URL, OLLAMA_MODEL), e.g. qwen3:14b

Every feature takes an optional engine; None means the default engine.
All features degrade gracefully when neither is available — the UI hides them."""
from __future__ import annotations

import contextvars
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
                   "models": [{"id": k, **v} for k, v in config.CLAUDE_MODELS.items()],
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


# The Claude model for the current request or overnight run: set from the app's model picker
# (X-Claude-Model header) or the overnight settings; falls back to CLAUDE_MODEL.
_claude_model: contextvars.ContextVar[str | None] = contextvars.ContextVar("claude_model", default=None)


def use_claude_model(model: str | None) -> None:
    _claude_model.set(model if model in config.CLAUDE_MODELS else None)


def claude_model() -> str:
    return _claude_model.get() or config.CLAUDE_MODEL


def claude_model_name(model: str | None = None) -> str:
    m = model or claude_model()
    return config.CLAUDE_MODELS.get(m, {}).get("name", m)


def label(engine: str) -> str:
    return f"Claude {claude_model_name()}" if engine == "claude" else f"Local ({config.OLLAMA_MODEL})"


# ---------- calls ----------

def _get_client():
    global _client
    if _client is None:
        from anthropic import Anthropic
        _client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# Current Claude models think before answering, and that thinking counts toward max_tokens.
# A tight limit cuts the answer off part-way, so give every request generous room
# (only what's used is billed). The per-feature numbers still apply to the local model.
CLAUDE_MAX_TOKENS = 16000
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _ask_claude(system: str, user: str, max_tokens: int) -> str:
    import anthropic
    model = claude_model()
    args = dict(model=model, max_tokens=max(max_tokens, CLAUDE_MAX_TOKENS), system=system,
                messages=[{"role": "user", "content": user}])
    try:
        try:
            # If the model's safety check declines, Anthropic retries on a suitable model in the same call.
            resp = _get_client().messages.create(**args, extra_headers={"anthropic-beta": FALLBACK_BETA},
                                                 extra_body={"fallbacks": "default"})
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            log.info("server-side fallback not accepted for %s; sending without it", model)
            resp = _get_client().messages.create(**args)
    except Exception as e:  # network, auth, rate limit…
        raise AIError(f"Claude request failed: {e.__class__.__name__}: {e}") from e
    stop = getattr(resp, "stop_reason", None)
    usage = getattr(resp, "usage", None)
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
    log.info("Claude %s: stopped=%s, tokens in=%s out=%s, %d chars of text", getattr(resp, "model", model), stop,
             getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"), len(text))
    if stop == "max_tokens":
        raise AIError("Claude's answer was cut off before it finished. Try again.")
    if stop == "refusal":
        raise AIError("Claude declined this request. Try again, or use the local model.")
    return text


LOCAL_MIN_PREDICT = 8192


def _ask_local(system: str, user: str, max_tokens: int, json_mode: bool) -> str:
    body = {
        "model": config.OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,  # qwen3 and other reasoning models: answer directly
        "keep_alive": "5m",
        # Generous room: a reasoning model that ignores think=False spends some of it thinking.
        "options": {"num_ctx": config.OLLAMA_NUM_CTX, "num_predict": max(max_tokens, LOCAL_MIN_PREDICT), "temperature": 0.4},
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
        data = r.json()
        text = (data.get("message") or {}).get("content", "")
        log.info("local model %s: stopped=%s, tokens in=%s out=%s, %d chars", config.OLLAMA_MODEL, data.get("done_reason"),
                 data.get("prompt_eval_count", "?"), data.get("eval_count", "?"), len(text))
        if data.get("done_reason") == "length":
            raise AIError("The local model's answer was cut off before it finished. Try again, or use Claude.")
    except AIError:
        raise
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
job. Write a specific cover letter of 250–350 words as clean plain text — no Markdown, no
asterisks, no headings, no bullet points.

Use exactly this shape, with a blank line between each part:

Dear <the hiring manager's name if the posting gives one, otherwise "Hiring Manager">,

<paragraph 1: why this role at this company, opening with something specific to them>

<paragraph 2: the most relevant experience from the resume, with concrete results>

<paragraph 3: more relevant experience or skills that match the posting's requirements>

<paragraph 4: a short close saying you'd welcome a conversation>

Kind regards,
<the candidate's full name>

Rules:
- Always start with the "Dear ...," line and always end with "Kind regards," and the candidate's name.
- Never open with "I am writing to express my interest".
- Reference concrete details from the posting (team, product, requirements).
- Use only real experience from the resume; never fabricate.
- No placeholders such as [Company] or [Your Name]: use the real names given.
- Plain, confident, warm tone.
- Output ONLY the letter, from "Dear" to the name. No notes before or after it."""

CLOSINGS = re.compile(r"^(kind regards|best regards|warm regards|regards|sincerely|yours sincerely|yours faithfully|"
                      r"best wishes|best|many thanks|thank you|thanks|warmly|cheers|respectfully)\b[ ,.!]*$", re.I)


def candidate_name(resume: str) -> str:
    """The name at the top of a resume: a "# Name" heading, else a short first line."""
    for line in resume.splitlines():
        s = line.strip().lstrip("#").strip().strip("*").strip()
        if not s:
            continue
        words = s.split()
        if (len(s) <= 60 and "@" not in s and not re.search(r"\d", s) and 2 <= len(words) <= 6
                and words[0][:1].isupper() and words[-1][:1].isupper()):
            return s
        return ""
    return ""


def letter_unfinished(text: str) -> bool:
    """True when a letter's last paragraph stops mid-sentence (the model ran out of room),
    ignoring a sign-off and name at the end."""
    lines = [l.strip() for l in re.sub(r"\*\*(.*?)\*\*", r"\1", text).splitlines() if l.strip()]
    while lines and (CLOSINGS.match(lines[-1]) or (len(lines[-1]) <= 60 and not re.search(r"[.!?:;]$", lines[-1])
                                                   and len(lines) >= 2 and CLOSINGS.match(lines[-2]))):
        lines.pop()  # the sign-off line, or the name under it
    if not lines:
        return True
    return not re.search(r"""[.!?…)"'”’]$""", lines[-1])


def finish_letter(text: str, name: str = "") -> str:
    """Make sure a cover letter reads as a letter, whatever the model returned:
    plain text, a greeting at the top, and a sign-off with the name at the end."""
    t = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", text.strip())
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)                  # no Markdown bold
    t = re.sub(r"^#+\s*", "", t, flags=re.M)                  # no Markdown headings
    lines = t.splitlines()
    # drop chatter before the letter ("Here is your cover letter:")
    start = next((i for i, l in enumerate(lines) if re.match(r"^\s*(dear|hi|hello|to whom)\b", l, re.I)), None)
    if start is not None and start <= 3:
        lines = lines[start:]
    elif lines and re.match(r"^\s*(here('| i)s|sure|certainly|below)\b.*:\s*$", lines[0], re.I):
        lines = lines[1:]
    t = "\n".join(lines).strip()
    if not re.match(r"^(dear|hi|hello|to whom)\b", t, re.I):
        t = "Dear Hiring Manager,\n\n" + t
    tail = [l.strip() for l in t.splitlines() if l.strip()][-3:]
    closing_at = next((i for i, l in enumerate(tail) if CLOSINGS.match(l)), None)
    if closing_at is None:
        t += "\n\nKind regards," + (f"\n{name}" if name else "")
    elif closing_at == len(tail) - 1 and name:  # sign-off but no name under it
        t += f"\n{name}"
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def tailor_resume(resume: str, job, extra: str = "", engine: str | None = None) -> str:
    if not resume.strip():
        raise AIError("Add your base resume in Settings first.")
    from .resume_format import base_roles, missing_roles, sort_experience
    roles = base_roles(resume)
    user = f"BASE RESUME:\n{resume}\n\nTARGET JOB:\n{_job_block(job)}"
    if roles:
        # A checklist helps smaller local models, which otherwise sometimes drop a role.
        user += ("\n\nROLES IN THE BASE RESUME. Every one must appear under Professional Experience, "
                 "most recent first, with its company, title and dates:\n" + "\n".join(f"- {r}" for r in roles))
    if extra.strip():
        user += f"\n\nEXTRA INSTRUCTIONS FROM THE CANDIDATE:\n{extra}"

    def ask(prompt: str) -> str:
        out = _ask(RESUME_SYSTEM, prompt, max_tokens=4000, engine=engine)
        return re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", out.strip())

    out = ask(user)
    lost = missing_roles(roles, out)
    if lost:
        log.warning("tailored resume left out %d role(s): %s; asking again", len(lost), "; ".join(lost))
        retry = ask(user + "\n\nIMPORTANT: a previous draft left out these roles. Include every one of them, "
                    "with its company, title and dates, in date order:\n" + "\n".join(f"- {r}" for r in lost))
        still = missing_roles(roles, retry)
        if len(still) < len(lost):
            out = retry
        if still:
            log.warning("tailored resume still leaves out: %s", "; ".join(still))
    return sort_experience(out)


def cover_letter(resume: str, job, extra: str = "", engine: str | None = None) -> str:
    if not resume.strip():
        raise AIError("Add your base resume in Settings first.")
    name = candidate_name(resume)
    company = (job.get("company") if isinstance(job, dict) else getattr(job, "company", "")) or ""
    user = (f"CANDIDATE'S NAME: {name or '(take it from the top of the resume)'}\n"
            f"COMPANY: {company or '(see the job below)'}\n\nRESUME:\n{resume}\n\nTARGET JOB:\n{_job_block(job)}")
    if extra.strip():
        user += f"\n\nEXTRA INSTRUCTIONS FROM THE CANDIDATE:\n{extra}"
    out = _ask(COVER_SYSTEM, user, max_tokens=1500, engine=engine)
    if letter_unfinished(out):
        log.warning("cover letter came back unfinished (%d chars, ends %r); asking again", len(out), out[-60:])
        out = _ask(COVER_SYSTEM, user + "\n\nIMPORTANT: a previous answer stopped in the middle of a sentence. "
                   "Write the complete letter, ending with the closing paragraph, \"Kind regards,\" and the name.",
                   max_tokens=1500, engine=engine)
        if letter_unfinished(out):
            raise AIError("The cover letter came back unfinished twice, so it wasn't saved. Try again, or pick another model.")
    return finish_letter(out, name)
