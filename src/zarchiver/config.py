"""Configuration loading for zarchiver.

Config is layered, later layers overriding earlier ones:

1. Built-in defaults (:data:`DEFAULTS`).
2. A TOML file (``config.toml`` by default, or ``--config PATH``).
3. Environment variables for secrets (``ZHIHU_*``, ``DEEPSEEK_API_KEY``).

Secrets never need to live in the TOML file; keeping them in env vars (or a
gitignored ``config.toml``) keeps them out of version control.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class BrowserConfig:
    # Zhihu blocks headless chrome-headless-shell, so default to headful.
    # In a desktop/WSLg session this just works; on a true headless server
    # run under xvfb-run.
    headless: bool = False
    user_agent: str = DEFAULT_UA
    locale: str = "zh-CN"
    # Where the persistent login profile / storage_state lives.
    storage_state: str = "storage_state.json"
    # Optional cookie string for headless/server use (fallback to login flow).
    cookie_string: str = ""
    # Per-page navigation timeout (ms) and polite delays between batch items.
    nav_timeout_ms: int = 40000
    min_delay_ms: int = 1200
    max_delay_ms: int = 3500
    # Max items to pull from a collection/column in one run (0 = unlimited).
    max_items: int = 0


@dataclass
class AIConfig:
    enabled: bool = True
    provider: str = "deepseek"
    base_url: str = "https://api.deepseek.com"
    api_key: str = ""  # prefer DEEPSEEK_API_KEY env var
    model: str = "deepseek-v4-flash"
    # deepseek-v4-flash is a reasoning model: it spends tokens on hidden
    # reasoning before the answer, so this must be generous.
    max_tokens: int = 1200
    temperature: float = 0.3
    # Truncate very long bodies before sending, to bound cost.
    max_input_chars: int = 12000
    timeout_s: int = 120
    language: str = "zh"  # language to summarize/tag in
    # Optional reference taxonomy for the `category` field. When non-empty, the
    # model is asked to prefer the closest match from this list; when empty, it
    # free-generates a category. Build one from your own archive via
    # scripts/category_stats.py — see docs/categories.md.
    category_reference: str = ""


@dataclass
class ObsidianConfig:
    enabled: bool = True
    # Vault root. Markdown is written under <vault>/<folder>.
    vault_path: str = "vault"
    folder: str = "Zhihu"
    # Assets (images) go here, relative to the vault root.
    assets_folder: str = "Zhihu/assets"
    download_images: bool = True
    # Filename template; available fields: {title}, {author}, {source_id},
    # {content_type}, {date}.
    filename_template: str = "{title} - {author}"
    # For batch archives (collection/column/question), place notes and assets in
    # a subdirectory named after the batch (e.g. <folder>/<column title>/).
    batch_subdirs: bool = True
    # Optional Obsidian CLI integration. Requires the desktop app running;
    # off by default because it does not work headless.
    use_cli: bool = False
    cli_vault_name: str = ""


@dataclass
class HtmlConfig:
    enabled: bool = True
    output_path: str = "archive/html"
    embed_images: bool = False  # if True, inline images as base64 (self-contained)
    # Mirror Obsidian: batch archives go in a subdirectory named after the batch.
    batch_subdirs: bool = True


@dataclass
class ArchiveConfig:
    # State/dedup database location (also the system of record for content).
    db_path: str = "zarchiver.db"
    # Root directory for DB-managed image assets; one subdir per item key.
    assets_root: str = "archive/assets"
    # Which exporters to run automatically after ingest (empty = ingest only).
    auto_export: list[str] = field(default_factory=lambda: ["obsidian", "html"])
    # skip | update | ask
    on_duplicate: str = "skip"
    # Record comments on archived content.
    comments: bool = True
    # Max comments to record per item, including child replies (0 = unlimited).
    # Popular content can have thousands of comments, so this is capped.
    max_comments: int = 100
    # Download embedded videos (<a class="video-box">) as MP4 files. When off,
    # videos degrade to a poster image + a link.
    download_videos: bool = True
    # Preferred video quality when downloading: FHD | HD | SD | LD.
    video_quality: str = "FHD"
    # Maximum size (in MB) for a single downloaded asset (image/video). Assets
    # larger than this are not stored locally; the content keeps the original
    # remote link instead. 0 disables the limit (archive everything).
    max_asset_mb: float = 20.0
    # Number of retries after the first attempt for transient asset-download
    # failures: timeouts, transport errors, HTTP 429/5xx. Permanent failures
    # (4xx) and over-size skips are never retried. 0 = a single attempt.
    max_asset_retries: int = 2
    # In batch archives, build items directly from the listing API's JSON
    # (which carries the full content body) instead of opening each item's
    # page. Pages are still opened as a fallback when the API entry lacks
    # usable content. Set False to force the old open-every-page behavior.
    prefer_api_content: bool = True


@dataclass
class Config:
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    html: HtmlConfig = field(default_factory=HtmlConfig)
    ai: AIConfig = field(default_factory=AIConfig)

    # ------------------------------------------------------------------ #
    # Loading
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "Config":
        """Load config from defaults + optional TOML file + env overrides."""
        cfg = cls()
        file_path = _resolve_config_path(path)
        if file_path and file_path.is_file():
            with file_path.open("rb") as fh:
                data = tomllib.load(fh)
            _merge_into(cfg, data)
        cfg._apply_env()
        return cfg

    def _apply_env(self) -> None:
        """Let environment variables override secrets/paths."""
        env = os.environ
        if v := env.get("DEEPSEEK_API_KEY"):
            self.ai.api_key = v
        if v := env.get("ZARCHIVER_AI_MODEL"):
            self.ai.model = v
        if v := env.get("ZHIHU_COOKIE"):
            self.browser.cookie_string = v
        if v := env.get("ZARCHIVER_VAULT"):
            self.obsidian.vault_path = v
        if v := env.get("ZARCHIVER_DB"):
            self.archive.db_path = v
        if v := env.get("ZARCHIVER_ASSETS_ROOT"):
            self.archive.assets_root = v
        if v := env.get("ZARCHIVER_AUTO_EXPORT"):
            self.archive.auto_export = [
                p.strip() for p in v.split(",") if p.strip()
            ]
        if v := env.get("ZARCHIVER_VIDEO_QUALITY"):
            self.archive.video_quality = v.strip().upper()
        if v := env.get("ZARCHIVER_MAX_ASSET_MB"):
            try:
                self.archive.max_asset_mb = max(0.0, float(v.strip()))
            except ValueError:
                pass  # ignore a malformed override, keep the configured value
        if v := env.get("ZARCHIVER_MAX_ASSET_RETRIES"):
            try:
                self.archive.max_asset_retries = max(0, int(v.strip()))
            except ValueError:
                pass  # ignore a malformed override, keep the configured value
        if (v := env.get("ZARCHIVER_PREFER_API_CONTENT")) is not None:
            self.archive.prefer_api_content = v.strip().lower() in (
                "1", "true", "yes"
            )
        if (v := env.get("ZARCHIVER_HEADLESS")) is not None:
            self.browser.headless = v.strip().lower() in ("1", "true", "yes")


def _resolve_config_path(path: Optional[str | Path]) -> Optional[Path]:
    if path:
        return Path(path).expanduser()
    candidate = Path("config.toml")
    return candidate if candidate.is_file() else None


def _merge_into(target: Any, data: dict) -> None:
    """Recursively merge a dict of TOML data into a dataclass instance."""
    if not is_dataclass(target):
        return
    valid = {f.name: f for f in fields(target)}
    for key, value in data.items():
        if key not in valid:
            # Unknown keys are ignored so configs are forward-compatible.
            continue
        current = getattr(target, key)
        if is_dataclass(current) and isinstance(value, dict):
            _merge_into(current, value)
        else:
            setattr(target, key, value)
