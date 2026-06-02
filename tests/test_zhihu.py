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
        ("https://www.zhihu.com/pin/2000653466067043281", K.PIN),
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


def test_classify_pin_extracts_id():
    t = u.classify("https://www.zhihu.com/pin/2000653466067043281")
    assert t.kind == K.PIN
    assert t.pin_id == "2000653466067043281"
    assert not t.is_batch  # a pin is a single item, not a batch


def test_is_zhihu_url():
    assert u.is_zhihu_url("https://www.zhihu.com/x")
    assert u.is_zhihu_url("https://zhuanlan.zhihu.com/p/1")
    assert not u.is_zhihu_url("https://google.com")


def test_batch_flag():
    assert u.classify("https://www.zhihu.com/collection/1").is_batch
    assert not u.classify("https://zhuanlan.zhihu.com/p/1").is_batch


def test_classify_collection_with_page_param():
    # A paged collection URL still classifies as a collection.
    t = u.classify("https://www.zhihu.com/collection/703771723?page=2")
    assert t.kind == K.COLLECTION
    assert t.collection_id == "703771723"
    assert t.is_batch


def test_with_page_adds_and_replaces():
    base = "https://www.zhihu.com/collection/703771723"
    assert u.with_page(base, 2) == base + "?page=2"
    # Existing page param is replaced, not duplicated.
    assert u.with_page(base + "?page=5", 3) == base + "?page=3"


def test_with_page_preserves_other_params():
    out = u.with_page("https://www.zhihu.com/collection/1?foo=bar", 4)
    assert "foo=bar" in out and "page=4" in out


def test_strip_page():
    assert u.strip_page("https://www.zhihu.com/collection/1?page=5") == (
        "https://www.zhihu.com/collection/1"
    )
    # No page param → unchanged path/query.
    assert u.strip_page("https://www.zhihu.com/collection/1") == (
        "https://www.zhihu.com/collection/1"
    )


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


# ---------------------------------------------------------------------- #
# Pin (想法)
# ---------------------------------------------------------------------- #
def _pin_data():
    """A minimal pin payload: one text block, one image, an author user."""
    return {
        "initialState": {
            "entities": {
                "pins": {
                    "42": {
                        "id": "42",
                        "type": "pin",
                        "author": "zhang-san",
                        "created": 1700000000,
                        "updated": 1700000100,
                        "likeCount": 7,
                        "commentCount": 2,
                        "excerptTitle": "今天聊聊归档 | 一些零碎的想法",
                        "topics": [{"name": "归档"}],
                        "content": [
                            {"type": "text", "content": "<p>正文一段。</p>"},
                            {
                                "type": "image",
                                "url": "https://pic.zhimg.com/thumb_720w.jpg",
                                "originalUrl": "https://pic.zhimg.com/full.png",
                                "watermarkUrl": "https://pic.zhimg.com/wm.png",
                            },
                        ],
                    }
                },
                "users": {
                    "zhang-san": {
                        "name": "张三",
                        "urlToken": "zhang-san",
                        "url": "/people/abc123",
                        "id": "abc123",
                    }
                },
            }
        }
    }


def test_parse_pin_basic():
    item = P.parse_pin(_pin_data(), "42")
    assert item.content_type == ContentType.PIN
    assert item.source_id == "42"
    assert item.url == "https://www.zhihu.com/pin/42"
    assert item.key == "zhihu:pin:42"
    # Author resolved from the users entity by urlToken.
    assert item.author and item.author.name == "张三"
    assert item.author.url == "https://www.zhihu.com/people/abc123"
    # Engagement + topics carried over.
    assert item.voteup_count == 7
    assert item.comment_count == 2
    assert item.topics == ["归档"]


def test_parse_pin_title_synthesized_from_excerpt():
    # A pin has no real title: take the first segment of the excerpt.
    item = P.parse_pin(_pin_data(), "42")
    assert item.title == "今天聊聊归档"  # split on the | separator


