"""Orchestration: preprocess → job parse → 4 parallel reviewers → grounding →
aggregate → deterministic score.

``run_review`` is an async generator of small JSON-serialisable event dicts;
main.py streams them to the browser as newline-delimited JSON so the UI can
show live progress.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import AsyncIterator

from . import grounding, preprocess, prompts, scoring
from .llm import LLMClient, LLMError
from .schemas import (
    AgentResult,
    AggregateResult,
    ATSResult,
    CoachResult,
    HiringManagerResult,
    JobParsed,
    RecruiterResult,
)

log = logging.getLogger("cvboard.pipeline")

TEMP_PARSER = 0.2
TEMP_REVIEWER = 0.4
TEMP_AGGREGATOR = 0.2


@dataclass(frozen=True)
class AgentSpec:
    key: str          # internal id, also the mock fixture name
    name: str         # display name, used in "raised_by"
    prompt: str       # prompts/<prompt>.md
    model: type[AgentResult]


AGENTS: list[AgentSpec] = [
    AgentSpec("ats", "ATS", "ats_screener", ATSResult),
    AgentSpec("recruiter", "Recruiter", "hr_recruiter", RecruiterResult),
    AgentSpec("hiring_manager", "Hiring Manager", "hiring_manager", HiringManagerResult),
    AgentSpec("coach", "Coach", "impact_coach", CoachResult),
]

TURKISH_CHARS = set("çğışöüÇĞİŞÖÜ")


def _mock_set(cv_text: str) -> str:
    """Pick the Turkish or English fixture set in mock mode."""
    return "tr" if sum(c in TURKISH_CHARS for c in cv_text) >= 15 else "en"


def _aggregator_view(spec: AgentSpec, result: AgentResult) -> dict:
    """Compact, quote-free view of a reviewer report for the aggregator.

    Leaving the raw quotes out saves tokens and shrinks the prompt-injection
    surface; the ``verified`` flags tell the aggregator which findings are grounded.
    """
    view: dict = {
        "reviewer": spec.name,
        "score": result.score,
        "verdict": result.verdict,
        "strengths": [{"point": s.point, "verified": s.verified} for s in result.strengths],
        "issues": [
            {"severity": i.severity, "section": i.section, "problem": i.problem, "fix": i.fix,
             "verified": i.verified}
            for i in result.issues
        ],
    }
    if isinstance(result, ATSResult):
        view["requirements_not_met"] = [
            {"requirement": rc.requirement, "priority": rc.priority, "status": rc.status}
            for rc in result.requirement_coverage if rc.status != "met"
        ]
        view["missing_keywords"] = result.missing_keywords
    if isinstance(result, CoachResult):
        view["bullets_rewritten"] = len(result.rewrites)
    return view


async def run_review(
    *,
    cv_text: str = "",
    cv_file: tuple[str, bytes] | None = None,
    job_text: str = "",
    output_language: str = "en",
) -> AsyncIterator[dict]:
    lang = output_language if output_language in prompts.LANGUAGE_NAMES else "en"
    language_name = prompts.LANGUAGE_NAMES[lang]
    started = time.perf_counter()

    # ---------------- Step 0: preprocess (local, no LLM) ----------------
    yield {"type": "stage", "stage": "preprocess"}
    try:
        if cv_file is not None and cv_file[1]:
            cv_text = preprocess.extract_text(cv_file[0], cv_file[1])
        prepared = preprocess.prepare(cv_text, job_text, lang)
    except preprocess.InputError as err:
        yield {"type": "error", "message": preprocess.error_message(err, lang), "code": err.code}
        return
    yield {"type": "redactions", "items": prepared.redactions, "cv_redacted": prepared.cv_redacted}

    async with LLMClient() as llm:
        mock_set = _mock_set(prepared.cv_redacted)

        # ---------------- Step 1: parse the job posting ----------------
        yield {"type": "stage", "stage": "parse_job"}
        system, user = prompts.render("job_parser", output_language=language_name, job_text=prepared.job_text)
        try:
            job = await llm.chat_json(name="job_parser", system=system, user=user, model_cls=JobParsed,
                                      temperature=TEMP_PARSER, mock_set=mock_set)
        except LLMError as err:
            if "API key" in str(err) or "balance" in str(err):
                yield {"type": "error", "message": str(err)}
                return
            log.warning("job parser failed, continuing with raw posting only: %s", err)
            job = JobParsed()
            yield {"type": "warning", "message": "job_parse_failed"}
        yield {"type": "job_parsed", "job": job.model_dump()}

        # ---------------- Step 2: four reviewers in parallel ----------------
        yield {"type": "stage", "stage": "agents"}
        job_json = job.model_dump_json(indent=2)
        seniority_hint = job.seniority if job.seniority != "unknown" else "not stated — infer from the posting"

        async def run_agent(spec: AgentSpec):
            system, user = prompts.render(
                spec.prompt, output_language=language_name, job_json=job_json, job_text=prepared.job_text,
                cv_text=prepared.cv_redacted, seniority_hint=seniority_hint,
            )
            try:
                result = await llm.chat_json(name=spec.key, system=system, user=user, model_cls=spec.model,
                                             temperature=TEMP_REVIEWER, mock_set=mock_set)
                result.agent = spec.name
                counts = grounding.ground_agent(result, prepared.cv_redacted)  # Step 3, per agent
                return spec, result, counts, None
            except LLMError as err:
                return spec, None, None, str(err)
            except Exception as err:  # never let one agent kill the whole board
                log.error("agent %s crashed: %s", spec.key, type(err).__name__)  # type only: never log CV content
                return spec, None, None, f"{spec.name}: internal error ({type(err).__name__})"

        results: dict[str, AgentResult] = {}
        errors: dict[str, str] = {}
        counts: list[dict] = []
        tasks = [asyncio.create_task(run_agent(spec)) for spec in AGENTS]
        for next_done in asyncio.as_completed(tasks):
            spec, result, count, error = await next_done
            if result is None:
                errors[spec.key] = error or "unknown error"
                yield {"type": "agent_done", "agent": spec.key, "ok": False, "error": errors[spec.key]}
                continue
            results[spec.key] = result
            counts.append(count)
            yield {"type": "agent_done", "agent": spec.key, "ok": True, "result": result.model_dump(),
                   "verification": grounding.verification_summary([count])}

        if not results:
            first_error = next(iter(errors.values()), "")
            yield {"type": "error", "message": f"All reviewers failed. {first_error}"}
            return

        verification = grounding.verification_summary(counts)

        # ---------------- Step 4: aggregate ----------------
        yield {"type": "stage", "stage": "aggregate"}
        reviews = [_aggregator_view(spec, results[spec.key]) for spec in AGENTS if spec.key in results]
        system, user = prompts.render(
            "aggregator", output_language=language_name, job_title=job.job_title,
            agents_json=json.dumps(reviews, ensure_ascii=False, indent=1),
        )
        aggregate, aggregate_error = None, None
        try:
            aggregate = await llm.chat_json(name="aggregator", system=system, user=user, model_cls=AggregateResult,
                                            temperature=TEMP_AGGREGATOR, mock_set=mock_set)
            allowed = {spec.name for spec in AGENTS}
            for action in aggregate.top_actions:  # keep "raised_by" to known reviewer names
                action.raised_by = [r for r in action.raised_by if r in allowed]
        except LLMError as err:
            aggregate_error = str(err)

        # ---------------- Step 5: deterministic score ----------------
        scores = scoring.compute(results, results.get("ats"))  # type: ignore[arg-type]

        log.info("review finished in %.1fs (agents ok=%d, failed=%d, mock=%s)",
                 time.perf_counter() - started, len(results), len(errors), llm.mock)
        yield {
            "type": "final",
            "result": {
                "job": job.model_dump(),
                "agents": {k: v.model_dump() for k, v in results.items()},
                "agent_errors": errors,
                "aggregate": aggregate.model_dump() if aggregate else None,
                "aggregate_error": aggregate_error,
                "scores": scores,
                "verification": verification,
                "redactions": prepared.redactions,
                "output_language": lang,
                "mock": llm.mock,
                "elapsed_s": round(time.perf_counter() - started, 1),
            },
        }
