"""Pipeline tests: dedup policies, AI gating, exporter fan-out (offline)."""

import tempfile
from pathlib import Path

import pytest

from zarchiver.config import Config
from zarchiver.exporters.base import Exporter, ExportResult
from zarchiver.models import ArchiveItem, ContentType
from zarchiver.pipeline import Action, Pipeline
from zarchiver.sources.base import Source, SourceError
from zarchiver.store import StateStore


def _item(content="<p>hello</p>"):
    return ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="1",
        url="https://zhuanlan.zhihu.com/p/1",
        title="T",
        content_html=content,
    )


class FakeSource(Source):
    platform = "zhihu"

    def __init__(self, item):
        self.item = item

    def supports(self, url):
        return True

    def fetch(self, url):
        if self.item is None:
            raise SourceError("boom")
        return self.item

    def fetch_batch(self, url):
        yield self.item


class RecordingExporter(Exporter):
    name = "rec"

    def __init__(self):
        self.exported = []

    def export(self, item):
        self.exported.append(item)
        return ExportResult(exporter=self.name, path=Path("/dev/null"))


@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = StateStore(Path(d) / "t.db")
        yield s
        s.close()


def test_new_item_archived_and_exported(store):
    exp = RecordingExporter()
    p = Pipeline(Config(), FakeSource(_item()), [exp], store)
    out = p.archive_url("u")
    assert out.action == Action.ARCHIVED
    assert len(exp.exported) == 1
    assert store.count() == 1


def test_duplicate_skip_default(store):
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    exp = RecordingExporter()
    p = Pipeline(cfg, FakeSource(item), [exp], store)
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.SKIPPED
    assert len(exp.exported) == 1  # not exported again


def test_duplicate_update_policy(store):
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "update"
    exp = RecordingExporter()
    p = Pipeline(cfg, FakeSource(item), [exp], store)
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.UPDATED
    assert len(exp.exported) == 2


def test_changed_content_with_skip_still_skips(store):
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    exp = RecordingExporter()
    # First archive
    p1 = Pipeline(cfg, FakeSource(_item("<p>v1</p>")), [exp], store)
    p1.archive_url("u")
    # Same key, changed content, skip policy
    p2 = Pipeline(cfg, FakeSource(_item("<p>v2</p>")), [exp], store)
    out = p2.archive_url("u")
    assert out.action == Action.SKIPPED
    assert out.detail == "changed"


def test_ask_policy_uses_prompt(store):
    cfg = Config()
    cfg.archive.on_duplicate = "ask"
    exp = RecordingExporter()
    p = Pipeline(
        cfg, FakeSource(_item()), [exp], store, duplicate_prompt=lambda i: True
    )
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.UPDATED


def test_source_error_reported(store):
    p = Pipeline(Config(), FakeSource(None), [RecordingExporter()], store)
    out = p.archive_url("u")
    assert out.action == Action.FAILED
    assert "boom" in out.detail


def test_exporter_failure_does_not_crash(store):
    class BadExporter(Exporter):
        name = "bad"

        def export(self, item):
            raise RuntimeError("disk full")

    p = Pipeline(Config(), FakeSource(_item()), [BadExporter()], store)
    out = p.archive_url("u")
    # Item still recorded; export failure captured in results.
    assert out.action == Action.ARCHIVED
    assert any("failed" in e.detail for e in out.exports)
