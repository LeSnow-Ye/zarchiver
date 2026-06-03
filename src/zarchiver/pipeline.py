"""The archiving pipeline: ingest (write) and export (render).

The flow is split into two halves around the store as the system of record:

* **Ingest** — ``source.fetch`` → DB-based dedup (by ``content_hash``) → download
  images + AI enrichment + persist the full item (delegated to
  :class:`~zarchiver.ingest.Ingestor`). Optionally auto-exports just-ingested
  items.
* **Export** — render stored items to each exporter, fully offline (images are
  rewritten from the asset map recorded at ingest). Used both for auto-export
  and by the standalone ``export`` command (:func:`export_items`).

It depends only on the ``Source``, ``Exporter``, ``Ingestor`` and ``StateStore``
abstractions, never on Zhihu or markdown specifics. Duplicate handling is driven
by the ``on_duplicate`` config policy against the DB, not the filesystem.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Optional

import httpx

from zarchiver.config import Config
from zarchiver.exporters.assets import FetchResult, FetchStatus
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.ingest import Ingestor
from zarchiver.models import ArchiveItem
from zarchiver.sources.base import Source, SourceError

log = logging.getLogger(__name__)


class Action(str, Enum):
    ARCHIVED = "archived"
    UPDATED = "updated"
    SKIPPED = "skipped"
    EXPORTED = "exported"
    SUMMARIZED = "summarized"
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
Progress = Callable[[str], None]


# ---------------------------------------------------------------------- #
# Export fan-out (shared by auto-export and the export command)
# ---------------------------------------------------------------------- #
def _run_exporters(
    item: ArchiveItem,
    exporters: list[Exporter],
    progress: Progress,
) -> list[ExportResult]:
    exports: list[ExportResult] = []
    for exporter in exporters:
        try:
            result = exporter.export(item)
            exports.append(result)
            if result.path:
                log.debug("  [%s] -> %s", exporter.name, result.path)
        except Exception as exc:
            log.error("exporter %s failed for %r: %s", exporter.name, item.title, exc)
            exports.append(
                ExportResult(exporter=exporter.name, detail=f"failed: {exc}")
            )
    return exports


def export_items(
    items: Iterable[ArchiveItem],
    exporters: list[Exporter],
    *,
    skip_existing: bool = False,
    progress: Optional[Progress] = None,
) -> list[ItemOutcome]:
    """Render stored items to each exporter (offline). Used by ``export``.

    With ``skip_existing`` True, an item whose every exporter target already
    exists on disk is skipped; otherwise outputs are overwritten (export is a
    deterministic function of the DB, so re-export is cheap and safe).
    """
    emit = progress or (lambda msg: None)
    outcomes: list[ItemOutcome] = []
    for item in items:
        if skip_existing and exporters and all(
            e.already_exists(item) for e in exporters
            if e.target_path(item) is not None
        ):
            emit(f"skip   {item.title}")
            outcomes.append(
                ItemOutcome(item, Action.SKIPPED, url=item.url, detail="exists")
            )
            continue
        exports = _run_exporters(item, exporters, emit)
        emit(f"export {item.title}")
        outcomes.append(
            ItemOutcome(item, Action.EXPORTED, url=item.url, exports=exports)
        )
    return outcomes


def resummarize_items(
    items: Iterable[ArchiveItem],
    summarizer,
    store,
    *,
    only_empty: bool = False,
    progress: Optional[Progress] = None,
) -> list[ItemOutcome]:
    """Re-run AI summarization for already-archived items and persist results.

    Reads each item from the DB, calls the summarizer on its stored content
    (no re-fetch, no network beyond the LLM), and saves the refreshed
    :class:`~zarchiver.models.AIResult` back via ``store.save_item``. Used by
    the ``reai`` command — e.g. to apply a new ``ai.category_reference`` to
    content archived before it was set.

    With ``only_empty`` True, items that already carry a non-empty AI result are
    skipped. An item whose summary call fails is reported as ``FAILED`` and left
    untouched; one failure never aborts the run.
    """
    emit = progress or (lambda msg: None)
    outcomes: list[ItemOutcome] = []
    for item in items:
        if only_empty and not item.ai.is_empty():
            emit(f"skip   {item.title}")
            outcomes.append(
                ItemOutcome(item, Action.SKIPPED, url=item.url, detail="has-ai")
            )
            continue
        try:
            item.ai = summarizer.summarize_with_retry(item)
            store.save_item(item)
        except Exception as exc:
            log.error("re-summarize failed for %r: %s", item.title, exc)
            outcomes.append(
                ItemOutcome(item, Action.FAILED, url=item.url, detail=str(exc))
            )
            continue
        emit(f"reai   {item.title}")
        outcomes.append(ItemOutcome(item, Action.SUMMARIZED, url=item.url))
    return outcomes


# ---------------------------------------------------------------------- #
# Ingest pipeline
# ---------------------------------------------------------------------- #
class Pipeline:
    def __init__(
        self,
        config: Config,
        source: Source,
        exporters: list[Exporter],
        store,
        ingestor: Ingestor,
        *,
        auto_export: bool = True,
        duplicate_prompt: Optional[DuplicatePrompt] = None,
        dry_run: bool = False,
        progress: Optional[Progress] = None,
    ):
        self.config = config
        self.source = source
        self.exporters = exporters
        self.store = store
        self.ingestor = ingestor
        self.auto_export = auto_export
        self.duplicate_prompt = duplicate_prompt
        self.dry_run = dry_run
        self._progress = progress or (lambda msg: None)

    # ------------------------------------------------------------------ #
    def archive_url(self, url: str) -> ItemOutcome:
        """Archive a single-item URL."""
        log.info("archiving %s", url)
        try:
            item = self.source.fetch(url)
        except SourceError as exc:
            log.error("failed to fetch %s: %s", url, exc)
            return ItemOutcome(None, Action.FAILED, url=url, detail=str(exc))
        return self._process(item)

    def archive_batch(self, url: str) -> list[ItemOutcome]:
        """Archive every item produced by a batch URL."""
        log.info("archiving batch %s", url)
        outcomes: list[ItemOutcome] = []
        try:
            items: Iterable[ArchiveItem] = self.source.fetch_batch(url)
        except SourceError as exc:
            log.error("failed to fetch batch %s: %s", url, exc)
            return [ItemOutcome(None, Action.FAILED, url=url, detail=str(exc))]
        for item in items:
            outcomes.append(self._process(item))
        log.info("batch complete: %d item(s) processed", len(outcomes))
        return outcomes

    # ------------------------------------------------------------------ #
    def _process(self, item: ArchiveItem) -> ItemOutcome:
        # Dedup against the DB by content_hash (not the filesystem).
        status = self.store.status_for(item)
        exists = status != "new"

        if self.dry_run:
            # Report what would happen without enriching, ingesting, or writing.
            # The "ask" policy is treated as "update" here (no prompt in a plan).
            planned = self._plan(exists)
            self._progress(f"{planned.value:8} {item.title}")
            return ItemOutcome(item, planned, url=item.url, detail=status)

        action = self._decide(item, exists)
        if action is None:
            log.info("skip (%s): %r", status, item.title)
            self._progress(f"skip   {item.title}")
            return ItemOutcome(item, Action.SKIPPED, url=item.url, detail=status)
        log.debug(
            "processing %r [%s] (status=%s -> %s)",
            item.title, item.key, status, action.value,
        )

        # Now that we're keeping the item, fetch supplementary data (e.g.
        # comments). Skipped duplicates never reach here, so they cost no
        # extra crawling. Best-effort: enrich must never block archiving.
        try:
            self.source.enrich(item)
        except Exception as exc:
            log.warning("enrich failed for %r: %s", item.title, exc)

        # Ingest: download images, run AI, persist the full item to the store.
        try:
            self.ingestor.ingest(item)
        except Exception as exc:
            log.error("ingest failed for %r: %s", item.title, exc)
            return ItemOutcome(item, Action.FAILED, url=item.url, detail=str(exc))

        exports: list[ExportResult] = []
        if self.auto_export and self.exporters:
            exports = _run_exporters(item, self.exporters, self._progress)

        log.info("%s: %r", action.value, item.title)
        self._progress(f"{action.value:8} {item.title}")
        return ItemOutcome(item, action, url=item.url, exports=exports)

    def _decide(self, item: ArchiveItem, exists: bool) -> Optional[Action]:
        """Return the action to take, or None to skip, per duplicate policy.

        ``exists`` is True when the item is already archived in the DB.
        """
        if not exists:
            return Action.ARCHIVED
        policy = self.config.archive.on_duplicate
        if policy == "update":
            return Action.UPDATED
        if policy == "ask" and self.duplicate_prompt is not None:
            return Action.UPDATED if self.duplicate_prompt(item) else None
        # Default "skip".
        return None

    def _plan(self, exists: bool) -> Action:
        """Non-interactive prediction of the action, for ``--dry-run``.

        Mirrors :meth:`_decide` but never prompts: under the ``ask`` policy an
        existing item is reported as ``UPDATED`` (the answer can't be known
        without prompting, and a plan shouldn't ask).
        """
        if not exists:
            return Action.ARCHIVED
        if self.config.archive.on_duplicate == "skip":
            return Action.SKIPPED
        return Action.UPDATED


def make_image_fetcher(
    config: Config,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[str], FetchResult]:
    """Build an image fetcher that satisfies Zhihu's hotlink/referer checks.

    Returns a function ``url -> FetchResult`` backed by a persistent httpx.Client
    with a Zhihu referer and browser-like UA. Assets larger than
    ``archive.max_asset_mb`` are classified as too-large so the content keeps
    its original remote link instead of storing an oversized file; the limit is
    enforced both via the ``Content-Length`` header and while streaming (in case
    the header is absent or lies). ``max_asset_mb = 0`` disables the limit.
    """
    client = httpx.Client(
        headers={
            "User-Agent": config.browser.user_agent,
            "Referer": "https://www.zhihu.com/",
        },
        # Generous: a single asset may be a multi-tens-of-MB FHD video.
        timeout=httpx.Timeout(120.0, connect=30.0),
        follow_redirects=True,
    )
    max_bytes = int(config.archive.max_asset_mb * 1024 * 1024)
    max_retries = max(0, int(config.archive.max_asset_retries))

    def delay_for(retry_index: int) -> float:
        return min(0.5 * (2 ** retry_index), 8.0)

    def should_retry_status(status_code: int) -> bool:
        return status_code == 429 or 500 <= status_code <= 599

    def fetch_once(url: str) -> tuple[FetchResult, Optional[str]]:
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    reason = f"HTTP {resp.status_code}"
                    if should_retry_status(resp.status_code):
                        return FetchResult(FetchStatus.FAILED), reason
                    return FetchResult(FetchStatus.FAILED), None
                # Trust an advertised size to skip the download entirely.
                if max_bytes:
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        log.info(
                            "skipping asset over %.0f MB -> keeping remote link: %s",
                            config.archive.max_asset_mb,
                            url,
                        )
                        return FetchResult(FetchStatus.TOO_LARGE), None
                chunks: list[bytes] = []
                size = 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    # Guard against a missing/incorrect Content-Length.
                    if max_bytes and size > max_bytes:
                        log.info(
                            "skipping asset over %.0f MB -> keeping remote link: %s",
                            config.archive.max_asset_mb, url,
                        )
                        return FetchResult(FetchStatus.TOO_LARGE), None
                    chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    return FetchResult(FetchStatus.FAILED), None
                return FetchResult(FetchStatus.OK, data), None
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return FetchResult(FetchStatus.FAILED), exc.__class__.__name__
        except httpx.HTTPError:
            return FetchResult(FetchStatus.FAILED), None

    def fetch(url: str) -> FetchResult:
        attempts = 1 + max_retries
        for attempt in range(attempts):
            result, retry_reason = fetch_once(url)
            if result.status != FetchStatus.FAILED or retry_reason is None:
                return result
            if attempt >= max_retries:
                log.warning("asset failed after %d attempts: %s", attempts, url)
                return result
            delay = delay_for(attempt)
            log.debug(
                "asset fetch failed (%s), retrying in %.1fs: %s",
                retry_reason, delay, url,
            )
            sleep(delay)
        return FetchResult(FetchStatus.FAILED)

    return fetch
