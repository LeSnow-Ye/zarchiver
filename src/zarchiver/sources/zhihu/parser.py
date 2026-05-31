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
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from zarchiver.models import ArchiveItem, Author, ContentType
from zarchiver.sources.base import SourceError
from zarchiver.sources.zhihu import urls as zurls

PLATFORM = "zhihu"

# Resolves a Zhihu video lens-id to {"url","cover","title","quality"} or None.
# Injected by the source (which has the browser session); None disables video
# resolution (the box degrades to a poster + label), keeping parsing offline.
VideoResolver = Callable[[str], Optional[dict]]


# ---------------------------------------------------------------------- #
# Content normalization (Zhihu-specific HTML quirks)
# ---------------------------------------------------------------------- #
def clean_content_html(
    html: str, *, video_resolver: Optional["VideoResolver"] = None
) -> str:
    """Normalize Zhihu's content HTML before it reaches generic exporters.

    * Convert equation images (``equation?tex=...``) into ``<span class="ztex"
      data-tex="..." data-block="...">`` so exporters can render real LaTeX
      (markdown ``$...$`` / MathJax) instead of downloading them as images.
    * Fix animated GIFs: Zhihu marks some ``<img>`` with an animated ``.gif``
      ``src`` but a *static* ``data-original`` JPEG frame. Prefer the ``.gif``
      so the asset pipeline downloads the animation, not a still.
    * Turn ``<a class="video-box">`` embeds into a real ``<video>`` (downloading
      the MP4) when a ``video_resolver`` is supplied; otherwise leave a readable
      poster + label.
    * Unwrap ``link.zhihu.com/?target=<encoded>`` redirects to the real URL.
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
    _normalize_gifs(soup)
    _normalize_videos(soup, video_resolver)

    # Unwrap remaining link.zhihu.com redirects.
    for a in soup.find_all("a", href=True):
        real = _unwrap_redirect(a["href"])
        if real:
            a["href"] = real

    _append_references(soup)

    return str(soup)


_GIF_RE = re.compile(r"\.gif(?:\?|$)", re.IGNORECASE)


def _normalize_gifs(soup: BeautifulSoup) -> None:
    """Ensure animated GIFs keep their ``.gif`` source, not a static frame.

    Zhihu's "gif2mp4" images carry the animated GIF in ``src`` (``..._1440w.gif``)
    but a static JPEG in ``data-original``. Since the asset pipeline prefers
    ``data-original``, it would otherwise save a still. When an ``<img>`` has a
    ``.gif`` anywhere in its src/original/token, we force ``src`` to the ``.gif``
    and drop the static ``data-original``/``data-thumbnail`` so the animation is
    what gets downloaded.
    """
    for img in soup.find_all("img"):
        src = img.get("src") or ""
        original = img.get("data-original") or ""
        gif_url = None
        if _GIF_RE.search(src):
            gif_url = src
        elif _GIF_RE.search(original):
            gif_url = original
        else:
            # data-thumbnail present + a token implies an animated image whose
            # .gif lives at <token>_1440w.gif on the same CDN host.
            token = img.get("data-original-token")
            thumb = img.get("data-thumbnail") or ""
            if token and thumb:
                base = re.sub(r"/[^/]+$", "", thumb)
                if base:
                    gif_url = f"{base}/{token}_1440w.gif"
        if not gif_url:
            continue
        img["src"] = gif_url
        for attr in ("data-original", "data-thumbnail", "data-actualsrc",
                     "srcset"):
            if img.has_attr(attr):
                del img[attr]
        classes = [c for c in (img.get("class") or []) if c]
        if "zarchiver-gif" not in classes:
            classes.append("zarchiver-gif")
        img["class"] = classes


def _video_lens_id(box) -> Optional[str]:
    """Extract a video lens-id from a video-box anchor."""
    lid = box.get("data-lens-id")
    if lid:
        return lid
    href = box.get("href", "")
    real = _unwrap_redirect(href) or href
    m = re.search(r"/video/(\d+)", real or "")
    return m.group(1) if m else None


def _normalize_videos(
    soup: BeautifulSoup, video_resolver: Optional["VideoResolver"]
) -> None:
    """Rewrite ``<a class="video-box">`` embeds.

    With a resolver, replace the box with a real ``<video>`` (poster + MP4 src)
    so the asset pipeline downloads the MP4 and exporters can play it offline.
    Without one (or on resolution failure), fall back to a poster image plus a
    labelled link — the original, offline-safe behavior.
    """
    for box in soup.select("a.video-box"):
        href = box.get("href", "")
        real = _unwrap_redirect(href)
        poster = box.get("data-poster")
        name = box.get("data-name") or ""
        lens_id = _video_lens_id(box)

        resolved = None
        if video_resolver is not None and lens_id:
            try:
                resolved = video_resolver(lens_id)
            except Exception:  # resolution must never break parsing
                resolved = None

        if resolved and resolved.get("url"):
            video = soup.new_tag("video")
            video["src"] = resolved["url"]
            video["controls"] = ""
            video["preload"] = "metadata"
            if resolved.get("cover") or poster:
                video["poster"] = resolved.get("cover") or poster
            if lens_id:
                video["data-zhihu-video"] = lens_id
            title = resolved.get("title") or name
            box.replace_with(video)
            if title:
                cap = soup.new_tag("p")
                cap.string = f"🎬 {title}"
                video.insert_after(cap)
            continue

        # Fallback: poster image + labelled link (offline-safe).
        box.attrs = {"href": real} if real else {}
        box.clear()
        new_content = []
        if poster:
            new_content.append(soup.new_tag("img", src=poster))
        label = soup.new_tag("span")
        label.string = f"🎬 视频{f'：{name}' if name else ''}"
        new_content.append(label)
        for node in new_content:
            box.append(node)


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
def parse_article(
    data: dict,
    article_id: str,
    *,
    video_resolver: Optional["VideoResolver"] = None,
) -> ArchiveItem:
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
        content_html=clean_content_html(
            raw.get("content") or "", video_resolver=video_resolver
        ),
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
# Pin (想法) — a short post: ordered text + image blocks, no real title.
# ---------------------------------------------------------------------- #
def parse_pin(
    data: dict,
    pin_id: str,
    *,
    video_resolver: Optional["VideoResolver"] = None,
) -> ArchiveItem:
    pins = _entities(data).get("pins", {})
    raw = pins.get(str(pin_id))
    if not raw:
        if len(pins) == 1:
            raw = next(iter(pins.values()))
        else:
            raise SourceError(f"pin {pin_id} not found in page data")

    pid = str(raw.get("id") or pin_id)
    # A pin's author is stored as a urlToken string referencing the users
    # entity (unlike articles/answers, which embed the author inline).
    author = _resolve_pin_author(data, raw.get("author"))
    content_html = _pin_content_html(raw)
    title = _pin_title(raw, content_html)
    item = ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.PIN,
        source_id=pid,
        url=zurls.pin_url(pid),
        title=title,
        content_html=clean_content_html(
            content_html, video_resolver=video_resolver
        ),
        author=author,
        created=ArchiveItem.epoch_to_dt(raw.get("created")),
        updated=ArchiveItem.epoch_to_dt(raw.get("updated")),
        voteup_count=raw.get("likeCount"),
        comment_count=raw.get("commentCount"),
        topics=_topics(raw),
        excerpt=_strip_html(raw.get("excerptTitle") or "")[:200],
    )
    return item


def _resolve_pin_author(data: dict, token) -> Optional[Author]:
    """Resolve a pin's author from the ``users`` entity by urlToken/id."""
    if isinstance(token, dict):
        return _make_author(token)
    if not isinstance(token, str) or not token:
        return None
    users = _entities(data).get("users", {})
    user = users.get(token)
    if user is None:
        # Fall back to matching by urlToken across the entity map.
        for u in users.values():
            if isinstance(u, dict) and u.get("urlToken") == token:
                user = u
                break
    if isinstance(user, dict):
        return _make_author(user)
    # No user record: keep the token as a best-effort name.
    return Author(name=token)