def test_parse_pin_image_uses_original_url():
    # Image blocks become <img> tags pointing at the full-res original.
    item = P.parse_pin(_pin_data(), "42")
    assert "pic.zhimg.com/full.png" in item.content_html
    assert "正文一段" in item.content_html
    assert item.content_html.count("<img") == 1


def test_parse_pin_only_one_entity():
    # Id not matching, but a single pin present → use it.
    data = _pin_data()
    item = P.parse_pin(data, "999")
    assert item.source_id == "42"


def test_parse_pin_missing_raises():
    import pytest as _pytest

    from zarchiver.sources.base import SourceError

    empty = {"initialState": {"entities": {"pins": {}}}}
    with _pytest.raises(SourceError):
        P.parse_pin(empty, "42")


@pytest.mark.skipif(
    not (FIXTURES / "pin_2000653466067043281.html").is_file(),
    reason="pin fixture not captured",
)
def test_parse_pin_fixture():
    html = (FIXTURES / "pin_2000653466067043281.html").read_text(encoding="utf-8")
    data = P.extract_initial_data(html)
    assert data is not None
    item = P.parse_pin(data, "2000653466067043281")
    assert item.content_type == ContentType.PIN
    assert item.title  # synthesized from the excerpt
    assert item.author and item.author.name
    # The pin embeds 6 images, all rendered as <img> tags.
    assert item.content_html.count("<img") == 6
    # Inline link.zhihu.com redirect is unwrapped during cleaning.
    assert "link.zhihu.com" not in item.content_html


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
    # Without a resolver, a video box degrades to poster + label + link.
    html = (
        '<a class="video-box" '
        'href="https://link.zhihu.com/?target=https%3A//www.zhihu.com/video/1" '
        'data-poster="https://pic.zhimg.com/p.jpg"></a>'
    )
    out = P.clean_content_html(html)
    assert "🎬 视频" in out
    assert "pic.zhimg.com/p.jpg" in out
    assert "www.zhihu.com/video/1" in out
    assert "<video" not in out


def test_clean_content_video_resolved():
    # With a resolver, the box becomes a real <video> pointing at the MP4.
    html = (
        '<a class="video-box" data-lens-id="555" data-name="演示" '
        'href="https://link.zhihu.com/?target=https%3A//www.zhihu.com/video/555" '
        'data-poster="https://pic.zhimg.com/p.jpg"></a>'
    )

    def resolver(lens_id):
        assert lens_id == "555"
        return {
            "url": "https://vdn.vzuu.com/FHD/x.mp4?pkey=k",
            "cover": "https://pic.zhimg.com/cover.jpg",
            "title": "演示视频",
            "quality": "FHD",
        }

    out = P.clean_content_html(html, video_resolver=resolver)
    assert "<video" in out
    assert "vdn.vzuu.com/FHD/x.mp4" in out
    assert 'poster="https://pic.zhimg.com/cover.jpg"' in out
    assert 'data-zhihu-video="555"' in out
    assert "🎬 演示视频" in out
    assert "video-box" not in out


def test_clean_content_video_resolver_failure_falls_back():
    html = (
        '<a class="video-box" data-lens-id="9" '
        'data-poster="https://pic.zhimg.com/p.jpg"></a>'
    )
    out = P.clean_content_html(html, video_resolver=lambda _id: None)
    assert "<video" not in out
    assert "🎬 视频" in out
    assert "pic.zhimg.com/p.jpg" in out


# ---------------------------------------------------------------------- #
# Animated GIFs: keep the .gif src, not the static data-original frame
# ---------------------------------------------------------------------- #
def test_clean_content_gif2mp4_prefers_gif():
    # gif2mp4: animated .gif in src, static .jpg in data-original.
    html = (
        '<figure><img class="origin_image" '
        'src="https://pic3.zhimg.com/v2-abc_1440w.gif" '
        'data-original="https://pic3.zhimg.com/v2-abc_r.jpg" '
        'data-thumbnail="https://pic3.zhimg.com/v2-abc_b.jpg" '
        'data-original-token="v2-abc"></figure>'
    )
    out = P.clean_content_html(html)
    assert "v2-abc_1440w.gif" in out
    assert "_r.jpg" not in out  # static frame dropped
    assert "data-original=" not in out
    assert "zarchiver-gif" in out


