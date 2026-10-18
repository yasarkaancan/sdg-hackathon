"""FastAPI app: serves the single-page UI and the streaming review endpoint.

Run:  uvicorn app.main:app --reload   →   http://localhost:8000
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from . import llm
from .pipeline import run_review
from .preprocess import MAX_FILE_BYTES

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("cvboard")

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
SAMPLES = ROOT / "samples"

app = FastAPI(title="CV Review Board", version="1.0.0")

_cfg = llm.settings()
log.info("LLM mode: %s (model=%s)", "MOCK fixtures" if _cfg["mock"] else "DeepSeek API", _cfg["model"])


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict:
    cfg = llm.settings()
    return {"status": "ok", "mock": cfg["mock"], "model": cfg["model"]}


@app.get("/api/samples")
async def samples() -> list[dict]:
    def read(name: str) -> str:
        return (SAMPLES / name).read_text(encoding="utf-8")

    return [
        {"id": "en", "label": "EN · Junior Data Analyst", "language": "en",
         "cv": read("cv_en.txt"), "job": read("job_en.txt")},
        {"id": "tr", "label": "TR · Dijital Pazarlama Uzman Yardımcısı", "language": "tr",
         "cv": read("cv_tr.txt"), "job": read("job_tr.txt")},
    ]


@app.post("/api/review")
async def review(
    job_text: str = Form(""),
    output_language: str = Form("en"),
    cv_text: str = Form(""),
    cv_file: UploadFile | None = File(None),
) -> StreamingResponse:
    # Read the upload now: the request body is gone once streaming starts.
    file_tuple = None
    if cv_file is not None and cv_file.filename:
        data = await cv_file.read(MAX_FILE_BYTES + 1)
        file_tuple = (cv_file.filename, data)

    async def stream():
        try:
            async for event in run_review(cv_text=cv_text, cv_file=file_tuple, job_text=job_text,
                                          output_language=output_language):
                yield json.dumps(event, ensure_ascii=False) + "\n"
        except Exception as err:  # never leak CV content or stack traces (to the client or the log)
            log.error("review pipeline crashed: %s", type(err).__name__)
            yield json.dumps({"type": "error", "message": "Internal error while reviewing. Please try again."}) + "\n"

    return StreamingResponse(
        stream(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
