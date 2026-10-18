"""Baseline-vs-final prompt experiments (ablations) → docs/ABLATIONS.md.

For each prompt we build a "v0" baseline by removing exactly the technique the
final prompt relies on (rubric, few-shot examples, chain-of-thought field,
no-invention rule, classification rules) and keep everything else identical:
same model, temperature, JSON schema and inputs. Every metric is computed by
code, not judged by hand.

Usage (needs DEEPSEEK_API_KEY):  python scripts/ablations.py
"""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel, ConfigDict  # noqa: E402
from rapidfuzz import fuzz, process  # noqa: E402

from app import grounding, llm, preprocess, prompts  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.pipeline import AGENTS, _aggregator_view  # noqa: E402
from app.schemas import (  # noqa: E402
    AggregateResult, ATSResult, CoachResult, HiringManagerResult, JobParsed, RecruiterResult,
)

LANG = {"en": "English", "tr": "Turkish (Türkçe)"}
SEM = asyncio.Semaphore(8)
RAW: list[dict] = []  # every model output, saved to docs/ablations_raw.json

# A fictional posting with discriminatory filters that still appear in some Turkish job ads.
BIASED_POSTING = """Satış Temsilcisi — Kurgusal Ticaret A.Ş. (İstanbul)

Aranan Nitelikler:
- Erkek aday
- Askerliğini tamamlamış
- 28 yaşını geçmemiş
- Bekar
- Üniversitelerin işletme veya iktisat bölümlerinden mezun
- MS Excel bilgisi
- B sınıfı ehliyet
- İyi derecede İngilizce

Tercih Sebebi:
- CRM yazılımı deneyimi
- Saha satış deneyimi
"""


class Raw(BaseModel):
    model_config = ConfigDict(extra="allow")


# --------------------------------------------------------------------------- #
# Baseline (v0) prompt builders: remove one technique from the final prompt
# --------------------------------------------------------------------------- #
HEADING = re.compile(r"^[A-Z][A-Z &—()'-]{3,}")


def drop_section(text: str, prefix: str) -> str:
    out, skipping = [], False
    for line in text.split("\n"):
        if line.startswith(prefix):
            skipping = True
            continue
        if skipping and HEADING.match(line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def rep(text: str, old: str, new: str = "") -> str:
    assert old in text, f"marker not found: {old[:60]}"
    return text.replace(old, new)


def v0_parser(system, user):
    return "\n".join(l for l in system.split("\n") if not re.match(r"^[2-8]\. ", l)), user


def v0_ats(system, user):
    system = rep(system, "decide its status using this rubric:", "decide its status: met, partial or missing.")
    system = "\n".join(l for l in system.split("\n") if not re.match(r'^   - "(met|partial|missing)"', l))
    system = re.sub(r" Anchor: [^\n]*", "", system)
    return system, rep(user, "Apply the met/partial/missing rubric to every requirement, ", "Classify every requirement, ")


def v0_recruiter(system, user):
    return (drop_section(system, "WHAT GOOD FINDINGS LOOK LIKE"),
            rep(user, "write findings as specific as the SPECIFIC examples, "))


def v0_hiring_manager(system, user):
    system = drop_section(system, "HOW TO THINK")
    system = rep(system, '  "analysis": "your step 1–4 reasoning, max ~150 words",\n')
    system = rep(system, '(including "analysis") ')
    user = rep(user, 'Reason step by step in "analysis" first (max ~150 words), then score and list issues consistent '
                     'with that reasoning, quoting', "Score the CV and list issues, quoting")
    return system, user


def v0_coach(system, user):
    system = drop_section(drop_section(system, "HARD RULE"), "EXAMPLES")
    system = rep(system, "rewritten bullet with [placeholders] where data is unknown", "rewritten bullet")
    system = rep(system, " (Do not confuse them with your own [placeholders].)")
    return system, rep(user, ", never invent facts or numbers (use [placeholders])")


def v0_aggregator(system, user):
    system = drop_section(system, "HOW TO THINK")
    system = drop_section(system, "RANKING RULES")
    system = rep(system, '  "reasoning": "brief: where reviewers agree, where they conflict, how conflicts were resolved",\n')
    user = rep(user, "Reason first (agreement → conflicts → impact ranking), then return at most 5 top actions that come "
                     "only from the reviewers' findings.", "Return the top actions.")
    return system, user


# --------------------------------------------------------------------------- #
async def call(client: LLMClient, name: str, system: str, user: str, temperature: float) -> dict | None:
    async with SEM:
        try:
            out = await client.chat_json(name=f"abl:{name}", system=system, user=user, model_cls=Raw,
                                         temperature=temperature)
            RAW.append({"call": name, "output": out.model_dump()})
            return out.model_dump()
        except llm.LLMError as err:
            print(f"  ! {name}: {err}")
            return None


def load(sample: str):
    cv = (ROOT / "samples" / f"cv_{sample}.txt").read_text(encoding="utf-8")
    job = (ROOT / "samples" / f"job_{sample}.txt").read_text(encoding="utf-8")
    return preprocess.prepare(cv, job, sample)


def reviewer_prompt(name: str, prep, job: JobParsed, lang: str):
    return prompts.render(name, output_language=LANG[lang], job_json=job.model_dump_json(indent=2),
                          job_text=prep.job_text, cv_text=prep.cv_redacted,
                          seniority_hint=job.seniority if job.seniority != "unknown" else "not stated")


def words(s: str) -> int:
    return len((s or "").split())


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.mean(xs), 2) if xs else None


