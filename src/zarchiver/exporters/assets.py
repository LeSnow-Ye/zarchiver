"""Image handling for archives.

Zhihu serves images from ``pic*.zhimg.com`` and often checks the ``Referer``
header, so a naive download can fail. The downloader therefore accepts an
``httpx.Client`` configured with a Zhihu referer (the pipeline wires this up).

:func:`localize_images` rewrites ``<img>`` ``src`` attributes in a content HTML
fragment to local relative paths and returns the rewritten HTML plus the list of
(remote_url, local_path) pairs to fetch. Zhihu lazy-loads images, putting the
real URL in ``data-original``/``data-actualsrc``; we prefer those.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# A function that fetches bytes for a URL (e.g. wrapping httpx). Returns None on
# failure so a single bad image never aborts an export.
Fetcher = Callable[[str], Optional[bytes]]

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}


def _best_src(img) -> Optional[str]:
    """Pick the real image URL from a (possibly lazy-loaded) <img> tag."""
    for attr in ("data-original", "data-actualsrc", "src"):
        val = img.get(attr)
        if val and not val.startswith("data:"):
            return val
    return None


def _filename_for(url: str) -> str:
    """Deterministic local filename for an image URL (hash + extension)."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext not in _EXT_BY_MIME.values():
        ext = ""  # resolved from content-type at download time if unknown
    return f"{digest}{ext}"


def localize_images(
    html: str, rel_prefix: str
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite <img> srcs to ``{rel_prefix}/{filename}`` local paths.

    Returns:
        (rewritten_html, [(remote_url, local_filename), ...])
    """
    soup = BeautifulSoup(html, "html.parser")
    pairs: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for img in soup.find_all("img"):
        url = _best_src(img)
        if not url:
            continue
        if url not in seen:
            fname = _filename_for(url)
            seen[url] = fname
            pairs.append((url, fname))
        else:
            fname = seen[url]
        img["src"] = f"{rel_prefix}/{fname}" if rel_prefix else fname
        # Strip lazy-load attrs so the rewritten src is authoritative.
        for attr in ("data-original", "data-actualsrc", "srcset", "data-rawwidth",
                     "data-rawheight"):
            if img.has_attr(attr):
                del img[attr]
    return str(soup), pairs


def download_images(
    pairs: list[tuple[str, str]],
    dest_dir: Path,
    fetch: Fetcher,
) -> dict[str, str]:
    """Download each (url, filename) into ``dest_dir``.

    Returns a map of url -> final filename (extension may be corrected from the
    downloaded content type). Failures are skipped silently.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, str] = {}
    cached = downloaded = failed = 0
    for url, fname in pairs:
        target = dest_dir / fname
        if target.exists() and target.stat().st_size > 0:
            result[url] = fname
            cached += 1
            continue
        data = fetch(url)
        if not data:
            failed += 1
            log.debug("image download failed: %s", url)
            continue
        # Fix missing extension from content if needed.
        if not Path(fname).suffix:
            ext = _sniff_ext(data) or ".jpg"
            fname = fname + ext
            target = dest_dir / fname
        target.write_bytes(data)
        result[url] = fname
        downloaded += 1
    if pairs:
        log.debug(
            "images for %s: %d downloaded, %d cached, %d failed",
            dest_dir, downloaded, cached, failed,
        )
        if failed:
            log.warning("%d image(s) failed to download into %s", failed, dest_dir)
    return result


def _sniff_ext(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None
