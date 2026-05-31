"""Pipeline tests: DB-based dedup, ingest, exporter fan-out, export (offline)."""

import tempfile
from pathlib import Path

import pytest

from zarchiver.config import Config
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.ingest import Ingestor
from zarchiver.models import ArchiveItem, ContentType
from zarchiver.pipeline import Action, Pipeline, export_items
from zarchiver.sources.base import Source, SourceError
from zarchiver.store import StateStore


def _item(content="<p>hello</p>", source_id="1"):
    return ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id=source_id,
        url="https://zhuanlan.zhihu.com/p/1",
        title="T",
        content_html=content,
    )


class FakeSource(Source):
    platform = "zhihu"

    def __init__(self, item):
        self.item = item
        self.enrich_calls = 0

    def supports(self, url):
        return True

    def fetch(self, url):
        if self.item is None:
            raise SourceError("boom")
        return self.item

    def fetch_batch(self, url):
        yield self.item

    def enrich(self, item):
        self.enrich_calls += 1


class RecordingExporter(Exporter):
    """Exporter that records each item it was asked to export."""

    name = "rec"

    def __init__(self, out_dir: Path):
        self.out_dir = Path(out_dir)
        self.exported: list = []

    def target_path(self, item):
        return self.out_dir / f"{item.platform}-{item.source_id}.txt"

    def export(self, item):
        self.exported.append(item)
        path = self.target_path(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item.content_html, encoding="utf-8")
        return ExportResult(exporter=self.name, path=path)


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = StateStore(Path(d) / "t.db")
        yield s
        s.close()


def _pipeline(cfg, item, exporters, store, tmp_path, **kw):
    ingestor = Ingestor(store, assets_root=tmp_path / "assets", fetch=None)
    return Pipeline(cfg, FakeSource(item), exporters, store, ingestor, **kw)


def test_new_item_archived_and_exported(tmp_path, store):
    exp = RecordingExporter(tmp_path)
    p = _pipeline(Config(), _item(), [exp], store, tmp_path)
    out = p.archive_url("u")
    assert out.action == Action.ARCHIVED
    assert len(exp.exported) == 1
    assert store.count() == 1
    # Item persisted in full.
    assert store.load_item(_item().key) is not None


def test_duplicate_skip_default(tmp_path, store):
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    exp = RecordingExporter(tmp_path)
    p = _pipeline(cfg, item, [exp], store, tmp_path)
    p.archive_url("u")  # ingests + records in DB
    out2 = p.archive_url("u")  # same content_hash → skip
    assert out2.action == Action.SKIPPED
    assert out2.detail == "unchanged"
    assert len(exp.exported) == 1  # not exported again


def test_duplicate_update_policy(tmp_path, store):
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "update"
    exp = RecordingExporter(tmp_path)
    p = _pipeline(cfg, item, [exp], store, tmp_path)
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.UPDATED
    assert len(exp.exported) == 2


def test_skipped_duplicate_does_not_enrich(tmp_path, store):
    # A skipped duplicate must not trigger enrich (e.g. a comment crawl):
    # enrich runs only for items we actually archive/update.
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    p = _pipeline(cfg, item, [RecordingExporter(tmp_path)], store, tmp_path)
    p.archive_url("u")  # new -> archived -> enriched once
    assert p.source.enrich_calls == 1
    out2 = p.archive_url("u")  # unchanged -> skipped -> NOT enriched
    assert out2.action == Action.SKIPPED
    assert p.source.enrich_calls == 1  # unchanged


def test_updated_duplicate_enriches_again(tmp_path, store):
    # Under the update policy, a re-archived item is enriched each time.
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "update"
    p = _pipeline(cfg, item, [RecordingExporter(tmp_path)], store, tmp_path)
    p.archive_url("u")
    p.archive_url("u")
    assert p.source.enrich_calls == 2


def test_dedup_is_db_based_not_file_based(tmp_path, store):
    # Once in the DB, an item is a duplicate regardless of where output went.
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    item = _item()
    a = RecordingExporter(tmp_path / "a")
    _pipeline(cfg, item, [a], store, tmp_path).archive_url("u")
    # Second run with a different output dir still skips (DB knows the item).
    b = RecordingExporter(tmp_path / "b")
    out = _pipeline(cfg, item, [b], store, tmp_path).archive_url("u")
    assert out.action == Action.SKIPPED
    assert len(b.exported) == 0


def test_changed_content_reingested_on_update(tmp_path, store):
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    p1 = _pipeline(cfg, _item(content="<p>v1</p>"), [], store, tmp_path)
    assert p1.archive_url("u").action == Action.ARCHIVED
    # Same key, different content → "changed"; skip policy still skips.
    p2 = _pipeline(cfg, _item(content="<p>v2</p>"), [], store, tmp_path)
    assert p2.archive_url("u").action == Action.SKIPPED
    # With update policy, changed content is re-ingested.
    cfg.archive.on_duplicate = "update"
    p3 = _pipeline(cfg, _item(content="<p>v2</p>"), [], store, tmp_path)
    assert p3.archive_url("u").action == Action.UPDATED
    assert store.load_item(_item().key).content_html == "<p>v2</p>"