def test_clean_content_plain_gif_kept():
    html = (
        '<img class="content_image" '
        'src="https://pic4.zhimg.com/v2-xyz_1440w.gif" '
        'data-thumbnail="https://pic4.zhimg.com/v2-xyz_b.jpg" '
        'data-original-token="v2-xyz">'
    )
    out = P.clean_content_html(html)
    assert "v2-xyz_1440w.gif" in out


def test_clean_content_static_image_untouched():
    html = (
        '<img src="https://pic1.zhimg.com/v2-q_1440w.jpg" '
        'data-original="https://pic1.zhimg.com/v2-q_r.jpg">'
    )
    out = P.clean_content_html(html)
    # No .gif anywhere → left exactly as a normal image (data-original kept).
    assert "zarchiver-gif" not in out
    assert "v2-q_r.jpg" in out


# ---------------------------------------------------------------------- #
# Formulas: equation images -> ztex spans (not downloaded as images)
# ---------------------------------------------------------------------- #
def test_clean_content_inline_formula():
    html = (
        '<p>速度 <img src="https://www.zhihu.com/equation?tex=v" alt="v" '
        'eeimg="1"/> 运动</p>'
    )
    out = P.clean_content_html(html)
    assert "equation?tex=" not in out  # no longer an image
    assert 'class="ztex"' in out
    assert 'data-tex="v"' in out
    assert "data-block" not in out  # inline, not block


def test_clean_content_block_formula():
    # A formula that is the sole content of its <p> is a display/block formula.
    html = (
        '<p><img src="https://www.zhihu.com/equation?tex=%5CDelta+N" '
        'alt="\\Delta N" eeimg="1"/></p>'
    )
    out = P.clean_content_html(html)
    assert 'data-block="true"' in out
    assert "data-tex=" in out
    assert "Delta N" in out  # decoded (+ -> space)


def test_clean_content_formula_url_decoded():
    html = (
        '<p><img src="https://www.zhihu.com/equation?tex=a%5E2%2Bb%5E2" '
        'eeimg="1"/></p>'
    )
    out = P.clean_content_html(html)
    assert "a^2 b^2" in out or "a^2+b^2" in out  # %5E -> ^, decoded


def test_parse_article_title_image_fixture():
    path = FIXTURES / "article_formula.html"
    if not path.is_file():
        pytest.skip("formula fixture not captured")
    data = P.extract_initial_data(path.read_text(encoding="utf-8"))
    item = P.parse_article(data, "88789807")
    assert item.title_image
    assert "zhimg.com" in item.title_image
    # Formulas converted, none left as images.
    assert "equation?tex=" not in item.content_html
    assert item.content_html.count('class="ztex"') > 10


def test_parse_article_gif_fixture():
    path = FIXTURES / "article_gif_679061686.html"
    if not path.is_file():
        pytest.skip("gif fixture not captured")
    data = P.extract_initial_data(path.read_text(encoding="utf-8"))
    item = P.parse_article(data, "679061686")
    # Animated GIFs keep their .gif source (not the static _r.jpg frame).
    assert ".gif" in item.content_html
    assert "zarchiver-gif" in item.content_html
    # Every animated (zarchiver-gif) <img> points at a .gif, and none of them
    # kept a static data-original frame. (Plain static images are untouched.)
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(item.content_html, "html.parser")
    gif_imgs = soup.select("img.zarchiver-gif")
    assert gif_imgs, "expected at least one animated gif image"
    for img in gif_imgs:
        assert ".gif" in (img.get("src") or "")
        assert not img.get("data-original")


