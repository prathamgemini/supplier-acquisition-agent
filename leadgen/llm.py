"""Groq LLM wrapper.

Agents call `LLM.structured(...)` and get back a validated pydantic object, or
`None` when the model is unavailable (no API key, auth failure, repeated
errors). Every agent has a deterministic fallback for the `None` case, so the
pipeline always completes and always says which engine produced each result.

Rate limits: Groq's free tier caps output tokens per minute and counts a
request's `max_tokens` against that cap up front. So every call asks for a
small, task-sized `max_tokens`, calls are throttled by a semaphore, and a 429
is handled by sleeping for the server's `retry-after` and trying again.
"""

from __future__ import annotations

import threading
import time
from typing import TypeVar

from pydantic import BaseModel

from .config import Settings

T = TypeVar("T", bound=BaseModel)

_MAX_CONSECUTIVE_FAILURES = 3
_MAX_WAIT_PER_CALL = 240  # seconds a single call may spend waiting on rate limits
_THINKING_ALLOWANCE = 700  # extra output tokens when reasoning is switched on
# Groq JSON-schema mode: strict (constrained decoding, keys guaranteed) first, then best-effort.
# (Tool calling is not used: Qwen's tool-call format fails Groq's validation.)
_STRICT_ATTEMPTS = (True, None)
_JSON_RULE = "\n\nRespond only with a JSON object that matches the required schema."
_FATAL = {"AuthenticationError", "PermissionDeniedError", "NotFoundError"}


def _retry_after(exc: Exception) -> float | None:
    """Seconds to wait if `exc` is a retryable rate limit, else None."""
    if type(exc).__name__ != "RateLimitError":
        return None
    if "too large" in str(exc).lower():  # the request itself can never fit: do not retry
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    try:
        return min(65.0, float(headers.get("retry-after", 8)) + 0.5)
    except (TypeError, ValueError):
        return 8.0


class LLM:
    def __init__(self, settings: Settings):
        self.model = settings.groq_model
        self._api_key = settings.groq_api_key
        self._reasoning = settings.reasoning
        self.concurrency = settings.llm_concurrency
        self.enabled = settings.llm_enabled
        self.calls = 0
        self.rate_limit_waits = 0
        self.seconds_waited = 0.0
        self.errors: list[str] = []
        self._failures = 0
        self._chats: dict[tuple[float, str, int], object] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(self.concurrency)

    @property
    def mode(self) -> str:
        return f"Groq · {self.model}" if self.enabled else "Offline heuristics (no LLM)"

    def _chat(self, temperature: float, reasoning_effort: str, max_tokens: int):
        key = (temperature, reasoning_effort, max_tokens)
        with self._lock:
            if key not in self._chats:
                from langchain_groq import ChatGroq

                self._chats[key] = ChatGroq(
                    model=self.model,
                    api_key=self._api_key,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    max_retries=1,  # transient network errors; 429s are handled below
                    timeout=60,
                    reasoning_effort=reasoning_effort,
                    reasoning_format="hidden",
                )
            return self._chats[key]

    def _record_failure(self, exc: Exception) -> None:
        message = f"{type(exc).__name__}: {str(exc)[:300]}"
        with self._lock:
            self.errors.append(message)
            self._failures += 1
            if type(exc).__name__ in _FATAL or self._failures >= _MAX_CONSECUTIVE_FAILURES:
                self.enabled = False  # circuit breaker: stop hammering a broken endpoint

    def _invoke(self, runnable, messages):
        """Invoke once, sleeping through rate limits. Raises on any other error."""
        waited = 0.0
        while True:
            try:
                return runnable.invoke(messages)
            except Exception as exc:  # noqa: BLE001
                delay = _retry_after(exc)
                if delay is None or waited + delay > _MAX_WAIT_PER_CALL:
                    raise
                with self._lock:
                    self.rate_limit_waits += 1
                    self.seconds_waited += delay
                time.sleep(delay)
                waited += delay

    def structured(
        self,
        schema: type[T],
        system: str,
        user: str,
        *,
        temperature: float = 0.3,
        think: bool = False,
        max_tokens: int = 450,
    ) -> T | None:
        """Return a validated `schema` instance, or None if the LLM is unavailable."""
        if not self.enabled:
            return None

        effort = self._reasoning if think else "none"
        budget = max_tokens + (_THINKING_ALLOWANCE if effort != "none" else 0)
        chat = self._chat(temperature, effort, budget)
        messages = [("system", system + _JSON_RULE), ("human", user)]
        last_exc: Exception | None = None

        with self._slots:
            for strict in _STRICT_ATTEMPTS:
                if not self.enabled:
                    break
                try:
                    result = self._invoke(chat.with_structured_output(schema, method="json_schema", strict=strict), messages)
                except Exception as exc:  # noqa: BLE001 - any provider error means "fall back"
                    last_exc = exc
                    if type(exc).__name__ in _FATAL | {"RateLimitError"}:
                        break
                    continue
                with self._lock:
                    self.calls += 1
                    self._failures = 0
                return result

        if last_exc is not None:
            self._record_failure(last_exc)
        return None

    def text(self, system: str, user: str, *, temperature: float = 0.7, max_tokens: int = 350) -> str | None:
        """Free-text generation, or None if the LLM is unavailable.

        Used for long prose (emails): in JSON mode this model tends to cut long
        string fields off mid-sentence, while plain text comes back complete.
        """
        if not self.enabled:
            return None
        chat = self._chat(temperature, "none", max_tokens)
        try:
            with self._slots:
                reply = self._invoke(chat, [("system", system), ("human", user)])
        except Exception as exc:  # noqa: BLE001
            self._record_failure(exc)
            return None
        with self._lock:
            self.calls += 1
            self._failures = 0
        content = reply.content if isinstance(reply.content, str) else str(reply.content)
        return content.strip() or None

    def ping(self) -> tuple[bool, str]:
        """One tiny round-trip to verify the key and model. Used by `main.py --check-llm`."""
        if not self._api_key:
            return False, "GROQ_API_KEY is not set in .env"

        class Pong(BaseModel):
            reply: str

        result = self.structured(Pong, "You are a health check.", 'Set "reply" to the word ok.', max_tokens=40)
        if result is None:
            return False, self.errors[-1] if self.errors else "Unknown error"
        return True, f"{self.model} responded: {result.reply!r}"
