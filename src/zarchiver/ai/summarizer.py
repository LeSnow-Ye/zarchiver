"""Summarization + tagging orchestration.

Turns an item's content into a structured :class:`AIResult` (summary, tags,
category) via an :class:`LLMProvider`, with:

* HTML stripped to plain text and truncated to bound cost.
* A prompt asking for strict JSON, parsed defensively (models sometimes wrap
  JSON in prose or code fences).
* An optional category reference taxonomy (``AIConfig.category_reference``):
  when set, the model is asked to prefer it; when empty, it free-generates the
  category. See :mod:`scripts.category_stats` and ``docs/categories.md``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Callable

from bs4 import BeautifulSoup

from zarchiver.ai.base import LLMError, LLMProvider
from zarchiver.config import AIConfig
from zarchiver.models import AIResult, ArchiveItem

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

# See https://obsidian.md/help/tags
_TAG_RULES_ZH = """标签必须满足如下规范：
- 不能包含空格、括号等字符（如 `#` `.` `+`。如使用 Cpp、dotNet、Csharp，而非 C++、.NET、C#）。如有必要，可以使用`-`或`_`连接词语。
- 不能是纯数字(如 "2024" 不合法，"y2024" 合法)，至少含一个非数字字符。
- 英文标签优先使用 PascalCase（如 Cpp、UE5、ComputeShader 等）
- 优先使用通用、可复用的主题词，避免过于具体或一次性的标签。"""

_TAG_RULES_EN = """Tags must follow these rules:
- Only letters, numbers, underscore (_), hyphen (-), no hash (#) or dot (.) or brackets. e.g. Use Cpp, dotNet, Csharp instead of C++, .NET, C#.
- No spaces; join words with PascalCase (e.g. ComputeShader, Cpp, UE5), not spaces or punctuation.
- Not purely numeric ("2024" is invalid, "y2024" is valid); include at least one non-numeric character.
- Prefer general, reusable topic words; avoid overly specific or one-off tags."""

# The `category` instruction is built at request time from
# ``AIConfig.category_reference``. When it's empty, the model free-generates a
# category (``_CATEGORY_FREE_*``) — that free pass is how a user bootstraps a
# taxonomy. When it's set, the model is asked to prefer the closest match from
# it (``_CATEGORY_REF_*``, with ``{reference}`` filled from config). The list
# itself is intentionally NOT hardcoded here: categories are corpus-specific, so
# users derive their own via scripts/category_stats.py — see docs/categories.md.
_CATEGORY_FREE_ZH = "- category：单一、概括性的分类名（中文）。"
_CATEGORY_FREE_EN = "- category: a single, broad category name."

_CATEGORY_REF_ZH = """- category：单一、概括性的分类名（中文）。**请优先从下方“参考分类列表”中选择最贴切的一个**并直接使用其名称（若列表项带括号说明，仅作判断参考，请勿写入结果）；仅当确实没有任何合适项时，才自拟一个简洁、概括性的中文分类。

参考分类列表：
{reference}"""

_CATEGORY_REF_EN = """- category: a single, broad category name. **Prefer the closest match from the reference list below** and use its name verbatim (parenthetical hints, if any, are only for guidance — do not include them); only invent a new concise category when none fits.

Reference categories:
{reference}"""

_INSTRUCTION_ZH = """请基于以下内容生成一个 JSON 对象，格式严格为：
{{"summary": "200字以内的摘要", "tags": ["标签1","标签2","标签3"], "category": "单一分类"}}

要求：
- summary：200 字以内，客观概括。
- tags：3至6个，中文为主，可包括 Cpp、UE5 等惯用英文词作为标签，按相关性排序。
{tag_rules}
{category_block}

标题：{title}
正文：
{body}
"""

_INSTRUCTION_EN = """Generate a single JSON object strictly in this format:
{{"summary": "a summary under 120 words", "tags": ["tag1","tag2","tag3"], "category": "one category"}}

Requirements:
- summary: under 120 words, objective.
- tags: 3 to 6 tags, ordered by relevance.
{tag_rules}
{category_block}

Title: {title}
Body:
{body}
"""


def retry_ai_summary(
    summarize: Callable[[], AIResult],
    *,
    title: str = "",
    max_retries: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> AIResult:
    """Run an AI summary call with exponential backoff retries.

    ``max_retries`` counts retries after the first attempt. The final exception
    is re-raised so callers can decide whether AI failure should abort their
    workflow.
    """
    retries = max(0, int(max_retries))
    attempts = 1 + retries
    for attempt in range(attempts):
        try:
            return summarize()
        except Exception as exc:
            if attempt >= retries:
                raise
            delay = min(0.5 * (2 ** attempt), 8.0)
            log.debug(
                "AI summarization failed (%s), retrying in %.1fs: %r",
                exc, delay, title,
            )
            sleep(delay)
    raise RuntimeError("unreachable AI retry state")


class Summarizer:
    def __init__(
        self,
        config: AIConfig,
        provider: LLMProvider,
    ):
        self.config = config
        self.provider = provider

    def summarize(self, item: ArchiveItem) -> AIResult:
        """Return an :class:`AIResult` for ``item``."""
        body = self._prepare_body(item.content_html)
        system, instruction = self._prompts(item.title, body)
        log.debug(
            "calling %s for %r (%d chars in)",
            self.config.model, item.title, len(body),
        )

        start = time.monotonic()

        reply = self.provider.complete(system, instruction, json_mode=True)
        result = self._parse(reply)
        result.model = self.config.model
        log.info(
            "summarized in %0.2f seconds: %d chars in, category=%r, %d tags",
            time.monotonic() - start,
            len(body), result.category, len(result.tags),
        )
        return result

    def summarize_with_retry(
        self,
        item: ArchiveItem,
        *,
        max_retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> AIResult:
        """Return an AI summary, retrying transient provider failures."""
        return retry_ai_summary(
            lambda: self.summarize(item),
            title=item.title,
            max_retries=max_retries,
            sleep=sleep,
        )

    # ------------------------------------------------------------------ #
    def _prepare_body(self, html: str) -> str:
        text = BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        limit = self.config.max_input_chars
        if limit > 0 and len(text) > limit:
            text = text[:limit] + "\n…（内容已截断）"
        return text

    def _category_block(self, *, english: bool) -> str:
        """Build the ``category`` instruction line(s) from config.

        Empty :pyattr:`AIConfig.category_reference` → free generation; a
        non-empty reference → ask the model to prefer the closest match.
        """
        reference = (self.config.category_reference or "").strip()
        if english:
            return _CATEGORY_REF_EN.format(reference=reference) if reference else _CATEGORY_FREE_EN
        return _CATEGORY_REF_ZH.format(reference=reference) if reference else _CATEGORY_FREE_ZH

    def _prompts(self, title: str, body: str) -> tuple[str, str]:
        english = self.config.language.lower().startswith("en")
        category_block = self._category_block(english=english)
        if english:
            return _SYSTEM_EN, _INSTRUCTION_EN.format(
                title=title, body=body,
                tag_rules=_TAG_RULES_EN, category_block=category_block,
            )
        return _SYSTEM_ZH, _INSTRUCTION_ZH.format(
            title=title, body=body,
            tag_rules=_TAG_RULES_ZH, category_block=category_block,
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
