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
from typing import Iterator, Optional

from zarchiver.config import Config
from zarchiver.models import ArchiveItem, BatchInfo, BatchKind
from zarchiver.sources.base import Source, SourceError
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
            return self._fetch_article(target.article_id, url)
        if target.kind == ZhihuKind.ANSWER:
            return self._fetch_answer(target.answer_id, target.question_id, url)
        if target.is_batch:
            raise SourceError(
                f"{url} is a batch ({target.kind.value}); use fetch_batch()"
            )
        raise SourceError(f"unsupported or unrecognized Zhihu URL: {url}")

    def fetch_batch(self, url: str) -> Iterator[ArchiveItem]:
        target = zurls.classify(url)
        if target.kind == ZhihuKind.COLLECTION:
            yield from self._fetch_collection(target)
        elif target.kind == ZhihuKind.COLUMN:
            yield from self._fetch_column(target)
        elif target.kind == ZhihuKind.QUESTION:
            yield from self._fetch_question_answers(target)
        elif target.kind in (ZhihuKind.ARTICLE, ZhihuKind.ANSWER):
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

    # ------------------------------------------------------------------ #
    # Batches
    # ------------------------------------------------------------------ #
    def _max_items(self) -> int:
        return self.config.browser.max_items  # 0 = unlimited

    def _scroll_collect_links(
        self, url: str, link_pattern: str
    ) -> tuple[list[str], Optional[dict]]:
        """Open a batch page, scroll to load entries, return links + page data.

        Candidate URLs are harvested from several signals, because Zhihu's
        lazy-loaded answer/article cards don't always expose a clean ``<a>``
        href: plain anchors, ``meta[itemprop="url"]`` tags, and answer ids on
        ``.AnswerItem[data-zop]`` (reconstructed into answer URLs). All
        candidates are then filtered by ``link_pattern``.

        Returns ``(links, initial_data)`` where ``initial_data`` is the page's
        parsed ``js-initialData`` (used to extract the batch title), or None.
        """
        page = self.browser.new_page()
        found: list[str] = []
        seen: set[str] = set()
        pat = re.compile(link_pattern)
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

    def _fetch_collection(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        # Collection entries link to /p/<id> (articles) and answer pages.
        links, data = self._scroll_collect_links(
            target.raw_url,
            r"(zhuanlan\.zhihu\.com/p/\d+|/question/\d+/answer/\d+|/answer/\d+)",
        )
        batch = self._make_batch(
            BatchKind.COLLECTION, data, target.collection_id, target.raw_url
        )
        yield from self._iter_item_links(links, batch)

    def _fetch_column(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        links, data = self._scroll_collect_links(
            target.raw_url, r"zhuanlan\.zhihu\.com/p/\d+"
        )
        batch = self._make_batch(
            BatchKind.COLUMN, data, target.column_id, target.raw_url
        )
        yield from self._iter_item_links(links, batch)

    def _fetch_question_answers(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        links, data = self._scroll_collect_links(
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
