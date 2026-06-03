# Architecture

zarchiver is organized around a small, platform-neutral **core** and a set of
**pluggable adapters** on each end. New platforms plug in as *sources*; new
output formats plug in as *exporters*. Nothing in the core knows about Zhihu or
about markdown specifically.

```
                  ┌──────────────────────────────────────────┐
   URL / batch ─▶ │  Source (zhihu)                            │
                  │   browser auth → fetch page → parse        │
                  └───────────────┬──────────────────────────┘
                                  │ ArchiveItem(s)
                                  ▼
                  ┌──────────────────────────────────────────┐
                  │  Ingest (pipeline)                         │
                  │   1. dedup check (content_hash in DB)      │
                  │   2. download images → assets_root/<key>/  │
                  │   3. AI summarize/tag                      │
                  │   4. save full item → Store (SQLite)       │
                  └───────────────┬──────────────────────────┘
                                  │  (DB = system of record)
                                  ▼
                  ┌──────────────────────────────────────────┐
                  │  Export (offline)                          │
                  │   load item ← Store; rewrite <img> from    │
                  │   the recorded asset map; fan out          │
                  └───────────────┬──────────────────────────┘
                        ┌─────────┴─────────┐
                        ▼                   ▼
               ┌──────────────┐    ┌──────────────┐
               │ Obsidian     │    │ HTML         │
               │ exporter     │    │ exporter     │
               └──────────────┘    └──────────────┘
```

Archiving runs ingest then (by default) export; the standalone `export` command
runs just the bottom half, reading from the DB with no network access.

## The neutral model

Everything flows through `zarchiver.models.ArchiveItem` — a dataclass holding
normalized content (title, HTML body, author, timestamps, metrics, topics),
threaded `Comment`s, an `AIResult`, an `asset_map` (remote image URL → local
stored path), and the original parsed `raw` dict. Sources produce it; ingest
enriches and persists it; export reconstructs it from the DB and renders it. Its
`key` (`platform:type:source_id`) is the archive identity, and its
`content_hash()` (title + body) keys duplicate detection. Duplicate
detection is based on whether that key already exists in the store with an
unchanged hash — not on output files.

`serialize.py` is the single place that round-trips an `ArchiveItem` to/from the
DB's JSON columns; the round-trip preserves `content_hash()` so dedup stays
stable across reloads.

## Modules

| Module | Responsibility |
| --- | --- |
| `models` | `ArchiveItem`, `Author`, `Comment`, `AIResult`, `BatchInfo`, `ContentType`. |
| `serialize` | `ArchiveItem` ⇄ JSON-friendly DB row (lossless round-trip). |
| `config` | Layered config: defaults → `config.toml` → env vars. |
| `store` | `StateStore`: SQLite system of record (items + comments + AI + asset map). |
| `sources/base` | `Source` ABC: `supports(url)`, `fetch(url)`, `fetch_batch(url)`. |
| `sources/zhihu` | Playwright browser, URL classification, `js-initialData` parser. |
| `ingest` | `Ingestor`: download images → assets, run AI, save full item to the store. |
| `exporters/base` | `Exporter` ABC: `export(item)`. |
| `exporters/obsidian` | Markdown + YAML frontmatter into a vault folder. |
| `exporters/html` | Standalone HTML archive. |
| `exporters/assets` | Image download (ingest) + offline link rewriting / asset copy (export). |
| `ai/base` | `LLMProvider` ABC. |
| `ai/deepseek` | DeepSeek (OpenAI-compatible) provider. |
| `ai/summarizer` | Builds prompts, parses the JSON result into an `AIResult`. |
| `pipeline` | Ingest orchestration + offline `export_items` fan-out. |
| `cli` | Typer entrypoint (`login`, `archive`, `export`, `status`). |

## Why a headful browser

Zhihu returns HTTP 403 to plain HTTP clients (and its `api/v4` endpoints require
a signed header). A real browser renders the page; **headless** Chromium is
itself detected and blocked, so zarchiver runs **headful**. Rather than scrape
the rendered DOM, it parses the JSON document Zhihu embeds in a
`<script id="js-initialData">` tag, which contains clean entities for articles,
answers, and questions. DOM scraping is only a fallback.

See [scraping.md](scraping.md) for details and findings.

## Extending

- **New platform:** implement a `Source` subclass that returns `ArchiveItem`s
  and register it. The pipeline and exporters need no changes.
- **New output:** implement an `Exporter` subclass and add it to
  `_build_exporters` in the CLI (gated by config). Exporters render offline from
  the DB-reconstructed item and its `asset_map`.
- **New LLM:** implement an `LLMProvider` subclass and select it via
  `ai.provider`.
