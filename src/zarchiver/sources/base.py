"""Source abstraction.

A *source* knows how to turn URLs from one platform into normalized
:class:`~zarchiver.models.ArchiveItem` objects. The pipeline depends only on
this interface, so adding a platform is a matter of writing a new ``Source``
subclass — nothing downstream changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Iterator, Optional

from zarchiver.models import ArchiveItem

#: Predicate the pipeline passes to ``fetch_batch`` for incremental archiving:
#: given an item ``key``, returns True if it's already archived (so the source
#: can stop walking once it reaches known content). See ``Pipeline.incremental``.
KnownPredicate = Callable[[str], bool]


class Source(ABC):
    """Base class for all content sources."""

    #: Short platform identifier stored on every produced item, e.g. ``"zhihu"``.
    platform: str = "base"

    @abstractmethod
    def supports(self, url: str) -> bool:
        """Return True if this source can handle ``url``."""

    @abstractmethod
    def fetch(self, url: str) -> ArchiveItem:
        """Fetch a single piece of content addressed by ``url``.

        Raises:
            SourceError: if the URL is unsupported or content can't be parsed.
        """

    @abstractmethod
    def fetch_batch(
        self, url: str, *, known: Optional["KnownPredicate"] = None
    ) -> Iterator[ArchiveItem]:
        """Yield items for a batch URL (collection, column, question, ...).

        Implementations should yield lazily so the pipeline can archive each
        item as it arrives rather than buffering the whole batch.

        ``known``, when given, marks already-archived items by ``key``. A source
        whose listing is in a stable chronological order (e.g. newest-first
        collection/column items) may use it to stop walking early once it has
        reached content it has seen before — making periodic re-archiving cheap.
        Sources whose listing order isn't stable should ignore it.
        """

    def enrich(self, item: ArchiveItem) -> None:
        """Fetch supplementary data for an item the pipeline will keep.

        Called only once the pipeline has decided to archive or update an item
        — never for skipped duplicates. This is where a source does extra,
        potentially expensive work (e.g. crawling comments) that isn't needed
        to identify the item or detect content changes. Optional: the default
        does nothing. Must be best-effort (never raise) so enrichment can't
        block archiving.
        """

    # Sources that hold resources (a browser) can override these.
    def __enter__(self) -> "Source":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:  # noqa: D401 - optional override
        """Release any held resources."""


class SourceError(Exception):
    """Raised when a source cannot fetch or parse content."""
