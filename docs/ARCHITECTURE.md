# Architecture

```mermaid
flowchart TD
    U["👩‍🎓 Job seeker<br/>(browser, static/index.html)"] -->|"job posting + CV file/text + language<br/>POST /api/review (multipart)"| API["FastAPI · app/main.py"]
    API -->|"NDJSON stream of events<br/>stage · redactions · job_parsed · agent_done · final · error"| U

    subgraph PIPE["app/pipeline.py"]
        direction TB
        P0["Step 0 · Preprocess (no LLM)<br/>app/preprocess.py<br/>extract PDF/DOCX/TXT · validate length<br/>redact PII + personal attributes"]
        P1["Step 1 · Job Posting Parser<br/>zero-shot · T=0.2"]
        subgraph AG["Step 2 · Reviewer agents in parallel (asyncio)"]
            direction LR
            A1["🤖 ATS & Keyword<br/>zero-shot + rubric"]
            A2["👔 HR Recruiter<br/>few-shot"]
            A3["🧠 Hiring Manager<br/>chain-of-thought"]
            A4["✍️ Impact Coach<br/>few-shot"]
        end
        P3["Step 3 · Grounding (no LLM)<br/>app/grounding.py<br/>verbatim quote check · rapidfuzz ≥ 90<br/>new-number fabrication check"]
        P4["Step 4 · Aggregator<br/>chain-of-thought · T=0.2<br/>sees reviews only, never the CV"]
        P5["Step 5 · Deterministic score (no LLM)<br/>app/scoring.py<br/>0.35·HM + 0.30·ATS + 0.20·Rec + 0.15·Coach<br/>must-have coverage"]
        P0 --> P1 --> AG --> P3 --> P4 --> P5
    end

    API --> P0
    P1 & A1 & A2 & A3 & A4 & P4 <-->|"JSON mode · retries · 1 repair call<br/>app/llm.py"| DS[("DeepSeek API<br/>(or mock fixtures)")]
    PR["prompts/*.md<br/>app/prompts.py"] -.-> P1 & AG & P4
```

## Components

| File | Responsibility |
|---|---|
| `app/main.py` | Routes: `GET /` (UI), `GET /api/health`, `GET /api/samples`, `POST /api/review` (streaming NDJSON). |
| `app/pipeline.py` | Orchestration. Runs the 4 reviewers with `asyncio.create_task` + `asyncio.as_completed`, so each `agent_done` event is streamed the moment that agent finishes. One failed agent never stops the others. |
| `app/llm.py` | DeepSeek client on raw `httpx`: 60 s timeout, 2 retries with exponential backoff on 429/5xx/network errors, JSON parsing (tolerates code fences), pydantic validation, one repair call on invalid JSON/schema. Mock mode when `MOCK_LLM=1` or no key. Logs metadata only, never content. |
| `app/preprocess.py` | Text extraction (pypdf, python-docx incl. tables, TXT with cp1254 fallback), validation (CV ≥ 300, job ≥ 150, ≤ 15 000 chars), regex redaction (emails, TR/intl phones, TC Kimlik, addresses, LinkedIn URL, name heuristic, and labelled personal-attribute fields in TR and EN). |
| `app/grounding.py` | Verifies each quoted `evidence` / rewrite `original` against the redacted CV (normalise → exact substring → `rapidfuzz.partial_ratio ≥ 90`). Flags, never drops. Detects numbers in rewrites that are not in the CV. |
| `app/scoring.py` | Weighted overall score and must-have coverage, in code. Weights are renormalised if an agent failed. |
| `app/schemas.py` | Pydantic models for every LLM output; lenient input normalisation (case, enum aliases, score clamping, max 5 actions). |
| `app/prompts.py` | Loads `prompts/*.md`, single-pass placeholder substitution. |
| `static/index.html` | Single page, vanilla JS + Tailwind CDN. Reads the stream with `fetch` + `ReadableStream`, renders live agent cards, results, Markdown report export. All LLM text is HTML-escaped before rendering. |

## Event stream (`POST /api/review`)

One JSON object per line:

```json
{"type":"stage","stage":"preprocess"}
{"type":"redactions","items":[{"kind":"date_of_birth","label":"date of birth","count":1}],"cv_redacted":"..."}
{"type":"stage","stage":"parse_job"}
{"type":"job_parsed","job":{...}}
{"type":"stage","stage":"agents"}
{"type":"agent_done","agent":"ats","ok":true,"result":{...},"verification":{"checked":18,"verified":18,"rate":1.0}}
{"type":"agent_done","agent":"coach","ok":false,"error":"..."}
{"type":"stage","stage":"aggregate"}
{"type":"final","result":{"job":{},"agents":{},"agent_errors":{},"aggregate":{},"scores":{},"verification":{},"redactions":[]}}
{"type":"error","message":"..."}
```

## Latency & cost

One review = 6 LLM calls (1 parser + 4 parallel reviewers + 1 aggregator), so wall-clock time ≈ parser + slowest
reviewer + aggregator. Each reviewer request is roughly 3–5k input tokens for a one-page CV. `scripts/evaluate.py`
records the real wall-clock time per run in `docs/EVALUATION.md`.
