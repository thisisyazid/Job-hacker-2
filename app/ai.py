"""Everything that talks to an AI model. Two engines:

  claude  Anthropic API (ANTHROPIC_API_KEY)
  local   Ollama, e.g. qwen3:14b; its address and model are set in Settings
          (OLLAMA_URL and OLLAMA_MODEL in .env are the defaults)

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


# ---------- local model settings ----------
# Set in the app (Settings → Local model) and stored in the database; .env values are the defaults.
_local_cfg: dict | None = None


def _load_local_cfg() -> dict:
    global _local_cfg
    if _local_cfg is None:
        cfg = {}
        try:
            from .db import AppSetting, SessionLocal
            with SessionLocal() as s:
                for row in s.query(AppSetting).filter(AppSetting.key.in_(("ollama_url", "ollama_model", "local_kind"))):
                    cfg[row.key] = row.value or ""
        except Exception:  # table not there yet (first start): use the defaults, look again next time
            return cfg
        _local_cfg = cfg
    return _local_cfg


def ollama_url() -> str:
    return (_load_local_cfg().get("ollama_url") or config.OLLAMA_URL or "").strip()


def ollama_model() -> str:
    """The saved local model (Settings); see local_model() for the one a request uses."""
    return (_load_local_cfg().get("ollama_model") or config.OLLAMA_MODEL or "").strip()


LOCAL_KINDS = ("ollama", "openai")  # openai = any server with the OpenAI chat API: LM Studio, llama.cpp, Jan, vLLM


def local_kind() -> str:
    k = _load_local_cfg().get("local_kind") or config.LOCAL_KIND
    return k if k in LOCAL_KINDS else "ollama"


def _openai_base(url: str) -> str:
    u = url.rstrip("/")
    return u if u.endswith("/v1") else u + "/v1"


# The local model for the current request or overnight run: the model picker in the app sends
# X-Local-Model; falls back to the model saved in Settings.
_local_model: contextvars.ContextVar[str | None] = contextvars.ContextVar("local_model", default=None)


def use_local_model(model: str | None) -> None:
    _local_model.set((model or "").strip()[:200] or None)


def local_model() -> str:
    return _local_model.get() or ollama_model()


def save_local_settings(url: str, model: str, kind: str = "") -> None:
    """Empty values go back to the .env defaults."""
    global _local_cfg
    from .db import AppSetting, SessionLocal
    kind = kind if kind in LOCAL_KINDS else ""
    with SessionLocal() as s:
        for key, val in (("ollama_url", url.strip().rstrip("/")), ("ollama_model", model.strip()), ("local_kind", kind)):
            row = s.get(AppSetting, key)
            if val:
                if row:
                    row.value = val
                else:
                    s.add(AppSetting(key=key, value=val))
            elif row:
                s.delete(row)
        s.commit()
    _local_cfg = None
    _local_cache.update(at=0.0, status=None)


def ollama_models(url: str | None = None, kind: str | None = None) -> list[str]:
    """Models installed on the local AI server at url (or the saved address): Ollama's list, or
    an OpenAI-compatible server's /v1/models (LM Studio, llama.cpp, Jan, vLLM)."""
    base, kind = (url or ollama_url()).rstrip("/"), (kind or local_kind())
    if kind == "openai":
        r = httpx.get(_openai_base(base) + "/models", timeout=httpx.Timeout(3.0))
        r.raise_for_status()
        return sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
    r = httpx.get(base + "/api/tags", timeout=httpx.Timeout(3.0))
    r.raise_for_status()
    return sorted(m.get("name", "") for m in r.json().get("models", []) if m.get("name"))


def _has_model(names, want: str) -> bool:
    return want in names or f"{want}:latest" in names or any(n.split(":")[0] == want for n in names)


# ---------- availability ----------

