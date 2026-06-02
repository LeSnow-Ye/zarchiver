"""The Zhihu :class:`Source` implementation.

Glues the browser and parser together: classifies a URL, navigates with the
shared browser, extracts the embedded data, and produces
:class:`~zarchiver.models.ArchiveItem` objects. Batch targets (collections,
columns, questions) are handled by scrolling to load more entries and visiting
each item's page.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterator, Optional

from zarchiver.config import Config
from zarchiver.models import ArchiveItem, BatchInfo, BatchKind
from zarchiver.sources.base import Source, SourceError
from zarchiver.sources.zhihu import comments as C
from zarchiver.sources.zhihu import parser as P
from zarchiver.sources.zhihu import urls as zurls
from zarchiver.sources.zhihu import video as V
from zarchiver.sources.zhihu.browser import ZhihuBrowser
from zarchiver.sources.zhihu.urls import ZhihuKind, ZhihuTarget

log = logging.getLogger(__name__)


class ZhihuSource(Source):
    platform = "zhihu"

    def __init__(self, config: Config, *, browser: Optional[ZhihuBrowser] = None):
        self.config = config
        self._browser = browser
        self._owns_browser = browser is None

    # ------------------------------------------------------------------ #
    # Source interface
    # ------------------------------------------------------------------ #
    def supports(self, url: str) -> bool:
        return zurls.is_zhihu_url(url)

    def fetch(self, url: str) -> ArchiveItem:
        target = zurls.classify(url)
        log.debug("classified %s as %s", url, target.kind.value)
        if target.kind == ZhihuKind.ARTICLE:
            item = self._fetch_article(target.article_id, url)
        elif target.kind == ZhihuKind.ANSWER:
            item = self._fetch_answer(target.answer_id, target.question_id, url)
        elif target.kind == ZhihuKind.PIN:
            item = self._fetch_pin(target.pin_id, url)
        elif target.is_batch:
            raise SourceError(
                f"{url} is a batch ({target.kind.value}); use fetch_batch()"
            )
        else:
            raise SourceError(f"unsupported or unrecognized Zhihu URL: {url}")
        return item

    def fetch_batch(self, url: str) -> Iterator[ArchiveItem]:
        target = zurls.classify(url)
        if target.kind == ZhihuKind.COLLECTION:
            yield from self._fetch_collection(target)
        elif target.kind == ZhihuKind.COLUMN:
            yield from self._fetch_column(target)
        elif target.kind == ZhihuKind.QUESTION:
            yield from self._fetch_question_answers(target)
        elif target.kind in (ZhihuKind.ARTICLE, ZhihuKind.ANSWER, ZhihuKind.PIN):
            # A single-item URL passed to batch mode: yield the one item.
            yield self.fetch(url)
        else:
            raise SourceError(f"unsupported batch URL: {url}")

    def close(self) -> None:
        if self._browser and self._owns_browser:
            self._browser.close()
            self._browser = None

    # ------------------------------------------------------------------ #
    # Browser access
    # ------------------------------------------------------------------ #
    @property
    def browser(self) -> ZhihuBrowser:
        if self._browser is None:
            log.debug("starting browser on first use")
            self._browser = ZhihuBrowser(self.config.browser)
            self._browser.start()
        return self._browser

    def _page_html(self, url: str) -> str:
        page = self.browser.new_page()
        try:
            self.browser.goto(page, url)
            html = page.content()
            log.debug("fetched %d bytes of HTML from %s", len(html), url)
            return html
        finally:
            page.close()

    def _get_json(self, url: str) -> Optional[dict]:
        """GET a Zhihu JSON API endpoint through the browser context.

        Going through ``context.request`` reuses the session's cookies and
        passes Zhihu's hotlink checks. Zhihu intermittently 403s API calls (the
        same edge quirk as navigation), so a transient 403 is retried briefly.
        Returns None on persistent failure so a failed comment fetch never
        aborts archiving the item itself.
        """
        headers = {
            "x-requested-with": "fetch",
            "referer": "https://www.zhihu.com/",
        }
        for attempt in range(3):
            try:
                resp = self.browser.context.request.get(url, headers=headers)
                if resp.status == 200:
                    return resp.json()
                if resp.status == 403 and attempt < 2:
                    # Transient edge 403: back off briefly and retry.
                    log.debug(
                        "comment API %s -> http 403 (retry %d)", url, attempt + 1
                    )
                    time.sleep(0.6)
                    continue
                log.debug("comment API %s -> http %s", url, resp.status)
                return None
            except Exception as exc:
                log.debug("comment API request failed (%s): %s", url, exc)
                return None
        return None

    def _video_resolver(self):
        """Build a video resolver bound to this session, or None if disabled."""
        if not self.config.archive.download_videos:
            return None
        quality = self.config.archive.video_quality

        def resolve(lens_id: str) -> Optional[dict]:
            return V.resolve_video(self._get_json, lens_id, quality=quality)

        return resolve

    def enrich(self, item: ArchiveItem) -> None:
        """Attach comments for an item the pipeline has decided to keep.

        Called by the pipeline only for archived/updated items, so skipped
        duplicates never trigger a comment crawl. Best-effort: a failed comment
        fetch is logged and never blocks archiving.
        """
        if not self.config.archive.comments:
            return
        resource_type = C.resource_type_for(item.content_type)
        if not resource_type:
            return
        try:
            item.comments = C.fetch_comments(
                self._get_json,
                resource_type,
                item.source_id,
                max_comments=self.config.archive.max_comments,
            )
        except Exception as exc:  # comments must never block archiving
            log.warning("comment fetch failed for %r: %s", item.title, exc)

    # ------------------------------------------------------------------ #
    # Single items
    # ------------------------------------------------------------------ #
    def _fetch_article(self, article_id: Optional[str], url: str) -> ArchiveItem:
        html = self._page_html(url)
        data = P.extract_initial_data(html)
        if data:
            try:
                item = P.parse_article(
                    data, article_id or "", video_resolver=self._video_resolver()
                )
                log.debug(
                    "parsed article %s from js-initialData: %r (%d chars, "
                    "%d images, title_image=%s)",
                    item.source_id, item.title, len(item.content_html),
                    item.content_html.count("<img"), bool(item.title_image),
                )
                return item
            except SourceError as exc:
                log.debug("js-initialData parse failed (%s); trying DOM", exc)
        else:
            log.debug("no js-initialData for %s; trying DOM", url)
        item = P.parse_article_dom(html, article_id or "", url)
        log.debug("parsed article %s from DOM fallback", item.source_id)
        return item

    def _fetch_answer(
        self, answer_id: Optional[str], question_id: Optional[str], url: str
    ) -> ArchiveItem:
        html = self._page_html(url)
        data = P.extract_initial_data(html)
        if not data:
            raise SourceError(f"no embedded data for answer at {url}")
        item = P.parse_answer(
            data, answer_id or "", question_id,
            video_resolver=self._video_resolver(),
        )
        log.debug(
            "parsed answer %s by %s (%d chars)",
            item.source_id,
            item.author.name if item.author else "?",
            len(item.content_html),
        )
        return item

    def _fetch_pin(self, pin_id: Optional[str], url: str) -> ArchiveItem:
        html = self._page_html(url)
        data = P.extract_initial_data(html)
        if not data:
            raise SourceError(f"no embedded data for pin at {url}")
        item = P.parse_pin(
            data, pin_id or "", video_resolver=self._video_resolver()
        )
        log.debug(
            "parsed pin %s by %s (%d chars, %d images)",
            item.source_id,
            item.author.name if item.author else "?",
            len(item.content_html),
            item.content_html.count("<img"),
        )
        return item

    # ------------------------------------------------------------------ #
    # Batches
    # ------------------------------------------------------------------ #
    def _max_items(self) -> int:
        return self.config.browser.max_items  # 0 = unlimited

    def _scroll_collect_links(
        self, url: str, link_pattern: str, *, cap: Optional[int] = None,
    ) -> tuple[list[str], Optional[dict]]:
        """Open a batch page, scroll to load entries, return links + page data.

        Used for question batches (answers lazy-load on scroll). Candidate URLs
        are harvested from several signals, because Zhihu's lazy-loaded answer
        cards don't always expose a clean ``<a>`` href: plain anchors,
        ``meta[itemprop="url"]`` tags, and answer ids on
        ``.AnswerItem[data-zop]`` (reconstructed into answer URLs). All
        candidates are then filtered by ``link_pattern``.

        ``cap`` limits how many links to collect (defaults to the configured
        ``max_items``).

        Returns ``(links, initial_data)`` where ``initial_data`` is the page's
        parsed ``js-initialData`` (used to resolve the batch title).
        """
        page = self.browser.new_page()
        found: list[str] = []
        seen: set[str] = set()
        pat = re.compile(link_pattern)
        if cap is None:
            cap = self._max_items()
        initial_data: Optional[dict] = None
        # JS that returns every candidate item URL currently in the DOM.
        harvest_js = """() => {
            const urls = new Set();
            document.querySelectorAll('a[href]').forEach(a => urls.add(a.href));
            document.querySelectorAll('meta[itemprop="url"]').forEach(
                m => { if (m.content) urls.add(m.content); });
            document.querySelectorAll('.AnswerItem[data-zop]').forEach(el => {
                try {
                    const z = JSON.parse(el.getAttribute('data-zop'));
                    const q = window.location.pathname.match(/question\\/(\\d+)/);
                    if (z.itemId && q) {
                        urls.add('https://www.zhihu.com/question/' + q[1] +
                                 '/answer/' + z.itemId);
                    }
                } catch (e) {}
            });
            return [...urls];
        }"""
        try:
            self.browser.goto(page, url)
            # Capture the batch's own metadata before scrolling churns the DOM.
            initial_data = P.extract_initial_data(page.content())
            log.debug("scrolling %s to collect item links (cap=%s)", url, cap or "∞")
            stagnant = 0
            last_count = 0
            for i in range(80):  # hard ceiling on scroll iterations
                candidates = page.evaluate(harvest_js)
                for h in candidates:
                    if h and pat.search(h) and h not in seen:
                        seen.add(h)
                        found.append(h)
                if cap and len(found) >= cap:
                    found = found[:cap]
                    log.debug("reached cap of %d links; stopping scroll", cap)
                    break
                if len(found) == last_count:
                    stagnant += 1
                    if stagnant >= 4:
                        log.debug(
                            "no new links after %d scrolls; stopping", i + 1
                        )
                        break
                else:
                    log.debug("scroll %d: %d links so far", i + 1, len(found))
                    stagnant = 0
                    last_count = len(found)
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1100)
            log.info("collected %d item links from %s", len(found), url)
            return found, initial_data
        finally:
            page.close()

    # Page size for the items APIs (columns/collections).
    _API_PAGE_LIMIT = 20
    # Fields requested from the question answers API so each entry carries the
    # full body (and metadata) — lets us archive without opening each page.
    _ANSWER_API_INCLUDE = (
        "data[*].content,voteup_count,comment_count,"
        "updated_time,created_time,author"
    )

    def _fetch_collection(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        cid = target.collection_id
        entries = self._walk_api_pages(
            f"https://www.zhihu.com/api/v4/collections/{cid}/items"
            f"?offset=0&limit={self._API_PAGE_LIMIT}",
            label="collection",
        )
        title = P.collection_title_from_api(
            self._get_json(f"https://www.zhihu.com/api/v4/collections/{cid}")
        )
        batch = self._make_batch(
            BatchKind.COLLECTION, title, cid, target.raw_url
        )
        # Zhihu lists newest items first. Archive from the tail toward the
        # front so repeated exports keep the collection's natural chronology.
        yield from self._iter_api_or_fetch(list(reversed(entries)), batch)

    def _fetch_column(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        cid = target.column_id
        entries = self._walk_api_pages(
            f"https://www.zhihu.com/api/v4/columns/{cid}/items"
            f"?limit={self._API_PAGE_LIMIT}&ws_qiangzhisafe=0&offset=0",
            label="column",
        )
        title = P.column_title_from_api(
            self._get_json(f"https://www.zhihu.com/api/v4/columns/{cid}")
        )
        batch = self._make_batch(BatchKind.COLUMN, title, cid, target.raw_url)
        # Zhihu lists newest items first. Archive from the tail toward the
        # front so repeated exports keep the column's natural chronology.
        yield from self._iter_api_or_fetch(list(reversed(entries)), batch)

    def _walk_api_pages(self, first_url: str, *, label: str) -> list[dict]:
        """Page through an items/answers API, collecting archivable entries.

        Walks ``offset``-paginated pages via ``paging.next`` until the API
        reports the end, the configured ``max_items`` cap is reached, or a
        request fails. Entries are the raw API objects (carrying the full
        ``content`` body), deduped by canonical URL while preserving order.
        """
        cap = self._max_items()
        entries: list[dict] = []
        seen: set[str] = set()
        url: Optional[str] = first_url
        page = 0
        while url is not None:
            if cap and len(entries) >= cap:
                break
            payload = self._get_json(url)
            if not payload:
                log.warning(
                    "%s items request failed; stopping at %d", label, len(entries)
                )
                break
            page += 1
            new = 0
            for obj in P.archivable_entries_from_api(payload):
                key = P.web_url_from_api_entry(obj)
                if key in seen:
                    continue
                seen.add(key)
                entries.append(obj)
                new += 1
                if cap and len(entries) >= cap:
                    break
            log.info(
                "%s page %d: +%d item(s) (%d total)", label, page, new, len(entries)
            )
            url = P.api_paging_next(payload)
        if cap:
            entries = entries[:cap]
        log.info(
            "collected %d %s item(s) across %d page(s)", len(entries), label, page
        )
        return entries

    def _collect_api_item_urls(self, first_url: str, *, label: str) -> list[str]:
        """Canonical item URLs from a column/collection ``/items`` API.

        Thin wrapper over :meth:`_walk_api_pages` (kept for callers that only
        need URLs, e.g. when ``prefer_api_content`` is off).
        """
        return [
            P._canonical_item_url(obj["url"])
            for obj in self._walk_api_pages(first_url, label=label)
        ]

    def _iter_api_or_fetch(
        self, entries: list[dict], batch: Optional[BatchInfo]
    ) -> Iterator[ArchiveItem]:
        """Yield items from API entries, opening the page only when needed.

        When ``prefer_api_content`` is on, each entry is built directly from its
        API JSON (full body included) — no page navigation. If that yields no
        usable content (empty body or unexpected shape), or the preference is
        off, the item's page is fetched as a fallback. Comments are *not*
        fetched here — the pipeline calls :meth:`enrich` once it decides to keep
        an item, so skipped duplicates never trigger a comment crawl.
        """
        cap = self._max_items()
        prefer_api = self.config.archive.prefer_api_content
        total = min(len(entries), cap) if cap else len(entries)
        resolver = self._video_resolver()
        count = 0
        for obj in entries:
            if cap and count >= cap:
                return
            url = P.web_url_from_api_entry(obj)
            item: Optional[ArchiveItem] = None
            if prefer_api:
                item = P.item_from_api_entry(obj, video_resolver=resolver)
            if item is not None:
                log.info(
                    "item %d/%d from API: %s", count + 1, total, url
                )
            else:
                log.info(
                    "fetching item %d/%d (page): %s", count + 1, total, url
                )
                try:
                    item = self.fetch(url)
                except SourceError as exc:
                    log.warning("skipping %s: %s", url, exc)
                    continue
            item.batch = batch
            count += 1
            yield item
            self.browser.polite_delay()

    def _fetch_question_answers(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        qid = target.question_id
        if self.config.archive.prefer_api_content:
            entries = self._walk_api_pages(
                f"https://www.zhihu.com/api/v4/questions/{qid}/answers"
                f"?include={self._ANSWER_API_INCLUDE}"
                f"&limit={self._API_PAGE_LIMIT}&offset=0",
                label="question",
            )
            if entries:
                title = P.question_title_from_answers({"data": entries})
                batch = self._make_batch(
                    BatchKind.QUESTION, title, qid, target.raw_url
                )
                yield from self._iter_api_or_fetch(entries, batch)
                return
            log.info("question answers API returned nothing; falling back to scroll")
        # Fallback: scroll the rendered page for answer links, fetch each page.
        links, data = self._scroll_collect_links(
            target.raw_url, rf"/question/{qid}/answer/\d+"
        )
        batch = self._make_batch_from_data(
            BatchKind.QUESTION, data, target.question_id, target.raw_url
        )
        yield from self._iter_item_links(links, batch)

    def _make_batch(
        self,
        kind: BatchKind,
        title: Optional[str],
        batch_id: Optional[str],
        url: str,
    ) -> BatchInfo:
        """Build batch context from an already-resolved title."""
        if not title:
            # Fall back to the batch id so a subdir always has a stable name.
            title = f"{kind.value}-{batch_id}" if batch_id else kind.value
            log.debug("no %s title found; using fallback %r", kind.value, title)
        log.info("batch %s: %r", kind.value, title)
        return BatchInfo(kind=kind, title=title, url=url, id=batch_id)

    def _make_batch_from_data(
        self,
        kind: BatchKind,
        data: Optional[dict],
        batch_id: Optional[str],
        url: str,
    ) -> BatchInfo:
        """Build batch context, resolving the title from page ``js-initialData``.

        Used by question batches, which still scrape the rendered page.
        """
        return self._make_batch(
            kind, P.batch_title(data, kind.value, batch_id), batch_id, url
        )


    def _iter_item_links(
        self, links: list[str], batch: Optional[BatchInfo] = None
    ) -> Iterator[ArchiveItem]:
        cap = self._max_items()
        total = min(len(links), cap) if cap else len(links)
        count = 0
        for link in links:
            if cap and count >= cap:
                return
            log.info("fetching item %d/%d: %s", count + 1, total, link)
            try:
                item = self.fetch(link)
            except SourceError as exc:
                log.warning("skipping %s: %s", link, exc)
                continue
            item.batch = batch
            count += 1
            yield item
            self.browser.polite_delay()
