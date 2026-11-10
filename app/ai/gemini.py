"""Thin, rate-limited Gemini client with bounded retries and strict JSON validation."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class AIError(Exception):
    pass


class AIUnavailable(AIError):
    """AI is not configured, rate-limited, or temporarily failing. Safe to retry later."""


class AIQuotaExhausted(AIUnavailable):
    """Every usable model hit its (daily) free-tier quota. `retry_at` is a unix timestamp."""

    def __init__(self, retry_at: float):
        super().__init__("AI daily limit reached")
        self.retry_at = retry_at


class AIInvalidResponse(AIError):
    """AI returned output that does not match the schema."""

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


class AIClient(Protocol):
    model: str

    async def generate_json(
        self, prompt: str, schema: type[T], *, system: str, audio: tuple[bytes, str] | None = None,
        background: bool = False,
    ) -> tuple[T, str]: ...


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_response(raw: str, schema: type[T]) -> T:
    if raw is None:
        raise AIInvalidResponse("empty response", raw)
    text = _FENCE_RE.sub("", raw.strip())
    try:
        data = json.loads(text, parse_float=str)  # keep amounts exact
    except json.JSONDecodeError as exc:
        raise AIInvalidResponse(f"malformed JSON: {exc.msg}", raw) from exc
    if isinstance(data, list) and len(data) == 1:
        data = data[0]
    if not isinstance(data, dict):
        raise AIInvalidResponse("JSON is not an object", raw)
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise AIInvalidResponse(f"schema validation failed: {exc.error_count()} errors", raw) from exc


class DisabledAI:
    model = "disabled"

    async def generate_json(self, prompt, schema, *, system, audio=None, background=False):
        raise AIUnavailable("GEMINI_API_KEY is not configured")


_RETRY_RE = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.IGNORECASE)
_RETRY_DELAY_RE = re.compile(r"'retryDelay': '(\d+)s'")


def quota_retry_after(exc: Exception) -> tuple[bool, float]:
    """For a 429: (is_daily_or_long_quota, seconds_until_retry)."""
    text = str(exc)
    seconds = 0.0
    m = _RETRY_RE.search(text)
    if m and any(m.groups()):
        h, mi, sec = m.groups()
        seconds = int(h or 0) * 3600 + int(mi or 0) * 60 + float(sec or 0)
    else:
        m2 = _RETRY_DELAY_RE.search(text)
        if m2:
            seconds = float(m2.group(1))
    daily = "per day" in text.lower() or "perday" in text.lower() or seconds > 120
    return daily, seconds or 60.0


class GeminiClient:
    """Rate-limited Gemini client with model fallback and quota awareness.

    - interactive calls (owner's text/voice) and background calls (classifying bank messages) use separate
      model chains and separate pacing, so a classification backlog never blocks the owner's requests;
    - a model whose daily free quota is exhausted is skipped until its reset time;
    - transient errors (5xx, timeouts, per-minute 429) are retried with backoff, bounded.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        fallback_models: list[str] | None = None,
        background_models: list[str] | None = None,
        max_concurrency: int = 1,
        min_interval: float = 4.0,
        max_retries: int = 3,
        timeout: float = 60.0,
        background_daily_budget: int | None = None,
    ):
        from google import genai  # imported lazily so tests never need the network
        from google.genai import types

        # v1beta supports system instructions + JSON mode for every key type (incl. "AQ." keys).
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        self.model = model
        self.interactive_models = [model] + [m for m in (fallback_models or []) if m != model]
        self.background_models = background_models or list(reversed(self.interactive_models))
        self._sem = {False: asyncio.Semaphore(max(1, max_concurrency)), True: asyncio.Semaphore(max(1, max_concurrency))}
        self._gate = {False: asyncio.Lock(), True: asyncio.Lock()}
        self._last_call = {False: 0.0, True: 0.0}
        self._min_interval = min_interval
        self._max_retries = max(1, max_retries)
        self._timeout = timeout
        self._exhausted_until: dict[str, float] = {}
        self._bg_budget = background_daily_budget
        self._bg_day = ""
        self._bg_used = 0

    def _take_background_budget(self) -> None:
        """Count background requests per UTC day; refuse once the daily budget is spent."""
        if self._bg_budget is None:
            return
        import datetime as _dt

        today = _dt.datetime.now(_dt.timezone.utc).date()
        if today.isoformat() != self._bg_day:
            self._bg_day, self._bg_used = today.isoformat(), 0
        if self._bg_used >= self._bg_budget:
            tomorrow = _dt.datetime.combine(today + _dt.timedelta(days=1), _dt.time.min, tzinfo=_dt.timezone.utc)
            raise AIQuotaExhausted(tomorrow.timestamp())
        self._bg_used += 1

    async def _respect_rate_limit(self, background: bool) -> None:
        async with self._gate[background]:
            wait = self._min_interval - (time.monotonic() - self._last_call[background])
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[background] = time.monotonic()

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        from google.genai import errors

        if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
            return True
        if isinstance(exc, errors.APIError):
            return exc.code in (408, 429, 500, 502, 503, 504)
        name = type(exc).__name__
        return any(k in name for k in ("Timeout", "Connect", "Network", "RemoteProtocol"))

    def available_models(self, background: bool) -> list[str]:
        now = time.time()
        chain = self.background_models if background else self.interactive_models
        return [m for m in chain if self._exhausted_until.get(m, 0) <= now]

    async def generate_json(self, prompt, schema, *, system, audio=None, background: bool = False):
        from google.genai import errors, types

        parts = [types.Part.from_text(text=prompt)]
        if audio is not None:
            data, mime = audio
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))
        config = types.GenerateContentConfig(
            system_instruction=system, temperature=0.0, response_mime_type="application/json"
        )
        if background:
            self._take_background_budget()
        models = self.available_models(background)
        if not models:
            chain = self.background_models if background else self.interactive_models
            raise AIQuotaExhausted(min(self._exhausted_until.get(m, 0) for m in chain))

        last_exc: Exception | None = None
        for model in models:
            for attempt in range(1, self._max_retries + 1):
                async with self._sem[background]:
                    await self._respect_rate_limit(background)
                    try:
                        resp = await asyncio.wait_for(
                            self._client.aio.models.generate_content(
                                model=model, contents=[types.Content(role="user", parts=parts)], config=config
                            ),
                            timeout=self._timeout,
                        )
                        raw = resp.text or ""
                        self.model = model
                        return parse_json_response(raw, schema), raw
                    except AIInvalidResponse as exc:
                        last_exc = exc
                        log.warning("Gemini %s returned invalid JSON (attempt %s/%s): %s", model, attempt, self._max_retries, exc)
                    except (asyncio.TimeoutError, TimeoutError) as exc:
                        # A slow/hanging model: don't keep the owner waiting, try the next one.
                        self._exhausted_until[model] = time.time() + 600
                        log.warning("Gemini %s timed out; skipping it for 10 minutes", model)
                        last_exc = exc
                        break
                    except Exception as exc:  # noqa: BLE001
                        if isinstance(exc, errors.APIError) and exc.code == 429:
                            daily, retry = quota_retry_after(exc)
                            if daily:
                                self._exhausted_until[model] = time.time() + retry
                                log.warning("Gemini %s quota exhausted for ~%dh; trying next model", model, retry // 3600)
                                last_exc = exc
                                break  # next model
                        if not self._is_transient(exc):
                            log.error("Gemini %s request failed permanently: %s", model, type(exc).__name__)
                            raise AIUnavailable(f"Gemini error: {type(exc).__name__}") from exc
                        last_exc = exc
                        log.warning("Gemini %s transient error (attempt %s/%s): %s", model, attempt, self._max_retries, type(exc).__name__)
                if attempt < self._max_retries:
                    base = 10.0 if "429" in str(last_exc) else 2.0
                    await asyncio.sleep(base * (2 ** (attempt - 1)) + random.uniform(0, 1))
            else:
                # retries exhausted on this model for transient reasons: don't hammer further models
                break
        if isinstance(last_exc, AIInvalidResponse):
            raise last_exc
        if not self.available_models(background):
            chain = self.background_models if background else self.interactive_models
            raise AIQuotaExhausted(min(self._exhausted_until.get(m, 0) for m in chain)) from last_exc
        raise AIUnavailable("Gemini unavailable, try again later") from last_exc


def build_ai_client(settings) -> AIClient:
    if not settings.gemini_api_key:
        log.warning("GEMINI_API_KEY not set: AI classification disabled, transactions will be saved as unclassified")
        return DisabledAI()
    return GeminiClient(
        settings.gemini_api_key.get_secret_value(),
        settings.gemini_model,
        fallback_models=settings.gemini_fallback_models,
        background_models=settings.gemini_background_models,
        max_concurrency=settings.ai_max_concurrency,
        min_interval=settings.ai_min_interval_seconds,
        max_retries=settings.ai_max_retries,
        timeout=settings.ai_request_timeout_seconds,
        background_daily_budget=settings.ai_background_daily_budget,
    )
