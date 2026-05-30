"""Classification of Zhihu URLs.

Zhihu uses a handful of URL shapes. We map a raw URL to a
:class:`ZhihuKind` plus the identifiers we need, so the source knows whether to
fetch a single item or iterate a batch, and how to build canonical URLs.

Recognized shapes::

    answer     https://www.zhihu.com/question/<qid>/answer/<aid>
               https://www.zhihu.com/answer/<aid>
    article    https://zhuanlan.zhihu.com/p/<pid>
    pin        https://www.zhihu.com/pin/<pid>
    question   https://www.zhihu.com/question/<qid>
    collection https://www.zhihu.com/collection/<cid>
    column     https://www.zhihu.com/column/<slug>
               https://zhuanlan.zhihu.com/<slug>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.parse import urlparse


class ZhihuKind(str, Enum):
    ANSWER = "answer"
    ARTICLE = "article"
    PIN = "pin"  # a "想法" (short post with text + images)
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
    pin_id: str | None = None
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
    (ZhihuKind.PIN, re.compile(r"/pin/(?P<pinid>\d+)")),
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
        if kind == ZhihuKind.PIN:
            return ZhihuTarget(kind=kind, pin_id=g["pinid"], raw_url=url)
        if kind == ZhihuKind.COLLECTION:
            return ZhihuTarget(kind=kind, collection_id=g["cid"], raw_url=url)
        if kind == ZhihuKind.QUESTION:
            return ZhihuTarget(kind=kind, question_id=g["qid"], raw_url=url)
        if kind == ZhihuKind.COLUMN:
            return ZhihuTarget(kind=kind, column_id=g["slug"], raw_url=url)

    return ZhihuTarget(kind=ZhihuKind.UNKNOWN, raw_url=url)


def article_url(article_id: str) -> str:
    return f"https://zhuanlan.zhihu.com/p/{article_id}"


def pin_url(pin_id: str) -> str:
    return f"https://www.zhihu.com/pin/{pin_id}"


def answer_url(question_id: str, answer_id: str) -> str:
    return f"https://www.zhihu.com/question/{question_id}/answer/{answer_id}"


def question_url(question_id: str) -> str:
    return f"https://www.zhihu.com/question/{question_id}"


def with_page(url: str, page: int) -> str:
    """Return ``url`` with its ``page`` query parameter set to ``page``.

    Used to walk paginated collections (``/collection/<id>?page=N``). Any
    existing ``page`` param is replaced; other query params are preserved.
    """
    parts = urlparse(url)
    query = parse_qs(parts.query)
    query["page"] = [str(page)]
    new_query = urlencode({k: v[-1] for k, v in query.items()})
    return urlunparse(parts._replace(query=new_query))


def strip_page(url: str) -> str:
    """Return ``url`` with any ``page`` query parameter removed."""
    parts = urlparse(url)
    query = {k: v[-1] for k, v in parse_qs(parts.query).items() if k != "page"}
    return urlunparse(parts._replace(query=urlencode(query)))