def test_parse_article_video_fixture():
    path = FIXTURES / "article_video_1993765835039342710.html"
    if not path.is_file():
        pytest.skip("video fixture not captured")
    data = P.extract_initial_data(path.read_text(encoding="utf-8"))

    def resolver(lens_id):
        return {
            "url": f"https://vdn.vzuu.com/FHD/{lens_id}.mp4?pkey=k",
            "cover": "https://pic.zhimg.com/cover.jpg",
            "title": "演示",
            "quality": "FHD",
        }

    item = P.parse_article(data, "1993765835039342710", video_resolver=resolver)
    assert "<video" in item.content_html
    assert "vzuu.com" in item.content_html and ".mp4" in item.content_html
    assert "video-box" not in item.content_html
    # Without a resolver, it degrades gracefully (no <video>, keeps a label).
    item2 = P.parse_article(data, "1993765835039342710")
    assert "<video" not in item2.content_html
    assert "🎬" in item2.content_html


# ---------------------------------------------------------------------- #
# References: rebuild list from inline reference sups
# ---------------------------------------------------------------------- #
def test_clean_content_references():
    html = (
        "<p>foo<sup data-text=\"来源\" data-url=\"https://example.com/a\" "
        'data-draft-type="reference" data-numero="1">[1]</sup> bar'
        "<sup data-text=\"\" data-url=\"https://example.com/b\" "
        'data-draft-type="reference" data-numero="2">[2]</sup></p>'
    )
    out = P.clean_content_html(html)
    # Reference section appended.
    assert "参考" in out
    assert 'class="reference-list"' in out
    assert 'id="ref-1"' in out and 'id="ref-2"' in out
    assert "https://example.com/a" in out
    # Inline markers became anchors to the entries.
    assert 'href="#ref-1"' in out
    assert 'class="ref-marker"' in out
    assert "<sup" not in out


def test_parse_article_references_fixture():
    path = FIXTURES / "article_references.html"
    if not path.is_file():
        pytest.skip("references fixture not captured")
    data = P.extract_initial_data(path.read_text(encoding="utf-8"))
    item = P.parse_article(data, "2017975495849952400")
    assert "参考" in item.content_html
    assert 'class="reference-list"' in item.content_html
    assert item.content_html.count('class="ref-marker"') >= 5


# ---------------------------------------------------------------------- #
# Column metadata + batch title extraction
# ---------------------------------------------------------------------- #
def test_parse_article_column_metadata():
    data = {
        "initialState": {
            "entities": {
                "articles": {
                    "5": {
                        "id": "5",
                        "title": "T",
                        "content": "<p>x</p>",
                        "column": {
                            "title": "我的专栏",
                            "url": "https://zhuanlan.zhihu.com/c",
                        },
                    }
                }
            }
        }
    }
    item = P.parse_article(data, "5")
    assert item.column_title == "我的专栏"
    assert item.column_url == "https://zhuanlan.zhihu.com/c"


def test_parse_article_no_column():
    data = {
        "initialState": {
            "entities": {
                "articles": {"5": {"id": "5", "title": "T", "content": "<p>x</p>"}}
            }
        }
    }
    item = P.parse_article(data, "5")
    assert item.column_title is None
    assert item.column_url is None


def test_batch_title_by_kind():
    data = {
        "initialState": {
            "entities": {
                "columns": {"abc": {"title": "专栏A"}},
                "favlists": {"123": {"title": "收藏夹B"}},
                "questions": {"99": {"title": "问题C"}},
            }
        }
    }
    assert P.batch_title(data, "column", "abc") == "专栏A"
    assert P.batch_title(data, "collection", "123") == "收藏夹B"
    assert P.batch_title(data, "question", "99") == "问题C"


