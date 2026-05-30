# Usage

All commands are run via `uv run zarchiver <command>` (or just `zarchiver` if
you've activated the venv). Every command accepts `--config/-c PATH` to point at
a specific config file; otherwise `config.toml` in the current directory is used
if present.

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

- **Single** answer or article → archived directly.
- **Batch** — a collection (收藏夹), column (专栏), or question → every item is
  archived, each placed in a subdirectory named after the batch (see below).

```bash
# Single
uv run zarchiver archive https://zhuanlan.zhihu.com/p/35562420
uv run zarchiver archive https://www.zhihu.com/question/19550225/answer/123456

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

For batch URLs, zarchiver scrolls the page to load entries, then archives each
one with a polite randomized delay between requests.

### Batch subdirectories

By default, a batch archive groups its items into a subdirectory named after the
collection / column / question, under both the vault folder and the HTML output
(and a matching subfolder under assets). For example, archiving the column
"次元壁" writes:

```
vault/Zhihu/次元壁/<note>.md
vault/Zhihu/assets/次元壁/<image>.jpg
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

A SQLite index (`archive.db_path`) tracks every archived item by a stable key.
On re-runs:

- `skip` (default) — already-archived items are skipped, even if edited.
- `update` — re-fetch and re-export, overwriting the previous output.
- `ask` — prompt per item.

AI summaries are cached by content hash, so even when you re-archive, unchanged
content is never re-sent to the LLM.