def local_status(force: bool = False) -> dict:
    """{'ok': bool, 'model': str, 'reason': str}. Cached for a minute so page
    loads don't wait on Ollama."""
    if not ollama_url():
        return {"ok": False, "model": ollama_model(), "reason": "No local model address is set. Add it in Settings."}
    if not force and _local_cache["status"] and time.time() - _local_cache["at"] < 60:
        return _local_cache["status"]
    st = {"ok": False, "model": ollama_model(), "reason": "", "models": []}
    server = "Ollama" if local_kind() == "ollama" else "the local AI server"
    try:
        names = ollama_models()
        st["models"] = names
        want = ollama_model()
        if _has_model(names, want):
            st["ok"] = True
        elif local_kind() == "openai" and names and not want:
            st.update(ok=True, model=names[0])
        elif local_kind() == "ollama":
            st["reason"] = f"Ollama is running but doesn't have {want}. Run: ollama pull {want}"
        else:
            st["reason"] = f"The local AI server is running but doesn't offer {want or 'any model'}. Load it there, or pick one in Settings."
    except Exception as e:
        st["reason"] = f"Couldn't reach {server} at {ollama_url()} ({e.__class__.__name__})."
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
        "local": {"available": loc["ok"], "label": "Local", "model": loc.get("model") or ollama_model(), "url": ollama_url(),
                  "kind": local_kind(), "models": loc.get("models", []), "reason": loc["reason"]},
        "default": default_engine(),
    }


def resolve(engine: str | None) -> str:
    """Pick the engine for a request, with a clear error if it can't be used."""
    if engine in (None, "", "default"):
        e = default_engine()
        if not e:
            raise AIError("AI features are off. Add ANTHROPIC_API_KEY to your .env, or set up a local model in Settings.")
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
    return f"Claude {claude_model_name()}" if engine == "claude" else f"Local ({local_model()})"


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


