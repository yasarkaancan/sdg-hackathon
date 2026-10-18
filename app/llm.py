"""DeepSeek (OpenAI-compatible) chat client with JSON mode.

Features
- direct httpx call to ``{DEEPSEEK_BASE_URL}/chat/completions`` (no SDK)
- 60 s timeout, 2 retries with exponential backoff on 429 / 5xx / network errors
- JSON parsing + pydantic validation, with ONE "repair" call if either fails
- mock mode (``MOCK_LLM=1`` or no API key): returns fixtures from
  ``samples/mock_responses/<set>/<name>.json`` after a small artificial delay

Privacy: this module never logs prompt or response *content*, only metadata
(prompt name, latency, token counts, status codes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from typing import TypeVar

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

log = logging.getLogger("cvboard.llm")

ROOT = Path(__file__).resolve().parent.parent
MOCK_DIR = ROOT / "samples" / "mock_responses"

TIMEOUT_S = 60.0
MAX_RETRIES = 2
MAX_TOKENS = 3000

# Artificial latency per prompt in mock mode, so the live progress UI looks
# like the real thing (agents finish at different times).
MOCK_DELAYS = {
    "job_parser": 1.0,
    "ats": 2.0,
    "recruiter": 2.8,
    "hiring_manager": 4.0,
    "coach": 3.4,
    "aggregator": 2.5,
}

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Raised when the model call fails for good (after retries / repair)."""


def settings() -> dict:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    mock_flag = os.getenv("MOCK_LLM", "0").strip().lower() in {"1", "true", "yes"}
    return {
        "api_key": key,
        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip() or "deepseek-chat",
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        "mock": mock_flag or not key,
    }


def _extract_json(text: str) -> dict:
    """Parse a JSON object from the model's reply (tolerates ```json fences)."""
    if not text or not text.strip():
        raise ValueError("empty response")
    cleaned = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.S | re.I)
    if fence:
        cleaned = fence.group(1)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # Last resort: take the outermost {...} block.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(cleaned[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("top-level json is not an object")
    return data


class LLMClient:
    """One instance per review request; use as an async context manager."""

    def __init__(self) -> None:
        cfg = settings()
        self.api_key: str = cfg["api_key"]
        self.model: str = cfg["model"]
        self.base_url: str = cfg["base_url"]
        self.mock: bool = cfg["mock"]
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LLMClient":
        if not self.mock:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT_S))
        return self

    async def __aexit__(self, *exc) -> None:
        if self._http is not None:
            await self._http.aclose()

    # ------------------------------------------------------------------ #
    async def chat_json(
        self,
        *,
        name: str,
        system: str,
        user: str,
        model_cls: type[T],
        temperature: float,
        mock_set: str = "en",
    ) -> T:
        """Call the model in JSON mode and return a validated pydantic object."""
        if self.mock:
            return await self._mock(name, model_cls, mock_set)

        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        content = await self._post(name, messages, temperature)
        try:
            return model_cls.model_validate(_extract_json(content))
        except (ValueError, ValidationError) as first_err:
            log.warning("%s: invalid json/schema, sending one repair request (%s)", name,
                        type(first_err).__name__)
            repair_messages = messages + [
                {"role": "assistant", "content": content or ""},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply was not valid json matching the schema "
                        f"(error: {_short_error(first_err)}). "
                        "Return only valid json matching the schema from the system prompt. "
                        "No markdown, no explanations, no text outside the json object."
                    ),
                },
            ]
            content = await self._post(name + ":repair", repair_messages, 0.0)
            try:
                return model_cls.model_validate(_extract_json(content))
            except (ValueError, ValidationError) as second_err:
                raise LLMError(
                    f"{name}: model returned invalid json twice ({_short_error(second_err)})"
                ) from second_err

    # ------------------------------------------------------------------ #
    async def _post(self, name: str, messages: list[dict], temperature: float) -> str:
        assert self._http is not None
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = f"{self.base_url}/chat/completions"

        last_error = "unknown error"
        for attempt in range(MAX_RETRIES + 1):
            started = time.perf_counter()
            try:
                resp = await self._http.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"network error: {type(exc).__name__}"
                log.warning("%s: attempt %d failed (%s)", name, attempt + 1, last_error)
            else:
                elapsed = time.perf_counter() - started
                if resp.status_code == 200:
                    data = resp.json()
                    usage = data.get("usage") or {}
                    log.info("%s: ok in %.1fs (prompt=%s, completion=%s tokens)", name, elapsed,
                             usage.get("prompt_tokens"), usage.get("completion_tokens"))
                    try:
                        return data["choices"][0]["message"].get("content") or ""
                    except (KeyError, IndexError, TypeError) as exc:
                        raise LLMError(f"{name}: unexpected API response shape") from exc
                if resp.status_code in (401, 403):
                    raise LLMError("DeepSeek API rejected the API key (check DEEPSEEK_API_KEY).")
                if resp.status_code == 402:
                    raise LLMError("DeepSeek API: insufficient balance on this account.")
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = f"HTTP {resp.status_code}"
                    log.warning("%s: attempt %d got %s", name, attempt + 1, last_error)
                else:
                    raise LLMError(f"{name}: DeepSeek API error HTTP {resp.status_code}")

            if attempt < MAX_RETRIES:
                await asyncio.sleep(1.5 * (2**attempt) + random.uniform(0, 0.5))
        raise LLMError(f"{name}: DeepSeek API unavailable after {MAX_RETRIES + 1} attempts ({last_error})")

    # ------------------------------------------------------------------ #
    async def _mock(self, name: str, model_cls: type[T], mock_set: str) -> T:
        path = MOCK_DIR / mock_set / f"{name}.json"
        if not path.exists():
            path = MOCK_DIR / "en" / f"{name}.json"
        await asyncio.sleep(MOCK_DELAYS.get(name, 1.5) * random.uniform(0.8, 1.2))
        try:
            return model_cls.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, ValidationError) as exc:
            raise LLMError(f"{name}: mock fixture missing or invalid ({path.name})") from exc


def _short_error(err: Exception) -> str:
    if isinstance(err, ValidationError):
        first = err.errors()[0]
        loc = ".".join(str(p) for p in first.get("loc", ()))
        return f"{loc}: {first.get('msg')}"[:200]
    return str(err)[:200]
