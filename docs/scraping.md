# Scraping notes

These are the findings that justify zarchiver's scraping design. They were
verified against live Zhihu during development.

## Plain HTTP doesn't work

Requests from `curl`, `requests`, or `httpx` get **HTTP 403** on question pages,
column articles, and the `api/v4/*` endpoints. The JSON API additionally
requires a signed `x-zse-96` header (it returns `code 10003` without it).
Reverse-engineering that signature is brittle and a moving target, so zarchiver
does not attempt it.

**Conclusion:** drive a real browser.

## Headless is detected — run headful

Playwright's default `headless=True` uses `chrome-headless-shell`, which Zhihu
detects and blocks (`code 40362`, "请求存在异常"). Launching **headful**
Chromium with light anti-detection tweaks loads pages normally.

zarchiver therefore defaults `browser.headless = false` and applies:

- launch args `--disable-blink-features=AutomationControlled`, `--no-sandbox`
- an init script masking `navigator.webdriver`, setting `navigator.languages`,
  and faking `window.chrome` / `navigator.plugins`

On a desktop or WSL2 (via WSLg) this just works. On a true headless server, run
under `xvfb-run` and supply cookies via `ZHIHU_COOKIE`.

## The 403-then-hydrate quirk

Even on a successful headful load, the **top-level navigation response status is
often 403** — Zhihu's edge returns a 403 document and then the page hydrates
with real content. So zarchiver does **not** treat the navigation status as a
failure signal. Instead it waits for the embedded data script and checks for
actual content.

## Parse `js-initialData`, don't scrape the DOM

Every Zhihu page embeds a complete JSON state document in
`<script id="js-initialData">`. Parsing
`JSON.parse(...).initialState.entities` gives clean, stable entities:

- `entities.articles[id]` — `title`, `content` (HTML), `author`, `created`,
  `updated`, `voteupCount`, `commentCount`, `excerpt`, `column`, `topics`
- `entities.answers[id]` — same shape plus `question`
- `entities.questions[id]` — question metadata
- `entities.pins[id]` — a 想法 (pin): `content` is an ordered list of blocks
  (`type: "text"` carries HTML in `content`; `type: "image"` carries `url` /
  `originalUrl` / `watermarkUrl`), `author` is a urlToken string into
  `entities.users` (not embedded inline), and there is no real title — the
  parser synthesizes one from `excerptTitle`. See `parser.parse_pin`.

This is far more robust than scraping rendered DOM (class names change often).
zarchiver only falls back to DOM parsing when the embedded data is missing.

Timestamps are unix epoch seconds. Content is raw HTML using `data-pid`
paragraphs, `pic*.zhimg.com` images (lazy-loaded via `data-original` /
`data-actualsrc`), and `link.zhihu.com/?target=...` redirect links — all
normalized in `parser.clean_content_html`.

## Formulas, title images, and references

Three Zhihu-specific structures need special handling, done in
`parser.clean_content_html` / `parse_article`:

- **Formulas.** Zhihu renders math as images
  (`<img src="https://www.zhihu.com/equation?tex=<urlencoded>" eeimg="1">`).
  Downloading these as pictures loses the math, so the parser decodes the TeX
  and converts each into `<span class="ztex" data-tex="...">`, marking it block
  if it's the sole content of its paragraph. Exporters then render real math:
  the Obsidian exporter emits `$...$` / `$$...$$` (protecting the LaTeX from
  markdownify's escaping via placeholder tokens), and the HTML exporter emits
  `\(...\)` / `\[...\]` and injects MathJax (only when formulas are present).
- **Title images.** An article's cover image lives in the `titleImage` entity
  field, separate from the content body. The parser captures it into
  `ArchiveItem.title_image`; exporters prepend it (Obsidian as the first image,
  HTML as a banner), downloading it like any other asset.
- **References.** Footnote references are inline
  `<sup data-draft-type="reference" data-numero="N" data-text="..."
  data-url="...">` markers, but Zhihu builds the bibliography client-side so it
  never reaches the HTML. The parser rebuilds a `参考` section from those markers
  and turns each inline marker into an anchor link to its entry.

## Batch pages: APIs vs. scroll

**Columns (专栏) and collections (收藏夹) use JSON list APIs** — far more
reliable and faster than scraping the rendered page:

- column items — `/api/v4/columns/{id}/items?limit=N&ws_qiangzhisafe=0&offset=M`
- collection items — `/api/v4/collections/{id}/items?offset=M&limit=N`

zarchiver pages through them (`offset`/`limit`) following `paging.next` until
`paging.is_end`, the configured cap is reached, or a request fails. Each entry
carries the item's canonical `url` and `type`; column items expose these at the
top level, while collection items wrap them under a `content` object. Only
archivable types (article / answer / pin) are kept — videos, ads, and deleted
items are skipped — and the URLs are deduped before each one is fetched and
parsed normally. Titles come from the matching metadata endpoint
(`/api/v4/collections/{id}` → `collection.title`, `/api/v4/columns/{id}` →
`title`). All of this goes through the browser context (`context.request`), so
session cookies and the referer apply; transient edge 403s are retried.

**Questions still lazy-load on scroll**: their answer cards don't always expose
a clean `<a href>`, so zarchiver harvests candidate item URLs while scrolling
from three signals:

1. plain `<a href>` anchors,
2. `meta[itemprop="url"]` tags,
3. answer ids on `.AnswerItem[data-zop]`, reconstructed into answer URLs.

It scrolls until no new links appear (or the cap is reached), then visits each.

## Images and referer

`pic*.zhimg.com` checks the `Referer` header, so the image downloader sends
`Referer: https://www.zhihu.com/` with a browser-like user agent. Filenames are
content-hash based and the extension is sniffed from magic bytes when the URL
lacks one.

## Comments

Comments are **not** in `js-initialData`; Zhihu loads them lazily from a JSON
API under `/api/v4/comment_v5`:

- root comments — `/{resource_type}/{id}/root_comment?order_by=score&limit=N`
- child replies — `/comment/{root_id}/child_comment?order_by=ts&limit=N`

where `resource_type` is `articles` / `answers` / `pins`. zarchiver calls these
through the browser context (`context.request`), so the session cookies and a
Zhihu referer are applied automatically. Each root comment embeds its first few
replies (`child_comments`) and reports the total (`child_comment_count`); the
remainder are paged from the child endpoint only when the cap allows. Paging
follows `paging.is_end` / `paging.next`.

Comments are threaded one level deep (a root plus direct replies), matching
Zhihu's model. The `archive.max_comments` cap counts **every** recorded comment
— root and child — so one popular thread can't blow the budget; root comments
are pulled most-liked-first (`order_by=score`) so truncation drops the long tail
rather than the top discussion. See `sources/zhihu/comments.py`. A failed
comment request is non-fatal: the item is still archived, just without (some)
comments.

## Politeness

Batch runs sleep a randomized `min_delay_ms`–`max_delay_ms` between items to
avoid hammering Zhihu. Keep this enabled and archive responsibly: only content
you have access to, for personal archival.
