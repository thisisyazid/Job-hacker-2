"""Turn a job URL into a draft job.

Order of attempts:
  1. Site-specific APIs that don't need a login (MyCareersFuture, LinkedIn guest view).
  2. schema.org JobPosting JSON-LD embedded in the page (Greenhouse, Lever,
     Workday, SmartRecruiters, most company career pages, JobStreet, Indeed...).
  3. Page text + Claude extraction (if an API key is configured).
If all fail, the caller shows the "paste it yourself" form.
"""
from __future__ import annotations

import html as htmllib
import json
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from . import ai

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-SG,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
TIMEOUT = httpx.Timeout(15.0, connect=8.0)

TRACKING_PARAMS = re.compile(r"^(utm_.*|ref|refId|trackingId|trk|trkInfo|src|source|gclid|fbclid|lipi|eBP|origin)$", re.I)


class ScrapeError(Exception):
    """Raised with a message that's safe to show to the user."""


# ---------- URL helpers ----------

_URLISH = re.compile(r"(^\s*(https?://|www\.)|://|[?&](trackingId|refId|eBP|currentJobId)=|^[\w.-]+\.(com|sg|net|org|io|co)/\S*$)", re.I)


def looks_like_url(text: str | None) -> bool:
    """True when a 'title' or 'company' is really a link. Job boards (LinkedIn in
    particular) leave the bare link behind when a posting has been taken down,
    so a job titled like this is dead and not worth keeping."""
    return bool(text) and bool(_URLISH.search(text.strip()))


def linkedin_job_id(url: str) -> str | None:
    p = urlparse(url)
    if "linkedin." not in p.netloc:
        return None
    qs = parse_qs(p.query)
    if "currentJobId" in qs:
        return qs["currentJobId"][0]
    m = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})", p.path)
    return m.group(1) if m else None


