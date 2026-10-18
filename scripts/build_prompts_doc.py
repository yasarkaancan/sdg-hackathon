"""Regenerate docs/PROMPTS.md from prompts/*.md so the documented text never drifts.

Usage: python scripts/build_prompts_doc.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INTRO = """# Prompt design

_Owner: Umut Öztürk (prompt engineering lead) · Team 10_

All prompts live in [`/prompts`](../prompts) as plain markdown files with a `## SYSTEM PROMPT` and a
`## USER PROMPT TEMPLATE` section. `app/prompts.py` loads them and fills `{placeholders}` in a single pass
(so user text containing `{cv_text}` cannot pull other content into the prompt). This document is generated
from those files by `scripts/build_prompts_doc.py`, so the text below is exactly what the model receives.

## Overview

| # | Prompt | Technique | Temp. | Output |
|---|---|---|---|---|
| 1 | Job Posting Parser | **Zero-shot**, strict schema | 0.2 | structured requirements |
| 2 | ATS & Keyword Screener | **Zero-shot** + explicit rubric | 0.4 | coverage table, missing keywords |
| 3 | HR Recruiter | **Few-shot** (contrastive vague vs. specific) | 0.4 | first-impression issues |
| 4 | Hiring Manager | **Chain-of-thought** (`analysis` field first) | 0.4 | evidence-based fit |
| 5 | Impact & Writing Coach | **Few-shot** (before/after, incl. Turkish) | 0.4 | bullet rewrites |
| 6 | Aggregator | **Chain-of-thought** (`reasoning` field first) + ranking rules | 0.2 | top 5 actions |

## Design principles shared by every prompt

1. **Role / persona** — each reviewer has a distinct hiring perspective, so the four reports disagree in useful ways
   instead of repeating each other.
2. **Exact JSON schema in the prompt** + DeepSeek JSON mode (`response_format: json_object`; the word "json" appears in
   both system and user messages, which JSON mode requires). Output is validated with pydantic; on failure we send
   one repair request.
3. **Grounding rule** — every `evidence` / `original` must be a *verbatim* quote from the CV. The prompt tells the model
   that software checks each quote; `app/grounding.py` actually does, and the UI badges anything unverified.
4. **No invention** — never invent facts, numbers, tools or achievements; unknown metrics become `[placeholders]`.
5. **Specific to THIS posting** — every issue must name a requirement, section or keyword.
6. **Prompt-injection defence** — user content is wrapped in `<job_posting>`, `<cv>`, `<job_requirements_json>`,
   `<reviews>` tags, and both the system prompt and a closing reminder in the user message say that content inside
   the tags is data, never instructions. (Putting the reminder *after* the untrusted content matters: it is the last
   thing the model reads.)
7. **Fairness instruction** in every reviewer — evaluate only job-relevant qualifications; ignore name, gender, age,
   nationality, religion, marital/military status, photo and university prestige. Combined with code-level redaction
   (`app/preprocess.py`), the model usually never sees these attributes at all.
8. **Redaction awareness** — `[REDACTED: …]` markers are explained so reviewers do not flag "missing phone number".
9. **Tone rule** — the users are first-time job seekers; feedback must be constructive, specific, never discouraging.
10. **Output language** — `{output_language}` (English or Turkish, chosen in the UI). JSON keys and enum values always
    stay in English so the code can parse them.
