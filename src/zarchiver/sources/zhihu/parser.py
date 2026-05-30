"""Parsing Zhihu pages into :class:`ArchiveItem` objects.

Zhihu embeds a complete JSON state document in a ``<script id="js-initialData">``
tag. We parse that (``initialState.entities.{articles,answers,questions}``)
because it is clean and stable — far more robust than scraping rendered DOM.

All functions here are pure: they take the already-extracted ``initialData``
dict (and optionally raw HTML for fallback) and return items. That keeps them
unit-testable against saved fixtures with no browser involved.
"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from zarchiver.models import ArchiveItem, Author, ContentType
from zarchiver.sources.base import SourceError
from zarchiver.sources.zhihu import urls as zurls

PLATFORM = "zhihu"


# ---------------------------------------------------------------------- #
# Content normalization (Zhihu-specific HTML quirks)
# ---------------------------------------------------------------------- #
def clean_content_html(html: str) -> str:
    """Normalize Zhihu's content HTML before it reaches generic exporters.

    * Unwrap ``link.zhihu.com/?target=<encoded>`` redirects to the real URL.
    * Turn ``<a class="video-box">`` embeds into a readable poster + label.
    * Drop ``<noscript>`` duplicates.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")

    for ns in soup.find_all("noscript"):
        ns.decompose()

    # Video boxes: replace with poster image + a labelled link.
    for box in soup.select("a.video-box"):
        href = box.get("href", "")
        real = _unwrap_redirect(href)
        poster = box.get("data-poster")
        box.attrs = {"href": real} if real else {}
        box.clear()
        box.string = ""
        new_content = []
        if poster:
            img = soup.new_tag("img", src=poster)
            new_content.append(img)
        label = soup.new_tag("span")
        label.string = "🎬 视频"
        new_content.append(label)
        for node in new_content:
            box.append(node)

    # Unwrap remaining link.zhihu.com redirects.
    for a in soup.find_all("a", href=True):
        real = _unwrap_redirect(a["href"])
        if real:
            a["href"] = real

    return str(soup)


def _unwrap_redirect(href: str) -> Optional[str]:
    """Return the real target of a link.zhihu.com redirect, else None."""
    if not href or "link.zhihu.com" not in href:
        return None
    try:
        qs = parse_qs(urlparse(href).query)
        target = qs.get("target", [None])[0]
        return unquote(target) if target else None
    except Exception:
        return None


