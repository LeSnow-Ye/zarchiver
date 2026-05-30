# zarchiver

Archive Zhihu content (answers and articles) to your local machine as
**Obsidian markdown** and **standalone HTML**, with **AI-generated summaries,
tags, and categories**.

zarchiver is built modular-first: a platform-neutral core pipeline
(`source → dedup → AI → exporters`) with pluggable sources and exporters, so
support for other platforms or output formats can be added without touching the
core.

## Status

Under active development. See [docs/architecture.md](docs/architecture.md) for
the design and [docs/usage.md](docs/usage.md) for commands.

## Highlights

- **Robust scraping.** Zhihu blocks plain HTTP requests, so zarchiver drives a
  real (headful) Chromium via Playwright and parses the structured state Zhihu
  embeds in each page — far more reliable than scraping the DOM.
- **Dual archive.** Every item is written as Obsidian-flavored markdown (YAML
  frontmatter + downloaded images) *and* as a self-contained HTML file.
- **AI assist.** Summaries, tags, and a category are generated per item via an
  LLM (DeepSeek by default) and cached so re-runs never re-pay.
- **Dedup.** A SQLite index tracks what's been archived; re-runs skip unchanged
  content or detect edits.
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

# 4. Archive
uv run zarchiver archive https://zhuanlan.zhihu.com/p/35562420
uv run zarchiver collection https://www.zhihu.com/collection/<id>
uv run zarchiver status
```

## Requirements

- Python 3.12+, [uv](https://docs.astral.sh/uv/)
- A graphical session for first-time login (Zhihu blocks headless browsers).
  On WSL2 this works out of the box via WSLg; on a true headless server, run
  under `xvfb-run` and supply cookies via `ZHIHU_COOKIE`.

## License

TBD.
