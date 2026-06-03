"""Exporter tests: markdown/frontmatter, HTML, asset localization (offline)."""

from pathlib import Path

import pytest
import yaml

from zarchiver.config import HtmlConfig, ObsidianConfig
from zarchiver.exporters.assets import localize_images
from zarchiver.exporters.html import HtmlExporter
from zarchiver.exporters.obsidian import ObsidianExporter, sanitize_filename
from zarchiver.models import (
    AIResult,
    ArchiveItem,
    Author,
    BatchInfo,
    BatchKind,
    Comment,
    ContentType,
)

FIXTURES = Path(__file__).parent / "fixtures"

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def _seed_asset(assets_root: Path, key: str, url: str, fname: str) -> str:
    """Write a fake downloaded image and return its asset_map relative path.

    Mirrors what ingest produces: ``<safe_key>/<filename>`` under the root.
    """
    safe = key.replace(":", "_")
    rel = f"{safe}/{fname}"
    target = assets_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(PNG)
    return rel


def _sample_item() -> ArchiveItem:
    item = ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="999",
        url="https://zhuanlan.zhihu.com/p/999",
        title="测试标题/with:illegal*chars",
        content_html=(
            '<p>第一段</p>'
            '<h2>小标题</h2>'
            '<p>第二段 <strong>加粗</strong></p>'
            '<img src="https://pic1.zhimg.com/x.jpg" '
            'data-original="https://pic1.zhimg.com/x_hd.jpg">'
        ),
        author=Author(name="作者", url="https://www.zhihu.com/people/abc"),
        voteup_count=42,
    )
    item.ai = AIResult(
        summary="这是摘要", tags=["美妆", "教程"], category="生活", model="test"
    )
    item.topics = ["化妆"]
    return item


# ---------------------------------------------------------------------- #
def test_sanitize_filename_strips_illegal():
    assert "/" not in sanitize_filename("a/b")
    assert ":" not in sanitize_filename("a:b")
    assert sanitize_filename("   ") == "untitled"


def test_localize_images_prefers_data_original():
    html = (
        '<img src="https://pic1.zhimg.com/small.jpg" '
        'data-original="https://pic1.zhimg.com/big.jpg">'
    )
    rewritten, pairs = localize_images(html, "assets")
    assert len(pairs) == 1
    assert pairs[0][0] == "https://pic1.zhimg.com/big.jpg"  # picked data-original
    assert pairs[0][1] in rewritten
    assert "assets/" in rewritten


def test_obsidian_export_writes_frontmatter(tmp_path):
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "vault"),
        folder="Zhihu",
        download_images=False,  # no network in tests
    )
    exp = ObsidianExporter(cfg)
    item = _sample_item()
    result = exp.export(item)
    assert result.path and result.path.is_file()
    text = result.path.read_text(encoding="utf-8")

    # Frontmatter parses and carries our fields.
    assert text.startswith("---\n")
    fm_raw = text.split("---\n", 2)[1]
    fm = yaml.safe_load(fm_raw)
    assert fm["source_id"] == "999"
    assert fm["voteup"] == 42
    assert fm["category"] == "生活"
    assert fm["summary"] == "这是摘要"
    # topics + ai tags merged & deduped
    assert "美妆" in fm["tags"] and "化妆" in fm["tags"]

    # Body converted to markdown.
    body = text.split("---\n", 2)[2]
    assert "## 小标题" in body
    assert "**加粗**" in body
    # Illegal chars removed from filename.
    assert "/" not in result.path.name and ":" not in result.path.name


def test_html_export_self_contained(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "html"))
    exp = HtmlExporter(cfg)
    item = _sample_item()
    result = exp.export(item)
    assert result.path and result.path.is_file()
    doc = result.path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in doc
    assert "AI 摘要" in doc  # ai box rendered
    assert "测试标题" in doc
    assert "▲ 42" in doc  # voteup metric


def test_obsidian_export_with_local_assets(tmp_path):
    # Ingest already downloaded the image; the item carries an asset_map and the
    # file exists under assets_root. Export is offline: rewrite + copy.
    assets_root = tmp_path / "assets"
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "vault"),
        folder="Zhihu",
        assets_folder="Zhihu/assets",
        download_images=True,
    )
    item = _sample_item()
    rel = _seed_asset(
        assets_root, item.key, "https://pic1.zhimg.com/x_hd.jpg", "x_hd.jpg"
    )
    item.asset_map = {"https://pic1.zhimg.com/x_hd.jpg": rel}
    exp = ObsidianExporter(cfg, assets_root=str(assets_root))
    result = exp.export(item)
    assets = tmp_path / "vault" / "Zhihu" / "assets"
    files = list(assets.glob("*"))
    assert files, "expected the stored image copied into the vault assets dir"
    text = result.path.read_text(encoding="utf-8")
    assert "assets/" in text


