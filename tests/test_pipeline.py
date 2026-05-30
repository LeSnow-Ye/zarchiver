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
    """Exporter that writes a real file so file-existence dedup can be tested."""

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


def test_new_item_archived_and_exported(tmp_path, store):
    exp = RecordingExporter(tmp_path)
    p = Pipeline(Config(), FakeSource(_item()), [exp], store)
    out = p.archive_url("u")
    assert out.action == Action.ARCHIVED
    assert len(exp.exported) == 1
    assert store.count() == 1


def test_duplicate_skip_default(tmp_path, store):
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    exp = RecordingExporter(tmp_path)
    p = Pipeline(cfg, FakeSource(item), [exp], store)
    p.archive_url("u")  # writes the file
    out2 = p.archive_url("u")  # file exists → skip
    assert out2.action == Action.SKIPPED
    assert out2.detail == "exists"
    assert len(exp.exported) == 1  # not exported again


def test_duplicate_update_policy(tmp_path, store):
    item = _item()
    cfg = Config()
    cfg.archive.on_duplicate = "update"
    exp = RecordingExporter(tmp_path)
    p = Pipeline(cfg, FakeSource(item), [exp], store)
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.UPDATED
    assert len(exp.exported) == 2


def test_skip_only_when_file_exists(tmp_path, store):
    # Dedup is file-based: a fresh output dir means archive, even if the item
    # is already in the SQLite store from a prior run.
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    item = _item()
    # First run writes into dir A.
    a = RecordingExporter(tmp_path / "a")
    Pipeline(cfg, FakeSource(item), [a], store).archive_url("u")
    # Second run targets a different dir B (no file there) → archives again.
    b = RecordingExporter(tmp_path / "b")
    out = Pipeline(cfg, FakeSource(item), [b], store).archive_url("u")
    assert out.action == Action.ARCHIVED
    assert len(b.exported) == 1


def test_partial_outputs_trigger_archive(tmp_path, store):
    # Two exporters; only one has its file present → not a full duplicate.
    cfg = Config()
    cfg.archive.on_duplicate = "skip"
    item = _item()
    a = RecordingExporter(tmp_path / "a")
    b = RecordingExporter(tmp_path / "b")
    # Pre-create only A's output.
    a.export(item)
    out = Pipeline(cfg, FakeSource(item), [a, b], store).archive_url("u")
    assert out.action == Action.ARCHIVED
    assert len(b.exported) == 1  # missing output B was written


def test_ask_policy_uses_prompt(tmp_path, store):
    cfg = Config()
    cfg.archive.on_duplicate = "ask"
    exp = RecordingExporter(tmp_path)
    p = Pipeline(
        cfg, FakeSource(_item()), [exp], store, duplicate_prompt=lambda i: True
    )
    p.archive_url("u")
    out2 = p.archive_url("u")
    assert out2.action == Action.UPDATED


def test_source_error_reported(tmp_path, store):
    p = Pipeline(Config(), FakeSource(None), [RecordingExporter(tmp_path)], store)
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

    p = Pipeline(Config(), FakeSource(_item()), [BadExporter()], store)
    out = p.archive_url("u")
    # Item still recorded; export failure captured in results.
    assert out.action == Action.ARCHIVED
    assert any("failed" in e.detail for e in out.exports)
