"""Regenerate docs/PROMPTS.md from prompts/*.md so the documented text never drifts.

Usage: python scripts/build_prompts_doc.py
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

INTRO = """# Prompt design

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

Each section below states the prompt's purpose, the technique and why it fits, the design iteration that led to
the current version (the failure mode each rule addresses), and the full prompt text.
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
        "iteration": "A first, minimal version (\"extract the requirements as json\") put almost everything into "
                     "`must_have` and kept compound requirements like \"Python and SQL\" as one item, which made coverage "
                     "scoring coarse. Turkish \"tercih sebebi\" items were sometimes treated as mandatory. We added the "
                     "explicit must/nice cue words, the split-compound rule and the ~12-word limit. Because Turkish "
                     "postings still sometimes contain discriminatory filters (\"erkek aday\", \"askerliğini yapmış\", "
                     "age limits), rule 8 explicitly forbids extracting personal attributes as requirements, so they can "
                     "never become a scoring criterion downstream.",
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
        "iteration": "Without a rubric, the same skill was \"met\" in one run and \"partial\" in the next (e.g. a skill only "
                     "listed in the Skills section). The model also sometimes quoted the *job posting* as evidence, which "
                     "inflates coverage. We added the rubric with the concrete \"listed but never used = partial\" rule, "
                     "\"never quote the job posting as CV evidence\", the requirement to return one row per requirement in "
                     "order, and the honesty rule for missing keywords (add only if you genuinely have the skill) to avoid "
                     "teaching keyword stuffing.",
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
        "iteration": "The zero-shot version produced findings like \"The summary could be better\" with no quote and no "
                     "actionable fix — useless for a first-time job seeker. Adding examples fixed the specificity, but the "
                     "first examples were from a data-analyst CV and the model started mentioning SQL in reviews of "
                     "marketing CVs. We moved the examples to a different, fictional domain and added \"never copy their "
                     "content\". We also added \"never recommend adding a photo or personal details\", because a "
                     "Türkiye-centric recruiter persona would otherwise sometimes suggest a photo (still common locally).",
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
        "iteration": "Without CoT, scores and issues were often inconsistent (a score of 78 alongside two high-severity "
                     "\"core requirement missing\" issues). Free-form \"think step by step\" fixed consistency but produced "
                     "long reasoning, occasionally outside the JSON, which broke parsing. Putting reasoning inside a JSON "
                     "field, first in key order, with a ~150-word cap and fixed steps solved both. We also added the "
                     "\"CV fails to show\" vs. \"candidate lacks\" distinction, so fixes point to surfacing real evidence "
                     "rather than implying the candidate is unqualified.",
    },
    {
        "file": "impact_coach",
        "title": "5. Reviewer — Impact & Writing Coach",
        "purpose": "Rewrite the 3–5 weakest job-relevant bullets as *action verb + what + measurable result*.",
        "technique": "**Few-shot (before/after examples, one in Turkish) + a hard no-fabrication constraint.** Rewriting is a "
                     "style-transfer task, where examples are the most efficient way to specify the target pattern. The "
                     "examples also demonstrate the *placeholder behaviour* (`[X%]`, `[N kişi]`), which instructions alone "
                     "convey poorly. The Turkish example keeps Turkish CVs from receiving English or translated-sounding rewrites.",
        "iteration": "This was the riskiest prompt. Early rewrites invented convincing metrics (\"increased engagement by "
                     "35%\") — exactly the fabrication that could hurt a young candidate in an interview. A \"do not invent\" "
                     "instruction alone reduced but did not remove it, because the model treats a number as part of the "
                     "\"good bullet\" pattern. What worked was combining (1) the HARD RULE section, (2) examples where every "
                     "unknown metric is a placeholder, (3) a `why` that tells the user to fill it in only if true, and "
                     "(4) a code-level check (`grounding.numbers_not_in_cv`) that flags any number not present in the CV. "
                     "The UI highlights placeholders and shows a warning above the rewrites.",
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
        "iteration": "The first version received the CV as well, and it added new observations that no reviewer had made "
                     "(and that were not grounded). It also tended to concatenate all issues (10+ items). We removed the "
                     "CV from its input entirely — it only sees a compact, quote-free view of the reviews with our "
                     "`verified` flags — so it structurally cannot add claims about the CV. We added the 5-item cap, the "
                     "multi-reviewer-ranks-higher rule, the fixed reviewer names for `raised_by` (also enforced in code) "
                     "and the rule to distrust findings that rest only on an unverified quote.",
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
            f"**Design iteration — what we tried first and why we changed it.** {s['iteration']}\n",
            "<details><summary><b>Full system prompt</b></summary>\n\n````text\n" + system.strip() + "\n````\n</details>\n",
            "<details><summary><b>User prompt template</b></summary>\n\n````text\n" + user.strip() + "\n````\n</details>\n",
        ]
    (ROOT / "docs" / "PROMPTS.md").write_text("\n".join(out), encoding="utf-8")
    print("Wrote docs/PROMPTS.md")


if __name__ == "__main__":
    main()