def test_obsidian_export_keeps_remote_when_not_in_map(tmp_path):
    # Image was never downloaded (not in asset_map) → keep remote URL, offline.
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "vault"), folder="Zhihu", download_images=True
    )
    item = _sample_item()  # asset_map empty
    text = (
        ObsidianExporter(cfg, assets_root=str(assets_root))
        .export(item)
        .path.read_text(encoding="utf-8")
    )
    assert "https://pic1.zhimg.com/x_hd.jpg" in text


def test_obsidian_escapes_ref_marker_brackets(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    item = _sample_item()
    item.content_html = (
        '<p>正文<a class="ref-marker" href="#ref-1">[1]</a></p>'
        '<h2>参考</h2><ol><li id="ref-1">出处</li></ol>'
    )
    body = (
        ObsidianExporter(cfg)
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )

    assert "\\[1\\]" in body
    assert "[[1]](#ref-1)" not in body
    assert "(#ref-1)" not in body


def test_obsidian_escapes_literal_bracket_emoji_text(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    item = _sample_item()
    item.content_html = (
        '<p>[doge] [思考] '
        '<a href="https://example.com">普通链接</a></p>'
    )
    body = (
        ObsidianExporter(cfg)
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )

    assert "\\[doge\\] \\[思考\\]" in body
    assert "[普通链接](https://example.com)" in body


def test_obsidian_does_not_escape_code_block_text(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    item = _sample_item()
    item.content_html = (
        "<pre><code>items[0] # keep literal</code></pre>"
        "<p><code>inline[1] # literal</code></p>"
    )
    body = (
        ObsidianExporter(cfg)
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )

    assert "items[0] # keep literal" in body
    assert "items\\[0\\] \\# keep literal" not in body
    assert "`inline[1] # literal`" in body
    assert "`inline\\[1\\] \\# literal`" not in body


def test_obsidian_escapes_hash_in_body_text(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    item = _sample_item()
    item.content_html = '<p>#话题 正文中的 #hashtag 需要转义</p>'
    body = (
        ObsidianExporter(cfg)
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )

    assert "\\#话题 正文中的 \\#hashtag 需要转义" in body


# ---------------------------------------------------------------------- #
# Formulas
# ---------------------------------------------------------------------- #
def _formula_item() -> ArchiveItem:
    return ArchiveItem(
        platform="zhihu",
        content_type=ContentType.ARTICLE,
        source_id="f1",
        url="u",
        title="T",
        content_html=(
            '<p>inline <span class="ztex" data-tex="a_b^2">x</span> end</p>'
            '<p><span class="ztex" data-tex="\\frac{1}{2}" data-block="true">'
            "y</span></p>"
        ),
    )


def test_obsidian_formula_to_latex(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    result = ObsidianExporter(cfg).export(_formula_item())
    body = result.path.read_text(encoding="utf-8").split("---\n", 2)[2]
    # Inline becomes $...$; underscores/carets are NOT markdown-escaped.
    assert "$a_b^2$" in body
    assert "a\\_b" not in body
    # Block becomes $$...$$.
    assert "$$" in body
    assert "\\frac{1}{2}" in body


def test_html_formula_to_mathjax(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))
    result = HtmlExporter(cfg).export(_formula_item())
    doc = result.path.read_text(encoding="utf-8")
    # MathJax script injected because formulas exist.
    assert "mathjax" in doc.lower()
    assert "\\(a_b^2\\)" in doc  # inline delimiter
    assert "\\[\\frac{1}{2}\\]" in doc  # block delimiter


def test_html_no_mathjax_without_formulas(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))
    item = _sample_item()  # no formulas
    doc = HtmlExporter(cfg).export(item).path.read_text(encoding="utf-8")
    assert "mathjax" not in doc.lower()


# ---------------------------------------------------------------------- #
# Title image
# ---------------------------------------------------------------------- #
def test_obsidian_title_image_prepended(tmp_path):
    assets_root = tmp_path / "assets"
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=True)
    item = _sample_item()
    item.title_image = "https://pic1.zhimg.com/title.jpg"
    rel = _seed_asset(assets_root, item.key, item.title_image, "title.jpg")
    item.asset_map = {item.title_image: rel}
    body = (
        ObsidianExporter(cfg, assets_root=str(assets_root))
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )
    # Title image appears as the first content image (local path).
    assert "![" in body
    assert "assets/" in body
    # It should come before the body text.
    assert body.index("![") < body.index("第一段")


def test_html_title_image_banner(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))  # no fetch -> stays remote
    item = _sample_item()
    item.title_image = "https://pic1.zhimg.com/title.jpg"
    doc = HtmlExporter(cfg).export(item).path.read_text(encoding="utf-8")
    assert 'class="title-image"' in doc
    assert "https://pic1.zhimg.com/title.jpg" in doc


# ---------------------------------------------------------------------- #
# Column / collection metadata + batch subdirectories
# ---------------------------------------------------------------------- #
def _batch_item() -> ArchiveItem:
    item = _sample_item()
    item.column_title = "我的专栏"
    item.column_url = "https://zhuanlan.zhihu.com/mycol"
    item.batch = BatchInfo(
        kind=BatchKind.COLLECTION,
        title="收藏夹: 好文/精选",
        url="https://www.zhihu.com/collection/123",
        id="123",
    )
    return item


def test_obsidian_records_column_and_collection(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    fm = (
        ObsidianExporter(cfg)
        .export(_batch_item())
        .path.read_text(encoding="utf-8")
    )
    meta = yaml.safe_load(fm.split("---\n", 2)[1])
    assert meta["column"] == "我的专栏"
    assert meta["column_url"] == "https://zhuanlan.zhihu.com/mycol"
    assert meta["collection"] == "收藏夹: 好文/精选"
    assert meta["collection_url"] == "https://www.zhihu.com/collection/123"


def test_obsidian_batch_subdir_placement(tmp_path):
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "v"),
        folder="Zhihu",
        assets_folder="Zhihu/assets",
        download_images=False,
        batch_subdirs=True,
    )
    result = ObsidianExporter(cfg).export(_batch_item())
    # Subdir is the sanitized batch title (slash removed).
    parent = result.path.parent
    assert parent.name == "收藏夹 好文精选"  # ":" and "/" stripped
    assert parent.parent == tmp_path / "v" / "Zhihu"


