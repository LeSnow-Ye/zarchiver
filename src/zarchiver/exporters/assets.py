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
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

class FetchStatus(str, Enum):
    OK = "ok"
    TOO_LARGE = "too_large"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: FetchStatus
    data: Optional[bytes] = None


# A function that fetches bytes for a URL (e.g. wrapping httpx). It classifies
# failures so deterministic size skips are not treated as download errors.
Fetcher = Callable[[str], FetchResult]


@dataclass(slots=True)
class DownloadOutcome:
    saved: dict[str, str]
    oversized: list[str]
    failed: list[str]

_EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
}

# Local-filename extensions we keep as-is from a URL (images + video). Anything
# else is dropped and re-derived from content at download time.
_KNOWN_EXTS = set(_EXT_BY_MIME.values()) | {".mp4", ".webm", ".mov", ".m4v"}

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
    """Deterministic local filename for a media URL (hash + extension)."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    ext = Path(url.split("?")[0]).suffix.lower()
    if ext not in _KNOWN_EXTS:
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
    *,
    concurrency: int = 1,
) -> DownloadOutcome:
    """Download each (url, filename) into ``dest_dir``.

    Returns downloaded/cached assets plus classified misses. Saved filenames may
    have a corrected extension from the downloaded content type.

    With ``concurrency > 1``, the (network-bound) ``fetch`` calls run on a thread
    pool, but results are written to disk and aggregated on the calling thread —
    so the bookkeeping stays race-free and each distinct hash filename is written
    once. ``fetch`` must be safe to call concurrently (the pipeline's httpx-based
    fetcher is). Order of the returned maps is not significant.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved: dict[str, str] = {}
    oversized: list[str] = []
    failed_urls: list[str] = []
    cached = downloaded = 0

    # Skip URLs already on disk (the previous behavior's fast path), and collect
    # the rest to fetch. De-dup by filename so the same asset isn't fetched twice.
    to_fetch: list[tuple[str, str]] = []
    seen_fnames: set[str] = set()
    for url, fname in pairs:
        target = dest_dir / fname
        if target.exists() and target.stat().st_size > 0:
            saved[url] = fname
            cached += 1
            continue
        if fname in seen_fnames:
            continue
        seen_fnames.add(fname)
        to_fetch.append((url, fname))

    def _store(url: str, fname: str, fetched: FetchResult) -> None:
        nonlocal downloaded
        if fetched.status == FetchStatus.TOO_LARGE:
            oversized.append(url)
            log.debug("image too large, keeping remote URL: %s", url)
            return
        if fetched.status != FetchStatus.OK or not fetched.data:
            failed_urls.append(url)
            log.debug("image download failed: %s", url)
            return
        data = fetched.data
        # Fix missing extension from content if needed.
        local = fname
        if not Path(local).suffix:
            ext = _sniff_ext(data) or ".jpg"
            local = local + ext
        (dest_dir / local).write_bytes(data)
        saved[url] = local
        downloaded += 1

    workers = max(1, int(concurrency))
    if workers == 1 or len(to_fetch) <= 1:
        for url, fname in to_fetch:
            _store(url, fname, fetch(url))
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(workers, len(to_fetch))) as pool:
            # Map preserves input order; fetch() runs concurrently, while every
            # _store (disk write + bookkeeping) runs here on one thread.
            results = list(pool.map(lambda uf: fetch(uf[0]), to_fetch))
        for (url, fname), fetched in zip(to_fetch, results):
            _store(url, fname, fetched)

    if pairs:
        failed = len(failed_urls)
        too_large = len(oversized)
        log.info(
            "images for %s: %d downloaded, %d cached, %d too-large "
            "(kept remote), %d failed",
            dest_dir, downloaded, cached, too_large, failed,
        )
        if too_large:
            log.info("%d image(s) too large in %s; kept remote URLs", too_large, dest_dir)
        if failed:
            log.warning("%d image(s) failed to download into %s", failed, dest_dir)
    return DownloadOutcome(saved=saved, oversized=oversized, failed=failed_urls)


def _sniff_ext(data: bytes) -> Optional[str]:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    # ISO base media (mp4): a 'ftyp' box near the start.
    if data[4:8] == b"ftyp":
        return ".mp4"
    # WebM/Matroska EBML header.
    if data[:4] == b"\x1aE\xdf\xa3":
        return ".webm"
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


def _video_urls(tag) -> list[str]:
    """All downloadable URLs on a <video>: poster, src, and <source> children."""
    found: list[str] = []
    poster = tag.get("poster")
    if poster and not poster.startswith("data:"):
        found.append(poster)
    src = tag.get("src")
    if src and not src.startswith("data:"):
        found.append(src)
    for source in tag.find_all("source"):
        s = source.get("src")
        if s and not s.startswith("data:"):
            found.append(s)
    return found


def collect_media_urls(html: str) -> list[str]:
    """De-duplicated URLs of every downloadable asset in ``html``.

    Covers ``<img>`` (via :func:`collect_image_urls`) plus ``<video>`` posters,
    ``src`` attributes, and ``<source>`` children — so ingest downloads videos
    and their poster frames alongside images.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    urls: list[str] = []
    seen: set[str] = set()

    def add(url: Optional[str]) -> None:
        if url and url not in seen:
            seen.add(url)
            urls.append(url)

    for img in soup.find_all("img"):
        add(_best_src(img))
    for vid in soup.find_all("video"):
        for u in _video_urls(vid):
            add(u)
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
    """Rewrite ``<img>``/``<video>`` srcs using a remote-URL → stored-path map.

    For each media URL in the map, the attribute becomes ``{rel_prefix}/{name}``
    (the basename of the stored path) and the stored relative path is collected
    so the caller can copy the file into the exporter's assets dir. URLs NOT in
    the map keep their remote value as a graceful, fully-offline degradation (no
    network access here). Covers ``<img src>``, ``<video src>``,
    ``<video poster>``, and ``<source src>``.

    Returns ``(rewritten_html, [stored_relative_path, ...])``.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    referenced: list[str] = []
    seen: set[str] = set()

    def local_for(url: str) -> Optional[str]:
        stored = asset_map.get(url)
        if not stored:
            return None
        if stored not in seen:
            seen.add(stored)
            referenced.append(stored)
        name = Path(stored).name
        return f"{rel_prefix}/{name}" if rel_prefix else name

    for img in soup.find_all("img"):
        url = _best_src(img)
        if not url:
            continue
        img["src"] = local_for(url) or url
        _strip_lazy_attrs(img)

    for vid in soup.find_all("video"):
        if vid.get("poster"):
            vid["poster"] = local_for(vid["poster"]) or vid["poster"]
        if vid.get("src"):
            vid["src"] = local_for(vid["src"]) or vid["src"]
        for source in vid.find_all("source"):
            if source.get("src"):
                source["src"] = local_for(source["src"]) or source["src"]

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
