r"""Shared rendering of normalized formula spans.

The Zhihu parser converts equation images into
``<span class="ztex" data-tex="..." data-block="true?">`` nodes. Exporters turn
those into their target representation:

* Markdown (Obsidian): ``$...$`` (inline) / ``$$...$$`` (block). Because
  markdownify escapes LaTeX-significant characters like ``_``, we first swap
  each formula for an unescapable placeholder token, run markdownify, then
  substitute the real LaTeX back in.
* HTML: ``\(...\)`` (inline) / ``\[...\]`` (block), rendered by MathJax.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# A token markdownify won't escape or alter, with an index we map back later.
_PLACEHOLDER = "ZARCHIVERMATH{idx}ENDMATH"
_PLACEHOLDER_RE = re.compile(r"ZARCHIVERMATH(\d+)ENDMATH")


def extract_formulas_for_markdown(html: str) -> tuple[str, list[str]]:
    """Replace ``span.ztex`` nodes with placeholder tokens.

    Returns ``(html_with_placeholders, latex_fragments)`` where each fragment is
    the final markdown math string (``$...$`` or ``$$...$$``) to substitute back
    after markdown conversion via :func:`restore_formulas_markdown`.
    """
    soup = BeautifulSoup(html, "html.parser")
    fragments: list[str] = []
    for span in soup.select("span.ztex"):
        tex = (span.get("data-tex") or "").strip()
        if not tex:
            span.decompose()
            continue
        is_block = span.get("data-block") == "true"
        if is_block:
            fragments.append(f"\n$$\n{tex}\n$$\n")
        else:
            fragments.append(f"${tex}$")
        span.replace_with(_PLACEHOLDER.format(idx=len(fragments) - 1))
    return str(soup), fragments


def restore_formulas_markdown(markdown: str, fragments: list[str]) -> str:
    """Substitute placeholder tokens back with their LaTeX math strings."""

    def repl(m: re.Match) -> str:
        idx = int(m.group(1))
        return fragments[idx] if 0 <= idx < len(fragments) else m.group(0)

    return _PLACEHOLDER_RE.sub(repl, markdown)


def render_formulas_html(soup: BeautifulSoup) -> bool:
    """Rewrite ``span.ztex`` nodes in-place to MathJax-style delimiters.

    Returns True if any formula was rendered (so the caller can decide whether
    to include the MathJax script).
    """
    found = False
    for span in soup.select("span.ztex"):
        tex = (span.get("data-tex") or "").strip()
        if not tex:
            span.decompose()
            continue
        found = True
        is_block = span.get("data-block") == "true"
        span.string = f"\\[{tex}\\]" if is_block else f"\\({tex}\\)"
        # Drop the marker class/attrs; keep it a plain span for MathJax to scan.
        span.attrs = {"class": "ztex-rendered"}
    return found
