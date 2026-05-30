"""Classification of Zhihu URLs.

Zhihu uses a handful of URL shapes. We map a raw URL to a
:class:`ZhihuKind` plus the identifiers we need, so the source knows whether to
fetch a single item or iterate a batch, and how to build canonical URLs.

Recognized shapes::

    answer     https://www.zhihu.com/question/<qid>/answer/<aid>
               https://www.zhihu.com/answer/<aid>
    article    https://zhuanlan.zhihu.com/p/<pid>
    question   https://www.zhihu.com/question/<qid>
    collection https://www.zhihu.com/collection/<cid>
    column     https://www.zhihu.com/column/<slug>
               https://zhuanlan.zhihu.com/<slug>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlparse


class ZhihuKind(str, Enum):
    ANSWER = "answer"
    ARTICLE = "article"
    QUESTION = "question"  # batch: all answers of a question
    COLLECTION = "collection"  # batch: a favorites folder
    COLUMN = "column"  # batch: a column's articles
    UNKNOWN = "unknown"


@dataclass(slots=True)
class ZhihuTarget:
    kind: ZhihuKind
    # Populated depending on kind:
    answer_id: str | None = None
    question_id: str | None = None
    article_id: str | None = None
    collection_id: str | None = None
    column_id: str | None = None
    raw_url: str = ""

    @property
    def is_batch(self) -> bool:
        return self.kind in (
            ZhihuKind.QUESTION,
            ZhihuKind.COLLECTION,
            ZhihuKind.COLUMN,
        )


# Ordered most-specific first.
_PATTERNS: list[tuple[ZhihuKind, re.Pattern[str]]] = [
    (
        ZhihuKind.ANSWER,
        re.compile(r"/question/(?P<qid>\d+)/answer/(?P<aid>\d+)"),
    ),
    (ZhihuKind.ANSWER, re.compile(r"/answer/(?P<aid>\d+)")),
    (ZhihuKind.ARTICLE, re.compile(r"/p/(?P<pid>\d+)")),
    (ZhihuKind.COLLECTION, re.compile(r"/collection/(?P<cid>\d+)")),
    (ZhihuKind.QUESTION, re.compile(r"/question/(?P<qid>\d+)")),
    (ZhihuKind.COLUMN, re.compile(r"/column/(?P<slug>[\w-]+)")),
]


def is_zhihu_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("zhihu.com")


def classify(url: str) -> ZhihuTarget:
    """Parse a Zhihu URL into a :class:`ZhihuTarget`."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path

    # zhuanlan.zhihu.com/<slug> (no /p/) is a column landing page.
    if host == "zhuanlan.zhihu.com" and not re.search(r"/p/\d+", path):
        slug = path.strip("/").split("/")[0] if path.strip("/") else ""
        if slug:
            return ZhihuTarget(
                kind=ZhihuKind.COLUMN, column_id=slug, raw_url=url
            )

    for kind, pattern in _PATTERNS:
        m = pattern.search(path)
        if not m:
            continue
        g = m.groupdict()
        if kind == ZhihuKind.ANSWER:
            return ZhihuTarget(
                kind=kind,
                answer_id=g.get("aid"),
                question_id=g.get("qid"),
                raw_url=url,
            )
        if kind == ZhihuKind.ARTICLE:
            return ZhihuTarget(kind=kind, article_id=g["pid"], raw_url=url)
        if kind == ZhihuKind.COLLECTION:
            return ZhihuTarget(kind=kind, collection_id=g["cid"], raw_url=url)
        if kind == ZhihuKind.QUESTION:
            return ZhihuTarget(kind=kind, question_id=g["qid"], raw_url=url)
        if kind == ZhihuKind.COLUMN:
            return ZhihuTarget(kind=kind, column_id=g["slug"], raw_url=url)

    return ZhihuTarget(kind=ZhihuKind.UNKNOWN, raw_url=url)


def article_url(article_id: str) -> str:
    return f"https://zhuanlan.zhihu.com/p/{article_id}"


def answer_url(question_id: str, answer_id: str) -> str:
    return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"


def question_url(question_id: str) -> str:
    return f"https://www.zhihu.com/question/{question_id}"
