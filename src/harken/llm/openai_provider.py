"""OpenAI-compatible provider (optional).

Talks to any OpenAI-compatible /chat/completions endpoint via httpx (no SDK
required). Works with OpenAI, OpenRouter, Together, etc. via ``HARKEN_LLM_BASE_URL``.
Requires ``OPENAI_API_KEY`` (or ``HARKEN_LLM_API_KEY``).
"""

from __future__ import annotations

import os
import time

import httpx

from harken.llm.base import LLMProvider

DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_BACKOFF_SECONDS = 1.0


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self, model: str | None = None, api_key: str | None = None, base_url: str | None = None, **_
    ):
        self.model = model or os.getenv("HARKEN_LLM_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.getenv("HARKEN_LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip(
            "/"
        )
        self._api_key = api_key or os.getenv("HARKEN_LLM_API_KEY") or os.getenv("OPENAI_API_KEY")

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def complete(self, prompt: str, system: str | None = None, max_tokens: int = 1024) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = httpx.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self.model, "messages": messages, "max_tokens": max_tokens},
                    timeout=60.0,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
            except httpx.HTTPStatusError as exc:
                if (
                    attempt >= _MAX_RETRIES
                    or exc.response.status_code not in _RETRYABLE_STATUS_CODES
                ):
                    raise
                delay = _retry_delay(exc.response, attempt)
            except httpx.RequestError:
                if attempt >= _MAX_RETRIES:
                    raise
                delay = _RETRY_BACKOFF_SECONDS * (2**attempt)

            time.sleep(delay)

        raise RuntimeError("unreachable")


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), 60.0)
        except ValueError:
            pass
    return min(_RETRY_BACKOFF_SECONDS * (2**attempt), 60.0)
