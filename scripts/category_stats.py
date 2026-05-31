#!/usr/bin/env python3
"""Read every AI ``category`` from the archive DB and report its frequency.

The archive assigns each item a single AI-generated ``category`` (see
:mod:`zarchiver.ai.summarizer`). Over a large corpus these drift into many
near-duplicate labels (e.g. ``游戏开发`` / ``游戏开发技术`` / ``游戏技术``), so this
script tallies them to (a) reveal the sprawl and (b) feed the result back to an
LLM as the raw material for a consolidated reference taxonomy.

Source of truth is the ``items`` table's ``ai_json`` column (the category
actually attached to each archived item). ``--source ai_cache`` instead reads
the ``ai_cache.category`` column (memoized results, keyed by content hash);
the two are usually near-identical.

Usage::

    uv run python scripts/category_stats.py                 # markdown to stdout
    uv run python scripts/category_stats.py -o cats.md       # ... to a file
    uv run python scripts/category_stats.py --format tsv     # count<TAB>category
    uv run python scripts/category_stats.py --format json    # {category: count}
    uv run python scripts/category_stats.py --source ai_cache
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path


def _categories_from_items(conn: sqlite3.Connection) -> tuple[Counter, int, int]:
    """Tally categories stored per archived item (``items.ai_json``).

    Returns ``(counts, total_items, items_without_category)``.
    """
    counts: Counter = Counter()
    total = 0
    missing = 0
    for (ai_json,) in conn.execute("SELECT ai_json FROM items"):
        total += 1
        category = ""
        if ai_json:
            try:
                data = json.loads(ai_json)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                category = str(data.get("category") or "").strip()
        if category:
            counts[category] += 1
        else:
            missing += 1
    return counts, total, missing


def _categories_from_cache(conn: sqlite3.Connection) -> tuple[Counter, int, int]:
    """Tally categories from the ``ai_cache.category`` column."""
    counts: Counter = Counter()
    total = 0
    missing = 0
    for (category,) in conn.execute("SELECT category FROM ai_cache"):
        total += 1
        category = (category or "").strip()
        if category:
            counts[category] += 1
        else:
            missing += 1
    return counts, total, missing


def _ordered(counts: Counter) -> list[tuple[str, int]]:
    """Most frequent first; ties broken alphabetically for stable output."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def render_markdown(
    counts: Counter, *, source: str, total: int, missing: int
) -> str:
    items = _ordered(counts)
    distinct = len(items)
    occurrences = sum(counts.values())
    lines = [
        "# 分类统计（category frequency）",
        "",
        f"- 数据来源：`{source}`",
        f"- 条目总数：{total}",
        f"- 含分类的条目：{occurrences}（{missing} 条无分类）",
        f"- 不同分类数：{distinct}",
        "",
        "按出现次数排序（次数 — 分类名）：",
        "",
    ]
    width = len(str(items[0][1])) if items else 1
    lines += [f"{count:>{width}} — {name}" for name, count in items]
    lines.append("")
    return "\n".join(lines)


def render_tsv(counts: Counter, **_: object) -> str:
    return "\n".join(f"{count}\t{name}" for name, count in _ordered(counts)) + "\n"


def render_json(counts: Counter, **_: object) -> str:
    ordered = {name: count for name, count in _ordered(counts)}
    return json.dumps(ordered, ensure_ascii=False, indent=2) + "\n"


_RENDERERS = {"markdown": render_markdown, "tsv": render_tsv, "json": render_json}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db", default="zarchiver.db", type=Path,
        help="Path to the SQLite archive (default: zarchiver.db).",
    )
    parser.add_argument(
        "--source", choices=("items", "ai_cache"), default="items",
        help="Where to read categories from (default: items).",
    )
    parser.add_argument(
        "--format", choices=tuple(_RENDERERS), default="markdown",
        help="Output format (default: markdown).",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Write to this file instead of stdout.",
    )
    args = parser.parse_args(argv)

    if not args.db.exists():
        parser.error(f"database not found: {args.db}")

    conn = sqlite3.connect(str(args.db))
    try:
        if args.source == "items":
            counts, total, missing = _categories_from_items(conn)
        else:
            counts, total, missing = _categories_from_cache(conn)
    finally:
        conn.close()

    text = _RENDERERS[args.format](
        counts, source=args.source, total=total, missing=missing
    )

    if args.output:
        args.output.write_text(text, encoding="utf-8")
        print(f"wrote {args.output} ({len(counts)} categories)", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
