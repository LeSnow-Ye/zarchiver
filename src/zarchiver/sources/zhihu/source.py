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
        self._attach_comments(item)
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

    def _attach_comments(self, item: ArchiveItem) -> None:
        """Fetch and attach comments for ``item`` per config (best-effort)."""
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
                item = P.parse_article(data, article_id or "")
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
        item = P.parse_answer(data, answer_id or "", question_id)
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
        item = P.parse_pin(data, pin_id or "")
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
        detect_max_page: bool = False,
    ) -> tuple[list[str], Optional[dict], Optional[int]]:
        """Open a batch page, scroll to load entries, return links + page data.

        Candidate URLs are harvested from several signals, because Zhihu's
        lazy-loaded answer/article cards don't always expose a clean ``<a>``
        href: plain anchors, ``meta[itemprop="url"]`` tags, and answer ids on
        ``.AnswerItem[data-zop]`` (reconstructed into answer URLs). All
        candidates are then filtered by ``link_pattern``.

        ``cap`` limits how many links to collect (defaults to the configured
        ``max_items``; pass a per-page remaining budget when paginating).
        ``detect_max_page`` reads the highest page number from the pagination
        control (used for collections).

        Returns ``(links, initial_data, max_page)`` where ``initial_data`` is
        the page's parsed ``js-initialData`` and ``max_page`` is the pager's
        last page number (or None).
        """
        page = self.browser.new_page()
        found: list[str] = []
        seen: set[str] = set()
        pat = re.compile(link_pattern)
        max_page: Optional[int] = None
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
            if detect_max_page:
                max_page = self._read_max_page(page)
            log.info("collected %d item links from %s", len(found), url)
            return found, initial_data, max_page
        finally:
            page.close()

    @staticmethod
    def _read_max_page(page) -> Optional[int]:
        """Read the highest page number from a Zhihu pagination control."""
        try:
            value = page.evaluate(
                """() => {
                    const els = document.querySelectorAll(
                        '.Pagination button, .Pagination a');
                    let max = 0;
                    els.forEach(e => {
                        const n = parseInt(e.textContent, 10);
                        if (!isNaN(n) && n > max) max = n;
                    });
                    return max;
                }"""
            )
            return int(value) if value and value > 0 else None
        except Exception:
            return None

    # Item links found inside a collection page (articles + answers + pins).
    _COLLECTION_LINK_RE = (
        r"(zhuanlan\.zhihu\.com/p/\d+|/question/\d+/answer/\d+|/answer/\d+"
        r"|/pin/\d+)"
    )

    def _fetch_collection(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        # Collections are paginated (~20 items/page) via ?page=N, unlike columns
        # and questions which lazy-load on scroll. Walk pages until one yields
        # no new links, the detected last page is passed, or the cap is reached.
        links, data = self._collect_collection_links(target.raw_url)
        batch = self._make_batch(
            BatchKind.COLLECTION, data, target.collection_id, target.raw_url
        )
        yield from self._iter_item_links(links, batch)

    def _collect_collection_links(
        self, url: str
    ) -> tuple[list[str], Optional[dict]]:
        """Walk a collection's pages, collecting all item links.

        Returns ``(links, page1_initial_data)`` — the first page's embedded data
        is kept for resolving the collection's title.
        """
        base = zurls.strip_page(url)
        cap = self._max_items()
        all_links: list[str] = []
        seen: set[str] = set()
        first_data: Optional[dict] = None
        max_page = 1
        page_num = 1
        while page_num <= max_page:
            remaining = (cap - len(all_links)) if cap else None
            if remaining is not None and remaining <= 0:
                break
            page_url = zurls.with_page(base, page_num)
            links, data, detected_max = self._scroll_collect_links(
                page_url, self._COLLECTION_LINK_RE, cap=remaining,
                detect_max_page=True,
            )
            if page_num == 1:
                first_data = data
                # Trust the pager's reported last page as the upper bound.
                if detected_max and detected_max > 1:
                    max_page = detected_max
                    log.info("collection has %d page(s)", max_page)
            new = [h for h in links if h not in seen]
            if not new:
                log.debug("page %d yielded no new links; stopping", page_num)
                break
            for h in new:
                seen.add(h)
                all_links.append(h)
            log.info(
                "collection page %d/%d: +%d links (%d total)",
                page_num, max_page, len(new), len(all_links),
            )
            page_num += 1
        if cap:
            all_links = all_links[:cap]
        log.info(
            "collected %d item links across %d page(s)",
            len(all_links), min(page_num, max_page),
        )
        return all_links, first_data

    def _fetch_column(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        links, data, _ = self._scroll_collect_links(
            target.raw_url, r"zhuanlan\.zhihu\.com/p/\d+"
        )
        batch = self._make_batch(
            BatchKind.COLUMN, data, target.column_id, target.raw_url
        )
        yield from self._iter_item_links(links, batch)

    def _fetch_question_answers(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        links, data, _ = self._scroll_collect_links(
            target.raw_url, rf"/question/{target.question_id}/answer/\d+"
        )
        batch = self._make_batch(
            BatchKind.QUESTION, data, target.question_id, target.raw_url
        )
        yield from self._iter_item_links(links, batch)

    def _make_batch(
        self,
        kind: BatchKind,
        data: Optional[dict],
        batch_id: Optional[str],
        url: str,
    ) -> BatchInfo:
        """Build batch context, resolving the best available title."""
        title = P.batch_title(data, kind.value, batch_id)
        if not title:
            # Fall back to the batch id so a subdir always has a stable name.
            title = f"{kind.value}-{batch_id}" if batch_id else kind.value
            log.debug("no %s title found; using fallback %r", kind.value, title)
        log.info("batch %s: %r", kind.value, title)
        return BatchInfo(kind=kind, title=title, url=url, id=batch_id)

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