def test_batch_title_fallback_single_entity():
    data = {"initialState": {"entities": {"favlists": {"7": {"title": "唯一收藏夹"}}}}}
    # id not matching, but only one favlist present
    assert P.batch_title(data, "collection", "999") == "唯一收藏夹"


def test_batch_title_missing():
    assert P.batch_title(None, "column", "x") is None
    assert P.batch_title({"initialState": {"entities": {}}}, "column", "x") is None


# ---------------------------------------------------------------------- #
# Column / collection items API (offline parsing)
# ---------------------------------------------------------------------- #
def test_item_urls_from_column_api():
    # Column items expose url/type at the top level.
    payload = {
        "data": [
            {"type": "article", "id": "1", "url": "https://zhuanlan.zhihu.com/p/1"},
            {"type": "article", "id": "2", "url": "https://zhuanlan.zhihu.com/p/2"},
        ],
        "paging": {"is_end": True},
    }
    assert P.item_urls_from_api(payload) == [
        "https://zhuanlan.zhihu.com/p/1",
        "https://zhuanlan.zhihu.com/p/2",
    ]


def test_item_urls_from_collection_api():
    # Collection items wrap the real object under "content".
    payload = {
        "data": [
            {"content": {"type": "answer", "url": "https://www.zhihu.com/question/9/answer/1"}},
            {"content": {"type": "article", "url": "https://zhuanlan.zhihu.com/p/3"}},
        ]
    }
    assert P.item_urls_from_api(payload) == [
        "https://www.zhihu.com/question/9/answer/1",
        "https://zhuanlan.zhihu.com/p/3",
    ]


def test_item_urls_skips_non_items_and_deleted():
    payload = {
        "data": [
            {"content": {"type": "zvideo", "url": "https://www.zhihu.com/zvideo/1"}},
            {"content": {"type": "answer", "url": "https://www.zhihu.com/answer/2", "is_deleted": True}},
            {"content": {"type": "pin", "url": "https://www.zhihu.com/pin/3"}},
        ]
    }
    # Only the (non-deleted) pin survives; zvideo and deleted answer dropped.
    assert P.item_urls_from_api(payload) == ["https://www.zhihu.com/pin/3"]


def test_item_urls_canonicalizes_http():
    payload = {"data": [{"type": "article", "url": "http://zhuanlan.zhihu.com/p/5"}]}
    assert P.item_urls_from_api(payload) == ["https://zhuanlan.zhihu.com/p/5"]


def test_item_urls_empty_payload():
    assert P.item_urls_from_api(None) == []
    assert P.item_urls_from_api({}) == []


def test_api_paging_next():
    assert P.api_paging_next({"paging": {"is_end": False, "next": "u2"}}) == "u2"
    assert P.api_paging_next({"paging": {"is_end": True, "next": "u2"}}) is None
    assert P.api_paging_next({"paging": {}}) is None  # missing is_end -> treat as end
    assert P.api_paging_next({}) is None


def test_collection_title_from_api():
    assert P.collection_title_from_api(
        {"collection": {"title": "我的收藏"}}
    ) == "我的收藏"
    assert P.collection_title_from_api({"collection": {"title": "  "}}) is None
    assert P.collection_title_from_api(None) is None


def test_column_title_from_api():
    assert P.column_title_from_api({"title": "次元壁"}) == "次元壁"
    assert P.column_title_from_api({}) is None


# ---------------------------------------------------------------------- #
# Column / collection fetching via the items API (offline, canned getter)
# ---------------------------------------------------------------------- #
def _source_with_api(pages, max_items=0):
    """Build a ZhihuSource whose _get_json replays canned API pages by URL.

    ``pages`` maps URL -> JSON payload. Records the URLs requested.
    """
    from zarchiver.config import Config
    from zarchiver.sources.zhihu.source import ZhihuSource

    cfg = Config()
    cfg.browser.max_items = max_items
    src = ZhihuSource(cfg)
    calls = []

    def fake_get(url):
        calls.append(url)
        return pages.get(url)

    src._get_json = fake_get
    return src, calls


