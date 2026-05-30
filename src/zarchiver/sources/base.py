"""Source abstraction.

A *source* knows how to turn URLs from one platform into normalized
:class:`~zarchiver.models.ArchiveItem` objects. The pipeline depends only on
this interface, so adding a platform is a matter of writing a new ``Source``
subclass — nothing downstream changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from zarchiver.models import ArchiveItem


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
    def fetch_batch(self, url: str) -> Iterator[ArchiveItem]:
        """Yield items for a batch URL (collection, column, question, ...).

        Implementations should yield lazily so the pipeline can archive each
        item as it arrives rather than buffering the whole batch.
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