def test_obsidian_batch_subdirs_disabled(tmp_path):
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "v"),
        folder="Zhihu",
        download_images=False,
        batch_subdirs=False,
    )
    result = ObsidianExporter(cfg).export(_batch_item())
    assert result.path.parent == tmp_path / "v" / "Zhihu"  # no subdir


def test_subdir_override_forces_dir(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    item = _sample_item()  # no batch
    result = ObsidianExporter(cfg, subdir_override="custom").export(item)
    assert result.path.parent.name == "custom"


def test_subdir_override_empty_disables(tmp_path):
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "v"), folder="Zhihu", download_images=False
    )
    result = ObsidianExporter(cfg, subdir_override="").export(_batch_item())
    assert result.path.parent == tmp_path / "v" / "Zhihu"


def test_obsidian_batch_assets_in_subdir(tmp_path):
    assets_root = tmp_path / "assets"
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "v"),
        folder="Zhihu",
        assets_folder="Zhihu/assets",
        download_images=True,
        batch_subdirs=True,
    )
    item = _batch_item()
    rel = _seed_asset(
        assets_root, item.key, "https://pic1.zhimg.com/x_hd.jpg", "x_hd.jpg"
    )
    item.asset_map = {"https://pic1.zhimg.com/x_hd.jpg": rel}
    result = ObsidianExporter(cfg, assets_root=str(assets_root)).export(item)
    # Assets nest inside the batch subdir (<folder>/<batch>/assets), so each
    # batch is self-contained and the note links to "assets/..." relatively.
    subdir = "收藏夹 好文精选"
    assets = tmp_path / "v" / "Zhihu" / subdir / "assets"
    assert list(assets.glob("*")), "expected image under <batch>/assets"
    text = result.path.read_text(encoding="utf-8")
    assert "(assets/" in text and ".jpg" in text
    # The old assets/<batch> layout must not be created.
    assert not (tmp_path / "v" / "Zhihu" / "assets" / subdir).exists()