def test_api_collection_walks_pages():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    p2 = "https://www.zhihu.com/api/v4/collections/1/items?offset=20&limit=20"
    pages = {
        base: {
            "data": [{"content": {"type": "article", "url": f"https://zhuanlan.zhihu.com/p/{i}"}} for i in range(2)],
            "paging": {"is_end": False, "next": p2},
        },
        p2: {
            "data": [{"content": {"type": "article", "url": "https://zhuanlan.zhihu.com/p/9"}}],
            "paging": {"is_end": True},
        },
    }
    src, calls = _source_with_api(pages)
    urls = src._collect_api_item_urls(base, label="collection")
    assert urls == [
        "https://zhuanlan.zhihu.com/p/0",
        "https://zhuanlan.zhihu.com/p/1",
        "https://zhuanlan.zhihu.com/p/9",
    ]
    assert calls == [base, p2]  # followed paging.next


def test_api_respects_cap_and_stops_paging():
    base = "https://www.zhihu.com/api/v4/columns/c/items?limit=20&ws_qiangzhisafe=0&offset=0"
    p2 = "https://www.zhihu.com/api/v4/columns/c/items?offset=20"
    pages = {
        base: {
            "data": [{"type": "article", "url": f"https://zhuanlan.zhihu.com/p/{i}"} for i in range(3)],
            "paging": {"is_end": False, "next": p2},
        },
        p2: {"data": [{"type": "article", "url": "https://zhuanlan.zhihu.com/p/99"}], "paging": {"is_end": True}},
    }
    src, calls = _source_with_api(pages, max_items=2)
    urls = src._collect_api_item_urls(base, label="column")
    assert urls == ["https://zhuanlan.zhihu.com/p/0", "https://zhuanlan.zhihu.com/p/1"]
    assert p2 not in calls  # cap met on page 1, no second request


def test_api_dedupes_across_pages():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    p2 = "https://www.zhihu.com/api/v4/collections/1/items?offset=20&limit=20"
    pages = {
        base: {
            "data": [{"content": {"type": "article", "url": u}} for u in ("a", "b")],
            "paging": {"is_end": False, "next": p2},
        },
        p2: {
            "data": [{"content": {"type": "article", "url": u}} for u in ("b", "c")],
            "paging": {"is_end": True},
        },
    }
    src, _ = _source_with_api(pages)
    assert src._collect_api_item_urls(base, label="collection") == ["a", "b", "c"]


def test_api_stops_on_failed_request():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    p2 = "https://www.zhihu.com/api/v4/collections/1/items?offset=20&limit=20"
    pages = {
        base: {
            "data": [{"content": {"type": "article", "url": "a"}}],
            "paging": {"is_end": False, "next": p2},
        }
        # p2 missing -> getter returns None -> stop with partial results.
    }
    src, _ = _source_with_api(pages)
    assert src._collect_api_item_urls(base, label="collection") == ["a"]


# ---------------------------------------------------------------------- #
# Building items directly from API JSON (no page fetch)
# ---------------------------------------------------------------------- #
def test_archivable_entries_unwraps_and_filters():
    payload = {
        "data": [
            {"content": {"type": "answer", "url": "https://www.zhihu.com/question/1/answer/2"}},
            {"type": "article", "url": "https://zhuanlan.zhihu.com/p/3"},  # flat
            {"content": {"type": "answer", "url": "u", "is_deleted": True}},  # deleted
            {"content": {"type": "zvideo", "url": "u"}},  # wrong type
            {"content": {"type": "answer"}},  # no url
        ]
    }
    objs = P.archivable_entries_from_api(payload)
    assert [o["type"] for o in objs] == ["answer", "article"]
    # item_urls_from_api is the URL projection of the same set.
    assert P.item_urls_from_api(payload) == [
        "https://www.zhihu.com/question/1/answer/2",
        "https://zhuanlan.zhihu.com/p/3",
    ]


