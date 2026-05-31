# zarchiver

Archive Zhihu content (answers, articles, and pins/想法) to your local machine
as **Obsidian markdown** and **standalone HTML**, with **AI-generated summaries,
tags, and categories**.

zarchiver is built modular-first: a platform-neutral core that **ingests**
content into a local database (the system of record) and then **exports** it to
output formats — `source → ingest (dedup + AI + assets → DB) → exporters` — with
pluggable sources and exporters, so support for other platforms or output
formats can be added without touching the core.

## Status

Working: single + batch archiving (answers, articles, pins, collections,
columns, questions), comment recording, a DB-backed archive (full content +
comments + AI + image asset map), offline Obsidian + HTML export with image
localization, AI summaries/tags via DeepSeek, and content-hash dedup. See
[docs/architecture.md](docs/architecture.md) for the design,
[docs/usage.md](docs/usage.md) for commands,
[docs/configuration.md](docs/configuration.md) for config, and
[docs/scraping.md](docs/scraping.md) for the scraping approach.

## Highlights

- **Robust scraping.** Zhihu blocks plain HTTP requests, so zarchiver drives a
  real (headful) Chromium via Playwright and parses the structured state Zhihu
  embeds in each page — far more reliable than scraping the DOM.
- **Database-backed.** Every item — content, threaded comments, AI result, and
  an image asset map — is stored in SQLite as the system of record; images are
  downloaded once into a per-item assets directory.
- **Dual archive, offline export.** A separate `export` step renders each item to
  Obsidian-flavored markdown *and* self-contained HTML from the DB, with no
  network access — re-render anytime without re-fetching.
- **Faithful content.** Math is preserved as real LaTeX (`$...$` in markdown,
  MathJax in HTML) rather than images; article title images and footnote
  references are captured too.
- **Comments.** Each item's comments (root + replies, threaded) are recorded and
  rendered as a `评论` section, capped at 100 per item by default.
- **AI assist.** Summaries, tags, and a category are generated per item via an
  LLM (DeepSeek by default) and cached so re-runs never re-pay.
- **Dedup.** Re-runs skip items already in the DB with an unchanged content
  hash; choose skip / update / ask per duplicate.
- **Batch or single.** Archive one URL, or a whole favorites collection or
  column.

## Quick start

```bash
# 1. Install (uv manages the venv and Playwright browser)
uv sync
uv run playwright install chromium

# 2. Configure
cp config.example.toml config.toml
export DEEPSEEK_API_KEY=sk-...        # or set ai.api_key in config.toml

# 3. Log in to Zhihu once (opens a browser window; cookies are saved)
uv run zarchiver login

# 4. Archive (single or batch — same command, URL kind is auto-detected)
uv run zarchiver archive https://zhuanlan.zhihu.com/p/35562420
uv run zarchiver archive https://www.zhihu.com/collection/<id>
uv run zarchiver status

# 5. Re-render from the database anytime (offline; no re-fetch)
uv run zarchiver export -f obsidian
```

## Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- A graphical session for first-time login (Zhihu blocks headless browsers).
  On WSL2 this works out of the box via WSLg; on a true headless server, run
  under `xvfb-run` and supply cookies via `ZHIHU_COOKIE`.

## Development

```bash
uv sync                       # install deps incl. dev (pytest)
uv run pytest -m "not live"   # fast offline tests (no network/browser)
uv run pytest -m live         # live tests (need DEEPSEEK_API_KEY, browser)
```

Tests run offline against trimmed HTML fixtures in `tests/fixtures/`; the
`live`-marked tests hit real Zhihu/DeepSeek.

## License

TBD.
