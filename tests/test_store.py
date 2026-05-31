"""Store tests: item save/load round-trip, dedup status, iteration (offline)."""

import tempfile
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from zarchiver.models import (
    AIResult,
    ArchiveItem,
    Author,
    BatchInfo,
    BatchKind,
    Comment,
    ContentType,
)
from zarchiver.store import StateStore


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = StateStore(Path(d) / "t.db")
        yield s
        s.close()


def _item(source_id="1", content="<p>hello</p>", ctype=ContentType.ARTICLE):
    return ArchiveItem(
        platform="zhihu",
        content_type=ctype,
        source_id=source_id,
        url=f"https://zhuanlan.zhihu.com/p/{source_id}",
        title="T",
        content_html=content,
    )


def test_save_then_load_round_trip(store):
    item = _item()
    item.author = Author(name="作者")
    item.created = datetime(2024, 1, 1, tzinfo=timezone.utc)
    item.topics = ["a", "b"]
    item.comments = [Comment(id="c1", content_html="<p>hi</p>")]
    item.asset_map = {"https://pic.zhimg.com/x.jpg": "zhihu_article_1/x.jpg"}
    item.asset_issues = {
        "https://pic.zhimg.com/big.gif": "too_large",
        "https://pic.zhimg.com/missing.jpg": "failed",
    }
    item.ai = AIResult(summary="s", tags=["t"], category="c", model="m")
    item.raw = {"k": "v"}
    store.save_item(item)

    loaded = store.load_item(item.key)
    assert loaded is not None
    assert loaded.title == "T"
    assert loaded.author.name == "作者"
    assert loaded.topics == ["a", "b"]
    assert loaded.comments[0].id == "c1"
    assert loaded.asset_map == item.asset_map
    assert loaded.asset_issues == item.asset_issues
    assert loaded.ai.summary == "s"
    assert loaded.raw == {"k": "v"}
    assert loaded.content_hash() == item.content_hash()


def test_load_missing_returns_none(store):
    assert store.load_item("zhihu:article:nope") is None


def test_status_for_new_unchanged_changed(store):
    item = _item()
    assert store.status_for(item) == "new"
    store.save_item(item)
    assert store.status_for(item) == "unchanged"
    changed = _item(content="<p>different</p>")
    assert store.status_for(changed) == "changed"


def test_count_and_recent(store):
    store.save_item(_item("1"))
    store.save_item(_item("2"))
    assert store.count() == 2
    rows = store.recent(10)
    assert len(rows) == 2


def test_save_is_upsert(store):
    item = _item()
    store.save_item(item)
    item.title = "updated"
    store.save_item(item)
    assert store.count() == 1
    assert store.load_item(item.key).title == "updated"


def test_iter_items_filter_by_type(store):
    store.save_item(_item("1", ctype=ContentType.ARTICLE))
    store.save_item(_item("2", ctype=ContentType.ANSWER))
    answers = list(store.iter_items(content_type="answer"))
    assert len(answers) == 1
    assert answers[0].content_type == ContentType.ANSWER
    assert len(list(store.iter_items())) == 2


def test_iter_items_limit(store):
    for i in range(5):
        store.save_item(_item(str(i)))
    assert len(list(store.iter_items(limit=3))) == 3


def test_ai_cache_unchanged(store):
    store.put_ai("hash123", AIResult(summary="s", tags=["x"], category="c", model="m"))
    got = store.get_ai("hash123")
    assert got is not None
    assert got.summary == "s"
    assert got.tags == ["x"]
    assert store.get_ai("missing") is None


def test_batch_round_trip(store):
    item = _item()
    item.batch = BatchInfo(
        kind=BatchKind.COLLECTION, title="夹", url="u", id="9"
    )
    store.save_item(item)
    loaded = store.load_item(item.key)
    assert loaded.batch.kind == BatchKind.COLLECTION
    assert loaded.batch.id == "9"


def test_existing_v1_db_migrates_asset_issues_column(tmp_path):
    db = tmp_path / "v1.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE items (
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
        CREATE TABLE ai_cache (
            content_hash  TEXT PRIMARY KEY,
            model         TEXT,
            summary       TEXT,
            tags_json     TEXT,
            category      TEXT,
            created_at    TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()

    store = StateStore(db)
    try:
        item = _item()
        item.asset_issues = {"https://pic.zhimg.com/missing.jpg": "failed"}
        store.save_item(item)
        assert store.load_item(item.key).asset_issues == item.asset_issues
    finally:
        store.close()
