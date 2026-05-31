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
import logging
import re
from typing import Optional

from bs4 import BeautifulSoup

from zarchiver.ai.base import LLMError, LLMProvider
from zarchiver.config import AIConfig
from zarchiver.models import AIResult, ArchiveItem
from zarchiver.store import StateStore

log = logging.getLogger(__name__)

_SYSTEM_ZH = (
    "你是一个内容归档助手。请阅读用户提供的内容，输出简洁、客观的中文摘要、"
    "一组主题标签和一个分类。只返回一个 JSON 对象，不要包含任何额外文字或代码块标记。"
)
_SYSTEM_EN = (
    "You are a content archiving assistant. Read the provided content and "
    "produce a concise, objective summary, a set of topic tags, and a single "
    "category. Return only a JSON object, with no extra text or code fences."
)

# Tag rules mirror Obsidian's (https://obsidian.md/help/tags) so the tags can be
# used directly as note tags: letters/numbers/_/-, '/' for nesting, no spaces,
# at least one non-numeric char, case-insensitive.
_TAG_RULES_ZH = """标签必须符合 Obsidian 标签规范，便于直接用作笔记标签：
- 用斜杠(/)表示层级标签(如 编程/cpp)。
- 不能包含空格、括号等字符。
- 不能是纯数字(如 "2024" 不合法，"y2024" 合法)，至少含一个非数字字符。
- 标签不带前导的 # 号。
- 优先使用通用、可复用的主题词，避免过于具体或一次性的标签。"""

_TAG_RULES_EN = """Tags must follow Obsidian's tag rules so they work as note tags:
- Only letters, numbers, underscore (_), hyphen (-); use a forward slash (/) for nested tags (e.g. programming/cpp).
- No spaces; join words with kebab-case (e.g. machine-learning), not spaces or punctuation.
- Not purely numeric ("2024" is invalid, "y2024" is valid); include at least one non-numeric character.
- Do not include a leading # in the tag.
- Use lowercase (tags are case-insensitive).
- Prefer general, reusable topic words; avoid overly specific or one-off tags."""

_INSTRUCTION_ZH = """请基于以下内容生成一个 JSON 对象，格式严格为：
{{"summary": "200字以内的摘要", "tags": ["标签1","标签2","标签3"], "category": "单一分类"}}

要求：
- summary：200 字以内，客观概括。
- tags：中文为主，可包括 Cpp、UE5 等惯用英文标签，按相关性排序。
{tag_rules}
- category：单一、概括性的分类名（中文）。

标题：{title}
正文：
{body}
"""

_INSTRUCTION_EN = """Generate a single JSON object strictly in this format:
{{"summary": "a summary under 120 words", "tags": ["tag1","tag2","tag3"], "category": "one category"}}

Requirements:
- summary: under 120 words, objective.
- tags: ordered by relevance.
{tag_rules}
- category: a single, broad category name.

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
                log.debug("AI cache hit for %s (%r)", item.source_id, item.title)
                return cached

        body = self._prepare_body(item.content_html)
        system, instruction = self._prompts(item.title, body)
        log.debug(
            "calling %s for %r (%d chars in)",
            self.config.model, item.title, len(body),
        )
        reply = self.provider.complete(system, instruction, json_mode=True)
        result = self._parse(reply)
        result.model = self.config.model
        log.debug(
            "AI result for %r: category=%r, %d tags",
            item.title, result.category, len(result.tags),
        )

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
            return _SYSTEM_EN, _INSTRUCTION_EN.format(
                title=title, body=body, tag_rules=_TAG_RULES_EN
            )
        return _SYSTEM_ZH, _INSTRUCTION_ZH.format(
            title=title, body=body, tag_rules=_TAG_RULES_ZH
        )

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
