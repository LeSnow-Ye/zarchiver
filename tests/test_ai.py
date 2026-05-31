"""AI summarizer tests: prompt building, robust JSON parsing, caching (offline)."""

import tempfile
from pathlib import Path

import pytest

from zarchiver.ai.base import LLMProvider
from zarchiver.ai.summarizer import Summarizer, _extract_json
from zarchiver.config import AIConfig
from zarchiver.models import ArchiveItem, ContentType
from zarchiver.store import StateStore


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = 0

    def complete(self, system, user, *, json_mode=False):
        self.calls += 1
        self.last_json_mode = json_mode
        return self.reply


def _item():
    return ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="1",
        url="u",
        title="如何学习 Python",
        content_html="<p>多写代码，多读文档，多做项目。</p>",
    )


# ---------------------------------------------------------------------- #
def test_extract_json_plain():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_embedded_in_prose():
    text = '好的，结果如下：{"summary": "x", "tags": ["a"]} 希望有用'
    assert _extract_json(text) == {"summary": "x", "tags": ["a"]}


def test_extract_json_garbage():
    assert _extract_json("no json here") is None


def test_summarize_parses_result():
    p = FakeProvider('{"summary":"摘要","tags":["python","学习"],"category":"编程"}')
    s = Summarizer(AIConfig(api_key="x"), p)
    r = s.summarize(_item(), use_cache=False)
    assert r.summary == "摘要"
    assert r.tags == ["python", "学习"]
    assert r.category == "编程"
    assert r.model == AIConfig().model


def test_summarize_requests_json_mode():
    p = FakeProvider('{"summary":"s","tags":["a"],"category":"c"}')
    s = Summarizer(AIConfig(api_key="x"), p)
    s.summarize(_item(), use_cache=False)
    assert p.last_json_mode is True


def test_prompt_includes_obsidian_tag_rules():
    s = Summarizer(AIConfig(api_key="x", language="zh"), FakeProvider("{}"))
    _system, instruction = s._prompts("标题", "正文")
    # ZH variant references the Obsidian spec and its key constraints.
    assert "Obsidian" in instruction
    assert "斜杠" in instruction  # nested tags
    assert "纯数字" in instruction  # no numbers-only tags
    # English variant carries the English rules, including kebab-case.
    s_en = Summarizer(AIConfig(api_key="x", language="en"), FakeProvider("{}"))
    _sys_en, instr_en = s_en._prompts("Title", "Body")
    assert "Obsidian" in instr_en and "kebab-case" in instr_en
    assert "nested tags" in instr_en


def test_summarize_tags_as_string():
    p = FakeProvider('{"summary":"s","tags":"a，b、c","category":"x"}')
    s = Summarizer(AIConfig(api_key="x"), p)
    r = s.summarize(_item(), use_cache=False)
    assert r.tags == ["a", "b", "c"]


def test_summarize_uses_cache():
    with tempfile.TemporaryDirectory() as d:
        store = StateStore(Path(d) / "t.db")
        p = FakeProvider('{"summary":"s","tags":["a"],"category":"c"}')
        s = Summarizer(AIConfig(api_key="x"), p, store)
        item = _item()
        r1 = s.summarize(item)
        r2 = s.summarize(item)  # should hit cache, not call provider again
        assert p.calls == 1
        assert r2.summary == "s"
        store.close()


def test_summarize_fallback_on_non_json():
    p = FakeProvider("这是一段没有 JSON 的回复")
    s = Summarizer(AIConfig(api_key="x"), p)
    r = s.summarize(_item(), use_cache=False)
    assert "没有 JSON" in r.summary


# ---------------------------------------------------------------------- #
# Live test (requires DEEPSEEK_API_KEY). Run with: pytest -m live
# ---------------------------------------------------------------------- #
@pytest.mark.live
def test_deepseek_live_summarize():
    import os

    from zarchiver.ai import build_provider

    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        pytest.skip("DEEPSEEK_API_KEY not set")
    cfg = AIConfig(api_key=key)
    provider = build_provider(cfg)
    s = Summarizer(cfg, provider)
    r = s.summarize(_item(), use_cache=False)
    assert r.summary
    assert r.model == cfg.model
