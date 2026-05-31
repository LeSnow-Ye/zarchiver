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

## Videos and GIFs

Zhihu encodes animated/video media three ways in the raw `content`, handled in
`parser.clean_content_html` (+ `sources/zhihu/video.py`):

- **Plain GIF** — `<img class="content_image" src="…_1440w.gif" data-thumbnail=…>`
  with no `data-original`. The `_1440w.gif` is the real animation; the normal
  image pipeline downloads it. Works as-is.
- **"gif2mp4" GIF** — `<img src="…_1440w.gif" data-original="…_r.jpg" data-thumbnail=…>`
  where `data-original` is a *static* JPEG frame. The asset pipeline prefers
  `data-original`, so without intervention it would save a still. `_normalize_gifs`
  forces `src` to the `.gif` and drops the static `data-original`, so the
  animation is what gets downloaded. (In the rendered DOM these play as a
  `vzuu.com` MP4, but that URL needs a per-session signature, so the `.gif` —
  which any `<token>{_1440w,_r,_b}.gif` variant serves identically — is used.)
- **Real video** — `<a class="video-box" data-lens-id="<id>" data-poster="…">`.
  The actual MP4 isn't in the page; `video.resolve_video` calls
  `GET https://lens.zhihu.com/api/v4/videos/<id>` (note: the `lens.` host, not
  `www.`) to get a `playlist` of quality variants (`FHD`/`HD`/`SD`/`LD`), each a
  signed `play_url` on `*.vzuu.com`, plus `cover_url` + `title`. The parser
  rewrites the box into a `<video poster=… src=<mp4>>`; ingest downloads the MP4
  (its `play_url` signature is short-lived, so it's fetched right away with the
  standard Zhihu referer). Quality is configurable; resolution failures degrade
  to a poster + link.

## Batch pages: APIs vs. scroll

**Columns (专栏), collections (收藏夹), and questions (问题) all use JSON list
APIs** — far more reliable and faster than scraping the rendered page:

- column items — `/api/v4/columns/{id}/items?limit=N&ws_qiangzhisafe=0&offset=M`
- collection items — `/api/v4/collections/{id}/items?offset=M&limit=N`
- question answers — `/api/v4/questions/{id}/answers?include=data[*].content,voteup_count,comment_count,updated_time,created_time,author&limit=N&offset=M`

zarchiver pages through them (`offset`/`limit`) following `paging.next` until
`paging.is_end`, the configured cap is reached, or a request fails. Each entry
carries the item's `type` *and its full `content` body* — column items expose
these at the top level, while collection items wrap them under a `content`
object. Only archivable types (article / answer / pin) are kept; videos, ads,
and deleted items are skipped. Titles come from the matching metadata endpoint
(`/api/v4/collections/{id}` → `collection.title`, `/api/v4/columns/{id}` →
`title`) or, for questions, from each answer's embedded `question.title`. All of
this goes through the browser context (`context.request`), so session cookies
and the referer apply; transient edge 403s are retried.

**Trust the API body, fall back to the page.** Because the list API already
returns the complete content, zarchiver builds each `ArchiveItem` directly from
that JSON (`item_from_api_entry`) — no per-item page navigation. This was
verified to produce a *byte-identical* `content_hash` to opening the page, so
duplicate detection works entirely on API data and unchanged items cost nothing.
If an entry ever lacks usable content (empty body or an unexpected shape), that
one item falls back to fetching its page. Set `archive.prefer_api_content =
false` to force the old open-every-page behavior.

One nuance: the question answers API and the rendered page can serve the same
inline image from different CDN mirrors (`pic1` vs `picx` in a lazy
`data-actualsrc`). Same image, but the two paths hash differently — so an answer
archived once via the question API and again via its own page reads as
"changed". This is a re-ingest, not a correctness problem.

Question answer URLs from the API come back as `/api/v4/answers/{id}` (no
question id), so `web_url_from_api_entry` rebuilds the canonical
`/question/{qid}/answer/{aid}` form (used for both dedup and any page fallback).

**Scroll is the questions fallback.** If the answers API yields nothing,
zarchiver still harvests candidate answer URLs while scrolling the rendered page
from three signals:

1. plain `<a href>` anchors,
2. `meta[itemprop="url"]` tags,
3. answer ids on `.AnswerItem[data-zop]`, reconstructed into answer URLs.

It scrolls until no new links appear (or the cap is reached), then visits each.

## Images and referer

`pic*.zhimg.com` checks the `Referer` header, so the image downloader sends
`Referer: https://www.zhihu.com/` with a browser-like user agent. Images are
downloaded **once at ingest** into `archive.assets_root/<item-key>/`; filenames
are content-hash based and the extension is sniffed from magic bytes when the
URL lacks one. The item's `asset_map` (remote URL → stored relative path) is
saved in the DB, so the later export step rewrites `<img>` links offline — no
network access at export time.

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
