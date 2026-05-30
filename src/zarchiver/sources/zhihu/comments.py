"""Fetching comments from Zhihu's ``comment_v5`` API.

Comments are not in the page's ``js-initialData``; Zhihu loads them lazily from
a JSON API. We call it through the browser context (so cookies and headers
apply) and parse the result into platform-neutral :class:`Comment` objects.

Endpoints (all under ``/api/v4/comment_v5``):

* root comments: ``/{resource_type}/{id}/root_comment?order_by=score&limit=N``
* child replies:  ``/comment/{root_id}/child_comment?order_by=ts&limit=N``

where ``resource_type`` is ``articles`` / ``answers`` / ``pins``. Each root
comment embeds its first few replies in ``child_comments`` and reports the total
in ``child_comment_count``; we fetch the remainder only when budget allows.

Comments are threaded a single level deep (a root comment plus direct replies),
which matches Zhihu's own model.

The fetcher is written against a ``get_json(url) -> dict | None`` callable
rather than a browser, so it is fully unit-testable offline.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from zarchiver.models import Author, Comment, ContentType

log = logging.getLogger(__name__)

# A function that GETs a URL and returns parsed JSON, or None on any failure.
JsonGetter = Callable[[str], Optional[dict]]

_API_ROOT = "https://www.zhihu.com/api/v4/comment_v5"

# ArchiveItem content types -> Zhihu API resource path segments.
_RESOURCE_TYPE = {
    ContentType.ARTICLE: "articles",
    ContentType.ANSWER: "answers",
    ContentType.PIN: "pins",
}

_PAGE_LIMIT = 20  # comments per API request


def resource_type_for(content_type: ContentType) -> Optional[str]:
    """Map a content type to its comment API resource segment, or None."""
    return _RESOURCE_TYPE.get(content_type)


def fetch_comments(
    get_json: JsonGetter,
    resource_type: str,
    resource_id: str,
    *,
    max_comments: int = 100,
    order_by: str = "score",
) -> list[Comment]:
    """Fetch up to ``max_comments`` comments (root + replies) for an item.

    The cap counts *every* recorded comment, root and child alike, so a single
    popular thread can't blow past the limit. ``max_comments <= 0`` means
    unlimited. Root comments are pulled in ``order_by`` order (``score`` keeps
    the most-liked when truncating); replies are pulled oldest-first to keep a
    readable thread.

    Returns root :class:`Comment` objects, each with its ``children`` populated.
    """
    if max_comments == 0:
        budget = None  # unlimited
    elif max_comments < 0:
        return []
    else:
        budget = max_comments

    roots: list[Comment] = []
    recorded = 0
    url: Optional[str] = (
        f"{_API_ROOT}/{resource_type}/{resource_id}/root_comment"
        f"?order_by={order_by}&limit={_PAGE_LIMIT}&offset="
    )
    pages = 0
    while url is not None:
        if budget is not None and recorded >= budget:
            break
        data = get_json(url)
        if not data:
            break
        pages += 1
        for raw in data.get("data", []) or []:
            if budget is not None and recorded >= budget:
                break
            if not isinstance(raw, dict) or raw.get("is_delete"):
                continue
            root = _parse_comment(raw)
            recorded += 1
            # Fill in replies with whatever budget remains.
            remaining = None if budget is None else budget - recorded
            if remaining is None or remaining > 0:
                children = _collect_children(get_json, raw, remaining)
                root.children = children
                recorded += sum(c.total_count() for c in children)
            roots.append(root)
        paging = data.get("paging") or {}
        url = None if paging.get("is_end", True) else paging.get("next")
    log.info(
        "fetched %d comment(s) (%d root, %d page(s)) for %s/%s",
        recorded, len(roots), pages, resource_type, resource_id,
    )
    return roots


def _collect_children(
    get_json: JsonGetter, root_raw: dict, remaining: Optional[int]
) -> list[Comment]:
    """Collect a root comment's replies, capped by ``remaining`` budget.

    Embedded replies (``child_comments``) are used first; if the root has more
    than were embedded and budget is left, the rest are paged from the child
    endpoint (oldest-first).
    """
    if remaining is not None and remaining <= 0:
        return []
    children: list[Comment] = []
    seen: set[str] = set()

    def add(raw: dict) -> bool:
        """Append a child; return False when the budget is exhausted."""
        if remaining is not None and len(children) >= remaining:
            return False
        if not isinstance(raw, dict) or raw.get("is_delete"):
            return True
        cid = str(raw.get("id") or "")
        if cid and cid in seen:
            return True
        seen.add(cid)
        children.append(_parse_comment(raw))
        return not (remaining is not None and len(children) >= remaining)

    for raw in root_raw.get("child_comments", []) or []:
        if not add(raw):
            return children

    total = root_raw.get("child_comment_count") or 0
    embedded = len(root_raw.get("child_comments", []) or [])
    if total <= embedded:
        return children
    # More replies exist than were embedded: page the child endpoint.
    root_id = str(root_raw.get("id") or "")
    if not root_id:
        return children
    url: Optional[str] = (
        f"{_API_ROOT}/comment/{root_id}/child_comment"
        f"?order_by=ts&limit={_PAGE_LIMIT}&offset="
    )
    while url is not None:
        if remaining is not None and len(children) >= remaining:
            break
        data = get_json(url)
        if not data:
            break
        for raw in data.get("data", []) or []:
            if not add(raw):
                return children
        paging = data.get("paging") or {}
        url = None if paging.get("is_end", True) else paging.get("next")
    return children


def _parse_comment(raw: dict) -> Comment:
    """Build a (childless) :class:`Comment` from a raw API comment dict."""
    author = _parse_author(raw.get("author"))
    return Comment(
        id=str(raw.get("id") or ""),
        content_html=raw.get("content") or "",
        author=author,
        created=_epoch(raw.get("created_time")),
        like_count=raw.get("like_count"),
    )


def _parse_author(raw) -> Optional[Author]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or "知乎用户"
    token = raw.get("url_token") or raw.get("id")
    url = f"https://www.zhihu.com/people/{token}" if token else None
    return Author(
        name=name,
        url=url,
        headline=raw.get("headline") or None,
        id=str(raw.get("id")) if raw.get("id") else None,
    )


def _epoch(value):
    from zarchiver.models import ArchiveItem

    return ArchiveItem.epoch_to_dt(value)