def test_ask_policy_uses_prompt(tmp_path, store):
    cfg = Config()
    cfg.archive.on_duplicate = "ask"
    exp = RecordingExporter(tmp_path)
    p = _pipeline(
        cfg, _item(), [exp], store, tmp_path, duplicate_prompt=lambda i: True
    )
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.UPDATED


def test_no_auto_export(tmp_path, store):
    exp = RecordingExporter(tmp_path)
    p = _pipeline(Config(), _item(), [exp], store, tmp_path, auto_export=False)
    out = p.archive_url("u")
    assert out.action == Action.ARCHIVED
    assert len(exp.exported) == 0  # ingested but not exported
    assert store.count() == 1


def test_source_error_reported(tmp_path, store):
    p = _pipeline(Config(), None, [RecordingExporter(tmp_path)], store, tmp_path)
    out = p.archive_url("u")
    assert out.action == Action.FAILED
    assert "boom" in out.detail


def test_exporter_failure_does_not_crash(tmp_path, store):
    class BadExporter(Exporter):
        name = "bad"

        def target_path(self, item):
            return tmp_path / "bad.txt"

        def export(self, item):
            raise RuntimeError("disk full")

    p = _pipeline(Config(), _item(), [BadExporter()], store, tmp_path)
    out = p.archive_url("u")
    # Item still ingested; export failure captured in results.
    assert out.action == Action.ARCHIVED
    assert any("failed" in e.detail for e in out.exports)


# ---------------------------------------------------------------------- #
# export_items (standalone export from DB)
# ---------------------------------------------------------------------- #
def test_export_items_renders_all(tmp_path, store):
    exp = RecordingExporter(tmp_path)
    items = [_item(source_id="1"), _item(source_id="2")]
    outcomes = export_items(items, [exp])
    assert len(outcomes) == 2
    assert all(o.action == Action.EXPORTED for o in outcomes)
    assert len(exp.exported) == 2


def test_export_items_skip_existing(tmp_path, store):
    exp = RecordingExporter(tmp_path)
    item = _item()
    export_items([item], [exp])  # writes the file
    exp.exported.clear()
    outcomes = export_items([item], [exp], skip_existing=True)
    assert outcomes[0].action == Action.SKIPPED
    assert len(exp.exported) == 0


def test_export_items_overwrites_by_default(tmp_path, store):
    exp = RecordingExporter(tmp_path)
    item = _item()
    export_items([item], [exp])
    outcomes = export_items([item], [exp])  # no skip_existing → re-export
    assert outcomes[0].action == Action.EXPORTED
    assert len(exp.exported) == 2


# ---------------------------------------------------------------------- #
# Image fetcher: max-asset-size enforcement
# ---------------------------------------------------------------------- #
def _fetcher_with_responses(monkeypatch, cfg, handler):
    """Build make_image_fetcher's fetch() backed by an httpx MockTransport."""
    import httpx

    from zarchiver import pipeline as P

    real_client = httpx.Client

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(P.httpx, "Client", fake_client)
    return P.make_image_fetcher(cfg)


def test_fetcher_downloads_under_limit(monkeypatch):
    import httpx

    cfg = Config()
    cfg.archive.max_asset_mb = 1.0  # 1 MB
    body = b"x" * 500_000  # 0.5 MB

    def handler(request):
        return httpx.Response(200, content=body)

    fetch = _fetcher_with_responses(monkeypatch, cfg, handler)
    assert fetch("https://pic.zhimg.com/a.jpg") == body


def test_fetcher_rejects_via_content_length(monkeypatch):
    import httpx

    cfg = Config()
    cfg.archive.max_asset_mb = 1.0
    big = b"y" * (2 * 1024 * 1024)  # 2 MB

    def handler(request):
        # Content-Length is set automatically from content.
        return httpx.Response(200, content=big)

    fetch = _fetcher_with_responses(monkeypatch, cfg, handler)
    assert fetch("https://vzuu.com/big.mp4") is None  # over the 1 MB cap


def test_fetcher_rejects_while_streaming_without_content_length(monkeypatch):
    import httpx

    cfg = Config()
    cfg.archive.max_asset_mb = 1.0

    def gen():
        for _ in range(3):
            yield b"z" * (512 * 1024)  # 1.5 MB total, streamed in chunks

    def handler(request):
        # A streaming response with no Content-Length header.
        return httpx.Response(200, content=gen())

    fetch = _fetcher_with_responses(monkeypatch, cfg, handler)
    assert fetch("https://vzuu.com/chunked.mp4") is None


def test_fetcher_zero_limit_disables_cap(monkeypatch):
    import httpx

    cfg = Config()
    cfg.archive.max_asset_mb = 0  # disabled → archive everything
    big = b"y" * (5 * 1024 * 1024)

    def handler(request):
        return httpx.Response(200, content=big)

    fetch = _fetcher_with_responses(monkeypatch, cfg, handler)
    assert fetch("https://vzuu.com/huge.mp4") == big
