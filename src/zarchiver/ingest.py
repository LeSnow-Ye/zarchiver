"""Ingest: turn a freshly-fetched item into a fully-archived DB record.

Ingest is the *write* half of the archive flow. Given an
:class:`~zarchiver.models.ArchiveItem` straight from a source, it:

1. Downloads every referenced image (article body, comment bodies, and the
   title image) **once** into a per-item directory under the assets root, and
   records a ``{remote_url: relative_local_path}`` map on the item.
2. Runs AI summarization/tagging.
3. Persists the complete item — content, comments, AI result, asset map, and
   the original parsed ``raw`` dict — to the store.

Images are fetched here (while the source links are still live) so the separate
export step can run fully offline. A single image failing never aborts ingest.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from zarchiver.ai import Summarizer
from zarchiver.exporters.assets import (
    Fetcher,
    collect_media_urls,
    download_images,
    filename_for,
)
from zarchiver.models import ArchiveItem, Comment
from zarchiver.store import StateStore

log = logging.getLogger(__name__)


def safe_key(key: str) -> str:
    """Filesystem-safe per-item directory name derived from an item key."""
    return key.replace(":", "_")


def _comment_html(comments: list[Comment]) -> str:
    """Concatenate all comment bodies (recursively) so their images are found."""
    parts: list[str] = []
    for c in comments:
        if c.content_html:
            parts.append(c.content_html)
        if c.children:
            parts.append(_comment_html(c.children))
    return "".join(parts)


class Ingestor:
    """Downloads assets, runs AI, and saves a complete item to the store."""

    def __init__(
        self,
        store: StateStore,
        *,
        assets_root: str | Path,
        fetch: Optional[Fetcher] = None,
        summarizer: Optional[Summarizer] = None,
        download_images: bool = True,
        download_concurrency: int = 1,
    ):
        self.store = store
        self.assets_root = Path(assets_root)
        self._fetch = fetch
        self.summarizer = summarizer
        self._download_images = download_images
        self._download_concurrency = max(1, int(download_concurrency))

    # ------------------------------------------------------------------ #
    def ingest(self, item: ArchiveItem) -> ArchiveItem:
        """Download images, enrich with AI, persist. Returns the saved item."""
        if self._download_images and self._fetch is not None:
            self._fetch_assets(item)

        if self.summarizer is not None:
            try:
                item.ai = self.summarizer.summarize_with_retry(item)
            except Exception as exc:  # AI must never block archiving
                log.warning("AI summarization failed for %r: %s", item.title, exc)

        self.store.save_item(item)
        log.debug("ingested %s (%d assets)", item.key, len(item.asset_map))
        return item

    # ------------------------------------------------------------------ #
    def _fetch_assets(self, item: ArchiveItem) -> None:
        """Collect every image URL on the item and download into its dir."""
        urls: list[str] = []
        seen: set[str] = set()

        def add(found: list[str]) -> None:
            for u in found:
                if u not in seen:
                    seen.add(u)
                    urls.append(u)

        add(collect_media_urls(item.content_html))
        add(collect_media_urls(_comment_html(item.comments)))
        if item.title_image and item.title_image not in seen:
            seen.add(item.title_image)
            urls.append(item.title_image)

        if not urls:
            item.asset_map = {}
            item.asset_issues = {}
            return

        dest = self.assets_root / safe_key(item.key)
        pairs = [(u, filename_for(u)) for u in urls]
        outcome = download_images(
            pairs, dest, self._fetch, concurrency=self._download_concurrency
        )

        prefix = safe_key(item.key)
        item.asset_map = {
            url: f"{prefix}/{fname}" for url, fname in outcome.saved.items()
        }
        item.asset_issues = (
            {url: "too_large" for url in outcome.oversized}
            | {url: "failed" for url in outcome.failed}
        )
