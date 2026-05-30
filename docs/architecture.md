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
                  │  Pipeline                                  │
                  │   1. dedup check (output file exists?)     │
                  │   2. AI summarize/tag (cached)             │
                  │   3. fan out to exporters                  │
                  └───────────────┬──────────────────────────┘
                        ┌─────────┴─────────┐
                        ▼                   ▼
               ┌──────────────┐    ┌──────────────┐
               │ Obsidian     │    │ HTML         │   (+ assets downloader)
               │ exporter     │    │ exporter     │
               └──────────────┘    └──────────────┘
```

## The neutral model

Everything flows through `zarchiver.models.ArchiveItem` — a dataclass holding
normalized content (title, HTML body, author, timestamps, metrics, topics) plus
an `AIResult` slot. Sources produce it; the pipeline, store, AI module, and
exporters all consume it. Its `key` (`platform:type:source_id`) is the archive
identity and its `content_hash()` keys the AI cache. Duplicate detection,
however, is based on whether an exporter's **output file already exists** (see
the pipeline), not on the store.

## Modules

| Module | Responsibility |
| --- | --- |
| `models` | `ArchiveItem`, `Author`, `AIResult`, `ContentType`. |
| `config` | Layered config: defaults → `config.toml` → env vars. |
| `store` | `StateStore`: SQLite AI-result cache + archived-history log. |
| `sources/base` | `Source` ABC: `supports(url)`, `fetch(url)`, `fetch_batch(url)`. |
| `sources/zhihu` | Playwright browser, URL classification, `js-initialData` parser. |
| `exporters/base` | `Exporter` ABC: `export(item)`. |
| `exporters/obsidian` | Markdown + YAML frontmatter into a vault folder. |
| `exporters/html` | Standalone HTML archive. |
| `exporters/assets` | Image download + link rewriting. |
| `ai/base` | `LLMProvider` ABC. |
| `ai/deepseek` | DeepSeek (OpenAI-compatible) provider. |
| `ai/summarizer` | Builds prompts, parses JSON result, caches via store. |
| `pipeline` | Orchestrates source → dedup → AI → exporters. |
| `cli` | Typer entrypoint. |

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
- **New output:** implement an `Exporter` subclass and add it to the pipeline's
  exporter list (gated by config).
- **New LLM:** implement an `LLMProvider` subclass and select it via
  `ai.provider`.
