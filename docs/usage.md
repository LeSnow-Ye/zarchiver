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

Archive a single answer or article. If you pass a batch URL (collection, column,
question) it automatically switches to batch mode.

```bash
uv run zarchiver archive https://zhuanlan.zhihu.com/p/35562420
uv run zarchiver archive https://www.zhihu.com/question/19550225/answer/123456
```

Options:
- `--no-ai` — skip AI summarization for this run.
- `--on-duplicate skip|update|ask` — override the duplicate policy.

## Batch commands

```bash
uv run zarchiver collection https://www.zhihu.com/collection/<id>
uv run zarchiver column     https://zhuanlan.zhihu.com/<slug>
uv run zarchiver question   https://www.zhihu.com/question/<id>
```

Each accepts `--limit/-n N` to cap how many items are pulled (0 = all), plus the
same `--no-ai` and `--on-duplicate` options as `archive`.

Batch commands scroll the page to load entries, then archive each one with a
polite randomized delay between requests.

## `status`

Show how many items are archived and the most recent ones.

```bash
uv run zarchiver status
uv run zarchiver status -n 30
```

## Output

For each item zarchiver writes:

- **Markdown** into `<vault_path>/<folder>/` with YAML frontmatter (title,
  author, URL, dates, metrics, merged topic + AI tags, AI category and summary).
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
