"""Offline asset helper tests: collect, rewrite from map, copy (no network)."""

from pathlib import Path

from zarchiver.exporters.assets import (
    FetchResult,
    FetchStatus,
    _sniff_ext,
    collect_image_urls,
    collect_media_urls,
    copy_assets,
    download_images,
    filename_for,
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


# ---------------------------------------------------------------------- #
# Video / media support
# ---------------------------------------------------------------------- #
def test_collect_media_urls_includes_video_and_poster():
    html = (
        '<img src="https://x/a.jpg">'
        '<video src="https://v/clip.mp4" poster="https://x/cover.jpg"></video>'
    )
    urls = collect_media_urls(html)
    assert "https://x/a.jpg" in urls
    assert "https://v/clip.mp4" in urls
    assert "https://x/cover.jpg" in urls


def test_collect_media_urls_source_children():
    html = '<video><source src="https://v/clip.webm"></video>'
    assert "https://v/clip.webm" in collect_media_urls(html)


def test_collect_image_urls_ignores_video():
    # The image-only collector stays images-only.
    html = '<video src="https://v/clip.mp4"></video><img src="https://x/a.jpg">'
    assert collect_image_urls(html) == ["https://x/a.jpg"]


def test_rewrite_video_src_and_poster():
    html = '<video src="https://v/clip.mp4" poster="https://x/cover.jpg"></video>'
    amap = {
        "https://v/clip.mp4": "k/abc.mp4",
        "https://x/cover.jpg": "k/cover.jpg",
    }
    out, refs = rewrite_with_asset_map(html, amap, "assets")
    assert 'src="assets/abc.mp4"' in out
    assert 'poster="assets/cover.jpg"' in out
    assert set(refs) == {"k/abc.mp4", "k/cover.jpg"}


def test_rewrite_video_keeps_remote_when_missing():
    html = '<video src="https://v/clip.mp4"></video>'
    out, refs = rewrite_with_asset_map(html, {}, "assets")
    assert "https://v/clip.mp4" in out
    assert refs == []


def test_filename_for_keeps_mp4_ext():
    assert filename_for("https://v/clip.mp4?pkey=x").endswith(".mp4")


def test_sniff_ext_mp4():
    # 'ftyp' box at offset 4 marks an MP4.
    assert _sniff_ext(b"\x00\x00\x00\x18ftypmp42") == ".mp4"


def test_sniff_ext_webm():
    assert _sniff_ext(b"\x1aE\xdf\xa3rest") == ".webm"


def test_download_images_classifies_results(tmp_path):
    results = {
        "https://x/ok": FetchResult(FetchStatus.OK, PNG),
        "https://x/too-large": FetchResult(FetchStatus.TOO_LARGE),
        "https://x/failed": FetchResult(FetchStatus.FAILED),
    }

    def fetch(url):
        return results[url]

    outcome = download_images(
        [
            ("https://x/ok", "ok"),
            ("https://x/too-large", "too-large.jpg"),
            ("https://x/failed", "failed.jpg"),
        ],
        tmp_path,
        fetch,
    )

    assert outcome.saved == {"https://x/ok": "ok.png"}
    assert outcome.oversized == ["https://x/too-large"]
    assert outcome.failed == ["https://x/failed"]
    assert (tmp_path / "ok.png").is_file()


def test_download_images_cached_files_count_as_saved(tmp_path):
    (tmp_path / "cached.jpg").write_bytes(PNG)

    def fetch(url):
        raise AssertionError("cached asset should not be fetched")

    outcome = download_images(
        [("https://x/cached.jpg", "cached.jpg")],
        tmp_path,
        fetch,
    )

    assert outcome.saved == {"https://x/cached.jpg": "cached.jpg"}
    assert outcome.oversized == []
    assert outcome.failed == []


def test_download_images_concurrent_saves_all(tmp_path):
    pairs = [(f"https://x/img{i}", f"img{i}.png") for i in range(10)]

    def fetch(url):
        return FetchResult(FetchStatus.OK, PNG)

    outcome = download_images(pairs, tmp_path, fetch, concurrency=4)

    # All ten land on disk; saved maps every URL (order-independent).
    assert set(outcome.saved) == {u for u, _ in pairs}
    assert outcome.oversized == []
    assert outcome.failed == []
    for _, fname in pairs:
        assert (tmp_path / fname).is_file()


def test_download_images_concurrent_classifies_mixed_results(tmp_path):
    results = {
        "https://x/ok": FetchResult(FetchStatus.OK, PNG),
        "https://x/too-large": FetchResult(FetchStatus.TOO_LARGE),
        "https://x/failed": FetchResult(FetchStatus.FAILED),
    }

    def fetch(url):
        return results[url]

    outcome = download_images(
        [
            ("https://x/ok", "ok.png"),
            ("https://x/too-large", "too-large.jpg"),
            ("https://x/failed", "failed.jpg"),
        ],
        tmp_path,
        fetch,
        concurrency=3,
    )

    assert outcome.saved == {"https://x/ok": "ok.png"}
    assert set(outcome.oversized) == {"https://x/too-large"}
    assert set(outcome.failed) == {"https://x/failed"}


def test_download_images_concurrent_runs_fetches_in_parallel(tmp_path):
    # A barrier proves the fetches overlap: each of the 4 fetchers waits for all
    # 4 to arrive. With concurrency=1 this would deadlock (timeout); concurrency
    # >= 4 lets them proceed together.
    import threading

    barrier = threading.Barrier(4, timeout=5)
    pairs = [(f"https://x/p{i}", f"p{i}.png") for i in range(4)]

    def fetch(url):
        barrier.wait()  # raises BrokenBarrierError on timeout if not parallel
        return FetchResult(FetchStatus.OK, PNG)

    outcome = download_images(pairs, tmp_path, fetch, concurrency=4)
    assert set(outcome.saved) == {u for u, _ in pairs}
