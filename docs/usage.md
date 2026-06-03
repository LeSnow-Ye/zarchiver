# Usage

All commands are run via `uv run zarchiver <command>` (or just `zarchiver` if
you've activated the venv). Every command accepts `--config/-c PATH` to point at
a specific config file; otherwise `config.toml` in the current directory is used
if present.

## Logging and verbosity

Global flags (placed before the command) control how much zarchiver logs:

```bash
uv run zarchiver archive <url>        # default: INFO — one line per item + steps
uv run zarchiver -v archive <url>     # DEBUG — per-page fetches, parses, writes
uv run zarchiver -vv archive <url>    # DEBUG + noisy third-party libs (Playwright…)
uv run zarchiver -q archive <url>     # quiet — only warnings and errors
```

Logs and progress go to **stderr**; the final summary and the `status` table go
to **stdout**, so you can pipe results without the log noise:

```bash
uv run zarchiver -q archive <url> 2>/dev/null   # just the "Done: …" summary
```

At `-v` you'll see, per item: the URL classification, page fetch size, parse
result (content length, image count, whether a title image was found), the
duplicate decision, AI model calls, image download counts, and the
exact path each exporter wrote.

## First-time setup

```bash
uv sync
uv run playwright install chromium
cp config.example.toml config.toml
export DEEPSEEK_API_KEY=sk-...        # or put it in config.toml [ai].api_key
```

## `login`

Open a browser, log in to Zhihu, and save the session so later runs are
authenticated. You only need to do this once (until the session expires).

```bash
uv run zarchiver login
```

A browser window opens on the Zhihu sign-in page. Scan the QR code or enter your
credentials, then press Enter in the terminal. The session is written to
`storage_state.json` (gitignored).

> On a headless server with no display, the login flow can't open a window.
> Instead, export a Zhihu cookie string from a logged-in browser and set
> `ZHIHU_COOKIE` (or `browser.cookie_string` in config). See
> [configuration.md](configuration.md).

## `archive URL`

Ingest content into the local database — the system of record. Archiving fetches
the content and its comments, downloads images once into `archive.assets_root`
(one subdirectory per item), runs AI summarization, and stores everything in the
DB. It then renders the exporters listed in `archive.auto_export` (Obsidian +
HTML by default) — or skip that with `--no-export` and run [`export`](#export)
later. The kind of URL is auto-detected:

- **Single** answer, article, or pin (想法) → archived directly.
- **Batch** — a collection (收藏夹), column (专栏), or question → every item is
  archived, each placed in a subdirectory named after the batch (see below).

```bash
# Single
uv run zarchiver archive https://zhuanlan.zhihu.com/p/35562420
uv run zarchiver archive https://www.zhihu.com/question/19550225/answer/123456
uv run zarchiver archive https://www.zhihu.com/pin/2000653466067043281

# Batch (same command)
uv run zarchiver archive https://www.zhihu.com/collection/<id>
uv run zarchiver archive https://zhuanlan.zhihu.com/<column-slug>
uv run zarchiver archive https://www.zhihu.com/question/<id>
```

Options:
- `--no-ai` — skip AI summarization for this run.
- `--no-comments` — don't record comments for this run.
- `--max-comments N` — cap recorded comments per item, counting replies
  (0 = all). Defaults to the config value (100).
- `--on-duplicate skip|update|ask` — override the duplicate policy.
- `--limit/-n N` — cap how many items a batch pulls (0 = all).
- `--subdir NAME` — force output into this subdirectory instead of the
  batch-named default. Use `--subdir ''` to write directly into the base folder
  with no subdirectory.
- `--no-export` — ingest into the DB (with images + AI) but don't render any
  exporter; useful for bulk-collecting, then exporting in one pass later.
- `--no-videos` — don't download embedded videos (keep a poster + link instead).
- `--video-quality FHD|HD|SD|LD` — preferred video quality (default FHD).
- `--dry-run` — classify each item against the DB and print the plan (would
  archive / update / skip) without fetching content, running AI, downloading
  images, or writing anything. The batch listing is still loaded (so zarchiver
  knows what's there), but no per-item work happens — a fast way to preview a
  re-run before spending crawl time and API tokens.

For batch URLs, zarchiver loads all entries — columns (专栏) and collections
(收藏夹) via their JSON list APIs, questions by scrolling — then archives each
one with a polite randomized delay between requests. `--limit/-n` caps the total
across pages.

### Batch subdirectories

By default, a batch archive groups its items into a self-contained subdirectory
named after the collection / column / question, with the note (or HTML page) and
its `assets/` folder side by side. For example, archiving the column "次元壁"
writes:

```
vault/Zhihu/次元壁/<note>.md
vault/Zhihu/次元壁/assets/<image>.jpg
archive/html/次元壁/<page>.html
archive/html/次元壁/assets/<image>.jpg
```

Disable this globally with `obsidian.batch_subdirs = false` /
`html.batch_subdirs = false`, or override per run with `--subdir`.

### Comments

By default zarchiver records each item's comments — root comments and their
replies (Zhihu threads one level deep). Because popular content can have
thousands, the count is capped at **100 per item including replies**; the
most-liked comments are kept when truncating. Adjust with `archive.max_comments`
in config or `--max-comments N` per run (0 = all), or turn recording off with
`archive.comments = false` / `--no-comments`.

Comments render as a `## 评论 (N)` section at the end of each note: a threaded
blockquote list in markdown, and styled, nested blocks in HTML. Each comment
shows its author, date, and like count. Comments are not part of an item's
content hash, so they don't affect duplicate detection.

### Videos and GIFs

Embedded media is archived locally alongside images:

- **GIFs** — animated GIFs are downloaded as real `.gif` files (Zhihu sometimes
  marks them with a static JPEG frame; zarchiver keeps the animation). They
  render inline in both Obsidian and HTML.
- **Videos** — `<a class="video-box">` embeds are resolved to a playable MP4 via
  Zhihu's video API and downloaded into the item's assets. HTML gets a real
  `<video>` player; Obsidian gets an `![[…mp4]]` embed (Obsidian plays embedded
  video). The poster frame is saved too.

Videos can be large (FHD is tens of MB). Choose a smaller quality with
`archive.video_quality = "SD"` (or `--video-quality SD`), or skip downloading
them entirely with `archive.download_videos = false` / `--no-videos` — in which
case a video degrades to its poster image plus a link. If a video can't be
resolved, it also falls back to the poster + link. All downloading happens at
archive time; `export` stays fully offline.

## `export`

Render already-archived items from the database — **fully offline**. It reads
content, comments, and the recorded image asset map from the DB and writes
Obsidian / HTML, rewriting `<img>` links to the locally stored images. No
network access: only items already ingested by `archive` are exported. This lets
you re-render after changing templates or config, or export formats you skipped
at archive time, without re-fetching anything.

```bash
uv run zarchiver export                       # all items, all enabled exporters
uv run zarchiver export -f obsidian           # only the Obsidian exporter
uv run zarchiver export --type answer -n 50   # 50 most-recent answers
uv run zarchiver export --key zhihu:article:35562420   # one specific item
```

Options:
- `--format/-f obsidian|html` — pick exporter(s); repeatable. Defaults to all
  enabled in config.
- `--type answer|article|pin` — filter by content type.
- `--key platform:type:id` — export a single item by its key.
- `--subdir NAME` — force output into this subdirectory.
- `--skip-existing` — skip items whose output files already exist (default is to
  overwrite, since export is a deterministic function of the DB).
- `--limit/-n N` — cap how many items to export (0 = all).

Images missing from an item's asset map (e.g. a download that failed at ingest)
keep their original remote URL rather than triggering a fetch — export never
touches the network.

## `reai`

Regenerate AI summaries, tags, and category for items **already in the
database**, re-running the LLM over their stored content — no re-fetch, no
browser. Useful to:

- apply a new `ai.category_reference` to content archived before you set it, or
- fill in items that were archived with `--no-ai`.

```bash
uv run zarchiver reai                       # all items (asks to confirm)
uv run zarchiver reai --only-empty          # only items with no AI result yet
uv run zarchiver reai --type article -n 50  # 50 most-recent articles
uv run zarchiver reai --key zhihu:article:35562420   # one item
uv run zarchiver reai -y --export           # no prompt, re-render after
```

Options:
- `--only-empty` — only items that don't already have an AI result (e.g. ones
  archived with `--no-ai`); items already summarized are skipped.
- `--type answer|article|pin` — filter by content type.
- `--key platform:type:id` — re-summarize a single item.
- `--limit/-n N` — cap how many items (0 = all).
- `--export/-e` — re-render the affected items (Obsidian/HTML) after
  re-summarizing, so the new tags/category land in your notes.
- `--yes/-y` — skip the confirmation prompt.

Each item costs one LLM call, so `reai` confirms before running unless you pass
`--yes`. It needs AI configured (`[ai].enabled` and an API key); without that it
exits with a clear error.

## `status`

Show how many items are archived and the most recent ones.

```bash
uv run zarchiver status
uv run zarchiver status -n 30
```

## Output

Archiving stores everything in the database (`archive.db_path`): the content,
threaded comments, AI summary/tags/category, engagement metrics, and a map of
each image to its local copy under `archive.assets_root`. Exporters then render
from the DB. For each item:

- **Markdown** into `<vault_path>/<folder>/` (or a batch subdirectory) with YAML
  frontmatter (title, author, URL, dates, metrics, merged topic + AI tags, AI
  category and summary, plus the column and/or collection it belongs to), and a
  threaded `## 评论` section when comments are recorded.
- **HTML** into `<html.output_path>/` — a styled, self-contained page including a
  comments section.
- **Images** are downloaded once at ingest into `<assets_root>/<item-key>/`;
  exporters copy the ones each item references into their own `assets/` folder
  (or inline them as base64 for HTML when `embed_images = true`) and rewrite the
  links to local relative paths.

Open the vault folder in Obsidian (point Obsidian at `<vault_path>`) and the
notes appear with working images and tags.

## Duplicate handling

An item is a duplicate when it's **already in the database** with the same
content hash. On a duplicate, the `on_duplicate` policy decides:

- `skip` (default) — leave the stored item and its exports untouched.
- `update` — re-fetch, re-ingest, and re-export, overwriting previous output.
- `ask` — prompt per item.

Because detection is content-hash based in the DB (not path based), deleting an
exported file doesn't cause a re-fetch — run `export` to regenerate it from the
DB. When an item's content actually changes, its hash changes and `update`
re-ingests it.

Under `skip` (the default), an item already in the DB is left untouched — it is
not re-fetched, re-summarized, or re-exported. To refresh AI summaries/tags for
already-archived items (for example after setting `ai.category_reference`), use
[`reai`](#reai); to re-render output from the DB without re-fetching, use
[`export`](#export).