# ---------------------------------------------------------------------- #
# Extraction of the embedded state
# ---------------------------------------------------------------------- #
def extract_initial_data(html: str) -> Optional[dict]:
    """Pull and parse the ``js-initialData`` JSON from page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    el = soup.find("script", id="js-initialData")
    if not el or not el.string:
        return None
    try:
        return json.loads(el.string)
    except json.JSONDecodeError:
        return None


def _entities(data: dict) -> dict:
    return (data or {}).get("initialState", {}).get("entities", {}) or {}


def _make_author(raw: Optional[dict]) -> Optional[Author]:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or "知乎用户"
    url = raw.get("url")
    # Zhihu author urls are sometimes API paths; normalize people links.
    if url and url.startswith("/"):
        url = "https://www.zhihu.com" + url
    elif url and "api/v4" in url:
        token = raw.get("urlToken") or raw.get("id")
        url = f"https://www.zhihu.com/people/{token}" if token else None
    return Author(
        name=name,
        url=url,
        headline=raw.get("headline") or None,
        id=str(raw.get("id")) if raw.get("id") else None,
    )


def _topics(raw: dict) -> list[str]:
    topics = []
    for t in raw.get("topics", []) or []:
        if isinstance(t, dict) and t.get("name"):
            topics.append(t["name"])
    return topics


# ---------------------------------------------------------------------- #
# Article
# ---------------------------------------------------------------------- #
def parse_article(data: dict, article_id: str) -> ArchiveItem:
    articles = _entities(data).get("articles", {})
    raw = articles.get(str(article_id))
    if not raw:
        # Some payloads key by the only present id.
        if len(articles) == 1:
            raw = next(iter(articles.values()))
        else:
            raise SourceError(f"article {article_id} not found in page data")

    column = raw.get("column") or {}
    item = ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.ARTICLE,
        source_id=str(raw.get("id") or article_id),
        url=zurls.article_url(str(raw.get("id") or article_id)),
        title=raw.get("title") or "(untitled)",
        content_html=clean_content_html(raw.get("content") or ""),
        author=_make_author(raw.get("author")),
        created=ArchiveItem.epoch_to_dt(raw.get("created")),
        updated=ArchiveItem.epoch_to_dt(raw.get("updated")),
        voteup_count=raw.get("voteupCount"),
        comment_count=raw.get("commentCount"),
        topics=_topics(raw),
        excerpt=raw.get("excerpt") or "",
        raw={"column": column.get("title")} if column else {},
    )
    return item


# ---------------------------------------------------------------------- #
# Answer
# ---------------------------------------------------------------------- #
def parse_answer(
    data: dict, answer_id: str, question_id: Optional[str] = None
) -> ArchiveItem:
    answers = _entities(data).get("answers", {})
    raw = answers.get(str(answer_id))
    if not raw:
        if len(answers) == 1:
            raw = next(iter(answers.values()))
        else:
            raise SourceError(f"answer {answer_id} not found in page data")

    question = raw.get("question") or {}
    qid = str(question.get("id") or question_id or "")
    q_title = question.get("title") or "(question)"
    item = ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.ANSWER,
        source_id=str(raw.get("id") or answer_id),
        url=zurls.answer_url(qid, str(raw.get("id") or answer_id)) if qid else
            f"https://www.zhihu.com/answer/{raw.get('id') or answer_id}",
        # An answer's "title" is its parent question — most useful for filing.
        title=q_title,
        content_html=clean_content_html(raw.get("content") or ""),
        author=_make_author(raw.get("author")),
        created=ArchiveItem.epoch_to_dt(raw.get("createdTime") or raw.get("created")),
        updated=ArchiveItem.epoch_to_dt(raw.get("updatedTime") or raw.get("updated")),
        question_title=q_title,
        question_url=zurls.question_url(qid) if qid else None,
        voteup_count=raw.get("voteupCount"),
        comment_count=raw.get("commentCount"),
        excerpt=raw.get("excerpt") or "",
    )
    return item


# ---------------------------------------------------------------------- #
# Question (used to enumerate answers in a batch)
# ---------------------------------------------------------------------- #
def parse_question_meta(data: dict, question_id: str) -> dict[str, Any]:
    questions = _entities(data).get("questions", {})
    raw = questions.get(str(question_id)) or {}
    return {
        "title": raw.get("title"),
        "answer_count": raw.get("answerCount"),
    }


def answer_ids_from_data(data: dict) -> list[str]:
    """Collect answer ids present in the embedded entities (current page)."""
    answers = _entities(data).get("answers", {})
    return [str(k) for k in answers.keys()]


# ---------------------------------------------------------------------- #
# DOM fallback (used only when js-initialData is missing/incomplete)
# ---------------------------------------------------------------------- #
def parse_article_dom(html: str, article_id: str, url: str) -> ArchiveItem:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("h1.Post-Title, .Post-Title, h1")
    content_el = soup.select_one(".Post-RichText, .RichText")
    author_el = soup.select_one(".AuthorInfo-name, .UserLink-link")
    if not content_el:
        raise SourceError("could not locate article content in DOM")
    return ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.ARTICLE,
        source_id=str(article_id),
        url=url,
        title=title_el.get_text(strip=True) if title_el else "(untitled)",
        content_html=content_el.decode_contents(),
        author=Author(name=author_el.get_text(strip=True)) if author_el else None,
    )
