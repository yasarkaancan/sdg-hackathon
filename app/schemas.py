"""Pydantic models for every LLM output.

The models are intentionally *lenient on input* (they normalise casing, clamp
scores, coerce numbers) and *strict on output* (the rest of the app can rely on
the shape). Fields such as ``verified`` are filled in by our own code
(grounding.py), never by the model.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["high", "medium", "low"]
Effort = Literal["low", "medium", "high"]


def _norm_choice(value, allowed: tuple[str, ...], default: str, aliases: dict | None = None):
    """Lower-case a free-text enum value and map unknown values to a default."""
    v = str(value or "").strip().lower()
    if aliases and v in aliases:
        v = aliases[v]
    return v if v in allowed else default


def _to_score(value) -> int:
    try:
        return max(0, min(100, round(float(value))))
    except (TypeError, ValueError):
        return 0


def _to_str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in value if str(v).strip()]


# --------------------------------------------------------------------------- #
# Step 1: job posting parser
# --------------------------------------------------------------------------- #
class Requirement(BaseModel):
    requirement: str
    keywords: list[str] = Field(default_factory=list)

    _kw = field_validator("keywords", mode="before")(_to_str_list)


class JobParsed(BaseModel):
    job_title: str = "Unknown role"
    seniority: Literal["intern", "junior", "mid", "senior", "unknown"] = "unknown"
    must_have: list[Requirement] = Field(default_factory=list)
    nice_to_have: list[Requirement] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    language_requirements: list[str] = Field(default_factory=list)

    @field_validator("seniority", mode="before")
    @classmethod
    def _sen(cls, v):
        return _norm_choice(
            v,
            ("intern", "junior", "mid", "senior", "unknown"),
            "unknown",
            {"internship": "intern", "entry": "junior", "entry-level": "junior", "entry level": "junior",
             "mid-level": "mid", "medior": "mid", "stajyer": "intern"},
        )

    _lists = field_validator("responsibilities", "soft_skills", "language_requirements", mode="before")(
        _to_str_list
    )


# --------------------------------------------------------------------------- #
# Step 2: reviewer agents (common schema + per-agent extras)
# --------------------------------------------------------------------------- #
class Strength(BaseModel):
    point: str
    evidence: str = ""
    verified: bool | None = None  # set by grounding.py


class Issue(BaseModel):
    severity: Severity = "medium"
    section: str = ""
    evidence: str = ""
    problem: str
    fix: str = ""
    verified: bool | None = None  # set by grounding.py; None = nothing to verify (missing item)

    @field_validator("severity", mode="before")
    @classmethod
    def _sev(cls, v):
        return _norm_choice(v, ("high", "medium", "low"), "medium",
                            {"critical": "high", "major": "high", "minor": "low", "info": "low"})

    @field_validator("evidence", "section", "fix", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return "" if v is None else v


class AgentResult(BaseModel):
    agent: str = ""
    score: int = 0
    verdict: str = ""
    strengths: list[Strength] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)

    _score = field_validator("score", mode="before")(_to_score)


class RequirementCoverage(BaseModel):
    requirement: str
    priority: Literal["must", "nice"] = "must"
    status: Literal["met", "partial", "missing"] = "missing"
    evidence: str = ""
    verified: bool | None = None

    @field_validator("priority", mode="before")
    @classmethod
    def _prio(cls, v):
        return _norm_choice(v, ("must", "nice"), "must", {"must_have": "must", "must-have": "must",
                                                          "nice_to_have": "nice", "nice-to-have": "nice"})

    @field_validator("status", mode="before")
    @classmethod
    def _status(cls, v):
        return _norm_choice(v, ("met", "partial", "missing"), "missing", {"partially met": "partial",
                                                                           "not met": "missing"})

    @field_validator("evidence", mode="before")
    @classmethod
    def _none_to_empty(cls, v):
        return "" if v is None else v


class ATSResult(AgentResult):
    requirement_coverage: list[RequirementCoverage] = Field(default_factory=list)
    missing_keywords: list[str] = Field(default_factory=list)

    _kw = field_validator("missing_keywords", mode="before")(_to_str_list)


class RecruiterResult(AgentResult):
    pass


class HiringManagerResult(AgentResult):
    analysis: str = ""


class Rewrite(BaseModel):
    original: str
    improved: str
    why: str = ""
    verified: bool | None = None          # is `original` really in the CV?
    new_numbers: list[str] = Field(default_factory=list)  # numbers in `improved` that are NOT in the CV


class CoachResult(AgentResult):
    rewrites: list[Rewrite] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Step 4: aggregator
# --------------------------------------------------------------------------- #
class TopAction(BaseModel):
    priority: int = 0
    action: str
    why: str = ""
    raised_by: list[str] = Field(default_factory=list)
    effort: Effort = "medium"

    _rb = field_validator("raised_by", mode="before")(_to_str_list)

    @field_validator("effort", mode="before")
    @classmethod
    def _eff(cls, v):
        return _norm_choice(v, ("low", "medium", "high"), "medium")

    @field_validator("priority", mode="before")
    @classmethod
    def _prio(cls, v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0


class Conflict(BaseModel):
    topic: str
    resolution: str = ""


class AggregateResult(BaseModel):
    reasoning: str = ""
    overall_summary: str = ""
    fit_assessment: Literal["strong", "moderate", "weak"] = "moderate"
    top_actions: list[TopAction] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    keep_doing: list[str] = Field(default_factory=list)

    _kd = field_validator("keep_doing", mode="before")(_to_str_list)

    @field_validator("fit_assessment", mode="before")
    @classmethod
    def _fit(cls, v):
        return _norm_choice(v, ("strong", "moderate", "weak"), "moderate")

    @field_validator("top_actions")
    @classmethod
    def _max_five(cls, v: list[TopAction]):
        v = sorted(v, key=lambda a: a.priority if a.priority > 0 else 99)[:5]
        for i, action in enumerate(v, start=1):
            action.priority = i
        return v
