"""Standalone HTML exporter.

Writes a clean, self-contained HTML file per item: a styled header with
metadata (and AI summary/tags when present) followed by the original content.
Images are localized to a sibling assets folder, or optionally inlined as
base64 for a single-file archive.
"""

from __future__ import annotations

import base64
import html as html_lib
from pathlib import Path
from typing import Optional

from zarchiver.config import HtmlConfig
from zarchiver.exporters.assets import Fetcher, download_images, localize_images
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.exporters.obsidian import _dedupe_tags, sanitize_filename
from zarchiver.models import ArchiveItem

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
  article {{ font-size: 1.05rem; }}
  figure {{ margin: 1rem 0; }}
  blockquote {{ border-left: 3px solid #ddd; margin-left: 0; padding-left: 1rem;
    color: #555; }}
  footer {{ border-top: 1px solid #eee; margin-top: 2rem; padding-top: 1rem;
    color: #999; font-size: .8rem; }}
</style>
</head>
<body>
<header>
  <h1>{title}</h1>
  <div class="meta">{meta}</div>
  {ai}
</header>
<article>
{content}
</article>
<footer>Archived by zarchiver from <a href="{url}">{url}</a></footer>
</body>
</html>
"""


class HtmlExporter(Exporter):
    name = "html"

    def __init__(self, config: HtmlConfig, *, fetch: Optional[Fetcher] = None):
        self.config = config
        self._fetch = fetch
        self.out_dir = Path(config.output_path)
        self.assets_dir = self.out_dir / "assets"

    def export(self, item: ArchiveItem) -> ExportResult:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        filename = sanitize_filename(self._basename(item))
        path = self.out_dir / f"{filename}.html"

        content_html = item.content_html
        if self._fetch is not None:
            if self.config.embed_images:
                content_html = self._inline_images(content_html)
            else:
                content_html, pairs = localize_images(content_html, "assets")
                if pairs:
                    download_images(pairs, self.assets_dir, self._fetch)

        document = _TEMPLATE.format(
            title=html_lib.escape(item.title),
            meta=self._meta_html(item),
            ai=self._ai_html(item),
            content=content_html,
            url=html_lib.escape(item.url),
        )
        path.write_text(document, encoding="utf-8")
        return ExportResult(exporter=self.name, path=path)

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

    def _inline_images(self, html: str) -> str:
        from bs4 import BeautifulSoup
        from zarchiver.exporters.assets import _best_src

        soup = BeautifulSoup(html, "html.parser")
        for img in soup.find_all("img"):
            url = _best_src(img)
            if not url:
                continue
            data = self._fetch(url) if self._fetch else None
            if not data:
                continue
            mime = _guess_mime(url)
            b64 = base64.b64encode(data).decode("ascii")
            img["src"] = f"data:{mime};base64,{b64}"
            for attr in ("data-original", "data-actualsrc", "srcset"):
                if img.has_attr(attr):
                    del img[attr]
        return str(soup)


def _guess_mime(url: str) -> str:
    u = url.lower().split("?")[0]
    if u.endswith(".png"):
        return "image/png"
    if u.endswith(".gif"):
        return "image/gif"
    if u.endswith(".webp"):
        return "image/webp"
    if u.endswith(".svg"):
        return "image/svg+xml"
    return "image/jpeg"
