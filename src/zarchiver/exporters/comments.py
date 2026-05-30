"""Rendering recorded comments into exporter output.

Comments are attached to an :class:`~zarchiver.models.ArchiveItem` as threaded
:class:`~zarchiver.models.Comment` objects (a root plus one level of replies).
This module turns them into HTML fragments that each exporter appends to the
content *before* its image-localization step, so images embedded in comments are
downloaded like any other asset.

Two shapes are produced:

* :func:`comments_markdown_fragment` — nested ``<blockquote>`` markup that
  markdownify turns into readable ``>`` / ``> >`` comment threads.
* :func:`comments_html_fragment` — styled ``<div>`` blocks for the standalone
  HTML exporter.
"""

from __future__ import annotations

import html as html_lib
from typing import Optional

from zarchiver.models import ArchiveItem, Comment


def comment_total(comments: list[Comment]) -> int:
    """Total number of comments including all nested replies."""
    return sum(c.total_count() for c in comments)


def _meta_text(comment: Comment) -> str:
    """A plain ``author · date · 👍 N`` metadata line for a comment."""
    parts: list[str] = []
    if comment.author and comment.author.name:
        parts.append(comment.author.name)
    if comment.created:
        parts.append(comment.created.strftime("%Y-%m-%d"))
    if comment.like_count:
        parts.append(f"👍 {comment.like_count}")
    return " · ".join(parts)


# ---------------------------------------------------------------------- #
# Markdown (via markdownify): nested blockquotes
# ---------------------------------------------------------------------- #
def comments_markdown_fragment(item: ArchiveItem) -> str:
    """HTML fragment for the markdown exporter (becomes blockquote threads).

    Returns an empty string when there are no comments, so callers can append
    unconditionally.
    """
    if not item.comments:
        return ""
    count = comment_total(item.comments)
    blocks = [f"<hr/><h2>评论 ({count})</h2>"]
    for c in item.comments:
        blocks.append(_comment_blockquote(c))
    return "".join(blocks)


def _comment_blockquote(comment: Comment) -> str:
    meta = _meta_text(comment)
    meta_html = f"<p><strong>{html_lib.escape(meta)}</strong></p>" if meta else ""
    # comment.content_html is raw comment body; keep it as-is so links/images
    # survive into the markdown conversion.
    inner = meta_html + (comment.content_html or "")
    for child in comment.children:
        inner += _comment_blockquote(child)
    return f"<blockquote>{inner}</blockquote>"


# ---------------------------------------------------------------------- #
# Standalone HTML: styled, threaded divs
# ---------------------------------------------------------------------- #
def comments_html_fragment(item: ArchiveItem) -> str:
    """HTML fragment for the standalone HTML exporter (styled comment blocks)."""
    if not item.comments:
        return ""
    count = comment_total(item.comments)
    parts = [f'<section class="comments"><h2>评论 ({count})</h2>']
    for c in item.comments:
        parts.append(_comment_div(c))
    parts.append("</section>")
    return "".join(parts)


def _comment_div(comment: Comment) -> str:
    meta = _meta_text(comment)
    meta_html = (
        f'<div class="comment-meta">{html_lib.escape(meta)}</div>' if meta else ""
    )
    body = f'<div class="comment-body">{comment.content_html or ""}</div>'
    children = ""
    if comment.children:
        children = '<div class="comment-children">' + "".join(
            _comment_div(c) for c in comment.children
        ) + "</div>"
    return f'<div class="comment">{meta_html}{body}{children}</div>'
