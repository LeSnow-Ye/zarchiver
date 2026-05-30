"""DeepSeek provider (OpenAI-compatible chat completions).

Notes specific to DeepSeek:

* The API is OpenAI-compatible at ``{base_url}/chat/completions``.
* ``deepseek-v4-flash`` is a *reasoning* model: the response carries both a
  hidden ``reasoning_content`` and the user-facing ``content``. Reasoning
  consumes output tokens, so ``max_tokens`` must be generous or ``content`` can
  come back empty with ``finish_reason == "length"``. We surface only
  ``content`` and treat an empty-due-to-length result as an error worth
  reporting.
"""

from __future__ import annotations

import httpx

from zarchiver.ai.base import LLMError, LLMProvider
from zarchiver.config import AIConfig


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(self, config: AIConfig):
        self.config = config
        if not config.api_key:
            raise LLMError(
                "DeepSeek API key missing. Set DEEPSEEK_API_KEY or ai.api_key."
            )

    def complete(self, system: str, user: str) -> str:
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(
                url, json=payload, headers=headers, timeout=self.config.timeout_s
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"DeepSeek request failed: {exc}") from exc

        if resp.status_code != 200:
            raise LLMError(
                f"DeepSeek HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
            choice = data["choices"][0]
            content = (choice["message"].get("content") or "").strip()
            finish = choice.get("finish_reason")
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError(f"Unexpected DeepSeek response: {exc}") from exc

        if not content:
            if finish == "length":
                raise LLMError(
                    "DeepSeek returned empty content (hit token limit during "
                    "reasoning); increase ai.max_tokens."
                )
            raise LLMError("DeepSeek returned empty content.")
        return content
