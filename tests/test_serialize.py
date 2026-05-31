"""Serialization round-trip tests: ArchiveItem <-> JSON-friendly row (offline)."""

from datetime import datetime, timezone

from zarchiver.models import (
    AIResult,
    ArchiveItem,
    Author,
    BatchInfo,
    BatchKind,
    Comment,
    ContentType,
)
from zarchiver.serialize import (
    dt_from_str,
    dt_to_str,
    item_from_row,
    row_from_item,
)


def _full_item() -> ArchiveItem:
    item = ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ANSWER,
        source_id="42",
        url="https://www.zhihu.com/question/1/answer/42",
        title="标题",
        content_html="<p>正文 <img src='https://pic1.zhimg.com/a.jpg'></p>",
        author=Author(name="作者", url="https://www.zhihu.com/people/x", id="x"),
        created=datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        updated=datetime(2024, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
        question_title="问题",
        question_url="https://www.zhihu.com/question/1",
        title_image="https://pic1.zhimg.com/cover.jpg",
        column_title="专栏",
        column_url="https://zhuanlan.zhihu.com/c",
        batch=BatchInfo(
            kind=BatchKind.COLLECTION, title="收藏夹", url="u", id="123"
        ),
        voteup_count=99,
        comment_count=5,
        topics=["话题1", "话题2"],
        excerpt="摘录",
    )
    item.comments = [
        Comment(
            id="c1",
            content_html="<p>评论</p>",
            author=Author(name="读者"),
            created=datetime(2024, 3, 1, tzinfo=timezone.utc),
            like_count=3,
            children=[
                Comment(
                    id="c1r",
                    content_html="<p>回复</p>",
                    author=Author(name="楼主"),
                    like_count=1,
                )
            ],
        )
    ]
    item.asset_map = {"https://pic1.zhimg.com/a.jpg": "zhihu_answer_42/abc.jpg"}
    item.asset_issues = {
        "https://pic1.zhimg.com/big.gif": "too_large",
        "https://pic1.zhimg.com/missing.jpg": "failed",
    }
    item.ai = AIResult(summary="摘要", tags=["t1", "t2"], category="分类", model="m")
    item.raw = {"nested": {"k": [1, 2, 3]}, "s": "v"}
    return item


def test_round_trip_preserves_all_fields():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))

    assert rebuilt.platform == item.platform
    assert rebuilt.content_type == item.content_type
    assert rebuilt.source_id == item.source_id
    assert rebuilt.url == item.url
    assert rebuilt.title == item.title
    assert rebuilt.content_html == item.content_html
    assert rebuilt.question_title == item.question_title
    assert rebuilt.question_url == item.question_url
    assert rebuilt.title_image == item.title_image
    assert rebuilt.column_title == item.column_title
    assert rebuilt.column_url == item.column_url
    assert rebuilt.voteup_count == item.voteup_count
    assert rebuilt.comment_count == item.comment_count
    assert rebuilt.topics == item.topics
    assert rebuilt.excerpt == item.excerpt
    assert rebuilt.raw == item.raw
    assert rebuilt.key == item.key


def test_round_trip_content_hash_stable():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.content_hash() == item.content_hash()


def test_round_trip_author():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.author is not None
    assert rebuilt.author.name == "作者"
    assert rebuilt.author.url == "https://www.zhihu.com/people/x"
    assert rebuilt.author.id == "x"


def test_round_trip_datetimes_tz_aware():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.created == item.created
    assert rebuilt.updated == item.updated
    assert rebuilt.created.tzinfo is not None


def test_round_trip_batch():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.batch is not None
    assert rebuilt.batch.kind == BatchKind.COLLECTION
    assert rebuilt.batch.title == "收藏夹"
    assert rebuilt.batch.id == "123"


def test_round_trip_comment_tree():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert len(rebuilt.comments) == 1
    root = rebuilt.comments[0]
    assert root.id == "c1"
    assert root.author.name == "读者"
    assert root.like_count == 3
    assert root.created == datetime(2024, 3, 1, tzinfo=timezone.utc)
    assert len(root.children) == 1
    assert root.children[0].id == "c1r"
    assert root.children[0].author.name == "楼主"
    assert root.total_count() == 2


def test_round_trip_asset_map():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.asset_map == item.asset_map


def test_round_trip_asset_issues():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.asset_issues == item.asset_issues


def test_missing_asset_issues_column_defaults_empty():
    row = row_from_item(_full_item())
    del row["asset_issues_json"]
    rebuilt = item_from_row(row)
    assert rebuilt.asset_issues == {}


def test_round_trip_ai_result():
    item = _full_item()
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.ai.summary == "摘要"
    assert rebuilt.ai.tags == ["t1", "t2"]
    assert rebuilt.ai.category == "分类"
    assert rebuilt.ai.model == "m"


def test_minimal_item_round_trip():
    item = ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="1",
        url="u",
        title="T",
        content_html="<p>x</p>",
    )
    rebuilt = item_from_row(row_from_item(item))
    assert rebuilt.author is None
    assert rebuilt.batch is None
    assert rebuilt.comments == []
    assert rebuilt.asset_map == {}
    assert rebuilt.asset_issues == {}
    assert rebuilt.ai.is_empty()
    assert rebuilt.raw == {}
    assert rebuilt.content_hash() == item.content_hash()


def test_dt_helpers_handle_none_and_naive():
    assert dt_to_str(None) is None
    assert dt_from_str(None) is None
    assert dt_from_str("") is None
    # Naive string defaults to UTC.
    naive = dt_from_str("2024-01-01T00:00:00")
    assert naive is not None and naive.tzinfo is not None
