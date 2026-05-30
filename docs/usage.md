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
duplicate decision, AI cache hits / model calls, image download counts, and the
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

The one command for everything. The kind of URL is auto-detected:

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
- `--on-duplicate skip|update|ask` — override the duplicate policy.
- `--limit/-n N` — cap how many items a batch pulls (0 = all).
- `--subdir NAME` — force output into this subdirectory instead of the
  batch-named default. Use `--subdir ''` to write directly into the base folder
  with no subdirectory.

For batch URLs, zarchiver loads all entries — scrolling for columns and
questions, and walking every `?page=N` for collections (收藏夹), which are
paginated — then archives each one with a polite randomized delay between
requests. `--limit/-n` caps the total across pages.

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

## `status`

Show how many items are archived and the most recent ones.

```bash
uv run zarchiver status
uv run zarchiver status -n 30
```

## Output

For each item zarchiver writes:

- **Markdown** into `<vault_path>/<folder>/` (or a batch subdirectory) with YAML
  frontmatter (title, author, URL, dates, metrics, merged topic + AI tags, AI
  category and summary, plus the column and/or collection it belongs to).
- **HTML** into `<html.output_path>/` — a styled, self-contained page.
- **Images** downloaded into the assets folders, with links rewritten to local
  relative paths.

Open the vault folder in Obsidian (point Obsidian at `<vault_path>`) and the
notes appear with working images and tags.

## Duplicate handling

An item is considered a duplicate when its output **already exists on disk** —
that is, when every enabled exporter's target file is already present. (If only
some outputs exist — say you enabled HTML after a markdown-only run — the missing
ones are still written.) On a duplicate, the `on_duplicate` policy decides:

- `skip` (default) — leave the existing files untouched.
- `update` — re-fetch and re-export, overwriting the previous output.
- `ask` — prompt per item.

Because detection is path-based, deleting an archived file makes that item
archive again on the next run, and pointing a run at a fresh output directory
re-archives everything regardless of past runs.

AI summaries are still cached by content hash (in `archive.db_path`), so even
when you re-archive, unchanged content is never re-sent to the LLM.
