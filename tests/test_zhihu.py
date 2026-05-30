"""Tests for the Zhihu source: URL classification + offline parser fixtures.

The parser tests run fully offline against saved HTML fixtures, so they need no
network and no browser. Capture/refresh fixtures with the live source.
"""

from pathlib import Path

import pytest

from zarchiver.models import ContentType
from zarchiver.sources.zhihu import parser as P
from zarchiver.sources.zhihu import urls as u
from zarchiver.sources.zhihu.urls import ZhihuKind as K

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------- #
# URL classification
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,kind",
    [
        ("https://zhuanlan.zhihu.com/p/35562420", K.ARTICLE),
        ("https://www.zhihu.com/question/19550225/answer/123", K.ANSWER),
        ("https://www.zhihu.com/answer/987", K.ANSWER),
        ("https://www.zhihu.com/question/19550225", K.QUESTION),
        ("https://www.zhihu.com/collection/123456", K.COLLECTION),
        ("https://www.zhihu.com/column/c_98765", K.COLUMN),
        ("https://zhuanlan.zhihu.com/mycolumn", K.COLUMN),
        ("https://example.com/foo", K.UNKNOWN),
    ],
)
def test_classify_kind(url, kind):
    assert u.classify(url).kind == kind


def test_classify_extracts_ids():
    t = u.classify("https://www.zhihu.com/question/19550225/answer/12345678")
    assert t.answer_id == "12345678"
    assert t.question_id == "19550225"
    assert not t.is_batch


def test_is_zhihu_url():
    assert u.is_zhihu_url("https://www.zhihu.com/x")
    assert u.is_zhihu_url("https://zhuanlan.zhihu.com/p/1")
    assert not u.is_zhihu_url("https://google.com")


def test_batch_flag():
    assert u.classify("https://www.zhihu.com/collection/1").is_batch
    assert not u.classify("https://zhuanlan.zhihu.com/p/1").is_batch


# ---------------------------------------------------------------------- #
# Parser (offline, against fixtures)
# ---------------------------------------------------------------------- #
@pytest.mark.skipif(
    not (FIXTURES / "article_35562420.html").is_file(),
    reason="article fixture not captured",
)
def test_parse_article_fixture():
    html = (FIXTURES / "article_35562420.html").read_text(encoding="utf-8")
    data = P.extract_initial_data(html)
    assert data is not None
    item = P.parse_article(data, "35562420")
    assert item.content_type == ContentType.ARTICLE
    assert item.source_id == "35562420"
    assert item.title
    assert len(item.content_html) > 0
    assert item.author and item.author.name
    assert item.key == "zhihu:article:35562420"
    # content hash is stable across calls
    assert item.content_hash() == item.content_hash()


@pytest.mark.skipif(
    not (FIXTURES / "answer_sample.html").is_file(),
    reason="answer fixture not captured",
)
def test_parse_answer_fixture():
    html = (FIXTURES / "answer_sample.html").read_text(encoding="utf-8")
    data = P.extract_initial_data(html)
    assert data is not None
    ids = P.answer_ids_from_data(data)
    assert ids, "expected at least one answer entity"
    item = P.parse_answer(data, ids[0])
    assert item.content_type == ContentType.ANSWER
    assert item.title  # question title
    assert item.question_url
    assert len(item.content_html) > 0


def test_extract_initial_data_missing():
    assert P.extract_initial_data("<html><body>no script</body></html>") is None


def test_clean_content_unwraps_redirect():
    html = (
        '<a href="https://link.zhihu.com/?target=https%3A//example.com/x">'
        "link</a>"
    )
    out = P.clean_content_html(html)
    assert "example.com/x" in out
    assert "link.zhihu.com" not in out


def test_clean_content_video_box():
    html = (
        '<a class="video-box" '
        'href="https://link.zhihu.com/?target=https%3A//www.zhihu.com/video/1" '
        'data-poster="https://pic.zhimg.com/p.jpg"></a>'
    )
    out = P.clean_content_html(html)
    assert "🎬 视频" in out
    assert "pic.zhimg.com/p.jpg" in out
    assert "www.zhihu.com/video/1" in out