def test_html_batch_subdir_placement(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"), batch_subdirs=True)
    result = HtmlExporter(cfg).export(_batch_item())
    assert result.path.parent.name == "收藏夹 好文精选"
    assert result.path.parent.parent == tmp_path / "h"


# ---------------------------------------------------------------------- #
# target_path / already_exists (file-based dedup support)
# ---------------------------------------------------------------------- #
def test_obsidian_target_path_matches_export(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    exp = ObsidianExporter(cfg)
    item = _sample_item()
    target = exp.target_path(item)
    assert not exp.already_exists(item)
    result = exp.export(item)
    assert result.path == target  # predicted path equals written path
    assert exp.already_exists(item)  # now present on disk


def test_html_target_path_matches_export(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))
    exp = HtmlExporter(cfg)
    item = _sample_item()
    target = exp.target_path(item)
    assert not exp.already_exists(item)
    result = exp.export(item)
    assert result.path == target
    assert exp.already_exists(item)


# ---------------------------------------------------------------------- #
# Comments rendered into exported output
# ---------------------------------------------------------------------- #
def _commented_item() -> ArchiveItem:
    item = _sample_item()
    item.comments = [
        Comment(
            id="c1",
            content_html="<p>很有启发</p>",
            author=Author(name="读者甲"),
            like_count=8,
            children=[
                Comment(
                    id="c1r",
                    content_html="<p>同意</p>",
                    author=Author(name="读者乙"),
                    like_count=1,
                )
            ],
        )
    ]
    return item


def test_obsidian_renders_comments(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    body = (
        ObsidianExporter(cfg)
        .export(_commented_item())
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )
    assert "## 评论 (2)" in body  # root + reply
    assert "读者甲" in body and "读者乙" in body
    # Threaded as blockquotes (reply nested deeper than root).
    assert ">" in body
    # Comment section comes after the article body.
    assert body.index("第一段") < body.index("## 评论")


def test_html_renders_comments(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))
    doc = HtmlExporter(cfg).export(_commented_item()).path.read_text(encoding="utf-8")
    assert 'class="comments"' in doc
    assert "评论 (2)" in doc
    assert "读者甲" in doc and "很有启发" in doc
    assert 'class="comment-children"' in doc  # reply threaded


def test_no_comment_section_when_empty(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=False)
    body = (
        ObsidianExporter(cfg)
        .export(_sample_item())  # no comments
        .path.read_text(encoding="utf-8")
    )
    assert "## 评论" not in body


# ---------------------------------------------------------------------- #
# Video rendering
# ---------------------------------------------------------------------- #
def _video_item() -> ArchiveItem:
    item = _sample_item()
    item.content_html = (
        '<p>看视频</p>'
        '<video src="https://v/clip.mp4" poster="https://x/cover.jpg" '
        'controls preload="metadata"></video>'
    )
    return item


def test_html_renders_local_video(tmp_path):
    assets_root = tmp_path / "assets"
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))
    item = _video_item()
    mp4 = _seed_asset(assets_root, item.key, "https://v/clip.mp4", "clip.mp4")
    cov = _seed_asset(assets_root, item.key, "https://x/cover.jpg", "cover.jpg")
    item.asset_map = {
        "https://v/clip.mp4": mp4,
        "https://x/cover.jpg": cov,
    }
    doc = (
        HtmlExporter(cfg, assets_root=str(assets_root))
        .export(item)
        .path.read_text(encoding="utf-8")
    )
    assert "<video" in doc
    assert 'src="assets/clip.mp4"' in doc
    assert 'poster="assets/cover.jpg"' in doc
    # The mp4 + poster were copied into the output assets dir.
    assert (tmp_path / "h" / "assets" / "clip.mp4").is_file()
    assert (tmp_path / "h" / "assets" / "cover.jpg").is_file()


def test_html_video_keeps_remote_when_unmapped(tmp_path):
    cfg = HtmlConfig(output_path=str(tmp_path / "h"))
    item = _video_item()  # empty asset_map
    doc = (
        HtmlExporter(cfg, assets_root=str(tmp_path / "assets"))
        .export(item)
        .path.read_text(encoding="utf-8")
    )
    assert "https://v/clip.mp4" in doc  # offline degrade, no fetch


def test_obsidian_renders_video_embed(tmp_path):
    assets_root = tmp_path / "assets"
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=True)
    item = _video_item()
    mp4 = _seed_asset(assets_root, item.key, "https://v/clip.mp4", "clip.mp4")
    item.asset_map = {"https://v/clip.mp4": mp4}
    body = (
        ObsidianExporter(cfg, assets_root=str(assets_root))
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )
    # Obsidian embed syntax for the locally-stored mp4.
    assert "![[" in body and "clip.mp4]]" in body
    assert (tmp_path / "v" / "Zhihu" / "assets" / "clip.mp4").is_file()


def test_obsidian_video_link_when_remote(tmp_path):
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=True)
    item = _video_item()  # empty asset_map → remote
    body = (
        ObsidianExporter(cfg, assets_root=str(tmp_path / "assets"))
        .export(item)
        .path.read_text(encoding="utf-8")
    )
    # Remote video survives as a link rather than being dropped by markdownify.
    assert "https://v/clip.mp4" in body
    assert "🎬" in body

