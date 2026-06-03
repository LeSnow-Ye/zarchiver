"""zarchiver command-line interface.

Commands:

* ``login``       — open a browser, log in to Zhihu once, save the session.
* ``archive URL`` — archive a single answer/article/pin, or a batch (collection,
  column, or question); the URL kind is auto-detected.
* ``export``      — re-render already-archived items from the DB, fully offline.
* ``reai``        — regenerate AI summaries/tags/category for archived items.
* ``status``      — show how many items are archived and the most recent ones.

Everything is driven by ``config.toml`` (see ``config.example.toml``); flags on
the commands override the most common settings.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from zarchiver.ai import Summarizer, build_provider
from zarchiver.config import Config
from zarchiver.exporters.base import Exporter
from zarchiver.exporters.html import HtmlExporter
from zarchiver.exporters.obsidian import ObsidianExporter
from zarchiver.ingest import Ingestor
from zarchiver.logging_setup import setup_logging
from zarchiver.pipeline import (
    Action,
    ItemOutcome,
    Pipeline,
    export_items,
    make_image_fetcher,
    resummarize_items,
)
from zarchiver.sources.zhihu import ZhihuSource
from zarchiver.sources.zhihu.browser import ZhihuBrowser
from zarchiver.sources.zhihu.urls import classify

app = typer.Typer(
    add_completion=False,
    help="Archive Zhihu content to Obsidian markdown + HTML with AI summaries.",
)
# Logs/progress go to stderr; final results (summary, status table) to stdout.
console = Console(stderr=True)
out = Console()
log = logging.getLogger("zarchiver.cli")


@app.callback()
def _main(
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="Increase log detail: -v for debug, -vv to also include "
        "third-party libraries.",
    ),
    quiet: bool = typer.Option(
        False, "--quiet", "-q", help="Only show warnings and errors."
    ),
):
    """Configure logging before any command runs."""
    setup_logging(verbosity=verbose, quiet=quiet, console=console)


# ---------------------------------------------------------------------- #
# Shared setup
# ---------------------------------------------------------------------- #
def _load_config(
    config_path: Optional[str], no_ai: bool, on_duplicate: Optional[str]
) -> Config:
    cfg = Config.load(config_path)
    log.debug(
        "config: db=%s, assets=%s, auto_export=%s, vault=%s, html=%s, ai=%s/%s, "
        "on_duplicate=%s, headless=%s, comments=%s/max=%s",
        cfg.archive.db_path, cfg.archive.assets_root, cfg.archive.auto_export,
        cfg.obsidian.vault_path, cfg.html.output_path,
        cfg.ai.enabled, cfg.ai.model, cfg.archive.on_duplicate,
        cfg.browser.headless, cfg.archive.comments, cfg.archive.max_comments,
    )
    if no_ai:
        cfg.ai.enabled = False
    if on_duplicate:
        cfg.archive.on_duplicate = on_duplicate
    return cfg


def _build_exporters(
    cfg: Config,
    *,
    subdir: Optional[str] = None,
    only: Optional[list[str]] = None,
) -> list[Exporter]:
    """Construct the configured exporters (offline; read from the asset store).

    ``only`` restricts to a subset by name (e.g. from ``export --format``);
    otherwise the per-exporter ``enabled`` flags decide.
    """
    assets_root = cfg.archive.assets_root
    exporters: list[Exporter] = []
    want = set(only) if only else None
    if cfg.obsidian.enabled and (want is None or "obsidian" in want):
        exporters.append(
            ObsidianExporter(
                cfg.obsidian, assets_root=assets_root, subdir_override=subdir
            )
        )
    if cfg.html.enabled and (want is None or "html" in want):
        exporters.append(
            HtmlExporter(cfg.html, assets_root=assets_root, subdir_override=subdir)
        )
    if not exporters:
        log.warning("no matching exporters enabled; nothing will be written")
    else:
        log.debug("exporters: %s", ", ".join(e.name for e in exporters))
    return exporters


def _build_summarizer(cfg: Config) -> Optional[Summarizer]:
    if not cfg.ai.enabled:
        return None
    if not cfg.ai.api_key:
        log.warning(
            "AI enabled but no API key (set DEEPSEEK_API_KEY); "
            "continuing without summaries"
        )
        return None
    try:
        s = Summarizer(cfg.ai, build_provider(cfg.ai))
        log.debug("AI summarizer ready (%s)", cfg.ai.model)
        return s
    except Exception as exc:
        log.warning("AI disabled: %s", exc)
        return None


def _build_pipeline(
    cfg: Config,
    source: ZhihuSource,
    subdir: Optional[str] = None,
    *,
    auto_export: bool = True,
    dry_run: bool = False,
):
    from zarchiver.store import StateStore

    store = StateStore(cfg.archive.db_path)
    # A dry run only classifies items against the DB, so skip the costly setup
    # (image fetcher, LLM provider, exporters) entirely.
    if dry_run:
        ingestor = Ingestor(store, assets_root=cfg.archive.assets_root, fetch=None)
        pipeline = Pipeline(
            cfg, source, [], store, ingestor, auto_export=False, dry_run=True,
        )
        return pipeline, store

    fetch = make_image_fetcher(cfg)
    summarizer = _build_summarizer(cfg)

    # Auto-export targets follow archive.auto_export; an empty list (or
    # --no-export) means ingest only.
    auto = bool(cfg.archive.auto_export) and auto_export
    exporters = (
        _build_exporters(cfg, subdir=subdir, only=cfg.archive.auto_export)
        if auto
        else []
    )

    ingestor = Ingestor(
        store,
        assets_root=cfg.archive.assets_root,
        fetch=fetch,
        summarizer=summarizer,
        download_images=cfg.obsidian.download_images or not cfg.html.embed_images,
        download_concurrency=cfg.archive.download_concurrency,
    )

    def ask(item) -> bool:
        return typer.confirm(f"  '{item.title}' already archived. Re-archive?")

    pipeline = Pipeline(
        cfg,
        source,
        exporters,
        store,
        ingestor,
        auto_export=auto,
        duplicate_prompt=ask,
    )
    return pipeline, store


def _report(outcomes: list[ItemOutcome], *, dry_run: bool = False) -> None:
    counts = {a: 0 for a in Action}
    asset_issues: Counter[str] = Counter()
    for o in outcomes:
        counts[o.action] += 1
        if o.item is not None:
            asset_issues.update(o.item.asset_issues.values())
    for o in outcomes:
        if o.action == Action.FAILED:
            log.error("FAILED %s: %s", o.url, o.detail)
    if dry_run:
        # A plan: report only the would-be actions, no asset/export counts.
        parts = [
            f"[green]{counts[Action.ARCHIVED]} to archive[/green]",
            f"[cyan]{counts[Action.UPDATED]} to update[/cyan]",
            f"[dim]{counts[Action.SKIPPED]} to skip[/dim]",
        ]
        if counts[Action.FAILED]:
            parts.append(f"[red]{counts[Action.FAILED]} failed[/red]")
        out.print("Would: " + ", ".join(parts) + " [dim](dry run; nothing written)[/dim]")
        return
    parts = [
        f"[green]{counts[Action.ARCHIVED]} archived[/green]",
        f"[cyan]{counts[Action.UPDATED]} updated[/cyan]",
        f"[dim]{counts[Action.SKIPPED]} skipped[/dim]",
    ]
    if counts[Action.EXPORTED]:
        parts.append(f"[green]{counts[Action.EXPORTED]} exported[/green]")
    if counts[Action.SUMMARIZED]:
        parts.append(f"[green]{counts[Action.SUMMARIZED]} summarized[/green]")
    if counts[Action.FAILED]:
        parts.append(f"[red]{counts[Action.FAILED]} failed[/red]")
    if asset_issues["too_large"]:
        parts.append(
            f"[yellow dim]{asset_issues['too_large']} assets too large[/yellow dim]"
        )
    if asset_issues["failed"]:
        parts.append(f"[red]{asset_issues['failed']} assets failed[/red]")
    out.print("Done: " + ", ".join(parts))


def _asset_issue_label(issues: dict[str, str]) -> str:
    counts = Counter(issues.values())
    parts: list[str] = []
    if counts["too_large"]:
        parts.append(f"{counts['too_large']}✕large")
    if counts["failed"]:
        parts.append(f"{counts['failed']}✕fail")
    return " ".join(parts)


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
        out.print(
            "[bold]A browser window has opened.[/bold] Log in to Zhihu "
            "(scan QR or enter credentials)."
        )
        typer.prompt(
            "Press Enter here once you're logged in", default="", show_default=False
        )
        if browser.is_logged_in(page):
            out.print("[green]Login detected.[/green]")
        else:
            out.print(
                "[yellow]Could not confirm login from page state; saving "
                "session anyway.[/yellow]"
            )
        path = browser.save_storage_state()
        out.print(f"Session saved to [bold]{path}[/bold].")
    finally:
        browser.close()


@app.command()
def archive(
    url: str = typer.Argument(
        ..., help="Zhihu URL: answer, article, collection, column, or question"
    ),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    no_ai: bool = typer.Option(False, "--no-ai", help="Disable AI summarization"),
    no_comments: bool = typer.Option(
        False, "--no-comments", help="Do not record comments for this run"
    ),
    max_comments: Optional[int] = typer.Option(
        None,
        "--max-comments",
        help="Max comments to record per item, incl. replies (0 = all)",
    ),
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
    no_export: bool = typer.Option(
        False,
        "--no-export",
        help="Ingest into the DB (with images + AI) but skip exporting; run "
        "`export` later to render Obsidian/HTML offline.",
    ),
    no_videos: bool = typer.Option(
        False,
        "--no-videos",
        help="Do not download embedded videos (keep a poster + link instead).",
    ),
    video_quality: Optional[str] = typer.Option(
        None,
        "--video-quality",
        help="Preferred video quality: FHD | HD | SD | LD (default FHD).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show what would be archived/updated/skipped against the DB, "
        "without fetching content, running AI, or writing anything.",
    ),
):
    """Archive a single answer/article, or a batch (collection/column/question).

    Archiving ingests the content, its comments, AI summary, and images into the
    local database (the system of record), then by default renders the exporters
    configured in ``archive.auto_export``. URL kind is auto-detected: single
    answers/articles are archived directly; collection, column, and question
    URLs are batch-archived, each item going into a subdirectory named after the
    batch by default.

    With ``--dry-run``, items are classified against the DB and the plan is
    printed (would archive / update / skip) without enriching, downloading,
    summarizing, or writing. Listing the batch still loads its entries to know
    what's there, but no per-item content work happens.
    """
    cfg = _load_config(config, no_ai, on_duplicate)
    if limit:
        cfg.browser.max_items = limit
    if no_comments:
        cfg.archive.comments = False
    if max_comments is not None:
        cfg.archive.max_comments = max_comments
    if no_videos:
        cfg.archive.download_videos = False
    if video_quality:
        cfg.archive.video_quality = video_quality.strip().upper()
    target = classify(url)
    source = ZhihuSource(cfg)
    pipeline, store = _build_pipeline(
        cfg, source, subdir=subdir, auto_export=not no_export, dry_run=dry_run
    )
    try:
        if target.is_batch:
            outcomes = pipeline.archive_batch(url)
        else:
            outcomes = [pipeline.archive_url(url)]
        _report(outcomes, dry_run=dry_run)
    finally:
        source.close()
        store.close()


@app.command()
def export(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    key: Optional[str] = typer.Option(
        None, "--key", help="Export only the item with this key (platform:type:id)"
    ),
    content_type: Optional[str] = typer.Option(
        None, "--type", help="Filter by content type: answer | article | pin"
    ),
    fmt: Optional[list[str]] = typer.Option(
        None,
        "--format",
        "-f",
        help="Exporter(s) to run: obsidian | html (repeatable; default all "
        "enabled).",
    ),
    subdir: Optional[str] = typer.Option(
        None, "--subdir", help="Force output into this subdirectory."
    ),
    skip_existing: bool = typer.Option(
        False, "--skip-existing", help="Skip items whose output already exists."
    ),
    limit: int = typer.Option(0, "--limit", "-n", help="Max items (0 = all)."),
):
    """Render already-archived items from the database (fully offline).

    Reads content + comments + the recorded image asset map from the DB and
    writes Obsidian markdown / HTML, rewriting image links to the locally stored
    assets. No network access — only items already ingested by ``archive`` are
    exported.
    """
    from zarchiver.store import StateStore

    cfg = Config.load(config)
    store = StateStore(cfg.archive.db_path)
    exporters = _build_exporters(cfg, subdir=subdir, only=fmt)
    try:
        if not exporters:
            out.print("[yellow]No exporters selected; nothing to do.[/yellow]")
            return
        if key:
            item = store.load_item(key)
            items = [item] if item is not None else []
            if not items:
                out.print(f"[red]No archived item with key {key}.[/red]")
                return
        else:
            items = list(
                store.iter_items(content_type=content_type, limit=limit)
            )
        outcomes = export_items(
            items, exporters, skip_existing=skip_existing,
            progress=lambda msg: log.info("%s", msg),
        )
        _report(outcomes)
    finally:
        store.close()


@app.command()
def reai(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    key: Optional[str] = typer.Option(
        None, "--key", help="Re-summarize only the item with this key."
    ),
    content_type: Optional[str] = typer.Option(
        None, "--type", help="Filter by content type: answer | article | pin."
    ),
    only_empty: bool = typer.Option(
        False,
        "--only-empty",
        help="Only items that have no AI result yet (skip ones already summarized).",
    ),
    limit: int = typer.Option(0, "--limit", "-n", help="Max items (0 = all)."),
    export: bool = typer.Option(
        False,
        "--export",
        "-e",
        help="Re-render the affected items after re-summarizing.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt."
    ),
):
    """Regenerate AI summaries/tags/category for already-archived items.

    Re-runs the LLM over content already in the DB (no re-fetch) and saves the
    refreshed result. Useful after setting or changing ``ai.category_reference``,
    or to fill in items archived with ``--no-ai``. This spends LLM tokens — one
    call per item — so it confirms first unless ``--yes`` is given.
    """
    from zarchiver.store import StateStore

    cfg = Config.load(config)
    summarizer = _build_summarizer(cfg)
    if summarizer is None:
        out.print(
            "[red]AI is disabled or has no API key.[/red] Enable [ai] and set "
            "DEEPSEEK_API_KEY (or ai.api_key) before running reai."
        )
        raise typer.Exit(code=1)

    store = StateStore(cfg.archive.db_path)
    try:
        if key:
            item = store.load_item(key)
            items = [item] if item is not None else []
            if not items:
                out.print(f"[red]No archived item with key {key}.[/red]")
                return
        else:
            items = list(store.iter_items(content_type=content_type, limit=limit))
        if only_empty:
            items = [it for it in items if it.ai.is_empty()]
        if not items:
            out.print("[yellow]No matching items to re-summarize.[/yellow]")
            return

        if not yes:
            ok = typer.confirm(
                f"Re-summarize {len(items)} item(s) with {cfg.ai.model}? "
                "This spends LLM tokens."
            )
            if not ok:
                out.print("Aborted.")
                return

        outcomes = resummarize_items(
            items, summarizer, store,
            only_empty=only_empty,
            progress=lambda msg: log.info("%s", msg),
        )
        if export:
            done = [o.item for o in outcomes if o.action == Action.SUMMARIZED]
            if done:
                exporters = _build_exporters(cfg)
                if exporters:
                    outcomes += export_items(
                        done, exporters,
                        progress=lambda msg: log.info("%s", msg),
                    )
        _report(outcomes)
    finally:
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
        out.print(f"Archived items: [bold]{total}[/bold] (db: {cfg.archive.db_path})")
        rows = store.recent(limit)
        if rows:
            table = Table(show_header=True, header_style="bold")
            table.add_column("Type")
            table.add_column("Title", overflow="fold")
            table.add_column("Assets")
            table.add_column("Updated")
            for r in rows:
                item = store.load_item(r["key"])
                assets = _asset_issue_label(item.asset_issues) if item else ""
                table.add_row(
                    r["content_type"],
                    r["title"] or "",
                    assets,
                    r["updated_at"][:19],
                )
            out.print(table)
    finally:
        store.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