def normalize_url(url: str) -> str:
    """Canonical form used for duplicate detection."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    li = linkedin_job_id(url)
    if li:
        return f"https://www.linkedin.com/jobs/view/{li}/"
    p = urlparse(url)
    q = [(k, v) for k, v in parse_qs(p.query, keep_blank_values=True).items() if not TRACKING_PARAMS.match(k)]
    query = urlencode([(k, x) for k, vs in q for x in vs])
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path or "/", "", query, ""))


def link_key(url: str) -> str:
    """Looser key for "is this the same posting": ignores http/https, www., a trailing
    slash and letter case in the address; LinkedIn by job ID, Indeed by its jk job key
    (the same Indeed job shows up under /viewjob, /rc/clk, /m/viewjob and country sites)."""
    u = normalize_url(url)
    if not u:
        return ""
    li = linkedin_job_id(u)
    if li:
        return f"linkedin:{li}"
    p = urlparse(u)
    host = p.netloc.lower().removeprefix("www.")
    qs = parse_qs(p.query)
    if "indeed." in host:
        jk = (qs.get("jk") or qs.get("vjk") or [""])[0]
        if jk:
            return f"indeed:{jk.lower()}"
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    fragment = htmllib.unescape(fragment) if "&lt;" in fragment else fragment
    soup = BeautifulSoup(fragment, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for block in soup.find_all(["p", "h1", "h2", "h3", "h4", "ul", "ol"]):
        block.insert_after("\n\n")
    for block in soup.find_all("div"):
        block.insert_after("\n")
    text = soup.get_text()
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _draft(**kw) -> dict:
    d = {k: "" for k in ("title", "company", "location", "salary", "employment_type", "posted_date", "description")}
    d.update({k: (v if k == "description" else _clean(v)) for k, v in kw.items() if v})
    return d


# ---------- site-specific ----------

def _mycareersfuture(client: httpx.Client, url: str) -> dict | None:
    if "mycareersfuture.gov.sg" not in url:
        return None
    m = re.search(r"([0-9a-f]{32})", url)
    if not m:
        return None
    r = client.get(f"https://api.mycareersfuture.gov.sg/v2/jobs/{m.group(1)}")
    if r.status_code != 200:
        return None
    j = r.json()
    sal = j.get("salary") or {}
    salary = ""
    if sal.get("minimum") or sal.get("maximum"):
        per = ((sal.get("type") or {}).get("salaryType") or "Monthly").lower()
        salary = f"S${sal.get('minimum', 0):,} – S${sal.get('maximum', 0):,} {per}"
    addr = j.get("address") or {}
    loc_bits = [addr.get("street"), addr.get("postalCode")]
    location = ", ".join(b for b in loc_bits if b) or "Singapore"
    if addr.get("isOverseas"):
        location = (addr.get("overseasCountry") or "Overseas")
    emp = ", ".join(e.get("employmentType", "") for e in (j.get("employmentTypes") or []))
    return _draft(
        title=j.get("title"),
        company=(j.get("postedCompany") or {}).get("name"),
        location=location,
        salary=salary,
        employment_type=emp,
        posted_date=((j.get("metadata") or {}).get("newPostingDate") or "")[:10],
        description=html_to_text(j.get("description", "")),
    )


def _linkedin(client: httpx.Client, url: str) -> dict | None:
    jid = linkedin_job_id(url)
    if not jid:
        return None
    r = client.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}")
    if r.status_code != 200 or len(r.text) < 500:
        return None
    soup = BeautifulSoup(r.text, "html.parser")

    def t(sel):
        el = soup.select_one(sel)
        return el.get_text(" ", strip=True) if el else ""

    desc_el = soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text")
    criteria = {}
    for li in soup.select(".description__job-criteria-item"):
        h = li.select_one("h3")
        s = li.select_one("span")
        if h and s:
            criteria[h.get_text(strip=True).lower()] = s.get_text(strip=True)
    title = t(".top-card-layout__title") or t("h2")
    if not title:
        return None
    return _draft(
        title=title,
        company=t(".topcard__org-name-link") or t(".topcard__flavor"),
        location=t(".topcard__flavor--bullet"),
        salary=t(".salary") or t(".compensation__salary"),
        employment_type=criteria.get("employment type", ""),
        posted_date=t(".posted-time-ago__text"),
        description=html_to_text(str(desc_el)) if desc_el else "",
    )


# ---------- generic ----------

def _walk_jsonld(node):
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                yield from _walk_jsonld(node[key])


def _is_jobposting(n: dict) -> bool:
    t = n.get("@type")
    return t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t)


def _fmt_location(loc) -> str:
    out = []
    for l in loc if isinstance(loc, list) else [loc]:
        if not isinstance(l, dict):
            if l:
                out.append(str(l))
            continue
        a = l.get("address") or {}
        if isinstance(a, str):
            out.append(a)
            continue
        country = a.get("addressCountry")
        if isinstance(country, dict):
            country = country.get("name")
        bits = [a.get("addressLocality"), a.get("addressRegion"), country]
        seen = []
        for b in bits:
            if b and b not in seen:
                seen.append(str(b))
        if seen:
            out.append(", ".join(seen))
    return "; ".join(dict.fromkeys(out))


def _fmt_salary(bs) -> str:
    if not isinstance(bs, dict):
        return _clean(bs) if bs else ""
    cur = bs.get("currency", "")
    v = bs.get("value")
    unit = ""
    if isinstance(v, dict):
        unit = (v.get("unitText") or "").lower()
        lo, hi, val = v.get("minValue"), v.get("maxValue"), v.get("value")
    else:
        lo = hi = None
        val = v

    def n(x):
        try:
            return f"{float(x):,.0f}"
        except (TypeError, ValueError):
            return str(x)

    if lo and hi:
        s = f"{cur} {n(lo)} – {n(hi)}"
    elif val or lo or hi:
        s = f"{cur} {n(val or lo or hi)}"
    else:
        return ""
    return (s + (f" per {unit}" if unit else "")).strip()


def from_jsonld(html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        try:
            data = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            # some sites put trailing commas / multiple objects; try a lenient pass
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()))
            except Exception:
                continue
        for n in _walk_jsonld(data):
            if not _is_jobposting(n):
                continue
            org = n.get("hiringOrganization") or {}
            company = org.get("name") if isinstance(org, dict) else org
            loc = _fmt_location(n.get("jobLocation") or [])
            if not loc and n.get("jobLocationType") == "TELECOMMUTE":
                loc = "Remote"
            emp = n.get("employmentType") or ""
            if isinstance(emp, list):
                emp = ", ".join(emp)
            d = _draft(
                title=n.get("title"),
                company=company,
                location=loc,
                salary=_fmt_salary(n.get("baseSalary") or n.get("estimatedSalary")),
                employment_type=str(emp).replace("_", " ").title(),
                posted_date=str(n.get("datePosted") or "")[:10],
                description=html_to_text(n.get("description") or ""),
            )
            if d["title"]:
                return d
    return None


def page_text(html: str) -> tuple[str, str]:
    """(title, visible text) for the LLM fallback."""
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        title = og["content"]
    elif soup.title:
        title = soup.title.get_text(strip=True)
    for el in soup(["script", "style", "noscript", "svg", "nav", "footer", "header", "form", "iframe"]):
        el.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return title, html_to_text(str(main))


def fetch_job(url: str, engine: str | None = None, allow_ai: bool = True) -> tuple[dict, str]:
    """Returns (draft, method). Raises ScrapeError when nothing usable was found.
    AI is only a last resort, and never used when allow_ai is False."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            for fn, name in ((_mycareersfuture, "mycareersfuture"), (_linkedin, "linkedin")):
                try:
                    d = fn(client, url)
                except (httpx.HTTPError, ValueError, KeyError):
                    d = None
                if d and d.get("title"):
                    return d, name

            r = client.get(url)
    except httpx.HTTPError as e:
        raise ScrapeError(f"Couldn't reach that page ({e.__class__.__name__}).")

    if r.status_code in (401, 403, 429, 999):
        raise ScrapeError("That site blocks automatic reading.")
    if r.status_code >= 400:
        raise ScrapeError(f"The page answered with an error ({r.status_code}).")

    html = r.text
    d = from_jsonld(html)
    if d and len(d.get("description", "")) > 200:
        return d, "structured data"

    title, text = page_text(html)
    if allow_ai and ai.enabled(engine) and len(text) > 400:
        try:
            ext = ai.extract_job(text[:20000], hint_title=title, engine=engine)
            if ext.get("title"):
                merged = _draft(**{k: ext.get(k) for k in ext})
                if d:  # keep structured fields, fill gaps from AI
                    for k, v in merged.items():
                        if not d.get(k):
                            d[k] = v
                    return d, "structured data + AI"
                return merged, "AI"
        except ai.AIError:
            pass

    if d:
        return d, "structured data"
    if "linkedin." in url:
        raise ScrapeError("LinkedIn wouldn't show this job without a login.")
    raise ScrapeError("Couldn't find the job details on that page — it may need a login or load them with JavaScript.")
