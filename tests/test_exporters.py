"""Exporter tests: markdown/frontmatter, HTML, asset localization (offline)."""

from pathlib import Path

import pytest
import yaml

from zarchiver.config import HtmlConfig, ObsidianConfig
from zarchiver.exporters.assets import localize_images
from zarchiver.exporters.html import HtmlExporter
from zarchiver.exporters.obsidian import ObsidianExporter, sanitize_filename
from zarchiver.models import AIResult, ArchiveItem, Author, ContentType

FIXTURES = Path(__file__).parent / "fixtures"


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


def test_obsidian_export_with_image_download(tmp_path):
    # Fake fetcher returns a 1x1 png for any url.
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    cfg = ObsidianConfig(
        vault_path=str(tmp_path / "vault"),
        folder="Zhihu",
        assets_folder="Zhihu/assets",
        download_images=True,
    )
    exp = ObsidianExporter(cfg, fetch=lambda url: png)
    item = _sample_item()
    result = exp.export(item)
    assets = tmp_path / "vault" / "Zhihu" / "assets"
    files = list(assets.glob("*"))
    assert files, "expected at least one downloaded image"
    # Note references the local asset via relative path.
    text = result.path.read_text(encoding="utf-8")
    assert "assets/" in text


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
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
    )
    cfg = ObsidianConfig(vault_path=str(tmp_path / "v"), download_images=True)
    item = _sample_item()
    item.title_image = "https://pic1.zhimg.com/title.jpg"
    body = (
        ObsidianExporter(cfg, fetch=lambda u: png)
        .export(item)
        .path.read_text(encoding="utf-8")
        .split("---\n", 2)[2]
    )
    # Title image appears as the first content image (downloaded, local path).
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