def _pin_content_html(raw: dict) -> str:
    """Assemble a pin's body HTML from its ordered ``content`` blocks.

    Text blocks carry HTML directly; image blocks carry only URLs, so we
    synthesize ``<img>`` tags pointing at the full-resolution ``originalUrl``
    (falling back to the watermarked or thumbnail URL). Blocks are concatenated
    in order, so text and images interleave as the author arranged them.
    """
    blocks = raw.get("content")
    if not isinstance(blocks, list):
        # Older/alternate payloads expose a single ``contentHtml`` string.
        return raw.get("contentHtml") or ""
    parts: list[str] = []
    for blk in blocks:
        if not isinstance(blk, dict):
            continue
        btype = blk.get("type")
        if btype == "image":
            src = (
                blk.get("originalUrl")
                or blk.get("watermarkUrl")
                or blk.get("url")
            )
            if src:
                parts.append(f'<p><img src="{src}"/></p>')
        else:
            # Text (and any unknown textual block): use its HTML content.
            html = blk.get("content") or blk.get("ownText") or ""
            if html:
                parts.append(f"<div>{html}</div>")
    if not parts:
        return raw.get("contentHtml") or ""
    return "".join(parts)


def _pin_title(raw: dict, content_html: str) -> str:
    """Synthesize a display title for a titleless pin.

    Uses the first line of the excerpt (or body) up to a separator, trimmed to
    a sane length — enough to make a meaningful filename and note heading.
    """
    source = raw.get("excerptTitle") or content_html or ""
    text = _strip_html(source).strip()
    if not text:
        return "想法"
    # First sentence/line, split on common separators and pipes.
    first = re.split(r"[\n|｜]|<br", text, maxsplit=1)[0].strip()
    first = first or text
    if len(first) > 60:
        first = first[:60].rstrip() + "…"
    return first or "想法"


