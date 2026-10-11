"""Minimal Groq chat client (OpenAI-compatible) with bounded retry and accounting."""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, field

import requests

from src import config


class LLMError(Exception):
    """Raised when the model cannot be reached or returns an unusable response."""


@dataclass
class LLMStats:
    calls: int = 0
    retries: int = 0
    wait_seconds: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    call_log: list[dict] = field(default_factory=list)


_RETRY_IN_RE = re.compile(r"try again in\s+([0-9.]+)\s*(ms|s|m)", re.IGNORECASE)


def _retry_delay(response: requests.Response, attempt: int) -> float:
    header = response.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    try:
        message = response.json().get("error", {}).get("message", "")
    except ValueError:
        message = ""
    match = _RETRY_IN_RE.search(message)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        return value / 1000 if unit == "ms" else value * 60 if unit == "m" else value
    return min(2.0 * (2 ** (attempt - 1)), config.LLM_MAX_BACKOFF_SECONDS)


_DASH_CODEPOINTS = {0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212}
_SPACE_CODEPOINTS = {0x00A0, 0x202F}
_QUOTE_MAP = {0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'}


def sanitize_text(text: str) -> str:
    """Normalise model text to plain ASCII punctuation: hyphen for any long dash, straight quotes, plain spaces."""
    chars = []
    for ch in text:
        code = ord(ch)
        if code in _DASH_CODEPOINTS:
            chars.append("-")
        elif code in _SPACE_CODEPOINTS:
            chars.append(" ")
        else:
            chars.append(_QUOTE_MAP.get(code, ch))
    return "".join(chars).strip()


class _TokenPacer:
    """Process-wide pacing from Groq's rate-limit headers.

    The free tier allows a small number of tokens per minute. Sleeping before a
    call that would exceed the bucket is cheaper than provoking a 429 (which also
    costs a request against the daily quota).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._remaining: float | None = None
        self._limit: float = 8000.0
        self._seen_at = 0.0

    def delay_for(self, estimated_tokens: float) -> float:
        with self._lock:
            if self._remaining is None:
                return 0.0
            rate = self._limit / 60.0
            available = min(self._limit, self._remaining + (time.monotonic() - self._seen_at) * rate)
            if available >= estimated_tokens:
                return 0.0
            return min((min(estimated_tokens, self._limit) - available) / rate, config.LLM_MAX_BACKOFF_SECONDS)

    def observe(self, headers: "requests.structures.CaseInsensitiveDict") -> None:
        try:
            remaining = float(headers.get("x-ratelimit-remaining-tokens"))
            limit = float(headers.get("x-ratelimit-limit-tokens", self._limit))
        except (TypeError, ValueError):
            return
        with self._lock:
            self._remaining, self._limit, self._seen_at = remaining, limit, time.monotonic()


_PACER = _TokenPacer()
_HTTP_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="llm-http")


def _post_with_deadline(url: str, headers: dict, payload: dict, deadline: float) -> requests.Response:
    """POST with a hard wall-clock deadline.

    requests' own timeout only bounds each socket read, so a server that keeps a
    connection open can hang a call indefinitely. The worker thread is abandoned
    when the deadline passes.
    """
    future = _HTTP_POOL.submit(requests.post, url, headers=headers, json=payload, timeout=(5, deadline))
    try:
        return future.result(timeout=deadline + 5)
    except FutureTimeout as exc:
        future.cancel()
        raise requests.Timeout(f"no response within {deadline + 5:.0f}s") from exc


class GroqClient:
    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or config.model_name()
        self.api_key = api_key or config.groq_api_key()
        self.stats = LLMStats()

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        response_format: dict | None = None,
        max_tokens: int = 900,
        label: str = "llm",
    ) -> dict:
        """One logical LLM call. Retries 429/5xx/timeouts with bounded backoff.

        Returns the assistant message dict. Raises LLMError when attempts are exhausted
        or the request is rejected (4xx other than 429 is not retried).
        """
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set")
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_completion_tokens": max_tokens,
        }
        if "gpt-oss" in self.model:
            payload["reasoning_effort"] = "low"
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if response_format:
            payload["response_format"] = response_format

        self.stats.calls += 1
        started = time.perf_counter()
        estimate = (len(json.dumps(messages)) + len(json.dumps(tools or []))) / 3.3 + 0.6 * max_tokens
        pause = _PACER.delay_for(estimate)
        if pause > 0.05:
            self.stats.wait_seconds += pause
            time.sleep(pause)
        last_error = "unknown error"
        for attempt in range(1, config.LLM_MAX_ATTEMPTS + 1):
            try:
                response = _post_with_deadline(
                    f"{config.GROQ_BASE_URL}/chat/completions",
                    {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    payload,
                    config.LLM_TIMEOUT_SECONDS,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:120]}"
                delay = min(1.5 * (2 ** (attempt - 1)), config.LLM_MAX_BACKOFF_SECONDS)
            else:
                code = response.status_code
                _PACER.observe(response.headers)
                if code == 200:
                    try:
                        body = response.json()
                        message = body["choices"][0]["message"]
                    except (ValueError, KeyError, IndexError) as exc:
                        raise LLMError(f"unreadable model response: {exc}") from exc
                    usage = body.get("usage") or {}
                    self.stats.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                    self.stats.completion_tokens += int(usage.get("completion_tokens") or 0)
                    self.stats.call_log.append(
                        {
                            "label": label,
                            "attempts": attempt,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                            "prompt_tokens": usage.get("prompt_tokens"),
                            "completion_tokens": usage.get("completion_tokens"),
                        }
                    )
                    return message
                last_error = _error_text(response)
                if code == 429 and re.search(r"per day|\(TPD\)|\(RPD\)", last_error):
                    # A daily quota will not clear within a retry window: fail fast so the
                    # caller can degrade to the deterministic path instead of stalling.
                    raise LLMError(f"daily quota exhausted: {last_error[:200]}")
                # 400 tool_use_failed / json_validate_failed: the model produced a malformed call.
                # One retry is reasonable; other 4xx will not change on repeat.
                retryable = code == 429 or code >= 500 or (code == 400 and "failed" in last_error.lower() and attempt < 2)
                if not retryable:
                    raise LLMError(f"HTTP {code}: {last_error}")
                delay = _retry_delay(response, attempt) if code == 429 else min(1.5 * (2 ** (attempt - 1)), config.LLM_MAX_BACKOFF_SECONDS)
            if attempt < config.LLM_MAX_ATTEMPTS:
                delay = min(delay + 0.25, config.LLM_MAX_BACKOFF_SECONDS)
                self.stats.retries += 1
                self.stats.wait_seconds += delay
                time.sleep(delay)
        raise LLMError(f"model call failed after {config.LLM_MAX_ATTEMPTS} attempts: {last_error}")


def _error_text(response: requests.Response) -> str:
    try:
        body = response.json()
        err = body.get("error", {})
        return f"{err.get('code', '')} {err.get('message', '')}".strip()[:300] or f"HTTP {response.status_code}"
    except ValueError:
        return f"HTTP {response.status_code}"


def parse_json_message(message: dict) -> dict:
    """Parse the JSON object returned under a strict schema."""
    content = message.get("content") or ""
    try:
        value = json.loads(content)
    except ValueError as exc:
        raise LLMError(f"model did not return valid JSON: {content[:120]!r}") from exc
    if not isinstance(value, dict):
        raise LLMError("model JSON was not an object")
    return value
