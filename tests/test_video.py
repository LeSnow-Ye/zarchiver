"""Video resolution tests (offline, canned getter)."""

from zarchiver.sources.zhihu.video import resolve_video


def _payload(qualities=("FHD", "HD", "SD", "LD")):
    playlist = {}
    for q in qualities:
        playlist[q] = {
            "format": "mp4",
            "play_url": f"https://vdn.vzuu.com/{q}/abc.mp4?pkey=xyz",
            "size": 1000,
        }
    return {
        "playlist": playlist,
        "cover_url": "https://pic.zhimg.com/cover.jpg",
        "title": "示例视频",
    }


def make_getter(payload):
    calls = []

    def get(url):
        calls.append(url)
        return payload

    get.calls = calls
    return get


def test_resolve_picks_requested_quality():
    got = resolve_video(make_getter(_payload()), "123", quality="FHD")
    assert got is not None
    assert got["quality"] == "FHD"
    assert "FHD/abc.mp4" in got["url"]
    assert got["cover"] == "https://pic.zhimg.com/cover.jpg"
    assert got["title"] == "示例视频"


def test_resolve_calls_lens_api():
    g = make_getter(_payload())
    resolve_video(g, "999")
    assert g.calls == ["https://lens.zhihu.com/api/v4/videos/999"]


def test_resolve_falls_back_when_target_missing():
    # Only HD/SD present; asking for FHD falls back to HD (best available).
    got = resolve_video(make_getter(_payload(("HD", "SD"))), "123", quality="FHD")
    assert got["quality"] == "HD"


def test_resolve_ld_prefers_smallest():
    got = resolve_video(make_getter(_payload()), "123", quality="LD")
    assert got["quality"] == "LD"


def test_resolve_ld_fallback_smallest_available():
    got = resolve_video(make_getter(_payload(("FHD", "HD"))), "123", quality="LD")
    # LD/SD absent → worst-first ladder picks HD over FHD.
    assert got["quality"] == "HD"


def test_resolve_none_on_empty_lens_id():
    assert resolve_video(make_getter(_payload()), "") is None


def test_resolve_none_on_failed_request():
    assert resolve_video(lambda url: None, "123") is None


def test_resolve_none_on_no_playlist():
    assert resolve_video(lambda url: {"title": "x"}, "123") is None


def test_resolve_none_when_no_play_url():
    payload = {"playlist": {"FHD": {"format": "mp4"}}}  # no play_url
    assert resolve_video(make_getter(payload), "123") is None
