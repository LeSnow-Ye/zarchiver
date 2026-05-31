"""Obsidian markdown exporter.

Writes each item as a markdown note with YAML frontmatter into a vault folder.
Images are downloaded into an assets folder and links rewritten to relative
paths so the note renders offline in Obsidian.

The Obsidian *CLI* is supported only as an optional path (``use_cli``); it
requires the desktop app to be running, so the default and primary path is
writing files straight into the vault directory, which works headless.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml
from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert

from zarchiver.config import ObsidianConfig
from zarchiver.exporters.assets import copy_assets, rewrite_with_asset_map
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.exporters.comments import comments_markdown_fragment
from zarchiver.exporters.formulas import (
    extract_formulas_for_markdown,
    restore_formulas_markdown,
)
from zarchiver.models import ArchiveItem

log = logging.getLogger(__name__)

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

    def __init__(
        self,
        config: ObsidianConfig,
        *,
        assets_root: Optional[str] = None,
        subdir_override: Optional[str] = None,
    ):
        self.config = config
        # Root of the DB-managed asset store (where ingest saved images). When
        # None, images are not localized and remote URLs are kept as-is.
        self.assets_root = Path(assets_root) if assets_root else None
        # An explicit subdir (from --subdir) forces all items into this folder,
        # regardless of batch context.
        self._subdir_override = subdir_override
        self.vault = Path(config.vault_path)
        self.base_notes_dir = self.vault / config.folder
        self.base_assets_dir = self.vault / config.assets_folder

    # ------------------------------------------------------------------ #
    def _subdir_for(self, item: ArchiveItem) -> str:
        """The per-item subdirectory (possibly empty) under the base folders."""
        if self._subdir_override is not None:
            return sanitize_filename(self._subdir_override) if self._subdir_override else ""
        if self.config.batch_subdirs and item.batch is not None:
            return sanitize_filename(item.batch.title)
        return ""

    def _dirs_for(self, item: ArchiveItem) -> tuple[Path, Path]:
        """Resolve (notes_dir, assets_dir) for an item, applying any subdir.

        Without a batch subdir, assets go in the configured ``assets_folder``.
        With a batch subdir, the note lives in ``<folder>/<batch>/`` and its
        assets nest beside it in ``<folder>/<batch>/assets/`` — so each batch is
        a self-contained directory (matching the HTML exporter's layout).
        """
        subdir = self._subdir_for(item)
        if subdir:
            notes_dir = self.base_notes_dir / subdir
            assets_dir = notes_dir / "assets"
        else:
            notes_dir = self.base_notes_dir
            assets_dir = self.base_assets_dir
        return notes_dir, assets_dir

    # ------------------------------------------------------------------ #
    def target_path(self, item: ArchiveItem) -> Path:
        """Where the note for ``item`` will be written."""
        notes_dir, _ = self._dirs_for(item)
        return notes_dir / f"{self._filename_for(item)}.md"

    def export(self, item: ArchiveItem) -> ExportResult:
        notes_dir, assets_dir = self._dirs_for(item)
        notes_dir.mkdir(parents=True, exist_ok=True)
        filename = self._filename_for(item)
        note_path = notes_dir / f"{filename}.md"

        # Prepend the article title image (if any) as the first content block.
        body_html = item.content_html
        if item.title_image:
            body_html = (
                f'<img src="{item.title_image}" alt="{item.title}"/>' + body_html
            )

        # Append recorded comments (becomes blockquote threads in markdown).
        body_html += comments_markdown_fragment(item)

        # Pull formulas out before image localization + markdown conversion so
        # they become real LaTeX ($...$) instead of downloaded images, and so
        # markdownify can't escape LaTeX-significant characters.
        body_html, formulas = extract_formulas_for_markdown(body_html)

        # Rewrite <img>/<video> from the pre-downloaded asset map (offline).
        # Media not in the map keeps its remote URL as a graceful degradation.
        if self.config.download_images and self.assets_root is not None:
            rel_prefix = self._assets_rel_prefix(notes_dir, assets_dir)
            body_html, refs = rewrite_with_asset_map(
                body_html, item.asset_map, rel_prefix
            )
            if refs:
                copy_assets(refs, self.assets_root, assets_dir)

        # markdownify drops <video>; convert each to an Obsidian embed/link first
        # so videos survive into the note.
        body_html = _videos_to_embeds(body_html)

        body_md = md_convert(body_html, heading_style="ATX", bullets="-")
        body_md = restore_formulas_markdown(body_md, formulas)
        body_md = _tidy_markdown(body_md)
        document = self._frontmatter(item) + "\n" + body_md + "\n"

        if self.config.use_cli and shutil.which("obsidian"):
            return self._export_via_cli(item, filename, document, notes_dir)

        note_path.write_text(document, encoding="utf-8")
        log.debug("wrote markdown note: %s", note_path)
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

    def _assets_rel_prefix(self, notes_dir: Path, assets_dir: Path) -> str:
        """Relative path from a note's folder to its assets folder."""
        try:
            import os

            return os.path.relpath(assets_dir, notes_dir).replace("\\", "/")
        except ValueError:
            return str(assets_dir)

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
        # The column (专栏) an article belongs to, if any.
        if item.column_title:
            fm["column"] = item.column_title
            if item.column_url:
                fm["column_url"] = item.column_url
        # The collection (收藏夹) / column / question this was archived from.
        if item.batch is not None:
            fm[item.batch.kind.value] = item.batch.title
            if item.batch.url:
                fm[f"{item.batch.kind.value}_url"] = item.batch.url
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
        self, item: ArchiveItem, filename: str, document: str, notes_dir: Path
    ) -> ExportResult:
        """Create the note through the Obsidian CLI (optional path)."""
        # Path relative to the vault root, including any batch subdir.
        rel = (notes_dir / f"{filename}.md").relative_to(self.vault).as_posix()
        cmd = ["obsidian", "create", f"path={rel}", f"content={document}",
               "overwrite"]
        if self.config.cli_vault_name:
            cmd.insert(1, f"vault={self.config.cli_vault_name}")
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30)
            return ExportResult(
                exporter=self.name, path=notes_dir / f"{filename}.md",
                detail="via obsidian cli",
            )
        except (subprocess.SubprocessError, OSError) as exc:
            # Fall back to a direct file write if the CLI fails.
            path = notes_dir / f"{filename}.md"
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


def _videos_to_embeds(html: str) -> str:
    """Replace ``<video>`` tags with Obsidian-friendly markup before markdownify.

    markdownify silently drops ``<video>``. A locally-stored video (its ``src``
    already rewritten to a relative ``assets/...`` path) becomes an Obsidian
    embed ``![[assets/x.mp4]]`` (Obsidian plays embedded video); a still-remote
    video becomes a plain link so it isn't lost. A poster image, if present, is
    kept as a preview image above it.
    """
    if "<video" not in html:
        return html
    soup = BeautifulSoup(html, "html.parser")
    for vid in soup.find_all("video"):
        src = vid.get("src")
        if not src:
            source = vid.find("source")
            src = source.get("src") if source else None
        poster = vid.get("poster")
        replacement = []
        is_local = bool(src) and not src.startswith(("http://", "https://"))
        if poster and not poster.startswith(("http://", "https://")):
            replacement.append(f"![]({poster})")
        if src:
            if is_local:
                replacement.append(f"![[{src}]]")
            else:
                replacement.append(f"[🎬 视频]({src})")
        else:
            replacement.append("🎬 视频")
        vid.replace_with(BeautifulSoup("\n\n".join(replacement), "html.parser"))
    return str(soup)
