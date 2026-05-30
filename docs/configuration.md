# Configuration

zarchiver reads `config.toml` from the current directory (or `--config PATH`).
Copy `config.example.toml` to `config.toml` and edit. Anything you omit falls
back to the built-in default.

## Layering

Values are resolved in this order, later winning:

1. Built-in defaults.
2. `config.toml` (or `--config`).
3. Environment variables (for secrets and common paths).

### Environment variables

| Variable | Overrides | Purpose |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | `ai.api_key` | LLM API key (keep it out of the file). |
| `ZARCHIVER_AI_MODEL` | `ai.model` | LLM model name. |
| `ZHIHU_COOKIE` | `browser.cookie_string` | Cookie string for headless/server use. |
| `ZARCHIVER_VAULT` | `obsidian.vault_path` | Vault root. |
| `ZARCHIVER_DB` | `archive.db_path` | Dedup/cache database path. |
| `ZARCHIVER_HEADLESS` | `browser.headless` | `1`/`true` forces headless. |

## Sections

### `[archive]`

| Key | Default | Notes |
| --- | --- | --- |
| `db_path` | `zarchiver.db` | SQLite file for dedup index + AI cache. |
| `on_duplicate` | `skip` | `skip`, `update`, or `ask`. |

### `[browser]`

| Key | Default | Notes |
| --- | --- | --- |
| `headless` | `false` | Keep false — Zhihu blocks headless Chromium. |
| `user_agent` | Chrome 131 UA | Sent by the browser and image fetcher. |
| `locale` | `zh-CN` | Browser locale. |
| `storage_state` | `storage_state.json` | Saved login session (gitignored). |
| `cookie_string` | `""` | Fallback auth for headless/server use. |
| `nav_timeout_ms` | `40000` | Per-page navigation timeout. |
| `min_delay_ms` / `max_delay_ms` | `1200` / `3500` | Randomized delay between batch items. |
| `max_items` | `0` | Cap per batch run (0 = unlimited). Overridden by `--limit`. |

### `[obsidian]`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Write markdown notes. |
| `vault_path` | `vault` | Vault root; open this in Obsidian. |
| `folder` | `Zhihu` | Subfolder for notes. |
| `assets_folder` | `Zhihu/assets` | Subfolder for images (relative to vault). Used for non-batch archives; batch items keep assets in `<batch>/assets`. |
| `download_images` | `true` | Download images and rewrite links. |
| `filename_template` | `{title} - {author}` | Fields: `{title} {author} {source_id} {content_type} {date}`. |
| `batch_subdirs` | `true` | Group batch items into a subdir named after the collection/column/question. Override per run with `--subdir`. |
| `use_cli` | `false` | Use the Obsidian CLI instead of writing files (see below). |
| `cli_vault_name` | `""` | Vault name for the CLI. |

### `[html]`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Write standalone HTML. |
| `output_path` | `archive/html` | Output directory. |
| `embed_images` | `false` | Inline images as base64 (single-file archive). |
| `batch_subdirs` | `true` | Group batch items into a subdir named after the batch. |

### `[ai]`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Generate summaries/tags/category. |
| `provider` | `deepseek` | Provider id. |
| `base_url` | `https://api.deepseek.com` | OpenAI-compatible endpoint. |
| `api_key` | `""` | Prefer `DEEPSEEK_API_KEY`. |
| `model` | `deepseek-v4-flash` | Also available: `deepseek-v4-pro`. |
| `max_tokens` | `1200` | Generous: the flash model is a *reasoning* model and spends tokens before answering. |
| `temperature` | `0.3` | Lower = more consistent tagging. |
| `max_input_chars` | `12000` | Body is truncated to this before sending. |
| `timeout_s` | `120` | Request timeout. |
| `language` | `zh` | `zh` or `en` for the summary/tags. |

## Authentication options

**Persistent login (default).** Run `zarchiver login` once; the session is
saved to `storage_state` and reused. Best for desktop/WSLg use.

**Cookie import (headless/server).** Export your Zhihu cookies from a logged-in
browser as a `k=v; k2=v2` string and set `ZHIHU_COOKIE` (or
`browser.cookie_string`). Combine with `ZARCHIVER_HEADLESS=1` under `xvfb-run`
if there's no display. Cookies expire, so refresh them periodically.

## Obsidian CLI (optional)

By default zarchiver writes markdown files straight into the vault folder, which
works everywhere (including headless). The Obsidian *CLI* path
(`obsidian.use_cli = true`) drives the running Obsidian desktop app instead, and
therefore only works when that app is running on the same machine. If the CLI
call fails, zarchiver falls back to writing the file directly.

## Proxies

zarchiver honors the standard `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`
environment variables for both the browser and the image/LLM HTTP clients. SOCKS
proxies are supported (the `httpx[socks]` extra is installed).
