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
    # State/dedup database location.
    db_path: str = "zarchiver.db"
    # skip | update | ask
    on_duplicate: str = "skip"


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
