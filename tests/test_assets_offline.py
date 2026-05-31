"""Offline asset helper tests: collect, rewrite from map, copy (no network)."""

from pathlib import Path

from zarchiver.exporters.assets import (
    collect_image_urls,
    copy_assets,
    inline_from_asset_map,
    rewrite_with_asset_map,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082"
)


def test_collect_image_urls_prefers_data_original():
    html = (
        '<img src="https://pic1.zhimg.com/small.jpg" '
        'data-original="https://pic1.zhimg.com/big.jpg">'
        '<img src="https://pic1.zhimg.com/two.jpg">'
    )
    urls = collect_image_urls(html)
    assert urls == [
        "https://pic1.zhimg.com/big.jpg",
        "https://pic1.zhimg.com/two.jpg",
    ]


def test_collect_dedupes():
    html = '<img src="https://x/a.jpg"><img src="https://x/a.jpg">'
    assert collect_image_urls(html) == ["https://x/a.jpg"]


def test_rewrite_with_asset_map_rewrites_known():
    html = '<img src="https://pic1.zhimg.com/a.jpg">'
    amap = {"https://pic1.zhimg.com/a.jpg": "zhihu_article_1/deadbeef.jpg"}
    out, refs = rewrite_with_asset_map(html, amap, "assets")
    assert 'src="assets/deadbeef.jpg"' in out
    assert refs == ["zhihu_article_1/deadbeef.jpg"]


def test_rewrite_keeps_remote_url_when_missing():
    html = '<img src="https://pic1.zhimg.com/missing.jpg">'
    out, refs = rewrite_with_asset_map(html, {}, "assets")
    # Degrades gracefully: remote URL preserved, nothing to copy.
    assert "https://pic1.zhimg.com/missing.jpg" in out
    assert refs == []


def test_rewrite_uses_data_original():
    html = (
        '<img src="https://x/small.jpg" data-original="https://x/big.jpg">'
    )
    amap = {"https://x/big.jpg": "k/h.jpg"}
    out, refs = rewrite_with_asset_map(html, amap, "assets")
    assert 'src="assets/h.jpg"' in out
    assert "data-original" not in out  # lazy attr stripped
    assert refs == ["k/h.jpg"]


def test_copy_assets_copies_files(tmp_path):
    root = tmp_path / "root"
    (root / "k").mkdir(parents=True)
    (root / "k" / "h.jpg").write_bytes(PNG)
    dest = tmp_path / "out" / "assets"
    n = copy_assets(["k/h.jpg"], root, dest)
    assert n == 1
    assert (dest / "h.jpg").read_bytes() == PNG


def test_copy_assets_skips_missing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    dest = tmp_path / "out"
    n = copy_assets(["k/missing.jpg"], root, dest)
    assert n == 0
    assert not dest.exists() or not list(dest.glob("*"))


def test_inline_from_asset_map(tmp_path):
    root = tmp_path / "root"
    (root / "k").mkdir(parents=True)
    (root / "k" / "h.png").write_bytes(PNG)
    html = '<img src="https://x/a.png">'
    amap = {"https://x/a.png": "k/h.png"}
    out = inline_from_asset_map(html, amap, root)
    assert "data:image/png;base64," in out


def test_inline_keeps_remote_when_missing(tmp_path):
    html = '<img src="https://x/a.png">'
    out = inline_from_asset_map(html, {}, tmp_path)
    assert "https://x/a.png" in out