def _strip_html(html: str) -> str:
    """Plain text of an HTML fragment, with <br> treated as spaces."""
    if not html:
        return ""
    soup = BeautifulSoup(html.replace("<br>", " ").replace("<br/>", " "), "html.parser")
    return soup.get_text(" ", strip=True)


# ---------------------------------------------------------------------- #
# Answer
# ---------------------------------------------------------------------- #
def parse_answer(
    data: dict,
    answer_id: str,
    question_id: Optional[str] = None,
    *,
    video_resolver: Optional["VideoResolver"] = None,
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
        content_html=clean_content_html(
            raw.get("content") or "", video_resolver=video_resolver
        ),
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
# Items / metadata APIs (columns + collections)
# ---------------------------------------------------------------------- #
def archivable_entries_from_api(payload: Optional[dict]) -> list[dict]:
    """Yield the archivable content objects from an items/answers API page.

    Two shapes are handled: column items expose the object at the top level of
    each ``data`` entry; collection items wrap it in a ``content`` field
    (``data[].content``). Only archivable item types (article / answer / pin)
    that aren't deleted and carry a ``url`` are kept; anything else (videos,
    ads, deleted) is skipped. The returned dicts are the raw API objects — feed
    them to :func:`item_from_api_entry` to build items, or read ``url`` for the
    page-based path.
    """
    if not isinstance(payload, dict):
        return []
    out: list[dict] = []
    for entry in payload.get("data", []) or []:
        if not isinstance(entry, dict):
            continue
        obj = entry.get("content") if isinstance(entry.get("content"), dict) else entry
        if not isinstance(obj, dict) or obj.get("is_deleted"):
            continue
        if obj.get("type") not in ("article", "answer", "pin"):
            continue
        if not isinstance(obj.get("url"), str) or not obj.get("url"):
            continue
        out.append(obj)
    return out


def item_urls_from_api(payload: Optional[dict]) -> list[str]:
    """Extract item URLs from a column/collection ``/items`` API page.

    Thin wrapper over :func:`archivable_entries_from_api`: returns just the
    canonical web URLs (the form :func:`classify` recognizes).
    """
    return [
        _canonical_item_url(obj["url"])
        for obj in archivable_entries_from_api(payload)
    ]


def _g(obj: dict, *keys, default=None):
    """First present, non-None value among ``keys`` (snake_case then camel)."""
    for k in keys:
        v = obj.get(k)
        if v is not None:
            return v
    return default


def web_url_from_api_entry(obj: dict) -> str:
    """Canonical web URL for an API entry (the form :func:`classify` accepts).

    Answer entries from the question API expose only ``/api/v4/answers/<id>``
    (no question id), so we rebuild ``/question/<qid>/answer/<aid>`` from the
    embedded ``question`` when possible. Articles/pins use their own URL
    builders; otherwise fall back to canonicalizing the entry's ``url``.
    """
    kind = obj.get("type")
    oid = str(obj.get("id") or "")
    if kind == "answer" and oid:
        q = obj.get("question") if isinstance(obj.get("question"), dict) else {}
        qid = str(q.get("id") or "")
        if qid:
            return zurls.answer_url(qid, oid)
    if kind == "article" and oid:
        return zurls.article_url(oid)
    if kind == "pin" and oid:
        return zurls.pin_url(oid)
    return _canonical_item_url(obj.get("url") or "")


def item_from_api_entry(
    obj: dict, *, video_resolver: Optional["VideoResolver"] = None
) -> Optional[ArchiveItem]:
    """Build a full :class:`ArchiveItem` directly from a listing-API object.

    The collection/column/answer APIs embed the complete ``content`` body, so a
    batch item can be archived without opening its page. Dispatches on the
    object's ``type``. Returns ``None`` when the entry lacks usable content (or
    has an unexpected shape) so the caller can fall back to fetching the page.
    Never raises: any parse surprise degrades to ``None``.
    """
    try:
        kind = obj.get("type")
        if kind == "answer":
            return _answer_from_api(obj, video_resolver)
        if kind == "article":
            return _article_from_api(obj, video_resolver)
        if kind == "pin":
            return _pin_from_api(obj, video_resolver)
    except Exception:
        return None
    return None


def _answer_from_api(obj: dict, video_resolver) -> Optional[ArchiveItem]:
    body = obj.get("content")
    if not isinstance(body, str) or not body.strip():
        return None
    question = obj.get("question") if isinstance(obj.get("question"), dict) else {}
    qid = str(question.get("id") or "")
    aid = str(obj.get("id") or "")
    q_title = question.get("title") or "(question)"
    return ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.ANSWER,
        source_id=aid,
        url=zurls.answer_url(qid, aid) if qid else
            f"https://www.zhihu.com/answer/{aid}",
        title=q_title,
        content_html=clean_content_html(body, video_resolver=video_resolver),
        author=_make_author(obj.get("author")),
        created=ArchiveItem.epoch_to_dt(_g(obj, "created_time", "createdTime", "created")),
        updated=ArchiveItem.epoch_to_dt(_g(obj, "updated_time", "updatedTime", "updated")),
        question_title=q_title,
        question_url=zurls.question_url(qid) if qid else None,
        voteup_count=_g(obj, "voteup_count", "voteupCount"),
        comment_count=_g(obj, "comment_count", "commentCount"),
        excerpt=obj.get("excerpt") or "",
    )


