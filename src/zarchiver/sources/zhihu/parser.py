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
import re
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

    * Convert equation images (``equation?tex=...``) into ``<span class="ztex"
      data-tex="..." data-block="...">`` so exporters can render real LaTeX
      (markdown ``$...$`` / MathJax) instead of downloading them as images.
    * Unwrap ``link.zhihu.com/?target=<encoded>`` redirects to the real URL.
    * Turn ``<a class="video-box">`` embeds into a readable poster + label.
    * Rebuild the reference list from inline ``<sup data-draft-type="reference">``
      markers (Zhihu renders that list client-side, so it's absent here).
    * Drop ``<noscript>`` duplicates.
    """
    if not html:
        return html
    soup = BeautifulSoup(html, "html.parser")

    for ns in soup.find_all("noscript"):
        ns.decompose()

    _normalize_formulas(soup)

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

    _append_references(soup)

    return str(soup)


_EQUATION_RE = re.compile(r"equation\?tex=(?P<tex>.*)$")


def _decode_tex(src: str) -> Optional[str]:
    """Extract and decode the TeX source from a Zhihu equation image URL."""
    m = _EQUATION_RE.search(src or "")
    if not m:
        return None
    # Zhihu encodes '+' as a literal space separator in the tex query.
    raw = m.group("tex").replace("+", " ")
    return unquote(raw).strip()


def _normalize_formulas(soup: BeautifulSoup) -> None:
    """Replace ``<img ...equation?tex=...>`` with ``<span class="ztex">`` nodes.

    A formula that is the sole meaningful child of its ``<p>`` is treated as a
    display (block) formula; otherwise it's inline.
    """
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        if "equation?tex=" not in src and img.get("eeimg") != "1":
            continue
        tex = _decode_tex(src)
        if not tex:
            # eeimg without decodable tex: fall back to alt text if present.
            tex = (img.get("alt") or "").strip()
        if not tex:
            continue
        parent = img.parent
        is_block = False
        if parent is not None and parent.name == "p":
            # Block if the paragraph has no real text and no other content.
            text = parent.get_text(strip=True)
            other_imgs = [i for i in parent.find_all("img") if i is not img]
            is_block = not text and not other_imgs
        span = soup.new_tag("span")
        span["class"] = "ztex"
        span["data-tex"] = tex
        if is_block:
            span["data-block"] = "true"
        img.replace_with(span)


def _append_references(soup: BeautifulSoup) -> None:
    """Rebuild a references section from inline reference ``<sup>`` markers.

    Zhihu stores references as ``<sup data-draft-type="reference"
    data-numero="N" data-text="..." data-url="...">[N]</sup>`` and renders the
    bibliography client-side, so it never reaches our HTML. We turn each marker
    into an anchor link and append a numbered reference list.
    """
    sups = soup.find_all("sup", attrs={"data-draft-type": "reference"})
    if not sups:
        return
    refs: list[tuple[str, str, str]] = []  # (numero, text, url)
    seen: set[str] = set()
    for sup in sups:
        numero = sup.get("data-numero") or str(len(refs) + 1)
        text = (sup.get("data-text") or "").strip()
        url = (sup.get("data-url") or "").strip()
        if numero not in seen:
            seen.add(numero)
            refs.append((numero, text, url))
        # Turn the inline marker into an anchor link to the reference entry.
        anchor = soup.new_tag("a", href=f"#ref-{numero}")
        anchor.string = f"[{numero}]"
        anchor["class"] = "ref-marker"
        sup.replace_with(anchor)

    if not refs:
        return
    hr = soup.new_tag("hr")
    heading = soup.new_tag("h2")
    heading.string = "参考"
    ol = soup.new_tag("ol")
    ol["class"] = "reference-list"
    for numero, text, url in sorted(refs, key=lambda r: _as_int(r[0])):
        li = soup.new_tag("li")
        li["id"] = f"ref-{numero}"
        if text:
            li.append(text + (" " if url else ""))
        if url:
            a = soup.new_tag("a", href=url)
            a.string = url
            li.append(a)
        if not text and not url:
            li.append("(无内容)")
        ol.append(li)
    for node in (hr, heading, ol):
        soup.append(node)


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


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


def _clean_image_url(url: Optional[str]) -> Optional[str]:
    """Return a usable title-image URL, or None if absent/blank."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    return url or None


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
    col_title = column.get("title") if isinstance(column, dict) else None
    col_url = column.get("url") if isinstance(column, dict) else None
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
        title_image=_clean_image_url(raw.get("titleImage")),
        column_title=col_title or None,
        column_url=col_url or None,
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


def batch_title(data: Optional[dict], kind: str, batch_id: Optional[str]) -> Optional[str]:
    """Extract a human title for a batch (collection/column/question) page.

    Looks up the matching entity by id, falling back to the only entity of that
    type if the id isn't keyed directly. Returns None if nothing is found, so
    callers can fall back to the document title or id.
    """
    if not data:
        return None
    ents = _entities(data)
    entity_map = {
        "collection": "favlists",
        "column": "columns",
        "question": "questions",
    }
    bucket = ents.get(entity_map.get(kind, ""), {}) or {}
    if not bucket:
        return None
    obj = None
    if batch_id and str(batch_id) in bucket:
        obj = bucket[str(batch_id)]
    elif len(bucket) == 1:
        obj = next(iter(bucket.values()))
    if not isinstance(obj, dict):
        return None
    title = obj.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None



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
