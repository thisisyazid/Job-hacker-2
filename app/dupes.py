"""Spot the same job saved twice from different sites (say LinkedIn and Indeed):
same company and a near-identical title. Links can't catch these because they differ.
The browser has a copy of these rules in static/app.js (normCompany, titleTokens,
similarTitles); keep the two in step."""
from __future__ import annotations

import re

COMPANY_DROP = {"pte", "ltd", "limited", "private", "inc", "incorporated", "llc", "plc", "co", "corp",
                "corporation", "company", "group", "holdings", "singapore", "sg", "the"}
TITLE_SYNONYMS = {"sr": "senior", "snr": "senior", "jr": "junior", "mgr": "manager", "eng": "engineer",
                  "dev": "developer", "assoc": "associate", "asst": "assistant", "mgmt": "management",
                  "ops": "operations", "pm": "product manager", "ba": "business analyst"}
TITLE_DROP = {"the", "a", "an", "of", "for", "and", "to", "in", "at", "with", "on"}


def norm_company(s: str | None) -> str:
    s = re.sub(r"\([^)]*\)", " ", (s or "").lower()).replace("&", " and ")
    return " ".join(w for w in re.split(r"[^a-z0-9]+", s) if w and w not in COMPANY_DROP)


def title_tokens(s: str | None) -> set[str]:
    words = re.split(r"[^a-z0-9]+", (s or "").lower().replace("&", " and "))
    out: set[str] = set()
    for w in words:
        if not w:
            continue
        for part in TITLE_SYNONYMS.get(w, w).split():
            if part not in TITLE_DROP:
                out.add(part)
    return out


def similar_titles(a: str | None, b: str | None) -> bool:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.8


def same_job(a_title: str | None, a_company: str | None, b_title: str | None, b_company: str | None) -> bool:
    ca = norm_company(a_company)
    return bool(ca) and ca == norm_company(b_company) and similar_titles(a_title, b_title)
