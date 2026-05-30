"""zarchiver command-line interface.

Commands:

* ``login``       — open a browser, log in to Zhihu once, save the session.
* ``archive URL`` — archive a single answer or article (auto-detects batch URLs).
* ``collection URL`` / ``column URL`` / ``question URL`` — batch archive.
* ``status``      — show how many items are archived and the most recent ones.

Everything is driven by ``config.toml`` (see ``config.example.toml``); flags on
the commands override the most common settings.
"""

from __future__ import annotations

from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from zarchiver.ai import Summarizer, build_provider
from zarchiver.config import Config
from zarchiver.exporters.html import HtmlExporter
from zarchiver.exporters.obsidian import ObsidianExporter
from zarchiver.pipeline import Action, ItemOutcome, Pipeline, make_image_fetcher
from zarchiver.sources.zhihu import ZhihuSource
from zarchiver.sources.zhihu.browser import ZhihuBrowser
from zarchiver.sources.zhihu.urls import classify

app = typer.Typer(
    add_completion=False,
    help="Archive Zhihu content to Obsidian markdown + HTML with AI summaries.",
)
console = Console()


# ---------------------------------------------------------------------- #
# Shared setup
# ---------------------------------------------------------------------- #
def _load_config(
    config_path: Optional[str], no_ai: bool, on_duplicate: Optional[str]
) -> Config:
    cfg = Config.load(config_path)
    if no_ai:
        cfg.ai.enabled = False
    if on_duplicate:
        cfg.archive.on_duplicate = on_duplicate
    return cfg


def _build_pipeline(cfg: Config, source: ZhihuSource, subdir: Optional[str] = None):
    from zarchiver.store import StateStore

    store = StateStore(cfg.archive.db_path)
    fetch = make_image_fetcher(cfg)

    exporters = []
    if cfg.obsidian.enabled:
        exporters.append(
            ObsidianExporter(cfg.obsidian, fetch=fetch, subdir_override=subdir)
        )
    if cfg.html.enabled:
        exporters.append(
            HtmlExporter(cfg.html, fetch=fetch, subdir_override=subdir)
        )
    if not exporters:
        console.print("[yellow]Warning: no exporters enabled in config.[/yellow]")

    summarizer = None
    if cfg.ai.enabled:
        if not cfg.ai.api_key:
            console.print(
                "[yellow]AI enabled but no API key (set DEEPSEEK_API_KEY); "
                "continuing without summaries.[/yellow]"
            )
        else:
            try:
                summarizer = Summarizer(cfg.ai, build_provider(cfg.ai), store)
            except Exception as exc:
                console.print(f"[yellow]AI disabled: {exc}[/yellow]")

    def ask(item) -> bool:
        return typer.confirm(f"  '{item.title}' already archived. Re-archive?")

    pipeline = Pipeline(
        cfg,
        source,
        exporters,
        store,
        summarizer,
        duplicate_prompt=ask,
        progress=lambda msg: console.print(f"  {msg}"),
    )
    return pipeline, store


def _report(outcomes: list[ItemOutcome]) -> None:
    counts = {a: 0 for a in Action}
    for o in outcomes:
        counts[o.action] += 1
    for o in outcomes:
        if o.action == Action.FAILED:
            console.print(f"[red]FAILED[/red] {o.url}: {o.detail}")
    parts = [
        f"[green]{counts[Action.ARCHIVED]} archived[/green]",
        f"[cyan]{counts[Action.UPDATED]} updated[/cyan]",
        f"[dim]{counts[Action.SKIPPED]} skipped[/dim]",
    ]
    if counts[Action.FAILED]:
        parts.append(f"[red]{counts[Action.FAILED]} failed[/red]")
    console.print("Done: " + ", ".join(parts))


# ---------------------------------------------------------------------- #
# Commands
# ---------------------------------------------------------------------- #
@app.command()
def login(
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Config path"),
):
    """Open a browser to log in to Zhihu; saves the session for future runs."""
    cfg = Config.load(config)
    # Login must be interactive → force headful regardless of config.
    browser = ZhihuBrowser(cfg.browser, headless=False)
    browser.start()
    try:
        page = browser.new_page()
        page.goto("https://www.zhihu.com/signin", wait_until="domcontentloaded")
        console.print(
            "[bold]A browser window has opened.[/bold] Log in to Zhihu "
            "(scan QR or enter credentials)."
        )
        typer.prompt(
            "Press Enter here once you're logged in", default="", show_default=False
        )
        if browser.is_logged_in(page):
            console.print("[green]Login detected.[/green]")
        else:
            console.print(
                "[yellow]Could not confirm login from page state; saving "
                "session anyway.[/yellow]"
            )
        path = browser.save_storage_state()
        console.print(f"Session saved to [bold]{path}[/bold].")
    finally:
        browser.close()


@app.command()
def archive(
    url: str = typer.Argument(
        ..., help="Zhihu URL: answer, article, collection, column, or question"
    ),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    no_ai: bool = typer.Option(False, "--no-ai", help="Disable AI summarization"),
    on_duplicate: Optional[str] = typer.Option(
        None, "--on-duplicate", help="skip | update | ask"
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Max items for batch URLs (0 = all)"
    ),
    subdir: Optional[str] = typer.Option(
        None,
        "--subdir",
        help="Place output in this subdirectory (overrides the batch-named "
        "default; use '' to force no subdir)",
    ),
):
    """Archive a single answer/article, or a batch (collection/column/question).

    The kind of URL is auto-detected: single answers and articles are archived
    directly; collection, column, and question URLs are batch-archived, each
    item going into a subdirectory named after the batch by default.
    """
    cfg = _load_config(config, no_ai, on_duplicate)
    if limit:
        cfg.browser.max_items = limit
    target = classify(url)
    source = ZhihuSource(cfg)
    pipeline, store = _build_pipeline(cfg, source, subdir=subdir)
    try:
        if target.is_batch:
            console.print(f"Batch ({target.kind.value}): {url}")
            outcomes = pipeline.archive_batch(url)
        else:
            outcomes = [pipeline.archive_url(url)]
        _report(outcomes)
    finally:
        source.close()
        store.close()


@app.command()
def status(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(15, "--limit", "-n"),
):
    """Show archive statistics and recent items."""
    from zarchiver.store import StateStore

    cfg = Config.load(config)
    store = StateStore(cfg.archive.db_path)
    try:
        total = store.count()
        console.print(f"Archived items: [bold]{total}[/bold] (db: {cfg.archive.db_path})")
        rows = store.recent(limit)
        if rows:
            table = Table(show_header=True, header_style="bold")
            table.add_column("Type")
            table.add_column("Title", overflow="fold")
            table.add_column("Updated")
            for r in rows:
                table.add_row(r["content_type"], r["title"] or "", r["updated_at"][:19])
            console.print(table)
    finally:
        store.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
