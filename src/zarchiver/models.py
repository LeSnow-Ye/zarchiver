"""Platform-neutral content models.

Every source (Zhihu today, other platforms tomorrow) normalizes its content
into an :class:`ArchiveItem`. Exporters, the dedup store, and the AI module all
operate on this type and never touch platform-specific structures, which keeps
the core decoupled from any single site.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class ContentType(str, Enum):
    """Kind of content an item represents."""

    ANSWER = "answer"
    ARTICLE = "article"
    QUESTION = "question"
    PIN = "pin"  # Zhihu "想法"; reserved for future use
    OTHER = "other"


@dataclass(slots=True)
class Author:
    """The person who wrote a piece of content."""

    name: str
    url: Optional[str] = None
    headline: Optional[str] = None
    id: Optional[str] = None


@dataclass(slots=True)
class AIResult:
    """Output of the AI summarization/classification step."""

    summary: str = ""
    tags: list[str] = field(default_factory=list)
    category: str = ""
    model: str = ""

    def is_empty(self) -> bool:
        return not (self.summary or self.tags or self.category)


@dataclass(slots=True)
class ArchiveItem:
    """A single archivable unit of content, normalized across platforms.

    Attributes:
        platform: Source platform identifier, e.g. ``"zhihu"``.
        content_type: One of :class:`ContentType`.
        source_id: Stable per-platform identifier (answer id, article id, ...).
            Combined with ``platform`` this uniquely identifies the item.
        url: Canonical URL the content was fetched from.
        title: Display title. For answers this is the parent question title.
        content_html: Raw HTML body as delivered by the platform.
        author: Original author.
        created/updated: Publication and last-edit timestamps (tz-aware).
        question_title/question_url: Context for answers.
        voteup_count/comment_count: Engagement metrics, if known.
        topics: Platform-native topic/tag strings.
        ai: Populated by the AI module after fetch.
        raw: Escape hatch holding the original parsed structure for debugging.
    """

    platform: str
    content_type: ContentType
    source_id: str
    url: str
    title: str
    content_html: str
    author: Optional[Author] = None
    created: Optional[datetime] = None
    updated: Optional[datetime] = None
    question_title: Optional[str] = None
    question_url: Optional[str] = None
    voteup_count: Optional[int] = None
    comment_count: Optional[int] = None
    topics: list[str] = field(default_factory=list)
    excerpt: str = ""
    ai: AIResult = field(default_factory=AIResult)
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Globally unique identity used by the dedup store."""
        return f"{self.platform}:{self.content_type.value}:{self.source_id}"

    def content_hash(self) -> str:
        """Stable hash of the body, used to detect content changes.

        Only the rendered content matters for change detection, so engagement
        counts (which drift constantly) are intentionally excluded.
        """
        h = hashlib.sha256()
        h.update(self.title.encode("utf-8"))
        h.update(b"\0")
        h.update(self.content_html.encode("utf-8"))
        return h.hexdigest()

    @staticmethod
    def epoch_to_dt(value: Optional[int]) -> Optional[datetime]:
        """Convert a unix-epoch-seconds int (as Zhihu provides) to a datetime."""
        if not value:
            return None
        try:
            return datetime.fromtimestamp(int(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