def test_item_from_api_entry_answer():
    obj = {
        "type": "answer",
        "id": 42,
        "url": "https://www.zhihu.com/api/v4/answers/42",
        "content": "<p>body</p>",
        "excerpt": "ex",
        "voteup_count": 7,
        "comment_count": 3,
        "created_time": 1700000000,
        "updated_time": 1700000100,
        "author": {"name": "A"},
        "question": {"id": 9, "title": "Q?"},
    }
    item = P.item_from_api_entry(obj)
    assert item is not None
    assert item.content_type is ContentType.ANSWER
    assert item.source_id == "42"
    assert item.title == "Q?"
    assert item.question_url == "https://www.zhihu.com/question/9"
    assert item.url == "https://www.zhihu.com/question/9/answer/42"
    assert item.voteup_count == 7 and item.comment_count == 3
    assert item.author and item.author.name == "A"
    assert "<p>body</p>" in item.content_html
    assert item.created is not None and item.updated is not None


def test_item_from_api_entry_article():
    obj = {
        "type": "article",
        "id": 100,
        "url": "https://zhuanlan.zhihu.com/p/100",
        "title": "T",
        "content": "<p>art</p>",
        "author": {"name": "W"},
        "column": {"title": "Col", "url": "https://zhuanlan.zhihu.com/col"},
        "voteup_count": 1,
    }
    item = P.item_from_api_entry(obj)
    assert item is not None
    assert item.content_type is ContentType.ARTICLE
    assert item.title == "T"
    assert item.column_title == "Col"
    assert item.url == "https://zhuanlan.zhihu.com/p/100"


def test_item_from_api_entry_returns_none_without_content():
    # Missing/empty content -> caller falls back to a page fetch.
    assert P.item_from_api_entry(
        {"type": "answer", "id": 1, "url": "u", "content": ""}
    ) is None
    assert P.item_from_api_entry(
        {"type": "answer", "id": 1, "url": "u"}
    ) is None
    # Unknown type -> None.
    assert P.item_from_api_entry(
        {"type": "zvideo", "id": 1, "url": "u", "content": "x"}
    ) is None


def test_question_title_from_answers():
    payload = {"data": [
        {"id": 1, "question": {"title": "How?"}},
    ]}
    assert P.question_title_from_answers(payload) == "How?"
    assert P.question_title_from_answers({"data": [{"id": 1}]}) is None
    assert P.question_title_from_answers(None) is None


def test_web_url_from_api_entry():
    # Question-API answers expose only /api/v4/answers/<id>; rebuild the
    # canonical /question/<qid>/answer/<aid> form for fetch()/dedup.
    assert P.web_url_from_api_entry({
        "type": "answer", "id": 2, "url": "https://www.zhihu.com/api/v4/answers/2",
        "question": {"id": 9, "title": "Q?"},
    }) == "https://www.zhihu.com/question/9/answer/2"
    # Answer without a question id falls back to the entry url.
    assert P.web_url_from_api_entry({
        "type": "answer", "id": 2, "url": "https://www.zhihu.com/answer/2",
    }) == "https://www.zhihu.com/answer/2"
    assert P.web_url_from_api_entry({
        "type": "article", "id": 100, "url": "x",
    }) == "https://zhuanlan.zhihu.com/p/100"


# ---------------------------------------------------------------------- #
# Source: API-first batch with page fallback
# ---------------------------------------------------------------------- #
class _NoSleepBrowser:
    """Stand-in browser whose polite_delay is a no-op (no real Chromium)."""

    def polite_delay(self):
        pass


def _answer_entry(aid, content="<p>x</p>", qid=9, qtitle="Q?"):
    return {
        "type": "answer",
        "id": aid,
        "url": f"https://www.zhihu.com/api/v4/answers/{aid}",
        "content": content,
        "question": {"id": qid, "title": qtitle},
    }


