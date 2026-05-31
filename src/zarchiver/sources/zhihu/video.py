"""Resolving Zhihu videos to a playable MP4 URL.

Zhihu embeds videos in article/answer/pin content as
``<a class="video-box" data-lens-id="<id>" ...>`` anchors — the actual MP4 is
not in the page. We resolve it through the lens API:

* ``GET https://lens.zhihu.com/api/v4/videos/<id>`` → ``playlist`` of quality
  variants (``FHD``/``HD``/``SD``/``LD``), each with a ``play_url`` (an MP4 on
  ``*.vzuu.com``), plus ``cover_url`` and ``title``.

The ``play_url`` carries a short-lived signature, so callers must download it
promptly (ingest does, right after fetching the page).

Like the comment fetcher, this is written against a ``get_json(url) -> dict``
callable so it stays unit-testable offline.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

log = logging.getLogger(__name__)

# GETs a URL and returns parsed JSON, or None on any failure (matches comments).
JsonGetter = Callable[[str], Optional[dict]]

_API = "https://lens.zhihu.com/api/v4/videos/{id}"

# Quality preference orders, best-first and worst-first. Resolution falls back
# along the chosen order until a variant with a usable play_url is found.
_BEST_FIRST = ("FHD", "HD", "SD", "LD")
_WORST_FIRST = ("LD", "SD", "HD", "FHD")


def resolve_video(
    get_json: JsonGetter,
    lens_id: str,
    *,
    quality: str = "FHD",
) -> Optional[dict]:
    """Resolve a Zhihu video id to a playable MP4.

    ``quality`` picks the *target* variant; if absent, resolution falls back
    toward the other end of the ladder (best-first for high targets, worst-first
    for ``LD``). Returns ``{"url", "cover", "title", "quality"}`` or None.
    """
    if not lens_id:
        return None
    payload = get_json(_API.format(id=lens_id))
    if not isinstance(payload, dict):
        return None
    playlist = payload.get("playlist")
    if not isinstance(playlist, dict) or not playlist:
        # Some responses nest under playlist_v2; tolerate either.
        playlist = payload.get("playlist_v2") if isinstance(
            payload.get("playlist_v2"), dict
        ) else None
        if not playlist:
            return None

    chosen = _pick_variant(playlist, quality)
    if not chosen:
        return None
    variant, url = chosen
    return {
        "url": url,
        "cover": payload.get("cover_url") or "",
        "title": payload.get("title") or "",
        "quality": variant,
    }


def _pick_variant(playlist: dict, quality: str) -> Optional[tuple[str, str]]:
    """Pick (variant_name, play_url) honoring the requested quality + fallback."""
    target = (quality or "FHD").upper()
    # Try the exact target first, then walk the appropriate ladder.
    order = _WORST_FIRST if target == "LD" else _BEST_FIRST
    candidates = [target] + [q for q in order if q != target]
    for name in candidates:
        variant = playlist.get(name)
        if isinstance(variant, dict):
            url = variant.get("play_url") or variant.get("playUrl")
            if isinstance(url, str) and url:
                return name, url
    return None
