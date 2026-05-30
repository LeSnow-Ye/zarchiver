"""The Zhihu :class:`Source` implementation.

Glues the browser and parser together: classifies a URL, navigates with the
shared browser, extracts the embedded data, and produces
:class:`~zarchiver.models.ArchiveItem` objects. Batch targets (collections,
columns, questions) are handled by scrolling to load more entries and visiting
each item's page.
"""

from __future__ import annotations

import re
from typing import Iterator, Optional

from zarchiver.config import Config
from zarchiver.models import ArchiveItem
from zarchiver.sources.base import Source, SourceError
from zarchiver.sources.zhihu import parser as P
from zarchiver.sources.zhihu import urls as zurls
from zarchiver.sources.zhihu.browser import ZhihuBrowser
from zarchiver.sources.zhihu.urls import ZhihuKind, ZhihuTarget


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
            self._browser = ZhihuBrowser(self.config.browser)
            self._browser.start()
        return self._browser

    def _page_html(self, url: str) -> str:
        page = self.browser.new_page()
        try:
            self.browser.goto(page, url)
            return page.content()
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
                return P.parse_article(data, article_id or "")
            except SourceError:
                pass  # fall through to DOM
        return P.parse_article_dom(html, article_id or "", url)

    def _fetch_answer(
        self, answer_id: Optional[str], question_id: Optional[str], url: str
    ) -> ArchiveItem:
        html = self._page_html(url)
        data = P.extract_initial_data(html)
        if not data:
            raise SourceError(f"no embedded data for answer at {url}")
        return P.parse_answer(data, answer_id or "", question_id)

    # ------------------------------------------------------------------ #
    # Batches
    # ------------------------------------------------------------------ #
    def _max_items(self) -> int:
        return self.config.browser.max_items  # 0 = unlimited

    def _scroll_collect_links(self, url: str, link_pattern: str) -> list[str]:
        """Open a batch page, scroll to load entries, return matching links.

        ``link_pattern`` is a regex matched against each anchor href.
        """
        page = self.browser.new_page()
        found: list[str] = []
        seen: set[str] = set()
        pat = re.compile(link_pattern)
        cap = self._max_items()
        try:
            self.browser.goto(page, url)
            stagnant = 0
            last_count = 0
            # Scroll until no new links appear (or cap reached).
            for _ in range(60):  # hard ceiling on scroll iterations
                hrefs = page.eval_on_selector_all(
                    "a",
                    "els => els.map(e => e.href).filter(Boolean)",
                )
                for h in hrefs:
                    if pat.search(h) and h not in seen:
                        seen.add(h)
                        found.append(h)
                if cap and len(found) >= cap:
                    found = found[:cap]
                    break
                if len(found) == last_count:
                    stagnant += 1
                    if stagnant >= 3:
                        break
                else:
                    stagnant = 0
                    last_count = len(found)
                page.mouse.wheel(0, 3000)
                page.wait_for_timeout(900)
            return found
        finally:
            page.close()

    def _fetch_collection(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        # Collection entries link to /p/<id> (articles) and answer pages.
        links = self._scroll_collect_links(
            target.raw_url,
            r"(zhuanlan\.zhihu\.com/p/\d+|/question/\d+/answer/\d+|/answer/\d+)",
        )
        yield from self._iter_item_links(links)

    def _fetch_column(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        links = self._scroll_collect_links(
            target.raw_url, r"zhuanlan\.zhihu\.com/p/\d+"
        )
        yield from self._iter_item_links(links)

    def _fetch_question_answers(self, target: ZhihuTarget) -> Iterator[ArchiveItem]:
        links = self._scroll_collect_links(
            target.raw_url, rf"/question/{target.question_id}/answer/\d+"
        )
        yield from self._iter_item_links(links)

    def _iter_item_links(self, links: list[str]) -> Iterator[ArchiveItem]:
        cap = self._max_items()
        count = 0
        for link in links:
            if cap and count >= cap:
                return
            try:
                item = self.fetch(link)
            except SourceError:
                continue
            count += 1
            yield item
            self.browser.polite_delay()
