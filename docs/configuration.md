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
| `ZARCHIVER_DB` | `archive.db_path` | Archive database path. |
| `ZARCHIVER_ASSETS_ROOT` | `archive.assets_root` | Downloaded-image store root. |
| `ZARCHIVER_AUTO_EXPORT` | `archive.auto_export` | Comma-separated exporters to auto-run (e.g. `obsidian,html`). |
| `ZARCHIVER_VIDEO_QUALITY` | `archive.video_quality` | Preferred video quality (`FHD`/`HD`/`SD`/`LD`). |
| `ZARCHIVER_MAX_ASSET_MB` | `archive.max_asset_mb` | Max size (MB) per downloaded asset; `0` disables the limit. |
| `ZARCHIVER_MAX_ASSET_RETRIES` | `archive.max_asset_retries` | Retries after the first attempt for transient asset download failures; `0` disables retries. |
| `ZARCHIVER_PREFER_API_CONTENT` | `archive.prefer_api_content` | `1`/`true`/`0`/`false`; build batch items from the API instead of opening pages. |
| `ZARCHIVER_HEADLESS` | `browser.headless` | `1`/`true` forces headless. |

## Sections

### `[archive]`

The database is the **system of record**: archiving ingests the full content,
comments, AI results, and an image asset map into it, and exporters render from
it (see [usage](usage.md)).

| Key | Default | Notes |
| --- | --- | --- |
| `db_path` | `zarchiver.db` | SQLite file: items + comments + AI results + asset map. |
| `assets_root` | `archive/assets` | Where images are downloaded, one subdir per item key. The DB records each image's relative path. |
| `auto_export` | `["obsidian", "html"]` | Exporters to run automatically after ingest. Empty list = ingest only; run `export` later. |
| `on_duplicate` | `skip` | `skip`, `update`, or `ask`. Matched by content hash in the DB. |
| `comments` | `true` | Record comments (root + replies). Disable per run with `--no-comments`. |
| `max_comments` | `100` | Max comments per item *including* child replies (0 = all). Override per run with `--max-comments`. |
| `download_videos` | `true` | Download embedded videos as MP4 (play offline). Off → poster + link. Disable per run with `--no-videos`. |
| `video_quality` | `FHD` | Preferred quality (`FHD`/`HD`/`SD`/`LD`); falls back to nearest. Override per run with `--video-quality`. |
| `max_asset_mb` | `20.0` | Max size (MB) for a single downloaded asset (image/video). Larger assets aren't stored locally; the content keeps the original remote link. `0` disables the limit. |
| `max_asset_retries` | `2` | Retries after the first attempt for transient asset failures: timeouts, transport errors, HTTP 429, and HTTP 5xx. Permanent 4xx failures and over-size skips are never retried. `0` = single attempt. |
| `prefer_api_content` | `true` | In batch archives, build items from the listing API's JSON (full body included) instead of opening each page — faster, and dedup happens up front. Pages are still opened as a fallback when an API entry lacks content. Set `false` to force opening every page. |

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
| `download_images` | `true` | Copy locally-stored images into the vault on export. Images are downloaded once at ingest (into `archive.assets_root`); this controls whether notes link to local copies or keep remote URLs. |
| `filename_template` | `{title} - {author}` | Fields: `{title} {author} {source_id} {content_type} {date}`. |
| `batch_subdirs` | `true` | Group batch items into a subdir named after the collection/column/question. Override per run with `--subdir`. |
| `use_cli` | `false` | Use the Obsidian CLI instead of writing files (see below). |
| `cli_vault_name` | `""` | Vault name for the CLI. |

### `[html]`

| Key | Default | Notes |
| --- | --- | --- |
| `enabled` | `true` | Write standalone HTML. |
| `output_path` | `archive/html` | Output directory. |
| `embed_images` | `false` | Inline images as base64 (single-file archive), read from the locally stored assets. |
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
| `max_input_chars` | `12000` | Body is truncated to this before sending. Set to 0 to disable truncation |
| `timeout_s` | `120` | Request timeout. |
| `language` | `zh` | `zh` or `en` for the summary/tags. |
| `category_reference` | `""` | Optional reference taxonomy for `category`. Empty → the AI free-generates a category; non-empty → it prefers the closest match (curbs sprawl). Build one for your corpus with `scripts/category_stats.py` — see [categories.md](categories.md). |

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