def test_fetch_collection_archives_from_tail_first():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    pages = {
        base: {
            "data": [{"content": _answer_entry(i)} for i in (1, 2, 3)],
            "paging": {"is_end": True},
        },
        "https://www.zhihu.com/api/v4/collections/1": {
            "collection": {"title": "收藏"}
        },
    }
    src, _ = _source_with_api(pages)
    src._browser = _NoSleepBrowser()
    src._video_resolver = lambda: None

    items = list(src.fetch_batch("https://www.zhihu.com/collection/1"))

    assert [i.source_id for i in items] == ["3", "2", "1"]


def test_fetch_column_archives_from_tail_first():
    base = (
        "https://www.zhihu.com/api/v4/columns/c/items"
        "?limit=20&ws_qiangzhisafe=0&offset=0"
    )
    pages = {
        base: {
            "data": [_answer_entry(i) for i in (1, 2, 3)],
            "paging": {"is_end": True},
        },
        "https://www.zhihu.com/api/v4/columns/c": {"title": "专栏"},
    }
    src, _ = _source_with_api(pages)
    src._browser = _NoSleepBrowser()
    src._video_resolver = lambda: None

    items = list(src.fetch_batch("https://www.zhihu.com/column/c"))

    assert [i.source_id for i in items] == ["3", "2", "1"]


def test_iter_api_or_fetch_uses_api_without_opening_page():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    pages = {base: {
        "data": [{"content": _answer_entry(2)}, {"content": _answer_entry(3)}],
        "paging": {"is_end": True},
    }}
    src, _ = _source_with_api(pages)
    src._browser = _NoSleepBrowser()
    src.config.archive.comments = False  # don't try to fetch comments
    # Any attempt to open a page is a failure for this test.
    def _boom(url):
        raise AssertionError(f"page opened for {url!r}")
    src.fetch = _boom  # type: ignore[assignment]
    src._video_resolver = lambda: None
    entries = src._walk_api_pages(base, label="collection")
    items = list(src._iter_api_or_fetch(entries, batch=None))
    assert [i.source_id for i in items] == ["2", "3"]
    assert all(i.content_html for i in items)


def test_iter_api_or_fetch_falls_back_when_no_content():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    # Entry has no body -> must fall back to fetching the page.
    no_body = {"type": "answer", "id": 5,
               "url": "https://www.zhihu.com/question/9/answer/5"}
    pages = {base: {"data": [{"content": no_body}], "paging": {"is_end": True}}}
    src, _ = _source_with_api(pages)
    src._browser = _NoSleepBrowser()
    src.config.archive.comments = False
    src._video_resolver = lambda: None
    fetched = []

    def _fetch(url):
        fetched.append(url)
        return P.item_from_api_entry(_answer_entry(5))  # stand-in page item

    src.fetch = _fetch  # type: ignore[assignment]
    entries = src._walk_api_pages(base, label="collection")
    items = list(src._iter_api_or_fetch(entries, batch=None))
    assert fetched == ["https://www.zhihu.com/question/9/answer/5"]
    assert [i.source_id for i in items] == ["5"]


def test_prefer_api_content_off_always_fetches_page():
    base = "https://www.zhihu.com/api/v4/collections/1/items?offset=0&limit=20"
    pages = {base: {"data": [{"content": _answer_entry(2)}], "paging": {"is_end": True}}}
    src, _ = _source_with_api(pages)
    src._browser = _NoSleepBrowser()
    src.config.archive.comments = False
    src.config.archive.prefer_api_content = False  # force page path
    src._video_resolver = lambda: None
    fetched = []

    def _fetch(url):
        fetched.append(url)
        return P.item_from_api_entry(_answer_entry(2))

    src.fetch = _fetch  # type: ignore[assignment]
    entries = src._walk_api_pages(base, label="collection")
    list(src._iter_api_or_fetch(entries, batch=None))
    assert fetched == ["https://www.zhihu.com/question/9/answer/2"]
