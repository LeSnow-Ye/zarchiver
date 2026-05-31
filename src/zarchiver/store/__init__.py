"""SQLite-backed archive store: system of record + AI cache.

The store is the single source of truth for archived content. It holds:

* **Items** — the full :class:`~zarchiver.models.ArchiveItem` for every archived
  piece of content: scalar metadata in columns, and the nested structures
  (author, comments, batch, AI result, asset map, raw parsed dict) as JSON text.
  Dedup is keyed by the item's globally-unique
  :pyattr:`~zarchiver.models.ArchiveItem.key`; the stored ``content_hash`` lets
  re-runs skip unchanged content or detect edits.
* **AI cache** — LLM results memoized by ``content_hash`` so the same body is
  never summarized twice, even across runs.

Images are *not* stored in the DB: their bytes live on disk under an assets root
(one directory per item key), and the item's ``asset_map`` (remote URL → local
relative path) is persisted as JSON so exporters can rewrite ``<img>`` offline.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from zarchiver.models import AIResult, ArchiveItem
from zarchiver.serialize import item_from_row, row_from_item

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    key            TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,
    content_type   TEXT NOT NULL,
    source_id      TEXT NOT NULL,
    url            TEXT,
    title          TEXT,
    content_html   TEXT,
    author_json    TEXT,
    created        TEXT,
    updated        TEXT,
    question_title TEXT,
    question_url   TEXT,
    title_image    TEXT,
    column_title   TEXT,
    column_url     TEXT,
    batch_json     TEXT,
    voteup_count   INTEGER,
    comment_count  INTEGER,
    topics_json    TEXT,
    excerpt        TEXT,
    comments_json  TEXT,
    asset_map_json TEXT,
    ai_json        TEXT,
    raw_json       TEXT,
    content_hash   TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    archived_at    TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_content_type ON items(content_type);
CREATE INDEX IF NOT EXISTS idx_items_updated_at ON items(updated_at);

CREATE TABLE IF NOT EXISTS ai_cache (
    content_hash  TEXT PRIMARY KEY,
    model         TEXT,
    summary       TEXT,
    tags_json     TEXT,
    category      TEXT,
    created_at    TEXT NOT NULL
);
"""

# Columns written by save_item that come from serialize.row_from_item().
_ITEM_COLUMNS = (
    "key", "platform", "content_type", "source_id", "url", "title",
    "content_html", "author_json", "created", "updated", "question_title",
    "question_url", "title_image", "column_title", "column_url", "batch_json",
    "voteup_count", "comment_count", "topics_json", "excerpt", "comments_json",
    "asset_map_json", "ai_json", "raw_json", "content_hash",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Persistent archive store + AI cache backed by a single SQLite file."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        if self.db_path.parent and not self.db_path.parent.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Dedup / status
    # ------------------------------------------------------------------ #
    def get_record(self, key: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM items WHERE key = ?", (key,))
        return cur.fetchone()

    def status_for(self, item: ArchiveItem) -> str:
        """Classify an item against what we've already archived.

        Returns one of ``"new"``, ``"unchanged"``, or ``"changed"``.
        """
        row = self.get_record(item.key)
        if row is None:
            return "new"
        if row["content_hash"] == item.content_hash():
            return "unchanged"
        return "changed"

    # ------------------------------------------------------------------ #
    # Item persistence (system of record)
    # ------------------------------------------------------------------ #
    def save_item(self, item: ArchiveItem) -> None:
        """Upsert the full item (content + comments + AI + asset map + raw)."""
        now = _now()
        values = row_from_item(item)
        existing = self.get_record(item.key)
        archived_at = existing["archived_at"] if existing else now

        cols = list(_ITEM_COLUMNS) + ["schema_version", "archived_at", "updated_at"]
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(
            f"{c}=excluded.{c}" for c in cols if c != "key" and c != "archived_at"
        )
        row_values = [values[c] for c in _ITEM_COLUMNS] + [
            SCHEMA_VERSION, archived_at, now
        ]
        self._conn.execute(
            f"INSERT INTO items ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT(key) DO UPDATE SET {updates}",
            row_values,
        )
        self._conn.commit()

    def load_item(self, key: str) -> Optional[ArchiveItem]:
        """Reconstruct a full :class:`ArchiveItem` from the DB, or None."""
        row = self.get_record(key)
        return item_from_row(row) if row is not None else None

    def iter_items(
        self,
        *,
        content_type: Optional[str] = None,
        since: Optional[str] = None,
        limit: int = 0,
    ) -> Iterator[ArchiveItem]:
        """Iterate stored items (most-recently-updated first), with filters.

        ``content_type`` filters by type value; ``since`` keeps items whose
        ``updated_at`` is >= the given ISO timestamp; ``limit`` of 0 means all.
        """
        clauses = []
        params: list = []
        if content_type:
            clauses.append("content_type = ?")
            params.append(content_type)
        if since:
            clauses.append("updated_at >= ?")
            params.append(since)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM items{where} ORDER BY updated_at DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        cur = self._conn.execute(sql, params)
        for row in cur:
            yield item_from_row(row)

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM items ORDER BY updated_at DESC LIMIT ?", (limit,)
        )
        return cur.fetchall()

    # ------------------------------------------------------------------ #
    # AI cache
    # ------------------------------------------------------------------ #
    def get_ai(self, content_hash: str) -> Optional[AIResult]:
        cur = self._conn.execute(
            "SELECT * FROM ai_cache WHERE content_hash = ?", (content_hash,)
        )
        row = cur.fetchone()
        if row is None:
            return None
        try:
            tags = json.loads(row["tags_json"]) if row["tags_json"] else []
        except json.JSONDecodeError:
            tags = []
        return AIResult(
            summary=row["summary"] or "",
            tags=tags,
            category=row["category"] or "",
            model=row["model"] or "",
        )

    def put_ai(self, content_hash: str, result: AIResult) -> None:
        self._conn.execute(
            """
            INSERT INTO ai_cache
                (content_hash, model, summary, tags_json, category, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(content_hash) DO UPDATE SET
                model=excluded.model, summary=excluded.summary,
                tags_json=excluded.tags_json, category=excluded.category,
                created_at=excluded.created_at
            """,
            (
                content_hash, result.model, result.summary,
                json.dumps(result.tags, ensure_ascii=False), result.category, _now(),
            ),
        )
        self._conn.commit()