def pct(x):
    return "—" if x is None else f"{x * 100:.0f}%"


# --------------------------------------------------------------------------- #
async def main() -> None:
    cfg = llm.settings()
    if cfg["mock"]:
        sys.exit("Ablations need a real DEEPSEEK_API_KEY (mock mode would compare identical fixtures).")
    preps = {s: load(s) for s in ("en", "tr")}
    report: list[str] = []

    async with LLMClient() as client:
        # ---------- 1. Job parser (fixed job JSON for all reviewer experiments) ----------
        async def parse(variant, text, lang, run):
            s, u = prompts.render("job_parser", output_language=LANG[lang], job_text=text)
            if variant == "v0":
                s, u = v0_parser(s, u)
            return await call(client, f"parser-{variant}-{run}", s, u, 0.2)

        jobs_v1 = {s: JobParsed.model_validate(await parse("v1", preps[s].job_text, s, 0)) for s in ("en", "tr")}
        parser_runs = await asyncio.gather(*[
            parse(v, text, "tr", r) for v in ("v0", "v1") for text in (preps["tr"].job_text, BIASED_POSTING)
            for r in range(2)])

        def classify_tr(d):
            jp = JobParsed.model_validate(d)
            must = " ".join(r.requirement + " " + " ".join(r.keywords) for r in jp.must_have).lower()
            nice = " ".join(r.requirement + " " + " ".join(r.keywords) for r in jp.nice_to_have).lower()
            pats = [r"sertifika|blueprint|certif", r"e-?ticaret|e-?commerce", r"video|reels", r"\bseo\b|arama motoru"]
            return sum(bool(re.search(p, nice)) and not re.search(p, must) for p in pats)

        def attrs(d):
            jp = JobParsed.model_validate(d)
            reqs = [r.requirement + " " + " ".join(r.keywords) for r in jp.must_have + jp.nice_to_have]
            return sum(bool(re.search(r"erkek|askerl|yaş|bekar|\bmale\b|military|\bage\b|marital|gender|cinsiyet|medeni",
                                      x.lower())) for x in reqs)

        rows = []
        i = 0
        for v in ("v0", "v1"):
            tr_runs, biased_runs = parser_runs[i:i + 2], parser_runs[i + 2:i + 4]
            i += 4
            rows.append((v, [classify_tr(d) for d in tr_runs if d], [attrs(d) for d in biased_runs if d]))
        report += ["## 1. Job parser — zero-shot classification rules",
                   "**v0** drops rules 2–8 (must/nice cue words incl. *tercih sebebi*, splitting, keywords, seniority, "
                   "language rule and the ban on personal attributes).", "",
                   "| Variant | Optional items correctly in `nice_to_have` (TR posting, of 4) | Personal attributes extracted as requirements (biased posting) |",
                   "|---|---|---|"]
        for v, cls, att in rows:
            report.append(f"| {v} | {', '.join(map(str, cls))} (runs) | {', '.join(map(str, att))} (runs) |")
        report.append("")

        # ---------- 2-5. Reviewers ----------
        specs = {"ats": ("ats_screener", v0_ats, ["en"], 3), "recruiter": ("hr_recruiter", v0_recruiter, ["en", "tr"], 2),
                 "hiring_manager": ("hiring_manager", v0_hiring_manager, ["en"], 3),
                 "coach": ("impact_coach", v0_coach, ["en", "tr"], 2)}
        jobs_meta = []
        for key, (pname, v0fn, samples, runs) in specs.items():
            for s in samples:
                for v in ("v0", "v1"):
                    for r in range(runs):
                        jobs_meta.append((key, s, v, r, pname, v0fn))

        async def run_reviewer(key, s, v, r, pname, v0fn):
            sy, us = reviewer_prompt(pname, preps[s], jobs_v1[s], s)
            if v == "v0":
                sy, us = v0fn(sy, us)
            return await call(client, f"{key}-{s}-{v}-{r}", sy, us, 0.4)

        outs = await asyncio.gather(*[run_reviewer(*m) for m in jobs_meta])
        results: dict = {}
        for (key, s, v, r, _, _), d in zip(jobs_meta, outs):
            results.setdefault((key, v), []).append((s, r, d))

        # ATS: rubric compliance on listed-but-unused skills + run-to-run agreement
        job_en = jobs_v1["en"]
        reqs = [x.requirement for x in job_en.must_have + job_en.nice_to_have]
        sql_req = next((x for x in reqs if "sql" in x.lower()), None)
        bi_req = next((x for x in reqs if re.search(r"power bi|tableau|\bbi\b", x.lower())), None)

        def statuses(d):
            res = ATSResult.model_validate(d)
            m = {}
            for rc in res.requirement_coverage:
                hit = process.extractOne(rc.requirement, reqs, scorer=fuzz.token_set_ratio)
                if hit and hit[1] >= 60:
                    m[hit[0]] = rc.status
            return m, res.score

        report += ["## 2. ATS screener — explicit met / partial / missing rubric",
                   "**v0** keeps the task and schema but removes the rubric definitions and the score anchors. "
                   "Rubric test: in the EN sample, SQL and Power BI appear only in the skills list, which the rubric "
                   "defines as `partial`.", "",
                   "| Variant | SQL status per run | Power BI status per run | Requirements with identical status in all runs | Score per run |",
                   "|---|---|---|---|---|"]
        for v in ("v0", "v1"):
            st = [statuses(d) for _, _, d in results[("ats", v)] if d]
            agree = sum(len({m.get(q) for m, _ in st}) == 1 and st[0][0].get(q) is not None for q in reqs) / len(reqs)
            report.append(f"| {v} | {', '.join(m.get(sql_req, '—') for m, _ in st)} | "
                          f"{', '.join(m.get(bi_req, '—') for m, _ in st)} | {pct(agree)} | "
                          f"{', '.join(str(sc) for _, sc in st)} |")
        report.append("")

        # Recruiter: specificity
        def rec_metrics(s, d):
            res = RecruiterResult.model_validate(d)
            grounding.ground_agent(res, preps[s].cv_redacted)
            iss = res.issues
            with_ev = [i for i in iss if i.evidence]
            return {"n": len(iss), "quoted": len(with_ev) / len(iss) if iss else None,
                    "verified": sum(bool(i.verified) for i in with_ev) / len(with_ev) if with_ev else None,
                    "fix_words": mean([words(i.fix) for i in iss]),
                    "short": sum(words(i.fix) < 12 for i in iss) / len(iss) if iss else None}

        report += ["## 3. HR recruiter — few-shot (vague vs. specific) examples",
                   "**v0** removes the two VAGUE/SPECIFIC example pairs and the pattern sentence. "
                   "Specificity metrics, averaged over 2 runs × 2 CVs (EN, TR).", "",
                   "| Variant | Issues per review | Issues quoting the CV | Quotes verified | Avg words per fix | Fixes under 12 words |",
                   "|---|---|---|---|---|---|"]
        for v in ("v0", "v1"):
            ms = [rec_metrics(s, d) for s, _, d in results[("recruiter", v)] if d]
            report.append(f"| {v} | {mean([m['n'] for m in ms])} | {pct(mean([m['quoted'] for m in ms]))} | "
                          f"{pct(mean([m['verified'] for m in ms]))} | {mean([m['fix_words'] for m in ms])} | "
                          f"{pct(mean([m['short'] for m in ms]))} |")
        report.append("")

        # Hiring manager: stability
        report += ["## 4. Hiring manager — chain-of-thought `analysis` field",
                   "**v0** removes the HOW TO THINK steps and the `analysis` field; the model answers directly. "
                   "3 runs on the EN sample.", "",
                   "| Variant | Score per run | Score spread | High-severity issues per run |", "|---|---|---|---|"]
        for v in ("v0", "v1"):
            rs = [HiringManagerResult.model_validate(d) for _, _, d in results[("hiring_manager", v)] if d]
            sc = [x.score for x in rs]
            report.append(f"| {v} | {', '.join(map(str, sc))} | {max(sc) - min(sc) if sc else '—'} | "
                          f"{', '.join(str(sum(i.severity == 'high' for i in x.issues)) for x in rs)} |")
        report.append("")

        # Coach: fabrication
        def coach_metrics(s, d):
            res = CoachResult.model_validate(d)
            grounding.ground_agent(res, preps[s].cv_redacted)
            return {"rw": len(res.rewrites), "ph": sum(len(grounding.PLACEHOLDER_RE.findall(x.improved)) for x in res.rewrites),
                    "new": sum(len(x.new_numbers) for x in res.rewrites),
                    "new_rw": sum(bool(x.new_numbers) for x in res.rewrites),
                    "orig_ok": sum(bool(x.verified) for x in res.rewrites) / len(res.rewrites) if res.rewrites else None}

        report += ["## 5. Impact coach — no-invention rule + few-shot placeholder examples",
                   "**v0** removes the HARD RULE section and the before/after examples (and the placeholder hint in the "
                   "schema). Totals over 2 runs × 2 CVs (EN, TR); *invented numbers* = numbers in a rewrite that appear "
                   "nowhere in the CV and are not inside a `[placeholder]`.", "",
                   "| Variant | Rewrites | `[placeholders]` used | Invented numbers | Rewrites containing an invented number | `original` quotes verified |",
                   "|---|---|---|---|---|---|"]
        for v in ("v0", "v1"):
            ms = [coach_metrics(s, d) for s, _, d in results[("coach", v)] if d]
            report.append(f"| {v} | {sum(m['rw'] for m in ms)} | {sum(m['ph'] for m in ms)} | {sum(m['new'] for m in ms)} | "
                          f"{sum(m['new_rw'] for m in ms)} | {pct(mean([m['orig_ok'] for m in ms]))} |")
        report.append("")

        # ---------- 6. Aggregator (inputs: v1 reviewer outputs, EN run 0) ----------
        views = []
        for spec in AGENTS:
            d = next(d for s, r, d in results[(spec.key, "v1")] if s == "en" and d)
            res = spec.model.model_validate(d)
            res.agent = spec.name
            grounding.ground_agent(res, preps["en"].cv_redacted)
            views.append(_aggregator_view(spec, res))
        sy1, us1 = prompts.render("aggregator", output_language=LANG["en"], job_title=job_en.job_title,
                                  agents_json=json.dumps(views, ensure_ascii=False, indent=1))
        sy0, us0 = v0_aggregator(sy1, us1)
        agg = await asyncio.gather(*[call(client, f"agg-{v}-{r}", s_, u_, 0.2)
                                     for v, (s_, u_) in (("v0", (sy0, us0)), ("v1", (sy1, us1))) for r in range(2)])
        allowed = {a.name for a in AGENTS}
        report += ["## 6. Aggregator — chain-of-thought `reasoning` + ranking rules",
                   "**v0** removes the HOW TO THINK steps, the RANKING RULES (max 5, multi-reviewer first, effort "
                   "scale, caution on unverified quotes) and the `reasoning` field. Same reviewer inputs, 2 runs each. "
                   "Action counts are taken from the raw output, before our code caps the list at 5.", "",
                   "| Variant | Actions returned (raw) | Actions credited to ≥1 reviewer | Avg reviewers per action | Priority-1 action raised by ≥2 reviewers |",
                   "|---|---|---|---|---|"]
        for v, idx in (("v0", (0, 1)), ("v1", (2, 3))):
            raws = [agg[k] for k in idx if agg[k]]
            acts = [a for d in raws for a in (d.get("top_actions") or [])]
            credited = [a for a in acts if set(a.get("raised_by") or []) & allowed]
            firsts = [AggregateResult.model_validate(d).top_actions[0] for d in raws
                      if AggregateResult.model_validate(d).top_actions]
            report.append(f"| {v} | {', '.join(str(len(d.get('top_actions') or [])) for d in raws)} | "
                          f"{pct(len(credited) / len(acts) if acts else None)} | "
                          f"{mean([len(set(a.get('raised_by') or []) & allowed) for a in acts])} | "
                          f"{sum(len(set(f.raised_by) & allowed) >= 2 for f in firsts)}/{len(firsts)} |")
        report.append("")

    header = [
        "# Prompt ablations: baseline vs. final",
        "",
        f"_Generated by `scripts/ablations.py` on {datetime.now():%Y-%m-%d %H:%M} with DeepSeek `{cfg['model']}`._",
        "",
        "Each **v0** baseline is the final prompt (**v1**) with one technique removed; model, temperature, JSON "
        "schema and inputs are identical. Inputs are the fictional samples in `samples/` (plus one fictional "
        "biased posting for the parser). Small sample sizes (2–3 runs): read these as indicative, not statistically "
        "significant.",
        "",
    ]
    header.append("Raw model outputs for every call: [`ablations_raw.json`](ablations_raw.json).\n")
    (ROOT / "docs" / "ablations_raw.json").write_text(
        json.dumps(sorted(RAW, key=lambda x: x["call"]), ensure_ascii=False, indent=1), encoding="utf-8")
    out = ROOT / "docs" / "ABLATIONS.md"
    out.write_text("\n".join(header + report), encoding="utf-8")
    print("\n".join(report))
    print(f"Wrote {out}")


if __name__ == "__main__":
    asyncio.run(main())