def _article_from_api(obj: dict, video_resolver) -> Optional[ArchiveItem]:
    body = obj.get("content")
    if not isinstance(body, str) or not body.strip():
        return None
    aid = str(obj.get("id") or "")
    column = obj.get("column") if isinstance(obj.get("column"), dict) else {}
    return ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.ARTICLE,
        source_id=aid,
        url=zurls.article_url(aid),
        title=obj.get("title") or "(untitled)",
        content_html=clean_content_html(body, video_resolver=video_resolver),
        author=_make_author(obj.get("author")),
        created=ArchiveItem.epoch_to_dt(_g(obj, "created", "created_time", "createdTime")),
        updated=ArchiveItem.epoch_to_dt(_g(obj, "updated", "updated_time", "updatedTime")),
        title_image=_clean_image_url(
            _g(obj, "title_image", "titleImage", "image_url")
        ),
        column_title=column.get("title") or None,
        column_url=column.get("url") or None,
        voteup_count=_g(obj, "voteup_count", "voteupCount"),
        comment_count=_g(obj, "comment_count", "commentCount"),
        topics=_topics(obj),
        excerpt=obj.get("excerpt") or "",
    )


def _pin_from_api(obj: dict, video_resolver) -> Optional[ArchiveItem]:
    content_html = _pin_content_html(obj)
    if not content_html or not content_html.strip():
        return None
    pid = str(obj.get("id") or "")
    title = _pin_title(obj, content_html)
    return ArchiveItem(
        platform=PLATFORM,
        content_type=ContentType.PIN,
        source_id=pid,
        url=zurls.pin_url(pid),
        title=title,
        content_html=clean_content_html(content_html, video_resolver=video_resolver),
        author=_make_author(obj.get("author")),
        created=ArchiveItem.epoch_to_dt(_g(obj, "created", "created_time", "createdTime")),
        updated=ArchiveItem.epoch_to_dt(_g(obj, "updated", "updated_time", "updatedTime")),
        voteup_count=_g(obj, "like_count", "likeCount", "voteup_count"),
        comment_count=_g(obj, "comment_count", "commentCount"),
        topics=_topics(obj),
        excerpt=_strip_html(obj.get("excerpt_title") or obj.get("excerptTitle") or "")[:200],
    )