11. **Score anchors** — every reviewer gets a 0–100 rubric with anchor points (e.g. "70 = most must-haves met or
    partial") to reduce run-to-run variance. The *overall* score is never produced by the LLM; it is computed in
    `app/scoring.py`.
12. **Temperatures** — 0.2 for extraction and synthesis (we want determinism), 0.4 for reviewers (some variety in
    phrasing, still stable scores). Repair calls use 0.0.

Each section below states the prompt's purpose, the technique and why it fits, a design-iteration note, and the
full prompt text. Iteration notes record only changes we have evidence for; the rest are TODOs for the prompt owner.
"""

SECTIONS = [
    {
        "file": "job_parser",
        "title": "1. Job Posting Parser",
        "purpose": "Convert a free-text posting (EN or TR, often messy) into a structured list of must-have / nice-to-have "
                   "requirements with ATS keywords. All four reviewers receive this JSON, so they judge the CV against "
                   "the same interpretation of the posting, and the ATS coverage table has a stable row set.",
        "technique": "**Zero-shot with a strict schema.** Extraction is a well-defined task the model already does well; "
                     "examples would mostly bias it toward the example's domain (a data-analyst example makes a marketing "
                     "posting sound technical). Instead we give precise classification rules (what counts as must vs. "
                     "nice, including Turkish cue words like *tercih sebebi*, *aranan nitelikler*) and a low temperature.",
        "iteration": "> TODO (Umut Öztürk, prompt engineering lead): describe what you tried first and why you changed it",
    },
    {
        "file": "ats_screener",
        "title": "2. Reviewer — ATS & Keyword Screener",
        "purpose": "Simulate an Applicant Tracking System plus junior screener: check every requirement against the CV, "
                   "list missing keywords and flag ATS-hostile formatting. Produces the requirement-coverage table and "
                   "the must-have coverage metric.",
        "technique": "**Zero-shot with an explicit rubric.** The task is mechanical classification, so consistency matters "
                     "more than creativity. A crisp three-level rubric (met = explicit evidence + usage, partial = weaker or "
                     "related evidence, missing = none) plus score anchors makes the output reproducible; few-shot examples "
                     "would add tokens without adding precision.",
        "iteration": "> TODO (Umut Öztürk, prompt engineering lead): describe what you tried first and why you changed it",
    },
    {
        "file": "hr_recruiter",
        "title": "3. Reviewer — HR Recruiter (6-second scan)",
        "purpose": "Judge the CV as a document: first impression, structure, length, clarity, red flags (gaps, typos, "
                   "generic objective, inconsistent dates).",
        "technique": "**Few-shot with contrastive examples (vague vs. specific).** The hard part of this task is not *what* "
                     "to check but *how specific* the feedback is. Zero-shot recruiter feedback tends to be generic "
                     "(\"improve formatting\"). Showing a VAGUE and a SPECIFIC version of the same finding teaches the target "
                     "granularity: quote the text, explain why it hurts for this job, give a fix doable in minutes.",
        "iteration": "While writing the ethics assessment we noticed that the prompt listed \"unexplained gaps\" as a red flag without guidance on how to treat them. We added: *\"suggest a short, honest one-line explanation — a gap is never a disqualifier on its own\"*.\n\n"
                     "> TODO (Umut Öztürk, prompt engineering lead): describe what you tried first for the few-shot examples and why you changed it",
    },
    {
        "file": "hiring_manager",
        "title": "4. Reviewer — Hiring Manager / Domain Expert",
        "purpose": "Decide whether the CV proves the candidate can do *this* job at *this* level: depth, relevance and impact "
                   "of experience and projects, quantified results, seniority fit.",
        "technique": "**Chain-of-thought, structured and bounded.** Judging fit requires weighing several requirements against "
                     "scattered evidence — a multi-step reasoning task where CoT clearly helps. The reasoning goes into an "
                     "`analysis` field placed *first* in the JSON, so it is generated before `score` and `issues` and the "
                     "verdict is conditioned on it. Four named steps (evidence per must-have → depth → seniority → decide) "
                     "keep it focused; the UI shows it in a collapsible \"Reasoning\" box for transparency.",
        "iteration": "> TODO (Umut Öztürk, prompt engineering lead): describe what you tried first and why you changed it",
    },
    {
        "file": "impact_coach",
        "title": "5. Reviewer — Impact & Writing Coach",
        "purpose": "Rewrite the 3–5 weakest job-relevant bullets as *action verb + what + measurable result*.",
        "technique": "**Few-shot (before/after examples, one in Turkish) + a hard no-fabrication constraint.** Rewriting is a "
                     "style-transfer task, where examples are the most efficient way to specify the target pattern. The "
                     "examples also demonstrate the *placeholder behaviour* (`[X%]`, `[N kişi]`), which instructions alone "
                     "convey poorly. The Turkish example keeps Turkish CVs from receiving English or translated-sounding rewrites.",
        "iteration": "> TODO (Umut Öztürk, prompt engineering lead): describe what you tried first and why you changed it",
    },
    {
        "file": "aggregator",
        "title": "6. Aggregator / Synthesis",
        "purpose": "Merge the four reviewer reports into one plan: at most 5 actions ranked by impact on getting an interview, "
                   "with who raised them, effort level, conflicts and their resolution, and what to keep doing.",
        "technique": "**Chain-of-thought (agreement → conflicts → impact ranking) with explicit ranking rules.** Synthesis "
                     "requires comparing four reports, detecting duplicates phrased differently and resolving "
                     "contradictions — classic multi-step reasoning. The `reasoning` field comes first and is visible in "
                     "the UI (\"How the board decided\").",
        "iteration": "> TODO (Umut Öztürk, prompt engineering lead): describe what you tried first and why you changed it",
    },
]


def main() -> None:
    out = [INTRO]
    for s in SECTIONS:
        text = (ROOT / "prompts" / f"{s['file']}.md").read_text(encoding="utf-8")
        body = text.split("## SYSTEM PROMPT", 1)[1]
        system, user = body.split("## USER PROMPT TEMPLATE", 1)
        out += [
            f"---\n\n## {s['title']}",
            f"**File:** [`prompts/{s['file']}.md`](../prompts/{s['file']}.md)\n",
            f"**Purpose.** {s['purpose']}\n",
            f"**Technique & why.** {s['technique']}\n",
            f"**Design iteration — what we tried first and why we changed it.**\n\n{s['iteration']}\n",
            "<details><summary><b>Full system prompt</b></summary>\n\n````text\n" + system.strip() + "\n````\n</details>\n",
            "<details><summary><b>User prompt template</b></summary>\n\n````text\n" + user.strip() + "\n````\n</details>\n",
        ]
    (ROOT / "docs" / "PROMPTS.md").write_text("\n".join(out), encoding="utf-8")
    print("Wrote docs/PROMPTS.md")


if __name__ == "__main__":
    main()
