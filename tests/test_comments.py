"""Tests for comment fetching (offline, against a fake JSON getter) and the
comment-rendering exporter fragments."""

from datetime import datetime, timezone

from zarchiver.exporters.comments import (
    comment_total,
    comments_html_fragment,
    comments_markdown_fragment,
)
from zarchiver.models import ArchiveItem, Author, Comment, ContentType
from zarchiver.sources.zhihu import comments as C


# ---------------------------------------------------------------------- #
# A canned comment API: maps URL -> JSON page. Lets us exercise paging,
# child fetching, and caps without any network.
# ---------------------------------------------------------------------- #
def _author(name):
    return {"id": name, "url_token": name, "name": name}


def _comment(cid, *, children=0, child_comments=None, like=0, deleted=False):
    return {
        "id": cid,
        "content": f"<p>comment {cid}</p>",
        "author": _author(f"user{cid}"),
        "created_time": 1700000000,
        "like_count": like,
        "is_delete": deleted,
        "child_comment_count": children,
        "child_comments": child_comments or [],
    }


def _page(data, *, is_end=True, next_url=None):
    return {"data": data, "paging": {"is_end": is_end, "next": next_url}}


def make_getter(pages: dict):
    """Return a get_json that serves canned pages and records calls."""
    calls = []

    def get(url):
        calls.append(url)
        return pages.get(url)

    return get, calls


ROOT = "https://www.zhihu.com/api/v4/comment_v5/articles/1/root_comment?order_by=score&limit=20&offset="


def test_resource_type_mapping():
    assert C.resource_type_for(ContentType.ARTICLE) == "articles"
    assert C.resource_type_for(ContentType.ANSWER) == "answers"
    assert C.resource_type_for(ContentType.PIN) == "pins"
    assert C.resource_type_for(ContentType.QUESTION) is None


def test_fetch_simple_roots_no_children():
    pages = {ROOT: _page([_comment("a"), _comment("b")])}
    get, _ = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=100)
    assert [c.id for c in roots] == ["a", "b"]
    assert all(not c.children for c in roots)
    assert roots[0].author.name == "usera"
    assert roots[0].author.url == "https://www.zhihu.com/people/usera"


def test_fetch_embedded_children():
    child = _comment("a1")
    pages = {ROOT: _page([_comment("a", children=1, child_comments=[child])])}
    get, _ = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=100)
    assert len(roots) == 1
    assert [c.id for c in roots[0].children] == ["a1"]
    assert comment_total(roots) == 2


def test_fetch_pages_child_endpoint_when_more_than_embedded():
    # Root has 2 children but only 1 embedded -> page the child endpoint.
    embedded = _comment("a1")
    root = _comment("a", children=2, child_comments=[embedded])
    child_url = (
        "https://www.zhihu.com/api/v4/comment_v5/comment/a/child_comment"
        "?order_by=ts&limit=20&offset="
    )
    pages = {
        ROOT: _page([root]),
        child_url: _page([_comment("a1"), _comment("a2")]),
    }
    get, calls = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=100)
    # a1 (embedded) deduped against a1 from the endpoint; a2 added.
    assert [c.id for c in roots[0].children] == ["a1", "a2"]
    assert child_url in calls


def test_fetch_follows_root_paging():
    next_url = "https://www.zhihu.com/api/v4/comment_v5/articles/1/root_comment?page2"
    pages = {
        ROOT: _page([_comment("a")], is_end=False, next_url=next_url),
        next_url: _page([_comment("b")]),
    }
    get, _ = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=100)
    assert [c.id for c in roots] == ["a", "b"]


def test_cap_counts_children():
    # 1 root + 5 embedded children, cap of 3 => root + 2 children recorded.
    kids = [_comment(f"a{i}") for i in range(5)]
    root = _comment("a", children=5, child_comments=kids)
    pages = {ROOT: _page([root])}
    get, _ = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=3)
    assert comment_total(roots) == 3
    assert len(roots) == 1
    assert len(roots[0].children) == 2


def test_cap_stops_fetching_more_roots():
    next_url = "https://www.zhihu.com/api/v4/comment_v5/articles/1/root_comment?p2"
    pages = {
        ROOT: _page([_comment("a"), _comment("b")], is_end=False, next_url=next_url),
        next_url: _page([_comment("c")]),
    }
    get, calls = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=2)
    assert comment_total(roots) == 2
    # Second page should never be requested once the cap is met.
    assert next_url not in calls


def test_unlimited_when_cap_zero():
    kids = [_comment(f"a{i}") for i in range(3)]
    root = _comment("a", children=3, child_comments=kids)
    pages = {ROOT: _page([root])}
    get, _ = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=0)
    assert comment_total(roots) == 4  # root + all 3 children


def test_deleted_comments_skipped():
    pages = {ROOT: _page([_comment("a", deleted=True), _comment("b")])}
    get, _ = make_getter(pages)
    roots = C.fetch_comments(get, "articles", "1", max_comments=100)
    assert [c.id for c in roots] == ["b"]


def test_failed_request_returns_partial():
    # First page ok but says there's a next page that fails to load.
    next_url = "https://www.zhihu.com/api/v4/comment_v5/articles/1/root_comment?p2"
    pages = {ROOT: _page([_comment("a")], is_end=False, next_url=next_url)}
    get, _ = make_getter(pages)  # next_url not in pages -> getter returns None
    roots = C.fetch_comments(get, "articles", "1", max_comments=100)
    assert [c.id for c in roots] == ["a"]


# ---------------------------------------------------------------------- #
# Exporter fragments
# ---------------------------------------------------------------------- #
def _item_with_comments() -> ArchiveItem:
    item = ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="1",
        url="u",
        title="T",
        content_html="<p>body</p>",
    )
    reply = Comment(
        id="r1",
        content_html="<p>回复内容</p>",
        author=Author(name="回复者"),
        created=datetime(2024, 1, 2, tzinfo=timezone.utc),
        like_count=3,
    )
    item.comments = [
        Comment(
            id="c1",
            content_html="<p>顶层评论</p>",
            author=Author(name="评论者"),
            created=datetime(2024, 1, 1, tzinfo=timezone.utc),
            like_count=10,
            children=[reply],
        )
    ]
    return item


def test_markdown_fragment_threads_and_counts():
    frag = comments_markdown_fragment(_item_with_comments())
    assert "<h2>评论 (2)</h2>" in frag  # 1 root + 1 reply
    # Nested blockquotes for threading.
    assert frag.count("<blockquote>") == 2
    assert "评论者" in frag and "👍 10" in frag
    assert "回复者" in frag


def test_html_fragment_threads_and_styles():
    frag = comments_html_fragment(_item_with_comments())
    assert 'class="comments"' in frag
    assert "<h2>评论 (2)</h2>" in frag
    assert frag.count('class="comment"') == 2
    assert 'class="comment-children"' in frag
    assert "顶层评论" in frag and "回复内容" in frag


def test_fragments_empty_without_comments():
    item = ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="1",
        url="u",
        title="T",
        content_html="<p>x</p>",
    )
    assert comments_markdown_fragment(item) == ""
    assert comments_html_fragment(item) == ""


def test_comment_total_nested():
    leaf = Comment(id="x", content_html="")
    mid = Comment(id="m", content_html="", children=[leaf, leaf])
    assert mid.total_count() == 3
    assert comment_total([mid, leaf]) == 4