def question_title_from_answers(payload: Optional[dict]) -> Optional[str]:
    """Resolve a question's title from a ``/questions/<id>/answers`` API page."""
    if not isinstance(payload, dict):
        return None
    for entry in payload.get("data", []) or []:
        if not isinstance(entry, dict):
            continue
        q = entry.get("question")
        if isinstance(q, dict) and isinstance(q.get("title"), str) and q["title"].strip():
            return q["title"].strip()
    return None


def api_paging_next(payload: Optional[dict]) -> Optional[str]:
    """Return the next-page URL from an API ``paging`` block, or None at end."""
    if not isinstance(payload, dict):
        return None
    paging = payload.get("paging") or {}
    if not isinstance(paging, dict) or paging.get("is_end", True):
        return None
    nxt = paging.get("next")
    return nxt if isinstance(nxt, str) and nxt else None


def collection_title_from_api(payload: Optional[dict]) -> Optional[str]:
    """Title from a ``GET /api/v4/collections/<id>`` metadata response."""
    if not isinstance(payload, dict):
        return None
    coll = payload.get("collection")
    obj = coll if isinstance(coll, dict) else payload
    title = obj.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def column_title_from_api(payload: Optional[dict]) -> Optional[str]:
    """Title from a ``GET /api/v4/columns/<id>`` metadata response."""
    if not isinstance(payload, dict):
        return None
    title = payload.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _canonical_item_url(url: str) -> str:
    """Normalize an API item URL to the canonical web URL ``fetch`` expects.

    The APIs sometimes return ``http://`` or ``api.zhihu.com`` style hosts;
    answers in particular may come back as ``/answer/<id>`` only. We coerce to
    the standard ``https://www.zhihu.com`` / ``https://zhuanlan.zhihu.com``
    forms that :func:`classify` recognizes.
    """
    url = url.strip().replace("http://", "https://", 1)
    # Articles live on zhuanlan; everything else on www.
    return url

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