def _ask_openai_compatible(system: str, user: str, max_tokens: int) -> str:
    """LM Studio, llama.cpp's server, Jan, vLLM and others that speak the OpenAI chat API."""
    model = local_model() or (local_status().get("model") or "")
    body = {"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max(max_tokens, LOCAL_MIN_PREDICT), "temperature": 0.4, "stream": False}
    timeout = httpx.Timeout(config.OLLAMA_TIMEOUT, connect=5.0)
    try:
        r = httpx.post(_openai_base(ollama_url()) + "/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        log.info("local model %s (OpenAI-compatible): stopped=%s, tokens in=%s out=%s, %d chars", model,
                 choice.get("finish_reason"), usage.get("prompt_tokens", "?"), usage.get("completion_tokens", "?"), len(text))
        if choice.get("finish_reason") == "length":
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


def _ask_local(system: str, user: str, max_tokens: int, json_mode: bool) -> str:
    if local_kind() == "openai":
        return _ask_openai_compatible(system, user, max_tokens)
    body = {
        "model": local_model(),
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "stream": False,
        "think": False,  # qwen3 and other reasoning models: answer directly
        "keep_alive": "5m",
        # Generous room: a reasoning model that ignores think=False spends some of it thinking.
        "options": {"num_ctx": config.OLLAMA_NUM_CTX, "num_predict": max(max_tokens, LOCAL_MIN_PREDICT), "temperature": 0.4},
    }
    if json_mode:
        body["format"] = "json"
    url = ollama_url().rstrip("/") + "/api/chat"
    timeout = httpx.Timeout(config.OLLAMA_TIMEOUT, connect=5.0)
    try:
        r = httpx.post(url, json=body, timeout=timeout)
        if r.status_code == 400 and "think" in r.text.lower():  # older Ollama without the think option
            body.pop("think")
            r = httpx.post(url, json=body, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        text = (data.get("message") or {}).get("content", "")
        log.info("local model %s: stopped=%s, tokens in=%s out=%s, %d chars", local_model(), data.get("done_reason"),
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
Count independent or side projects in the resume as real evidence of the skills they show.
Return ONLY JSON:
{"score": <int 1-10>,
 "verdict": "<one plain sentence, max 25 words>",
 "strengths": ["<max 4 short points, each tied to real resume evidence>"],
 "gaps": ["<max 4 short points: missing requirements or concerns>"],
 "keywords": ["<10-12 must-have hard skills, tools, methods, certifications or domain terms, in the posting's exact wording, most important first>"]}"""


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
        raise AIError("The AI didn't return a score. Try again.")
    for k in ("strengths", "gaps", "keywords"):
        d[k] = [str(x) for x in (d.get(k) or [])][:12 if k == "keywords" else 8]
    d["verdict"] = str(d.get("verdict") or "")
    return d


# ---------- documents ----------

RESUME_SYSTEM = """You are an expert resume writer. You get a candidate's base resume and a target
job. Produce a tailored resume that passes applicant tracking systems (ATS) and reads well to a
recruiter skimming it for seven seconds. Use the exact Markdown structure below — nothing else.

# Full Name
email | phone | city | website
Headline 1 | Headline 2 | Headline 3

## Professional Summary
One or two short paragraphs written for this role (two only if the base summary has two).

## Core Capabilities
- **Group name:** item, item, item

## Independent Projects
One line introducing the projects, if the base resume has one.
### Project Name
**Short description of the project**
- What was built or achieved
- **Tech:** item, item

## Professional Experience
### Company | Month YYYY – Month YYYY
**Job Title — Team or Department**
- Achievement bullet
- Achievement bullet

## Certifications & Education
- **Certifications:** ...
- **Education:** ...

## Tools & Technology
Comma-separated list, or "- **Group:** item, item" lines if the base resume groups its tools.

Before writing, work out (without writing it down) the posting's exact job title and its 8–12 most
important requirements: hard skills, tools, methods, certifications and domain terms, in the
posting's own words. Then:

Truth (these override everything else):
- Never invent employers, titles, dates, certifications, tools, skills or numbers. A keyword goes in
  only where the base resume shows that experience; skip requirements it doesn't support.
- ROLE ORDER IS FIXED: list every role in reverse chronological order (most recent first), with the
  same companies, titles and dates as the base resume. Never move a role up or down because it is
  more relevant, and never drop a role, including short, fixed-term or contract roles and the most
  recent one.
- DEPTH: a tailored resume is as substantial as the base resume, not a summary of it. For every role
  that ended in the last ten years, keep as many bullets as the base resume has for it (at least
  three where it has three or more), rewritten for this job. Keep every number and result in the base
  resume (percentages, counts, money, time saved). Only roles that ended more than ten years ago may
  drop to one or two bullets.
- KEEP EVERY SECTION of the base resume. Sections beyond the ones above (projects, independent or
  side projects, portfolio, volunteering, publications, awards, languages and the like) stay, with
  their own heading; the "Independent Projects" block above shows the format. Keep every project.
  If the job is mainly about building software or products, keep projects where the base resume has
  them, in full. Otherwise put Independent Projects after Professional Experience and keep each project
  to its name, the short description and one bullet, so the work experience leads.

Keywords (for the ATS):
- Use the posting's exact wording for each requirement the candidate meets ("stakeholder
  management", not a synonym). Write a term in full with its acronym once, e.g. "Certified
  ScrumMaster (CSM)", "Business Process Model and Notation (BPMN)".
- Core Capabilities: five or six groups, each named in your own words (never copy the posting's
  section headings as group names), each item backed by the resume.
- Put the matched hard skills in Core Capabilities, and also show each important one in use in a
  bullet. Use a key term two or three times across the resume at most; never list keywords on
  their own or repeat them to pad.

Top of the page (for the recruiter's first look):
- Headline 1 is the posting's exact job title if the candidate has held that role or done the same
  work; otherwise their closest real title. The other headlines come from the base resume's
  headlines and real job titles. Never add seniority (Senior, Lead, Head of) the base resume doesn't
  claim.
- The summary's first sentence names that title, years of experience and two or three of the
  posting's must-have skills the candidate has. If the base summary mentions the candidate's own
  projects or building work, keep a sentence about it.

Bullets:
- Within a role, put the bullet most relevant to this job first. You may reword and trim bullets.
- Start each with a plain past-tense action verb (present tense for a current "Present" role).
- Where the base resume gives a result, show it as result, then number, then how ("Cut ticket
  processing time 40% by redesigning the intake workflow"). Keep every number exactly as given.
- No stock phrases: results-driven, detail-oriented, dynamic, passionate, proven track record,
  team player, self-starter, go-getter, synergy, spearheaded, leveraged, utilized, thought leader,
  "responsible for". Plain, specific language a person would write.

Format:
- Use only the markers shown: #, ##, ###, "- " bullets and **bold**. No tables, no columns, no
  horizontal rules, no decorative symbols (❖ ▪ • ★ etc.) — remove any from the base resume.
- Standard section headings as shown. Contact and headline lines separate items with " | ".
- Keep it to about two pages; cut the least relevant bullets first, never whole sections.
- Leave out link labels such as "Download for Windows" or "Source on GitHub"; keep website addresses.
- Output ONLY the resume."""

COVER_SYSTEM = """You are an expert cover letter writer. You get a candidate's resume and a target
job. Write a specific cover letter of 250–350 words as clean plain text — no Markdown, no
asterisks, no headings, no bullet points.

Use exactly this shape, with a blank line between each part:

Dear <the hiring manager's name if the posting gives one, otherwise "Hiring Manager">,

<paragraph 1: why this role at this company, opening with something specific to them>

<paragraph 2: the most relevant experience from the resume, with concrete results>

<paragraph 3: more relevant experience or skills that match the posting's requirements; where it
fits the role, the candidate's own projects (software or products they built) as evidence>

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


def fit_hints(job) -> dict:
    """Keywords and gaps from the job's saved fit check, if it has one."""
    fit = job.get("fit") if isinstance(job, dict) else None
    if fit is None:
        raw = job.get("fit_json") if isinstance(job, dict) else getattr(job, "fit_json", "")
        try:
            fit = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            fit = None
    if not isinstance(fit, dict):
        return {}
    clean = lambda xs: [str(x).strip() for x in (xs or []) if str(x).strip()][:12]
    return {"keywords": clean(fit.get("keywords")), "gaps": clean(fit.get("gaps"))[:4]}


def tailor_resume(resume: str, job, extra: str = "", engine: str | None = None) -> str:
    if not resume.strip():
        raise AIError("Add your base resume in Settings first.")
    from .resume_format import (base_roles, extra_sections, missing_numbers, missing_roles, missing_sections,
                                sort_experience, too_thin)
    roles = base_roles(resume)
    extras = extra_sections(resume)
    user = f"BASE RESUME:\n{resume}\n\nTARGET JOB:\n{_job_block(job)}"
    if roles:
        # A checklist helps smaller local models, which otherwise sometimes drop a role.
        user += ("\n\nROLES IN THE BASE RESUME. Every one must appear under Professional Experience, "
                 "most recent first, with its company, title and dates:\n" + "\n".join(f"- {r}" for r in roles))
    hints = fit_hints(job)
    if hints.get("keywords"):
        user += ("\n\nKEYWORDS FROM THE POSTING (found by the fit check): " + ", ".join(hints["keywords"])
                 + ". Use the posting's exact wording for each one the base resume supports; skip the rest.")
    if hints.get("gaps"):
        user += ("\n\nGAPS THE FIT CHECK FOUND:\n" + "\n".join(f"- {g}" for g in hints["gaps"])
                 + "\nWhere the base resume has related experience, bring it forward; never claim what it doesn't show.")
    if extras:
        user += ("\n\nOTHER SECTIONS IN THE BASE RESUME. Keep each one, with its heading, in the same place:\n"
                 + "\n".join(f"- {s}" for s in extras))
    if extra.strip():
        user += f"\n\nEXTRA INSTRUCTIONS FROM THE CANDIDATE:\n{extra}"

    def ask(prompt: str) -> str:
        out = _ask(RESUME_SYSTEM, prompt, max_tokens=4000, engine=engine)
        return re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", out.strip())

    def issues(text: str) -> list[str]:
        """What a draft lost from the base resume, as lines for a repair request."""
        out_issues = []
        gone = missing_sections(extras, text)
        if gone:
            out_issues.append("It left out these sections of the base resume. Include each one, with its heading "
                              "and all its items:\n" + "\n".join(f"- {x}" for x in gone))
        lost = missing_roles(roles, text)
        if lost:
            out_issues.append("It left out these roles. Include every one of them, with its company, title and "
                              "dates, in date order:\n" + "\n".join(f"- {r}" for r in lost))
        if too_thin(resume, text):
            out_issues.append("It is much shorter than the base resume. Keep the base resume's bullets for every "
                              "recent role, rewritten for this job, rather than summarising them.")
        nums = missing_numbers(resume, text)
        if len(nums) >= 2 or (nums and out_issues):  # one dropped number alone isn't worth a retry
            out_issues.append("It dropped these results from the base resume. Put each back in the bullet it "
                              "belongs to: " + ", ".join(nums))
        return out_issues

    out = ask(user)
    found = issues(out)
    if found:
        log.warning("tailored resume needs repair (%d issue(s)): %s; asking again", len(found),
                    " | ".join(f.split(".")[0] for f in found))
        retry = ask(user + "\n\nIMPORTANT: a previous draft had problems.\n" + "\n".join(f"- {f}" for f in found))
        again = issues(retry)
        if len(again) < len(found) and not missing_roles(roles, retry):
            out = retry
        if again:
            log.warning("tailored resume still has: %s", " | ".join(f.split(".")[0] for f in again))
    return sort_experience(out)


LINKEDIN_SYSTEM = """You are a LinkedIn profile coach who also knows how recruiters search LinkedIn.
You get the candidate's resume, their current LinkedIn headline and About section, and the roles they
are targeting. Recruiters find people by searching job titles and skills, and the headline is the
most searched and most seen line, so it must carry the target titles and key skills.

Write plain text with exactly these headings, each on its own line:

HEADLINE OPTIONS
Three headlines of at most 220 characters each, one per line starting with "- ". Lead with the target
job titles the candidate can truthfully claim, then two or three searchable skills or domains, then
one concrete proof point (a result, sector or certification) from the resume.

ABOUT SECTION
A rewritten About section of 150–250 words in first person, short paragraphs: who they are and the
roles they want, two or three concrete results from the resume with their numbers, what they're
known for, and a last line inviting people to get in touch. Work the target titles and key skills in
naturally.

SKILLS TO LIST
Up to 15 skills, one per line starting with "- ", in the words recruiters search for, each backed by
the resume. Most important first (LinkedIn shows the top few).

QUICK FIXES
Three to five specific changes to make, one per line starting with "- ", e.g. a stronger first line,
a missing keyword, the "Open to work" job titles to set, a featured project to add.

Rules: use only facts from the resume and current profile; never invent titles, employers, numbers or
skills. No stock phrases (results-driven, passionate, dynamic, guru, ninja, proven track record).
No Markdown symbols other than "- " bullets. Output only the four sections."""


def linkedin_help(resume: str, profile: str, target: str, preferences: str = "", engine: str | None = None) -> str:
    if not resume.strip() and not profile.strip():
        raise AIError("Add your base resume in Settings, or paste your current LinkedIn profile text.")
    user = (f"TARGET ROLES: {target.strip() or '(not given: infer them from the resume and preferences)'}\n\n"
            f"WHAT THE CANDIDATE WANTS:\n{preferences.strip() or '(not specified)'}\n\n"
            f"CURRENT LINKEDIN HEADLINE AND ABOUT:\n{profile.strip() or '(not provided)'}\n\n"
            f"RESUME:\n{resume.strip() or '(not provided)'}")
    out = _ask(LINKEDIN_SYSTEM, user, max_tokens=2500, engine=engine)
    return re.sub(r"\*\*(.+?)\*\*", r"\1", out).strip()


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
