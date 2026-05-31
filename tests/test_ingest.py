"""Ingest tests: image download to per-key dir, asset map, AI, save (offline)."""

import tempfile
from pathlib import Path

import pytest

from zarchiver.exporters.assets import FetchResult, FetchStatus
from zarchiver.ingest import Ingestor, safe_key
from zarchiver.models import AIResult, ArchiveItem, Author, Comment, ContentType
from zarchiver.store import StateStore

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def ok_fetch(_url):
    return FetchResult(FetchStatus.OK, PNG)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = StateStore(Path(d) / "t.db")
        yield s
        s.close()


def _item(source_id="1"):
    return ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id=source_id,
        url="https://zhuanlan.zhihu.com/p/1",
        title="T",
        content_html='<p><img src="https://pic1.zhimg.com/a.jpg"></p>',
    )


def test_safe_key():
    assert safe_key("zhihu:article:42") == "zhihu_article_42"


def test_ingest_downloads_to_per_key_dir(tmp_path, store):
    item = _item()
    ing = Ingestor(store, assets_root=tmp_path / "assets", fetch=ok_fetch)
    ing.ingest(item)
    key_dir = tmp_path / "assets" / "zhihu_article_1"
    assert key_dir.is_dir()
    assert list(key_dir.glob("*")), "expected a downloaded image"


def test_ingest_records_asset_map(tmp_path, store):
    item = _item()
    ing = Ingestor(store, assets_root=tmp_path / "assets", fetch=ok_fetch)
    ing.ingest(item)
    assert "https://pic1.zhimg.com/a.jpg" in item.asset_map
    rel = item.asset_map["https://pic1.zhimg.com/a.jpg"]
    assert rel.startswith("zhihu_article_1/")
    # Stored path resolves to a real file under assets_root.
    assert (tmp_path / "assets" / rel).is_file()


def test_ingest_saves_to_store(tmp_path, store):
    item = _item()
    Ingestor(store, assets_root=tmp_path / "a", fetch=ok_fetch).ingest(item)
    loaded = store.load_item(item.key)
    assert loaded is not None
    assert loaded.asset_map == item.asset_map


def test_ingest_collects_title_and_comment_images(tmp_path, store):
    item = _item()
    item.title_image = "https://pic1.zhimg.com/cover.jpg"
    item.comments = [
        Comment(
            id="c1",
            content_html='<p><img src="https://pic1.zhimg.com/c.jpg"></p>',
            author=Author(name="x"),
        )
    ]
    ing = Ingestor(store, assets_root=tmp_path / "a", fetch=ok_fetch)
    ing.ingest(item)
    # All three distinct images recorded.
    assert len(item.asset_map) == 3
    assert "https://pic1.zhimg.com/cover.jpg" in item.asset_map
    assert "https://pic1.zhimg.com/c.jpg" in item.asset_map


def test_ingest_runs_summarizer(tmp_path, store):
    class FakeSummarizer:
        def summarize_with_retry(self, item):
            return AIResult(summary="s", tags=["t"], category="c", model="m")

    item = _item()
    ing = Ingestor(
        store,
        assets_root=tmp_path / "a",
        fetch=ok_fetch,
        summarizer=FakeSummarizer(),
    )
    ing.ingest(item)
    assert item.ai.summary == "s"
    assert store.load_item(item.key).ai.summary == "s"


def test_ingest_ai_failure_non_fatal(tmp_path, store):
    class BoomSummarizer:
        def summarize_with_retry(self, item):
            raise RuntimeError("api down")

    item = _item()
    ing = Ingestor(
        store,
        assets_root=tmp_path / "a",
        fetch=ok_fetch,
        summarizer=BoomSummarizer(),
    )
    ing.ingest(item)  # must not raise
    assert store.load_item(item.key) is not None


def test_ingest_no_fetch_skips_download(tmp_path, store):
    item = _item()
    ing = Ingestor(store, assets_root=tmp_path / "a", fetch=None)
    ing.ingest(item)
    assert item.asset_map == {}
    assert store.load_item(item.key) is not None


def test_ingest_download_disabled(tmp_path, store):
    item = _item()
    ing = Ingestor(
        store,
        assets_root=tmp_path / "a",
        fetch=ok_fetch,
        download_images=False,
    )
    ing.ingest(item)
    assert item.asset_map == {}


def test_ingest_failed_image_omitted_from_map(tmp_path, store):
    item = _item()
    # Fetcher returns FAILED → URL not in map (offline degrade).
    ing = Ingestor(
        store,
        assets_root=tmp_path / "a",
        fetch=lambda u: FetchResult(FetchStatus.FAILED),
    )
    ing.ingest(item)
    assert item.asset_map == {}
    assert item.asset_issues == {"https://pic1.zhimg.com/a.jpg": "failed"}


def test_ingest_downloads_video_and_poster(tmp_path, store):
    item = _item()
    item.content_html = (
        '<video src="https://v/clip.mp4" poster="https://x/cover.jpg"></video>'
    )
    ing = Ingestor(store, assets_root=tmp_path / "a", fetch=ok_fetch)
    ing.ingest(item)
    assert "https://v/clip.mp4" in item.asset_map
    assert "https://x/cover.jpg" in item.asset_map
    # The mp4 lands under the per-key dir.
    rel = item.asset_map["https://v/clip.mp4"]
    assert (tmp_path / "a" / rel).is_file()


def test_ingest_records_oversized_and_failed_asset_issues(tmp_path, store):
    item = _item()
    item.content_html = (
        '<img src="https://pic1.zhimg.com/a.jpg">'
        '<img src="https://pic1.zhimg.com/b.jpg">'
        '<img src="https://pic1.zhimg.com/c.jpg">'
    )

    def fetch(url):
        if url.endswith("/a.jpg"):
            return FetchResult(FetchStatus.OK, PNG)
        if url.endswith("/b.jpg"):
            return FetchResult(FetchStatus.TOO_LARGE)
        return FetchResult(FetchStatus.FAILED)

    ing = Ingestor(store, assets_root=tmp_path / "a", fetch=fetch)
    ing.ingest(item)

    assert "https://pic1.zhimg.com/a.jpg" in item.asset_map
    assert item.asset_issues == {
        "https://pic1.zhimg.com/b.jpg": "too_large",
        "https://pic1.zhimg.com/c.jpg": "failed",
    }
    assert store.load_item(item.key).asset_issues == item.asset_issues
