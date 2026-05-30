"""Summarization + tagging orchestration.

Turns an item's content into a structured :class:`AIResult` (summary, tags,
category) via an :class:`LLMProvider`, with:

* HTML stripped to plain text and truncated to bound cost.
* A prompt asking for strict JSON, parsed defensively (models sometimes wrap
  JSON in prose or code fences).
* Caching keyed by ``content_hash`` through the :class:`StateStore`, so the same
  body is never summarized twice.
"""

from __future__ import annotations

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from zarchiver.ai.base import LLMError, LLMProvider
from zarchiver.config import AIConfig
from zarchiver.models import AIResult, ArchiveItem
from zarchiver.store import StateStore

_SYSTEM_ZH = (
    "你是一个内容归档助手。请阅读用户提供的知乎内容，并输出简洁、客观的中文摘要、"
    "主题标签和一个分类。只返回 JSON，不要包含任何额外文字或代码块标记。"
)
_SYSTEM_EN = (
    "You are a content archiving assistant. Read the provided content and "
    "produce a concise, objective summary, topic tags, and a single category. "
    "Return only JSON, with no extra text or code fences."
)

_INSTRUCTION_ZH = """请基于以下内容生成 JSON，格式严格为：
{{"summary": "100字以内的摘要", "tags": ["标签1","标签2","标签3"], "category": "单一分类"}}

标题：{title}
正文：
{body}
"""

_INSTRUCTION_EN = """Generate JSON strictly in this format:
{{"summary": "a summary under 60 words", "tags": ["tag1","tag2","tag3"], "category": "one category"}}

Title: {title}
Body:
{body}
"""


class Summarizer:
    def __init__(
        self,
        config: AIConfig,
        provider: LLMProvider,
        store: Optional[StateStore] = None,
    ):
        self.config = config
        self.provider = provider
        self.store = store

    def summarize(self, item: ArchiveItem, *, use_cache: bool = True) -> AIResult:
        """Return an :class:`AIResult` for ``item`` (cached when possible)."""
        chash = item.content_hash()
        if use_cache and self.store is not None:
            cached = self.store.get_ai(chash)
            if cached is not None and not cached.is_empty():
                return cached

        body = self._prepare_body(item.content_html)
        system, instruction = self._prompts(item.title, body)
        reply = self.provider.complete(system, instruction)
        result = self._parse(reply)
        result.model = self.config.model

        if self.store is not None and not result.is_empty():
            self.store.put_ai(chash, result)
        return result

    # ------------------------------------------------------------------ #
    def _prepare_body(self, html: str) -> str:
        text = BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        limit = self.config.max_input_chars
        if len(text) > limit:
            text = text[:limit] + "\n…（内容已截断）"
        return text

    def _prompts(self, title: str, body: str) -> tuple[str, str]:
        if self.config.language.lower().startswith("en"):
            return _SYSTEM_EN, _INSTRUCTION_EN.format(title=title, body=body)
        return _SYSTEM_ZH, _INSTRUCTION_ZH.format(title=title, body=body)

    def _parse(self, reply: str) -> AIResult:
        data = _extract_json(reply)
        if not isinstance(data, dict):
            # Last resort: keep the raw reply as the summary so nothing is lost.
            return AIResult(summary=reply.strip()[:500])
        tags = data.get("tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in re.split(r"[,，、]", tags) if t.strip()]
        tags = [str(t).strip() for t in tags if str(t).strip()][:8]
        return AIResult(
            summary=str(data.get("summary", "")).strip(),
            tags=tags,
            category=str(data.get("category", "")).strip(),
        )


def _extract_json(text: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from an LLM reply."""
    text = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Grab the first balanced-looking {...} block.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None
