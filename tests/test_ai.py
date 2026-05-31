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
# Category reference (optional, config-driven)
# ---------------------------------------------------------------------- #
def test_category_free_generation_when_reference_empty():
    # Empty category_reference => free generation, no reference block injected.
    for lang in ("zh", "en"):
        s = Summarizer(
            AIConfig(api_key="x", language=lang, category_reference=""),
            FakeProvider("{}"),
        )
        _system, instruction = s._prompts("标题", "正文")
        assert "参考分类列表" not in instruction
        assert "Reference categories" not in instruction


def test_category_reference_injected_when_set():
    ref = "- 计算机图形学\n- 游戏开发"
    s = Summarizer(
        AIConfig(api_key="x", language="zh", category_reference=ref),
        FakeProvider("{}"),
    )
    _system, instruction = s._prompts("标题", "正文")
    assert "参考分类列表" in instruction
    assert "计算机图形学" in instruction and "游戏开发" in instruction
    assert "优先" in instruction  # prefer-from-list wording


def test_category_reference_whitespace_only_is_free_generation():
    s = Summarizer(
        AIConfig(api_key="x", category_reference="   \n  "), FakeProvider("{}")
    )
    _system, instruction = s._prompts("标题", "正文")
    assert "参考分类列表" not in instruction


def test_category_reference_with_braces_is_safe():
    # A reference containing { } must not break str.format.
    s = Summarizer(
        AIConfig(api_key="x", category_reference="- a{b}c\n- 数学"),
        FakeProvider("{}"),
    )
    _system, instruction = s._prompts("t", "b")
    assert "a{b}c" in instruction


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
