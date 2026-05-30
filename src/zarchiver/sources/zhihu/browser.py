"""Playwright browser management for Zhihu.

Encapsulates everything about *driving a browser at Zhihu*: launching headful
Chromium with anti-detection tweaks, loading/saving the persistent login state,
importing cookies for headless/server use, and a polite navigation helper.

Findings that shape this module (see docs/scraping.md):

* Plain HTTP is 403; a real browser is required.
* **Headless** Chromium is detected and blocked, so we launch **headful**.
* The top-level navigation response is often 403 even on success — Zhihu's edge
  serves a 403 then hydrates the page — so navigation status is *not* used as a
  failure signal. Callers check for actual content instead.
"""

from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

from zarchiver.config import BrowserConfig

log = logging.getLogger(__name__)


# Injected before any page script runs, to mask automation fingerprints.
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = window.chrome || {runtime: {}};
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
]


def _parse_cookie_string(cookie_string: str) -> list[dict]:
    """Turn a ``k=v; k2=v2`` cookie header into Playwright cookie dicts."""
    cookies: list[dict] = []
    for part in cookie_string.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies.append(
            {
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".zhihu.com",
                "path": "/",
            }
        )
    return cookies


class ZhihuBrowser:
    """Owns a Playwright browser + context configured for Zhihu.

    Use as a context manager::

        with ZhihuBrowser(cfg) as br:
            page = br.new_page()
            br.goto(page, url)
    """

    def __init__(self, config: BrowserConfig, *, headless: Optional[bool] = None):
        self.config = config
        self.headless = config.headless if headless is None else headless
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def __enter__(self) -> "ZhihuBrowser":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        log.debug(
            "launching Chromium (headless=%s, ua=%r, locale=%s)",
            self.headless, self.config.user_agent, self.config.locale,
        )
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, args=_LAUNCH_ARGS
        )
        storage = self._storage_path()
        context_kwargs: dict = {
            "user_agent": self.config.user_agent,
            "locale": self.config.locale,
            "viewport": {"width": 1366, "height": 900},
        }
        if storage and storage.is_file():
            context_kwargs["storage_state"] = str(storage)
            log.debug("loaded saved session from %s", storage)
        else:
            log.debug("no saved session at %s; starting logged out", storage)
        self._context = self._browser.new_context(**context_kwargs)
        self._context.add_init_script(_STEALTH_JS)

        # Cookie-string fallback (headless/server): inject if provided.
        if self.config.cookie_string:
            try:
                cookies = _parse_cookie_string(self.config.cookie_string)
                self._context.add_cookies(cookies)
                log.debug("injected %d cookies from cookie_string", len(cookies))
            except Exception as exc:
                # Bad cookie strings shouldn't crash startup; login flow remains.
                log.warning("could not apply cookie_string: %s", exc)
        log.debug("browser context ready")

    def close(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()
            self._context = self._browser = self._pw = None

    # ------------------------------------------------------------------ #
    # Pages / navigation
    # ------------------------------------------------------------------ #
    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("ZhihuBrowser not started")
        return self._context

    def new_page(self) -> Page:
        return self.context.new_page()

    def goto(self, page: Page, url: str, *, wait_selector: Optional[str] = None) -> None:
        """Navigate to ``url`` and wait for content.

        The HTTP status is intentionally ignored (Zhihu serves 403 + hydrates).
        If ``wait_selector`` is given we wait for it to be visible; otherwise we
        wait for the embedded ``#js-initialData`` script to be *attached* (a
        ``<script>`` never becomes "visible", so it must not be waited on with
        the default visible state) or fall back to a short settle delay.
        """
        log.debug("navigating to %s", url)
        start = time.monotonic()
        resp = page.goto(
            url, wait_until="domcontentloaded", timeout=self.config.nav_timeout_ms
        )
        status = resp.status if resp else None
        if wait_selector:
            selector, state = wait_selector, "visible"
        else:
            selector, state = "#js-initialData", "attached"
        try:
            page.wait_for_selector(selector, state=state, timeout=8000)
            log.debug(
                "loaded %s (http %s, %.1fs, %r present)",
                url, status, time.monotonic() - start, selector,
            )
        except Exception:
            # Not fatal: parser will fall back to DOM or report a clear error.
            page.wait_for_timeout(1500)
            log.debug(
                "loaded %s (http %s, %.1fs, %r NOT found — will fall back)",
                url, status, time.monotonic() - start, selector,
            )

    def polite_delay(self) -> None:
        """Sleep a randomized human-ish interval between batch requests."""
        lo = self.config.min_delay_ms
        hi = max(lo, self.config.max_delay_ms)
        seconds = random.uniform(lo, hi) / 1000.0
        log.debug("polite delay: sleeping %.1fs", seconds)
        time.sleep(seconds)

    # ------------------------------------------------------------------ #
    # Auth helpers
    # ------------------------------------------------------------------ #
    def _storage_path(self) -> Optional[Path]:
        return Path(self.config.storage_state) if self.config.storage_state else None

    def save_storage_state(self) -> Optional[Path]:
        """Persist cookies/localStorage so future runs are logged in."""
        storage = self._storage_path()
        if storage:
            self.context.storage_state(path=str(storage))
            log.info("saved session to %s", storage)
        return storage

    def is_logged_in(self, page: Optional[Page] = None) -> bool:
        """Best-effort login check via the embedded ``currentUser`` state."""
        own_page = page is None
        page = page or self.new_page()
        try:
            self.goto(page, "https://www.zhihu.com/")
            result = page.evaluate(
                """() => {
                    const el = document.getElementById('js-initialData');
                    if (!el) return false;
                    try {
                        const d = JSON.parse(el.textContent);
                        const cu = d.initialState && d.initialState.currentUser;
                        return !!(cu && (cu.id || cu.uid || cu.urlToken));
                    } catch (e) { return false; }
                }"""
            )
            log.debug("login check: %s", "logged in" if result else "logged out")
            return bool(result)
        except Exception as exc:
            log.debug("login check failed: %s", exc)
            return False
        finally:
            if own_page:
                page.close()
