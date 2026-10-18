"""Step 5 — deterministic scoring (computed in code, NOT by the LLM).

overall = 0.35 * Hiring Manager + 0.30 * ATS + 0.20 * Recruiter + 0.15 * Coach

If a reviewer failed, its weight is redistributed proportionally over the
reviewers that succeeded (and the UI says so). The score is a heuristic for
"how well does this CV present you for THIS posting", not a prediction of a
hiring outcome.
"""

from __future__ import annotations

from .schemas import ATSResult

WEIGHTS = {"hiring_manager": 0.35, "ats": 0.30, "recruiter": 0.20, "coach": 0.15}


def overall_score(agent_scores: dict[str, int]) -> dict:
    available = {k: w for k, w in WEIGHTS.items() if k in agent_scores}
    total_weight = sum(available.values())
    if not total_weight:
        return {"overall": None, "weights_used": {}, "missing_agents": list(WEIGHTS)}
    weights_used = {k: round(w / total_weight, 4) for k, w in available.items()}
    overall = sum(agent_scores[k] * w for k, w in weights_used.items())
    return {
        "overall": round(overall),
        "weights_used": weights_used,
        "missing_agents": [k for k in WEIGHTS if k not in agent_scores],
    }


def must_have_coverage(ats: ATSResult | None) -> dict:
    if ats is None:
        return {"coverage": None, "met": 0, "partial": 0, "missing": 0, "total": 0}
    musts = [rc for rc in ats.requirement_coverage if rc.priority == "must"]
    met = sum(rc.status == "met" for rc in musts)
    partial = sum(rc.status == "partial" for rc in musts)
    missing = sum(rc.status == "missing" for rc in musts)
    total = len(musts)
    return {
        "coverage": round((met + 0.5 * partial) / total, 3) if total else None,
        "met": met,
        "partial": partial,
        "missing": missing,
        "total": total,
    }


def compute(agent_results: dict, ats: ATSResult | None) -> dict:
    scores = {k: r.score for k, r in agent_results.items() if r is not None}
    result = overall_score(scores)
    result["agent_scores"] = scores
    result["must_have"] = must_have_coverage(ats)
    result["formula"] = "0.35·HiringManager + 0.30·ATS + 0.20·Recruiter + 0.15·Coach"
    return result
