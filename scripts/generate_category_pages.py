#!/usr/bin/env python3
"""Generate one directory note per Obsidian frontmatter ``category``.

Usage::

    uv run python scripts/generate_category_pages.py /path/to/vault
    uv run python scripts/generate_category_pages.py /path/to/vault --output-dir 分类
    uv run python scripts/generate_category_pages.py /path/to/vault --if-exists merge
    uv run python scripts/generate_category_pages.py /path/to/vault --dataview-serializer
    uv run python scripts/generate_category_pages.py /path/to/vault --generate-graph-settings

By default files are written under ``<vault>/目录/``. If that directory already
exists, the script asks whether to delete it, merge into it, or abort. In
non-interactive shells, use ``--if-exists delete`` or ``--if-exists merge``.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import json
import random
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import yaml


QUERY_TEMPLATE = (
    '<!-- QueryToSerialize: TABLE tags AS "Tags", summary AS "Summary" '
    '{sort_clause}WHERE category="{category}" -->\n'
)
GOLDEN_RATIO_CONJUGATE = 0.618033988749895

# Characters not allowed in file names on common filesystems / Obsidian.
_ILLEGAL_FILENAME = re.compile(r'[\\/:*?"<>|#^\[\]]')


class NoteEntry(NamedTuple):
    title: str
    file_name: str
    categories: list[str]
    tags: list[str]
    summary: str
    archived_at: str


def sanitize_filename(name: str, max_len: int = 120) -> str:
    """Return a filesystem-safe note basename for an Obsidian category."""
    name = _ILLEGAL_FILENAME.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name


def _frontmatter_text(markdown: str) -> str | None:
    """Extract YAML frontmatter text from a Markdown document, if present."""
    if markdown.startswith("\ufeff"):
        markdown = markdown.removeprefix("\ufeff")
    if not markdown.startswith("---"):
        return None

    first_line_end = markdown.find("\n")
    if first_line_end == -1:
        return None
    if markdown[:first_line_end].strip() != "---":
        return None

    closing = markdown.find("\n---", first_line_end + 1)
    while closing != -1:
        line_end = markdown.find("\n", closing + 1)
        line = (
            markdown[closing + 1:]
            if line_end == -1
            else markdown[closing + 1:line_end]
        )
        if line.strip() == "---":
            return markdown[first_line_end + 1:closing + 1]
        closing = markdown.find("\n---", closing + 1)
    return None


def _category_values(value: Any) -> list[str]:
    """Normalize a frontmatter category value to one or more labels."""
    return _unique_strings(_metadata_values(value))


def _metadata_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        values = value
    else:
        values = [value]

    out: list[str] = []
    for item in values:
        text = re.sub(r"\s+", " ", str(item)).strip()
        if text:
            out.append(text)
    return out


def _unique_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def _tag_values(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_values = re.split(r"[\s,]+", value)
    else:
        raw_values = _metadata_values(value)

    tags: list[str] = []
    for raw in raw_values:
        tag = re.sub(r"\s+", "", str(raw)).strip()
        if not tag:
            continue
        if not tag.startswith("#"):
            tag = f"#{tag}"
        tags.append(tag)
    return _unique_strings(tags)


def _frontmatter_data(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="utf-8-sig", errors="replace")

    frontmatter = _frontmatter_text(text)
    if frontmatter is None:
        return {}
    data = yaml.safe_load(frontmatter) or {}
    return data if isinstance(data, dict) else {}


def categories_from_markdown(path: Path) -> list[str]:
    """Read category metadata from a single Markdown note."""
    data = _frontmatter_data(path)
    return _category_values(data.get("category"))


def note_from_markdown(path: Path) -> NoteEntry | None:
    """Read the metadata needed for directory rendering from one Markdown note."""
    data = _frontmatter_data(path)
    categories = _category_values(data.get("category"))
    if not categories:
        return None

    title = re.sub(r"\s+", " ", str(data.get("title") or path.stem)).strip()
    if not title:
        title = path.stem
    summary = re.sub(r"\s+", " ", str(data.get("summary") or "")).strip()
    archived_at = re.sub(r"\s+", " ", str(data.get("archived_at") or "")).strip()
    return NoteEntry(
        title=title,
        file_name=path.stem,
        categories=categories,
        tags=_tag_values(data.get("tags")),
        summary=summary,
        archived_at=archived_at,
    )


def scan_notes(
    vault: Path, *, exclude_dir: Path | None = None
) -> tuple[dict[str, list[NoteEntry]], int, int]:
    """Return ``(notes_by_category, markdown_count, notes_with_category)``."""
    notes_by_category: dict[str, list[NoteEntry]] = {}
    markdown_count = 0
    notes_with_category = 0
    exclude_resolved = exclude_dir.resolve() if exclude_dir else None

    for path in sorted(vault.rglob("*.md")):
        if exclude_resolved is not None:
            try:
                path.resolve().relative_to(exclude_resolved)
            except ValueError:
                pass
            else:
                continue

        markdown_count += 1
        try:
            note = note_from_markdown(path)
        except (OSError, yaml.YAMLError) as exc:
            print(f"warning: skipped {path}: {exc}", file=sys.stderr)
            continue
        if note is None:
            continue
        notes_with_category += 1
        for category in note.categories:
            notes_by_category.setdefault(category, []).append(note)
    return notes_by_category, markdown_count, notes_with_category


def scan_categories(
    vault: Path, *, exclude_dir: Path | None = None
) -> tuple[Counter, int, int]:
    """Return ``(category_counts, markdown_count, notes_with_category)``."""
    notes_by_category, markdown_count, notes_with_category = scan_notes(
        vault, exclude_dir=exclude_dir
    )
    counts: Counter = Counter(
        {
            category: len(notes)
            for category, notes in notes_by_category.items()
        }
    )
    return counts, markdown_count, notes_with_category


def _query_category_literal(category: str) -> str:
    return category.replace("\\", "\\\\").replace('"', '\\"')


def _table_cell(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip()
    return value.replace("|", r"\|")


def _wiki_link(title: str) -> str:
    return f"[[{title}]]"


def _archived_at_sort_key(note: NoteEntry) -> tuple[bool, float, str]:
    if not note.archived_at:
        return (False, 0.0, note.file_name.casefold())
    normalized = note.archived_at.replace("Z", "+00:00")
    try:
        archived_at = datetime.fromisoformat(normalized)
    except ValueError:
        return (False, 0.0, note.file_name.casefold())
    if archived_at.tzinfo is None:
        archived_at = archived_at.replace(tzinfo=timezone.utc)
    return (True, archived_at.timestamp(), note.file_name.casefold())


def _archived_at_desc_sort_key(note: NoteEntry) -> tuple[bool, float, str]:
    has_archived_at, timestamp, file_name = _archived_at_sort_key(note)
    return (not has_archived_at, -timestamp if has_archived_at else 0.0, file_name)


def _ordered_notes(
    notes: list[NoteEntry], *, sort_archived_desc: bool
) -> list[NoteEntry]:
    if sort_archived_desc:
        return sorted(notes, key=_archived_at_desc_sort_key)
    return sorted(notes, key=lambda item: item.file_name.casefold())


def render_markdown_table(
    notes: list[NoteEntry], *, sort_archived_desc: bool = False
) -> str:
    lines = [
        "| File | Tags | Summary |",
        "| --- | --- | --- |",
    ]
    for note in _ordered_notes(notes, sort_archived_desc=sort_archived_desc):
        tags = " ".join(note.tags)
        lines.append(
            "| "
            + " | ".join(
                [
                    _table_cell(_wiki_link(note.file_name)),
                    _table_cell(tags),
                    _table_cell(note.summary),
                ]
            )
            + " |"
        )
    lines.append("")
    return "\n".join(lines)


def render_dataview_serializer_query(
    category: str, *, sort_archived_desc: bool = False
) -> str:
    sort_clause = "SORT archived_at DESC " if sort_archived_desc else ""
    return QUERY_TEMPLATE.format(
        sort_clause=sort_clause,
        category=_query_category_literal(category),
    )


def _rgb_int_from_hsl(hue: float, saturation: float, lightness: float) -> int:
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    r = round(red * 255)
    g = round(green * 255)
    b = round(blue * 255)
    return (r << 16) + (g << 8) + b


def _category_graph_query(category: str) -> str:
    return f'["category":{category}]'


def render_graph_settings(
    categories: list[str],
    *,
    rng: random.Random | None = None,
) -> str:
    """Render Obsidian graph settings with readable, distributed colors."""
    rng = rng or random.Random()
    hue = rng.random()
    color_groups = []
    for category in categories:
        hue = (hue + GOLDEN_RATIO_CONJUGATE) % 1.0
        saturation = rng.uniform(0.58, 0.74)
        lightness = rng.uniform(0.54, 0.66)
        color_groups.append(
            {
                "query": _category_graph_query(category),
                "color": {
                    "a": 1,
                    "rgb": _rgb_int_from_hsl(hue, saturation, lightness),
                },
            }
        )

    settings = {
        "showTags": True,
        "colorGroups": color_groups,
    }
    return json.dumps(settings, ensure_ascii=False, indent=2) + "\n"


def write_graph_settings(vault: Path, categories: list[str]) -> Path:
    graph_path = vault / ".obsidian" / "graph.json"
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(render_graph_settings(categories), encoding="utf-8")
    return graph_path


def category_filename(category: str, used: set[str]) -> str:
    """Return a safe, unique ``.md`` filename for ``category``."""
    stem = sanitize_filename(category)
    if not stem:
        stem = "category"

    candidate = f"{stem}.md"
    if candidate not in used:
        used.add(candidate)
        return candidate

    suffix = hashlib.sha1(category.encode("utf-8")).hexdigest()[:8]
    candidate = f"{stem}-{suffix}.md"
    if candidate not in used:
        used.add(candidate)
        return candidate

    index = 2
    while True:
        candidate = f"{stem}-{suffix}-{index}.md"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


def _confirm_existing_dir(path: Path) -> str:
    prompt = (
        f"Output directory already exists: {path}\n"
        "Choose [d]elete, [m]erge, or [a]bort: "
    )
    while True:
        answer = input(prompt).strip().lower()
        if answer in {"d", "delete", "删除"}:
            return "delete"
        if answer in {"m", "merge", "合并"}:
            return "merge"
        if answer in {"a", "abort", "取消", "退出"}:
            return "abort"
        print("Please enter d/delete, m/merge, or a/abort.", file=sys.stderr)


def prepare_output_dir(path: Path, if_exists: str) -> None:
    if not path.exists():
        path.mkdir(parents=True)
        return
    if not path.is_dir():
        raise RuntimeError(f"output path exists but is not a directory: {path}")

    action = if_exists
    if action == "ask":
        if not sys.stdin.isatty():
            raise RuntimeError(
                f"output directory already exists: {path}; "
                "use --if-exists delete or --if-exists merge in non-interactive shells"
            )
        action = _confirm_existing_dir(path)

    if action == "abort":
        raise RuntimeError(f"aborted because output directory exists: {path}")
    if action == "delete":
        shutil.rmtree(path)
        path.mkdir(parents=True)
    elif action == "merge":
        path.mkdir(parents=True, exist_ok=True)
    else:
        raise RuntimeError(f"unknown --if-exists action: {action}")


def write_category_pages(
    notes_by_category: dict[str, list[NoteEntry]],
    output_dir: Path,
    *,
    dataview_serializer: bool,
    sort_archived_desc: bool,
) -> dict[str, Path]:
    """Write category directory notes and return category -> output path."""
    used: set[str] = set()
    written: dict[str, Path] = {}
    categories = list(notes_by_category)
    ordered = sorted(
        categories,
        key=lambda category: (
            sanitize_filename(category) != category,
            sanitize_filename(category),
            category,
        ),
    )
    for category in ordered:
        filename = category_filename(category, used)
        path = output_dir / filename
        if dataview_serializer:
            content = render_dataview_serializer_query(
                category, sort_archived_desc=sort_archived_desc
            )
        else:
            content = render_markdown_table(
                notes_by_category[category],
                sort_archived_desc=sort_archived_desc,
            )
        path.write_text(content, encoding="utf-8")
        written[category] = path
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("vault", type=Path, help="Path to the Obsidian vault.")
    parser.add_argument(
        "-o", "--output-dir", default="目录",
        help=(
            "Output directory, relative to the vault unless absolute "
            "(default: 目录)."
        ),
    )
    parser.add_argument(
        "--if-exists",
        choices=("ask", "delete", "merge", "abort"),
        default="ask",
        help=(
            "What to do if the output directory exists. "
            "Default: ask interactively."
        ),
    )
    parser.add_argument(
        "-ds", "--dataview-serializer",
        action="store_true",
        help="Write QueryToSerialize comments instead of static Markdown tables.",
    )
    parser.add_argument(
        "-sbt", "--sort-by-time",
        action="store_true",
        help="Sort notes by archived_at descending, newest first.",
    )
    parser.add_argument(
        "--generate-graph-settings",
        action="store_true",
        help="Overwrite <vault>/.obsidian/graph.json with category color groups.",
    )
    args = parser.parse_args(argv)

    vault = args.vault.expanduser().resolve()
    if not vault.exists():
        parser.error(f"vault not found: {vault}")
    if not vault.is_dir():
        parser.error(f"vault is not a directory: {vault}")

    output_dir = Path(args.output_dir).expanduser()
    if not output_dir.is_absolute():
        output_dir = vault / output_dir
    output_dir = output_dir.resolve()
    if output_dir == vault:
        parser.error("output directory must not be the vault root")
    try:
        output_dir.relative_to(vault)
    except ValueError:
        parser.error(f"output directory must be inside the vault: {output_dir}")

    notes_by_category, markdown_count, notes_with_category = scan_notes(
        vault, exclude_dir=output_dir
    )
    categories = sorted(notes_by_category)

    try:
        prepare_output_dir(output_dir, args.if_exists)
        written = write_category_pages(
            notes_by_category,
            output_dir,
            dataview_serializer=args.dataview_serializer,
            sort_archived_desc=args.sort_by_time,
        )
    except RuntimeError as exc:
        parser.exit(1, f"error: {exc}\n")

    graph_path = None
    if args.generate_graph_settings:
        graph_path = write_graph_settings(vault, categories)

    print(
        f"scanned {markdown_count} markdown files; "
        f"found {len(notes_by_category)} categories in {notes_with_category} notes",
        file=sys.stderr,
    )
    print(f"wrote {len(written)} files to {output_dir}", file=sys.stderr)
    if graph_path is not None:
        print(f"wrote graph settings to {graph_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
