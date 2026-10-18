"""Step 3 — no LLM: verify that quoted evidence really exists in the CV.

Every ``evidence`` (strengths, issues, requirement coverage) and every rewrite
``original`` must be a verbatim quote from the *redacted* CV that the model saw.
We normalise case / whitespace / quote characters, try an exact substring
match, then fall back to rapidfuzz ``partial_ratio >= 90`` to tolerate tiny
differences (PDF line breaks, a dropped comma). Unverified items are kept and
flagged — never silently dropped — so the user can judge them.

We also run a cheap fabrication check on the Coach's rewrites: any number in
the improved bullet that is *not* in the CV and not inside a [placeholder] is
reported as ``new_numbers``.
"""

from __future__ import annotations

import re
import unicodedata

from rapidfuzz import fuzz

from .schemas import AgentResult, ATSResult, CoachResult

FUZZY_THRESHOLD = 90

_QUOTES = str.maketrans({"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "•": " ", "…": "..."})
PLACEHOLDER_RE = re.compile(r"\[[^\]]+\]")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_QUOTES)
    text = text.replace("İ", "i").replace("I", "i").replace("ı", "i").lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip(" .,;:-*\"'")


def is_grounded(quote: str, cv_norm: str) -> bool | None:
    """True/False for a non-empty quote; None when there is nothing to check."""
    q = normalise(quote)
    if not q:
        return None
    if q in cv_norm:
        return True
    # Models sometimes join two adjacent bullets with "..." — accept if every part is present.
    parts = [p for p in (normalise(x) for x in re.split(r"\.\.\.|…", quote)) if p]
    if len(parts) > 1 and all(p in cv_norm or fuzz.partial_ratio(p, cv_norm) >= FUZZY_THRESHOLD for p in parts):
        return True
    return fuzz.partial_ratio(q, cv_norm) >= FUZZY_THRESHOLD


def numbers_not_in_cv(improved: str, cv_norm: str) -> list[str]:
    without_placeholders = PLACEHOLDER_RE.sub(" ", improved or "")
    cv_numbers = set(NUMBER_RE.findall(cv_norm))
    found = []
    for num in NUMBER_RE.findall(without_placeholders):
        if num not in cv_numbers and num not in found:
            found.append(num)
    return found


def ground_agent(result: AgentResult, cv_redacted: str) -> dict:
    """Mutates ``result`` in place (sets ``verified`` flags). Returns counts."""
    cv_norm = normalise(cv_redacted)
    checked = verified = 0

    def mark(item, attr: str = "evidence") -> None:
        nonlocal checked, verified
        item.verified = is_grounded(getattr(item, attr), cv_norm)
        if item.verified is not None:
            checked += 1
            verified += int(item.verified)

    for s in result.strengths:
        mark(s)
    for i in result.issues:
        mark(i)
    if isinstance(result, ATSResult):
        for rc in result.requirement_coverage:
            mark(rc)
    if isinstance(result, CoachResult):
        for rw in result.rewrites:
            mark(rw, "original")
            rw.new_numbers = numbers_not_in_cv(rw.improved, cv_norm)
    return {"checked": checked, "verified": verified}


def verification_summary(counts: list[dict]) -> dict:
    checked = sum(c["checked"] for c in counts)
    verified = sum(c["verified"] for c in counts)
    return {
        "checked": checked,
        "verified": verified,
        "rate": round(verified / checked, 3) if checked else None,
    }
