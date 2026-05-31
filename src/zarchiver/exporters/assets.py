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
import shutil
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

# Lazy-load attributes Zhihu adds to <img>; stripped once we set an authoritative
# src so the rewritten value wins.
_LAZY_ATTRS = (
    "data-original", "data-actualsrc", "srcset", "data-rawwidth", "data-rawheight",
)


def _strip_lazy_attrs(img) -> None:
    for attr in _LAZY_ATTRS:
        if img.has_attr(attr):
            del img[attr]


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
        _strip_lazy_attrs(img)
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


# ---------------------------------------------------------------------- #
# Ingest: collect image URLs to download
# ---------------------------------------------------------------------- #
def collect_image_urls(html: str) -> list[str]:
    """Return the de-duplicated real image URLs referenced in ``html``.

    Used at ingest to know which images to download. Prefers lazy-load
    attributes (``data-original``/``data-actualsrc``) over ``src``.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        url = _best_src(img)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def filename_for(url: str) -> str:
    """Public, deterministic local filename for an image URL (hash + ext)."""
    return _filename_for(url)


# ---------------------------------------------------------------------- #
# Export: rewrite <img> from a pre-downloaded asset map (offline, no fetch)
# ---------------------------------------------------------------------- #
def rewrite_with_asset_map(
    html: str,
    asset_map: dict[str, str],
    rel_prefix: str,
) -> tuple[str, list[str]]:
    """Rewrite ``<img>`` srcs using a remote-URL → stored-path ``asset_map``.

    For each image whose URL is in the map, the src becomes
    ``{rel_prefix}/{filename}`` (the basename of the stored path) and the stored
    relative path is collected so the caller can copy the file into the
    exporter's assets dir. Images NOT in the map keep their remote URL as a
    graceful, fully-offline degradation (no network access here).

    Returns ``(rewritten_html, [stored_relative_path, ...])``.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    referenced: list[str] = []
    seen: set[str] = set()
    for img in soup.find_all("img"):
        url = _best_src(img)
        if not url:
            continue
        stored = asset_map.get(url)
        if stored:
            fname = Path(stored).name
            img["src"] = f"{rel_prefix}/{fname}" if rel_prefix else fname
            if stored not in seen:
                seen.add(stored)
                referenced.append(stored)
        else:
            # Not downloaded at ingest: keep the best remote URL as-is.
            img["src"] = url
        _strip_lazy_attrs(img)
    return str(soup), referenced


def copy_assets(
    stored_paths: list[str],
    assets_root: Path,
    dest_dir: Path,
) -> int:
    """Copy stored assets (paths relative to ``assets_root``) into ``dest_dir``.

    Files are copied flat (basename only) since stored filenames are URL hashes
    and so collision-free. Missing source files are skipped. Returns the number
    of files copied or already present.
    """
    if not stored_paths:
        return 0
    assets_root = Path(assets_root)
    dest_dir = Path(dest_dir)
    copied = 0
    for rel in stored_paths:
        src = assets_root / rel
        if not src.is_file():
            log.debug("stored asset missing, skipping: %s", src)
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        dst = dest_dir / Path(rel).name
        if not (dst.exists() and dst.stat().st_size > 0):
            shutil.copyfile(src, dst)
        copied += 1
    return copied


def inline_from_asset_map(
    html: str,
    asset_map: dict[str, str],
    assets_root: Path,
) -> str:
    """Inline images as base64 data URIs read from local stored assets (offline).

    Images not present locally keep their remote URL.
    """
    assets_root = Path(assets_root)
    soup = BeautifulSoup(html or "", "html.parser")
    for img in soup.find_all("img"):
        url = _best_src(img)
        if not url:
            continue
        stored = asset_map.get(url)
        if stored and (assets_root / stored).is_file():
            data = (assets_root / stored).read_bytes()
            mime = _guess_mime_from_name(stored)
            import base64

            b64 = base64.b64encode(data).decode("ascii")
            img["src"] = f"data:{mime};base64,{b64}"
        else:
            img["src"] = url
        _strip_lazy_attrs(img)
    return str(soup)


def _guess_mime_from_name(name: str) -> str:
    mime, _ = mimetypes.guess_type(name)
    return mime or "image/jpeg"

