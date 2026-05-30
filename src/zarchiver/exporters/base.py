"""Exporter abstraction.

An *exporter* persists an :class:`~zarchiver.models.ArchiveItem` to some output
format. The pipeline holds a list of exporters and fans each item out to all of
them, so adding a new output format means writing one ``Exporter`` subclass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from zarchiver.models import ArchiveItem


@dataclass(slots=True)
class ExportResult:
    exporter: str
    path: Optional[Path] = None
    skipped: bool = False
    detail: str = ""


class Exporter(ABC):
    """Base class for all exporters."""

    #: Short identifier, e.g. ``"obsidian"`` or ``"html"``.
    name: str = "base"

    @abstractmethod
    def export(self, item: ArchiveItem) -> ExportResult:
        """Write ``item`` and return where it went."""

    def target_path(self, item: ArchiveItem) -> Optional[Path]:
        """Where :meth:`export` would write ``item``, without writing it.

        Used for duplicate detection (does the output already exist?). Returns
        None if this exporter can't predict a path; such exporters never count
        as already-archived.
        """
        return None

    def already_exists(self, item: ArchiveItem) -> bool:
        """True if this exporter's output for ``item`` is already on disk."""
        path = self.target_path(item)
        return path is not None and path.exists()

