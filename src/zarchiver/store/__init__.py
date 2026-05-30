"""SQLite-backed state: dedup index + AI summary cache.

Two responsibilities, one small database:

* **Dedup** — remember which items have been archived (keyed by the item's
  globally-unique :pyattr:`~zarchiver.models.ArchiveItem.key`) and the
  ``content_hash`` we last saw, so re-runs can skip unchanged content or detect
  edits.
* **AI cache** — memoize expensive LLM results keyed by ``content_hash`` so the
  same body is never summarized twice, even across different runs.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from zarchiver.models import AIResult, ArchiveItem

_SCHEMA = """
CREATE TABLE IF NOT EXISTS archived (
    key           TEXT PRIMARY KEY,
    platform      TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    source_id     TEXT NOT NULL,
    url           TEXT,
    title         TEXT,
    content_hash  TEXT NOT NULL,
    archived_at   TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_cache (
    content_hash  TEXT PRIMARY KEY,
    model         TEXT,
    summary       TEXT,
    tags_json     TEXT,
    category      TEXT,
    created_at    TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Persistent dedup + AI cache backed by a single SQLite file."""

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
    # Dedup
    # ------------------------------------------------------------------ #
    def get_record(self, key: str) -> Optional[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM archived WHERE key = ?", (key,))
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

    def record_archived(self, item: ArchiveItem) -> None:
        """Upsert the dedup record for an item we just archived."""
        now = _now()
        chash = item.content_hash()
        existing = self.get_record(item.key)
        archived_at = existing["archived_at"] if existing else now
        self._conn.execute(
            """
            INSERT INTO archived
                (key, platform, content_type, source_id, url, title,
                 content_hash, archived_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                url=excluded.url, title=excluded.title,
                content_hash=excluded.content_hash, updated_at=excluded.updated_at
            """,
            (
                item.key, item.platform, item.content_type.value, item.source_id,
                item.url, item.title, chash, archived_at, now,
            ),
        )
        self._conn.commit()

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM archived").fetchone()[0]

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM archived ORDER BY updated_at DESC LIMIT ?", (limit,)
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
