"""Obsidian markdown exporter.

Writes each item as a markdown note with YAML frontmatter into a vault folder.
Images are downloaded into an assets folder and links rewritten to relative
paths so the note renders offline in Obsidian.

The Obsidian *CLI* is supported only as an optional path (``use_cli``); it
requires the desktop app to be running, so the default and primary path is
writing files straight into the vault directory, which works headless.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from markdownify import markdownify as md_convert

from zarchiver.config import ObsidianConfig
from zarchiver.exporters.assets import Fetcher, download_images, localize_images
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.exporters.formulas import (
    extract_formulas_for_markdown,
    restore_formulas_markdown,
)
from zarchiver.models import ArchiveItem

# Characters not allowed in file names on common filesystems / Obsidian.
_ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]]')


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = _ILLEGAL.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"


class ObsidianExporter(Exporter):
    name = "obsidian"

    def __init__(self, config: ObsidianConfig, *, fetch: Optional[Fetcher] = None):
        self.config = config
        self._fetch = fetch  # image fetcher; if None, images are not downloaded
        self.vault = Path(config.vault_path)
        self.notes_dir = self.vault / config.folder
        self.assets_dir = self.vault / config.assets_folder

    # ------------------------------------------------------------------ #
    def export(self, item: ArchiveItem) -> ExportResult:
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        filename = self._filename_for(item)
        note_path = self.notes_dir / f"{filename}.md"

        # Prepend the article title image (if any) as the first content block.
        body_html = item.content_html
        if item.title_image:
            body_html = (
                f'<img src="{item.title_image}" alt="{item.title}"/>' + body_html
            )

        # Pull formulas out before image localization + markdown conversion so
        # they become real LaTeX ($...$) instead of downloaded images, and so
        # markdownify can't escape LaTeX-significant characters.
        body_html, formulas = extract_formulas_for_markdown(body_html)

        if self.config.download_images and self._fetch is not None:
            # Relative path from a note in notes_dir to the assets dir.
            rel_prefix = self._assets_rel_prefix()
            body_html, pairs = localize_images(body_html, rel_prefix)
            if pairs:
                download_images(pairs, self.assets_dir, self._fetch)

        body_md = md_convert(body_html, heading_style="ATX", bullets="-")
        body_md = restore_formulas_markdown(body_md, formulas)
        body_md = _tidy_markdown(body_md)
        document = self._frontmatter(item) + "\n" + body_md + "\n"

        if self.config.use_cli and shutil.which("obsidian"):
            return self._export_via_cli(item, filename, document)

        note_path.write_text(document, encoding="utf-8")
        return ExportResult(exporter=self.name, path=note_path)

    # ------------------------------------------------------------------ #
    def _filename_for(self, item: ArchiveItem) -> str:
        author = item.author.name if item.author else "unknown"
        date = item.created.strftime("%Y-%m-%d") if item.created else ""
        raw = self.config.filename_template.format(
            title=item.title,
            author=author,
            source_id=item.source_id,
            content_type=item.content_type.value,
            date=date,
        )
        return sanitize_filename(raw)

    def _assets_rel_prefix(self) -> str:
        """Relative path from the notes folder to the assets folder."""
        try:
            import os

            return os.path.relpath(self.assets_dir, self.notes_dir).replace("\\", "/")
        except ValueError:
            return str(self.assets_dir)

    def _frontmatter(self, item: ArchiveItem) -> str:
        fm: dict = {
            "title": item.title,
            "platform": item.platform,
            "type": item.content_type.value,
            "source_id": item.source_id,
            "url": item.url,
        }
        if item.author:
            fm["author"] = item.author.name
            if item.author.url:
                fm["author_url"] = item.author.url
        if item.question_url:
            fm["question_url"] = item.question_url
        if item.created:
            fm["created"] = item.created.strftime("%Y-%m-%d %H:%M:%S")
        if item.updated:
            fm["updated"] = item.updated.strftime("%Y-%m-%d %H:%M:%S")
        if item.voteup_count is not None:
            fm["voteup"] = item.voteup_count
        if item.comment_count is not None:
            fm["comments"] = item.comment_count
        # AI + topic tags merged for Obsidian's tag system.
        tags = list(item.topics)
        if item.ai.tags:
            tags.extend(item.ai.tags)
        if tags:
            fm["tags"] = _dedupe_tags(tags)
        if item.ai.category:
            fm["category"] = item.ai.category
        if item.ai.summary:
            fm["summary"] = item.ai.summary
        fm["archived_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        dumped = yaml.safe_dump(
            fm, allow_unicode=True, sort_keys=False, default_flow_style=False
        )
        return f"---\n{dumped}---\n"

    def _export_via_cli(
        self, item: ArchiveItem, filename: str, document: str
    ) -> ExportResult:
        """Create the note through the Obsidian CLI (optional path)."""
        rel = f"{self.config.folder}/{filename}.md"
        cmd = ["obsidian", "create", f"path={rel}", f"content={document}",
               "overwrite"]
        if self.config.cli_vault_name:
            cmd.insert(1, f"vault={self.config.cli_vault_name}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            return ExportResult(
                exporter=self.name, path=self.notes_dir / f"{filename}.md",
                detail="via obsidian cli",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # Fall back to a direct file write if the CLI fails.
            path = self.notes_dir / f"{filename}.md"
            path.write_text(document, encoding="utf-8")
            return ExportResult(
                exporter=self.name, path=path,
                detail=f"cli failed ({exc}); wrote file directly",
            )


def _dedupe_tags(tags: list[str]) -> list[str]:
    seen, out = set(), []
    for t in tags:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def _tidy_markdown(md: str) -> str:
    """Collapse excessive blank lines markdownify can leave behind."""
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()
