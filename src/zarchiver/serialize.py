"""(De)serialization between :class:`ArchiveItem` and JSON-friendly dicts.

The DB is the system of record for every archived item, so the full
``ArchiveItem`` — including its threaded comments, AI result, batch context,
asset map, and the original parsed ``raw`` dict — must round-trip losslessly
through SQLite. SQLite holds the scalar columns directly and the nested
structures as JSON text; this module is the single place that knows how to turn
each dataclass into JSON and back.

Round-trip invariant: ``item_from_row(row_from_item(item))`` reconstructs an
``ArchiveItem`` whose :meth:`~zarchiver.models.ArchiveItem.content_hash` equals
the original's, so dedup keys stay stable across a store reload.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from zarchiver.models import (
    AIResult,
    ArchiveItem,
    Author,
    BatchInfo,
    BatchKind,
    Comment,
    ContentType,
)


# ---------------------------------------------------------------------- #
# Scalars
# ---------------------------------------------------------------------- #
def dt_to_str(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime to ISO 8601, preserving timezone."""
    return value.isoformat() if value is not None else None


def dt_from_str(value: Optional[str]) -> Optional[datetime]:
    """Parse an ISO 8601 string back to a (tz-aware) datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    # Sources produce tz-aware UTC datetimes; default any naive value to UTC so
    # the round-trip never silently drops the zone.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _json_loads(value: Optional[str]) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------- #
# Author
# ---------------------------------------------------------------------- #
def author_to_dict(author: Optional[Author]) -> Optional[dict]:
    if author is None:
        return None
    return {
        "name": author.name,
        "url": author.url,
        "headline": author.headline,
        "id": author.id,
    }


def author_from_dict(data: Optional[dict]) -> Optional[Author]:
    if not isinstance(data, dict):
        return None
    return Author(
        name=data.get("name", ""),
        url=data.get("url"),
        headline=data.get("headline"),
        id=data.get("id"),
    )


# ---------------------------------------------------------------------- #
# Comment tree (recursive)
# ---------------------------------------------------------------------- #
def comment_to_dict(comment: Comment) -> dict:
    return {
        "id": comment.id,
        "content_html": comment.content_html,
        "author": author_to_dict(comment.author),
        "created": dt_to_str(comment.created),
        "like_count": comment.like_count,
        "children": [comment_to_dict(c) for c in comment.children],
    }


def comment_from_dict(data: dict) -> Comment:
    return Comment(
        id=str(data.get("id", "")),
        content_html=data.get("content_html", ""),
        author=author_from_dict(data.get("author")),
        created=dt_from_str(data.get("created")),
        like_count=data.get("like_count"),
        children=[
            comment_from_dict(c)
            for c in (data.get("children") or [])
            if isinstance(c, dict)
        ],
    )


# ---------------------------------------------------------------------- #
# AIResult
# ---------------------------------------------------------------------- #
def ai_to_dict(ai: AIResult) -> dict:
    return {
        "summary": ai.summary,
        "tags": list(ai.tags),
        "category": ai.category,
        "model": ai.model,
    }


def ai_from_dict(data: Optional[dict]) -> AIResult:
    if not isinstance(data, dict):
        return AIResult()
    tags = data.get("tags") or []
    if not isinstance(tags, list):
        tags = []
    return AIResult(
        summary=data.get("summary", ""),
        tags=[str(t) for t in tags],
        category=data.get("category", ""),
        model=data.get("model", ""),
    )


# ---------------------------------------------------------------------- #
# BatchInfo
# ---------------------------------------------------------------------- #
def batch_to_dict(batch: Optional[BatchInfo]) -> Optional[dict]:
    if batch is None:
        return None
    return {
        "kind": batch.kind.value,
        "title": batch.title,
        "url": batch.url,
        "id": batch.id,
    }


def batch_from_dict(data: Optional[dict]) -> Optional[BatchInfo]:
    if not isinstance(data, dict):
        return None
    try:
        kind = BatchKind(data.get("kind", ""))
    except ValueError:
        return None
    return BatchInfo(
        kind=kind,
        title=data.get("title", ""),
        url=data.get("url", ""),
        id=data.get("id"),
    )


# ---------------------------------------------------------------------- #
# ArchiveItem <-> row dict
# ---------------------------------------------------------------------- #
def row_from_item(item: ArchiveItem) -> dict:
    """Flatten an ``ArchiveItem`` into a dict of DB column values.

    Scalars map to columns directly; nested structures become JSON text.
    """
    return {
        "key": item.key,
        "platform": item.platform,
        "content_type": item.content_type.value,
        "source_id": item.source_id,
        "url": item.url,
        "title": item.title,
        "content_html": item.content_html,
        "author_json": _json_dumps(author_to_dict(item.author)),
        "created": dt_to_str(item.created),
        "updated": dt_to_str(item.updated),
        "question_title": item.question_title,
        "question_url": item.question_url,
        "title_image": item.title_image,
        "column_title": item.column_title,
        "column_url": item.column_url,
        "batch_json": _json_dumps(batch_to_dict(item.batch)),
        "voteup_count": item.voteup_count,
        "comment_count": item.comment_count,
        "topics_json": _json_dumps(list(item.topics)),
        "excerpt": item.excerpt,
        "comments_json": _json_dumps([comment_to_dict(c) for c in item.comments]),
        "asset_map_json": _json_dumps(dict(item.asset_map)),
        "asset_issues_json": _json_dumps(dict(item.asset_issues)),
        "ai_json": _json_dumps(ai_to_dict(item.ai)),
        "raw_json": _json_dumps(item.raw),
        "content_hash": item.content_hash(),
    }


def item_from_row(row: Any) -> ArchiveItem:
    """Reconstruct an ``ArchiveItem`` from a DB row (sqlite3.Row or mapping)."""
    def col(name: str) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError):
            return None

    try:
        content_type = ContentType(col("content_type"))
    except ValueError:
        content_type = ContentType.OTHER

    comments_data = _json_loads(col("comments_json")) or []
    asset_map = _json_loads(col("asset_map_json")) or {}
    asset_issues = _json_loads(col("asset_issues_json")) or {}
    topics = _json_loads(col("topics_json")) or []
    raw = _json_loads(col("raw_json")) or {}

    return ArchiveItem(
        platform=col("platform") or "",
        content_type=content_type,
        source_id=col("source_id") or "",
        url=col("url") or "",
        title=col("title") or "",
        content_html=col("content_html") or "",
        author=author_from_dict(_json_loads(col("author_json"))),
        created=dt_from_str(col("created")),
        updated=dt_from_str(col("updated")),
        question_title=col("question_title"),
        question_url=col("question_url"),
        title_image=col("title_image"),
        column_title=col("column_title"),
        column_url=col("column_url"),
        batch=batch_from_dict(_json_loads(col("batch_json"))),
        voteup_count=col("voteup_count"),
        comment_count=col("comment_count"),
        topics=[str(t) for t in topics] if isinstance(topics, list) else [],
        excerpt=col("excerpt") or "",
        comments=[
            comment_from_dict(c) for c in comments_data if isinstance(c, dict)
        ],
        asset_map=asset_map if isinstance(asset_map, dict) else {},
        asset_issues=(
            {str(k): str(v) for k, v in asset_issues.items()}
            if isinstance(asset_issues, dict)
            else {}
        ),
        ai=ai_from_dict(_json_loads(col("ai_json"))),
        raw=raw if isinstance(raw, dict) else {},
    )
