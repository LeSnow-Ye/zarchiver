"""Standalone HTML exporter.

Writes a clean, self-contained HTML file per item: a styled header with
metadata (and AI summary/tags when present) followed by the original content.
Images are localized to a sibling assets folder, or optionally inlined as
base64 for a single-file archive.
"""

from __future__ import annotations

import html as html_lib
import logging
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

from zarchiver.config import HtmlConfig
from zarchiver.exporters.assets import (
    copy_assets,
    inline_from_asset_map,
    rewrite_with_asset_map,
)
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.exporters.comments import comments_html_fragment
from zarchiver.exporters.formulas import render_formulas_html
from zarchiver.exporters.obsidian import _dedupe_tags, sanitize_filename
from zarchiver.models import ArchiveItem

log = logging.getLogger(__name__)

_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  body {{ max-width: 820px; margin: 2rem auto; padding: 0 1rem;
    font-family: -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei",
    sans-serif; line-height: 1.7; color: #1a1a1a; }}
  header {{ border-bottom: 2px solid #eee; padding-bottom: 1rem;
    margin-bottom: 1.5rem; }}
  h1 {{ font-size: 1.6rem; margin: 0 0 .5rem; }}
  .meta {{ color: #666; font-size: .9rem; }}
  .meta a {{ color: #0066cc; text-decoration: none; }}
  .ai-box {{ background: #f6f8fa; border-left: 4px solid #0066cc;
    padding: .75rem 1rem; margin: 1rem 0; border-radius: 4px; font-size: .95rem; }}
  .ai-box .label {{ font-weight: 600; color: #0066cc; }}
  .tags span {{ display: inline-block; background: #eef; color: #336;
    border-radius: 10px; padding: 1px 10px; margin: 2px; font-size: .8rem; }}
  article img {{ max-width: 100%; height: auto; }}
  article video {{ max-width: 100%; height: auto; display: block;
    margin: 1rem 0; background: #000; border-radius: 6px; }}
  article {{ font-size: 1.05rem; }}
  .title-image {{ width: 100%; max-height: 420px; object-fit: cover;
    border-radius: 6px; margin: 1rem 0; }}
  figure {{ margin: 1rem 0; }}
  blockquote {{ border-left: 3px solid #ddd; margin-left: 0; padding-left: 1rem;
    color: #555; }}
  .reference-list {{ font-size: .9rem; color: #444; }}
  .reference-list a {{ color: #0066cc; word-break: break-all; }}
  .ref-marker {{ color: #0066cc; text-decoration: none; vertical-align: super;
    font-size: .75em; }}
  .comments {{ border-top: 2px solid #eee; margin-top: 2.5rem;
    padding-top: 1rem; }}
  .comments h2 {{ font-size: 1.2rem; }}
  .comment {{ margin: .9rem 0; padding: .6rem .9rem; background: #fafafa;
    border-radius: 6px; font-size: .95rem; }}
  .comment-meta {{ color: #888; font-size: .8rem; margin-bottom: .25rem; }}
  .comment-body {{ color: #222; }}
  .comment-body img {{ max-width: 100%; height: auto; }}
  .comment-children {{ margin: .5rem 0 0 1rem; padding-left: .8rem;
    border-left: 2px solid #e3e3e3; }}
  .comment-children .comment {{ background: #f3f3f3; }}
  footer {{ border-top: 1px solid #eee; margin-top: 2rem; padding-top: 1rem;
    color: #999; font-size: .8rem; }}
</style>
{mathjax}
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  {ai}
</header>
{title_image}
<article>
{content}
</article>
<footer>Archived by zarchiver from <a href="{url}">{url}</a></footer>
</body>
</html>
"""

# Loaded only when an item actually contains formulas.
_MATHJAX = """<script>
window.MathJax = {
  tex: { inlineMath: [['\\\\(', '\\\\)']], displayMath: [['\\\\[', '\\\\]']] },
  options: { skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre'] }
};
</script>
<script async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>"""


class HtmlExporter(Exporter):
    name = "html"

    def __init__(
        self,
        config: HtmlConfig,
        *,
        assets_root: Optional[str] = None,
        subdir_override: Optional[str] = None,
    ):
        self.config = config
        self.assets_root = Path(assets_root) if assets_root else None
        self._subdir_override = subdir_override
        self.base_out_dir = Path(config.output_path)

    # ------------------------------------------------------------------ #
    def _subdir_for(self, item: ArchiveItem) -> str:
        if self._subdir_override is not None:
            return sanitize_filename(self._subdir_override) if self._subdir_override else ""
        if self.config.batch_subdirs and item.batch is not None:
            return sanitize_filename(item.batch.title)
        return ""

    def _dirs_for(self, item: ArchiveItem) -> tuple[Path, Path]:
        """Resolve (out_dir, assets_dir) for an item, applying any subdir.

        Assets always live in an ``assets`` folder beside the HTML file, so a
        note in a batch subdir gets ``<subdir>/assets`` and the relative link
        stays ``assets/...``.
        """
        subdir = self._subdir_for(item)
        out_dir = self.base_out_dir / subdir if subdir else self.base_out_dir
        return out_dir, out_dir / "assets"

    def target_path(self, item: ArchiveItem) -> Path:
        """Where the HTML page for ``item`` will be written."""
        out_dir, _ = self._dirs_for(item)
        return out_dir / f"{sanitize_filename(self._basename(item))}.html"

    def export(self, item: ArchiveItem) -> ExportResult:
        out_dir, assets_dir = self._dirs_for(item)
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(self._basename(item))
        path = out_dir / f"{filename}.html"

        # Render formulas to MathJax delimiters before image handling (so the
        # ztex spans aren't treated as images). Comments are appended as a
        # styled section and localized along with the body.
        full_html = item.content_html + comments_html_fragment(item)
        soup = BeautifulSoup(full_html, "html.parser")
        has_formulas = render_formulas_html(soup)
        content_html = str(soup)

        # Rewrite media offline from the pre-downloaded asset map. Either inline
        # images as base64 (self-contained) or rewrite to a sibling assets/
        # folder and copy the stored files in. Videos are never base64-inlined
        # (too large), so even in embed mode they're copied to assets/ and
        # referenced relatively. Media missing from the map keeps remote URLs.
        if self.assets_root is not None:
            if self.config.embed_images:
                content_html = inline_from_asset_map(
                    content_html, item.asset_map, self.assets_root
                )
                # Still localize <video> (and posters) to local files.
                content_html, refs = rewrite_with_asset_map(
                    content_html, item.asset_map, "assets"
                )
                if refs:
                    copy_assets(refs, self.assets_root, assets_dir)
            else:
                content_html, refs = rewrite_with_asset_map(
                    content_html, item.asset_map, "assets"
                )
                if refs:
                    copy_assets(refs, self.assets_root, assets_dir)

        document = _TEMPLATE.format(
            title=html_lib.escape(item.title),
            meta=self._meta_html(item),
            ai=self._ai_html(item),
            title_image=self._title_image_html(item, assets_dir),
            content=content_html,
            url=html_lib.escape(item.url),
            mathjax=_MATHJAX if has_formulas else "",
        )
        path.write_text(document, encoding="utf-8")
        log.debug("wrote HTML page: %s (mathjax=%s)", path, has_formulas)
        return ExportResult(exporter=self.name, path=path)

    # ------------------------------------------------------------------ #
    def _title_image_html(self, item: ArchiveItem, assets_dir: Path) -> str:
        """Render the article title image as a banner, localized if possible."""
        if not item.title_image:
            return ""
        src = item.title_image
        stored = item.asset_map.get(item.title_image) if self.assets_root else None
        if stored:
            if self.config.embed_images:
                inlined = inline_from_asset_map(
                    f'<img src="{item.title_image}">', item.asset_map,
                    self.assets_root,
                )
                m = re.search(r'src="([^"]+)"', inlined)
                if m:
                    src = m.group(1)
            else:
                copy_assets([stored], self.assets_root, assets_dir)
                src = f"assets/{Path(stored).name}"
        return (
            f'<img class="title-image" src="{html_lib.escape(src)}" '
            f'alt="{html_lib.escape(item.title)}">'
        )

    # ------------------------------------------------------------------ #
    def _basename(self, item: ArchiveItem) -> str:
        author = item.author.name if item.author else "unknown"
        return f"{item.title} - {author} ({item.source_id})"

    def _meta_html(self, item: ArchiveItem) -> str:
        parts = []
        if item.author:
            if item.author.url:
                parts.append(
                    f'<a href="{html_lib.escape(item.author.url)}">'
                    f"{html_lib.escape(item.author.name)}</a>"
                )
            else:
                parts.append(html_lib.escape(item.author.name))
        if item.created:
            parts.append(item.created.strftime("%Y-%m-%d"))
        if item.voteup_count is not None:
            parts.append(f"▲ {item.voteup_count}")
        if item.comment_count is not None:
            parts.append(f"💬 {item.comment_count}")
        return " · ".join(parts)

    def _ai_html(self, item: ArchiveItem) -> str:
        if item.ai.is_empty():
            return ""
        bits = []
        if item.ai.summary:
            bits.append(
                f'<div class="ai-box"><span class="label">AI 摘要：</span>'
                f"{html_lib.escape(item.ai.summary)}</div>"
            )
        tags = _dedupe_tags(list(item.topics) + list(item.ai.tags))
        if tags or item.ai.category:
            spans = ""
            if item.ai.category:
                spans += f"<span>📁 {html_lib.escape(item.ai.category)}</span>"
            spans += "".join(
                f"<span>{html_lib.escape(t)}</span>" for t in tags
            )
            bits.append(f'<div class="tags">{spans}</div>')
        return "\n".join(bits)

