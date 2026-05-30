"""The archiving pipeline.

Orchestrates the full flow for one or many items:

    source.fetch ─▶ dedup check ─▶ (AI summarize) ─▶ each exporter

It is deliberately small and platform-agnostic: it depends on the ``Source``,
``Exporter``, ``Summarizer`` and ``StateStore`` abstractions, never on Zhihu or
markdown specifics. Duplicate handling, AI gating, and which exporters run are
all driven by config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional

import httpx

from zarchiver.ai import Summarizer
from zarchiver.config import Config
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.models import ArchiveItem
from zarchiver.sources.base import Source, SourceError
from zarchiver.store import StateStore


class Action(str, Enum):
    ARCHIVED = "archived"
    UPDATED = "updated"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class ItemOutcome:
    item: Optional[ArchiveItem]
    action: Action
    url: str = ""
    detail: str = ""
    exports: list[ExportResult] = field(default_factory=list)


# Callback used to resolve "ask" duplicate decisions; returns True to re-archive.
DuplicatePrompt = Callable[[ArchiveItem], bool]


class Pipeline:
    def __init__(
        self,
        config: Config,
        source: Source,
        exporters: list[Exporter],
        store: StateStore,
        summarizer: Optional[Summarizer] = None,
        *,
        duplicate_prompt: Optional[DuplicatePrompt] = None,
        progress: Optional[Callable[[str], None]] = None,
    ):
        self.config = config
        self.source = source
        self.exporters = exporters
        self.store = store
        self.summarizer = summarizer
        self.duplicate_prompt = duplicate_prompt
        self._progress = progress or (lambda msg: None)

    # ------------------------------------------------------------------ #
    def archive_url(self, url: str) -> ItemOutcome:
        """Archive a single-item URL."""
        try:
            item = self.source.fetch(url)
        except SourceError as exc:
            return ItemOutcome(None, Action.FAILED, url=url, detail=str(exc))
        return self._process(item)

    def archive_batch(self, url: str) -> list[ItemOutcome]:
        """Archive every item produced by a batch URL."""
        outcomes: list[ItemOutcome] = []
        try:
            items: Iterable[ArchiveItem] = self.source.fetch_batch(url)
        except SourceError as exc:
            return [ItemOutcome(None, Action.FAILED, url=url, detail=str(exc))]
        for item in items:
            outcomes.append(self._process(item))
        return outcomes

    # ------------------------------------------------------------------ #
    def _process(self, item: ArchiveItem) -> ItemOutcome:
        status = self.store.status_for(item)  # new | unchanged | changed
        action = self._decide(item, status)
        if action is None:
            self._progress(f"skip   {item.title}")
            return ItemOutcome(item, Action.SKIPPED, url=item.url, detail=status)

        # AI enrichment (cached by content hash inside the summarizer).
        if self.summarizer is not None:
            try:
                item.ai = self.summarizer.summarize(item)
            except Exception as exc:  # AI must never block archiving
                self._progress(f"  ai failed: {exc}")

        exports: list[ExportResult] = []
        for exporter in self.exporters:
            try:
                exports.append(exporter.export(item))
            except Exception as exc:
                exports.append(
                    ExportResult(exporter=exporter.name, detail=f"failed: {exc}")
                )

        self.store.record_archived(item)
        self._progress(f"{action.value:8} {item.title}")
        return ItemOutcome(item, action, url=item.url, exports=exports)

    def _decide(self, item: ArchiveItem, status: str) -> Optional[Action]:
        """Return the action to take, or None to skip, per duplicate policy."""
        if status == "new":
            return Action.ARCHIVED
        # status is "unchanged" or "changed" → it's a known duplicate.
        policy = self.config.archive.on_duplicate
        if policy == "update":
            return Action.UPDATED
        if policy == "ask" and self.duplicate_prompt is not None:
            return Action.UPDATED if self.duplicate_prompt(item) else None
        # Default "skip": still re-export if content actually changed? No —
        # skip means skip. Users who want edits picked up use "update".
        return None


def make_image_fetcher(config: Config) -> Callable[[str], Optional[bytes]]:
    """Build an image fetcher that satisfies Zhihu's hotlink/referer checks.

    Returns a function ``url -> bytes | None`` backed by a persistent
    httpx.Client with a Zhihu referer and browser-like UA.
    """
    client = httpx.Client(
        headers={
            "User-Agent": config.browser.user_agent,
            "Referer": "https://www.zhihu.com/",
        },
        timeout=30.0,
        follow_redirects=True,
    )

    def fetch(url: str) -> Optional[bytes]:
        try:
            resp = client.get(url)
            if resp.status_code == 200 and resp.content:
                return resp.content
        except httpx.HTTPError:
            return None
        return None

    return fetch
