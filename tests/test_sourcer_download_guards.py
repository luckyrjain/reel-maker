"""Download guards of the asset sourcers (engine/render/asset_sourcer.py).

The download URLs (`chosen["link"]` for Pexels, `originalimage` / `thumbnail` `source` for
Wikipedia) come straight from remote JSON. Before these guards they were fetched with
`follow_redirects=True`, no host check and no size limit, so a tampered upstream response could
make the worker request an arbitrary URL (blind SSRF) or fill the disk. A download is now:

* https only, to an expected host (`*.pexels.com`, `upload.wikimedia.org`), no userinfo, port 443;
* redirect-checked hop by hop (never `follow_redirects=True`), at most `_MAX_REDIRECTS` hops;
* size-capped (`_MAX_VIDEO_BYTES` / `_MAX_IMAGE_BYTES`), both by `Content-Length` and by bytes read.

HTTP is faked at `engine.render.asset_sourcer.httpx`; nothing touches the network.
"""
import time
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import httpx
import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import PexelsVideoSource, WikipediaImageSource
from tests.test_sourcer_selection import _MOD, _json_resp, _summary, _video, _vf

PEXELS_OK = "https://videos.pexels.com/video-files/856973/856973-hd_1080_1920_25fps.mp4"
WIKI_OK = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Messi.jpg"
WIKI_THUMB = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Messi.jpg/320px-Messi.jpg"


# ── the URL predicates ───────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    PEXELS_OK,
    "https://images.pexels.com/photos/1/a.jpeg",
    "https://VIDEOS.Pexels.COM/video-files/1/a.mp4",
    "https://videos.pexels.com:443/video-files/1/a.mp4",
    "https://a.b.pexels.com/x",
])
def test_pexels_url_ok_accepts_https_pexels_subdomains(url):
    assert AS._pexels_url_ok(url) is True


@pytest.mark.parametrize("url", [
    "http://videos.pexels.com/video-files/1/a.mp4",             # not https
    "ftp://videos.pexels.com/a.mp4",
    "file:///etc/passwd",
    "//videos.pexels.com/a.mp4",                                # no scheme
    "videos.pexels.com/a.mp4",
    "https://pexels.com/a.mp4",                                 # apex is not "*.pexels.com"
    "https://.pexels.com/a.mp4",                                # empty label
    "https://a..pexels.com/a.mp4",
    "https://evilpexels.com/a.mp4",                             # suffix without the dot
    "https://videos.pexels.com.evil.com/a.mp4",                 # allowed host as a prefix
    "https://videos.pexels.com@evil.com/a.mp4",                 # userinfo trick: host is evil.com
    "https://user:pw@videos.pexels.com/a.mp4",                  # credentials on an allowed host
    "https://videos.pexels.com:8443/a.mp4",                     # non-default port
    "https://videos.pexels.com:80/a.mp4",
    "https://videos.pexels.com:bad/a.mp4",                      # unparsable port
    "https://videos.pexels.com./a.mp4",                         # trailing-dot host
    "https://127.0.0.1/a.mp4",
    "https://169.254.169.254/latest/meta-data/",
    "https://localhost/a.mp4",
    "https://[::1]/a.mp4",
    "https://upload.wikimedia.org/a.jpg",                       # the other source's host
    "",
    "not a url",
    "https://",
    "https:///a.mp4",
])
def test_pexels_url_ok_rejects_everything_else(url):
    assert AS._pexels_url_ok(url) is False


@pytest.mark.parametrize("bad", [None, 5, b"https://videos.pexels.com/a.mp4", ["https://videos.pexels.com/a"]])
def test_url_predicates_reject_non_strings(bad):
    assert AS._pexels_url_ok(bad) is False and AS._wikimedia_url_ok(bad) is False


def test_wikimedia_url_ok_accepts_the_real_response_shapes():
    # both shapes come from the Wikipedia REST summary: originalimage.source and thumbnail.source
    assert AS._wikimedia_url_ok(WIKI_OK) is True
    assert AS._wikimedia_url_ok(WIKI_THUMB) is True
    assert AS._wikimedia_url_ok("https://UPLOAD.wikimedia.org/x.png") is True


@pytest.mark.parametrize("url", [
    "http://upload.wikimedia.org/a.jpg",
    "https://commons.wikimedia.org/a.jpg",                      # only the image CDN host
    "https://wikimedia.org/a.jpg",
    "https://en.wikipedia.org/a.jpg",
    "https://upload.wikimedia.org.evil.com/a.jpg",
    "https://evil-upload.wikimedia.org/a.jpg",
    "https://xupload.wikimedia.org/a.jpg",
    "https://upload.wikimedia.org@evil.com/a.jpg",
    "https://user@upload.wikimedia.org/a.jpg",
    "https://upload.wikimedia.org:8080/a.jpg",
    "https://upload.wikimedia.org./a.jpg",
    "https://upload.wi\u212Aimedia.org/a.jpg",                   # Kelvin sign lower()s to ASCII "k"
    "https://videos.pexels.com/a.mp4",                          # the other source's host
    "https://127.0.0.1/a.jpg",
    "",
    "//upload.wikimedia.org/a.jpg",
])
def test_wikimedia_url_ok_rejects_everything_else(url):
    assert AS._wikimedia_url_ok(url) is False


# ── fakes ────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status=200, chunks=(b"data",), headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks
        self.chunks_read = 0

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=MagicMock(), response=self)

    def iter_bytes(self, chunk_size=None):
        for c in self._chunks:
            self.chunks_read += 1
            yield c


def _redirect(to, status=302):
    return _Resp(status=status, chunks=(), headers={"location": to})


class _Stream:
    """`_http_stream` stand-in (same call shape as httpx.stream): `script` maps URL -> a _Resp, an Exception, or a list of them."""

    def __init__(self, script):
        self.script = {u: (list(v) if isinstance(v, list) else [v]) for u, v in script.items()}
        self.requests: list[tuple[str, dict]] = []

    @property
    def urls(self):
        return [u for u, _ in self.requests]

    @contextmanager
    def __call__(self, method, url, **kw):
        assert method == "GET"
        self.requests.append((url, kw))
        outcome = self.script[url].pop(0) if len(self.script[url]) > 1 else self.script[url][0]
        if isinstance(outcome, Exception):
            raise outcome
        yield outcome


def _pexels(tmp_path, videos, stream):
    src = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": videos})), \
            patch(f"{_MOD}._http_stream", stream):
        return src.search("q", 1.0)


def _wiki(tmp_path, summary, stream, sleeps=None, title="Lionel Messi"):
    misrouted: list[str] = []
    def fake_get(url, **kw):
        action = (kw.get("params") or {}).get("action")
        if action == "opensearch":
            return _json_resp([title, [title], [], []])
        if action == "query":
            return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {}}]}}}})
        if url.startswith(WikipediaImageSource._SUMMARY):
            return _json_resp(summary)
        misrouted.append(url)       # search() swallows exceptions, so record instead of raising
        return _json_resp({})

    with patch(f"{_MOD}.httpx.get", side_effect=fake_get), patch(f"{_MOD}._http_stream", stream), \
            patch(f"{_MOD}.time.sleep") as sleep:
        result = WikipediaImageSource(tmp_path).search("Lionel Messi")
    if sleeps is not None:
        sleeps.extend(c.args[0] for c in sleep.call_args_list)
    assert misrouted == [], f"image downloads must go through _http_stream, not httpx.get: {misrouted}"
    return result


# ── Pexels: host allowlist ───────────────────────────────────────────────────

@pytest.mark.parametrize("link", [
    "http://videos.pexels.com/video-files/1/a.mp4",
    "https://evil.example/a.mp4",
    "https://169.254.169.254/latest/meta-data/",
    "https://videos.pexels.com@evil.example/a.mp4",
    "file:///etc/passwd",
])
def test_pexels_never_requests_a_disallowed_link_and_moves_on_to_the_next_hit(tmp_path, link):
    stream = _Stream({PEXELS_OK: _Resp()})
    videos = [_video(1, [_vf(1080, 1920, link=link)]), _video(2, [_vf(1080, 1920, link=PEXELS_OK)])]
    result = _pexels(tmp_path, videos, stream)
    assert result.source_ref == "2"
    assert stream.urls == [PEXELS_OK]


def test_pexels_with_only_disallowed_links_requests_nothing_and_returns_none(tmp_path):
    stream = _Stream({})
    videos = [_video(1, [_vf(1080, 1920, link="https://evil.example/a.mp4")])]
    assert _pexels(tmp_path, videos, stream) is None
    assert stream.requests == [] and list(tmp_path.iterdir()) == []


def test_pexels_a_file_already_on_disk_is_served_without_any_request(tmp_path):
    """The check guards the download only; a hit already cached locally needs no URL at all."""
    (tmp_path / "pexels_1.mp4").write_bytes(b"cached")
    stream = _Stream({})
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link="https://evil.example/a.mp4")])], stream)
    assert result.local_path.read_bytes() == b"cached" and stream.requests == []


# ── Pexels: redirects ────────────────────────────────────────────────────────

def test_pexels_never_asks_httpx_to_follow_redirects(tmp_path):
    stream = _Stream({PEXELS_OK: _Resp()})
    _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream)
    assert [kw["follow_redirects"] for _, kw in stream.requests] == [False]
    assert stream.requests[0][1]["timeout"] == 120.0
    assert stream.requests[0][1]["headers"] == {"Accept-Encoding": "identity"}


def test_pexels_follows_a_redirect_to_an_allowed_host(tmp_path):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    stream = _Stream({PEXELS_OK: _redirect(other), other: _Resp(chunks=(b"vi", b"deo"))})
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream)
    assert stream.urls == [PEXELS_OK, other]
    assert result.local_path.read_bytes() == b"video"
    assert all(kw["follow_redirects"] is False for _, kw in stream.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_pexels_follows_every_redirect_status(tmp_path, status):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    stream = _Stream({PEXELS_OK: _redirect(other, status), other: _Resp(chunks=(b"final",))})
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream)
    assert stream.urls == [PEXELS_OK, other]                      # really followed, not served as-is
    assert result.local_path.read_bytes() == b"final"


def test_pexels_resolves_a_relative_location_against_the_current_url(tmp_path):
    stream = _Stream({PEXELS_OK: _redirect("/video-files/3/c.mp4"),
                      "https://videos.pexels.com/video-files/3/c.mp4": _Resp()})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is not None
    assert stream.urls[1] == "https://videos.pexels.com/video-files/3/c.mp4"


def test_pexels_resolves_a_relative_location_against_the_host_it_came_from(tmp_path):
    start = "https://images.pexels.com/video-files/1/a.mp4"
    stream = _Stream({start: _redirect("/video-files/3/c.mp4"),
                      "https://images.pexels.com/video-files/3/c.mp4": _Resp()})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=start)])], stream) is not None
    assert stream.urls[1] == "https://images.pexels.com/video-files/3/c.mp4"


@pytest.mark.parametrize("target", [
    "https://evil.example/a.mp4",
    "http://videos.pexels.com/a.mp4",                           # https -> http downgrade
    "https://169.254.169.254/latest/meta-data/",
    "https://videos.pexels.com@evil.example/a.mp4",
    "file:///etc/passwd",
])
def test_pexels_does_not_follow_a_redirect_off_the_allowlist(tmp_path, target):
    stream = _Stream({PEXELS_OK: _redirect(target)})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert stream.urls == [PEXELS_OK]                           # the bad hop was never requested
    assert list(tmp_path.iterdir()) == []


def test_pexels_a_redirect_without_a_location_fails_the_candidate(tmp_path):
    stream = _Stream({PEXELS_OK: _Resp(status=302, chunks=())})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert len(stream.requests) == 1                            # not re-requested until the hop limit


def test_the_redirect_limit_is_three_hops():
    assert AS._MAX_REDIRECTS == 3


def test_pexels_redirect_chain_is_bounded(tmp_path):
    hops = [f"https://videos.pexels.com/h{i}.mp4" for i in range(AS._MAX_REDIRECTS + 3)]
    script = {PEXELS_OK: _redirect(hops[0])}
    script.update({h: _redirect(n) for h, n in zip(hops, hops[1:])})
    script[hops[-1]] = _Resp()
    stream = _Stream(script)
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert len(stream.requests) == 4                            # the first request + 3 hops


def test_pexels_a_chain_of_exactly_the_maximum_length_succeeds(tmp_path):
    hops = [f"https://videos.pexels.com/h{i}.mp4" for i in range(3)]
    script = {PEXELS_OK: _redirect(hops[0])}
    script.update({h: _redirect(n) for h, n in zip(hops, hops[1:])})
    script[hops[-1]] = _Resp(chunks=(b"end",))
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream(script))
    assert result.local_path.read_bytes() == b"end"


def test_pexels_a_failed_redirected_download_tries_the_next_hit_and_leaves_no_tmp(tmp_path):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    stream = _Stream({PEXELS_OK: _redirect(other), other: _Resp(status=500),
                      "https://videos.pexels.com/video-files/9.mp4": _Resp()})
    videos = [_video(1, [_vf(1080, 1920, link=PEXELS_OK)]),
              _video(2, [_vf(1080, 1920, link="https://videos.pexels.com/video-files/9.mp4")])]
    assert _pexels(tmp_path, videos, stream).source_ref == "2"
    assert [p.name for p in tmp_path.iterdir()] == ["pexels_2.mp4"]


# ── Pexels: size cap ─────────────────────────────────────────────────────────

def test_download_caps_are_sane_numbers():
    assert 50 * 1024 * 1024 <= AS._MAX_VIDEO_BYTES <= 1024 * 1024 * 1024
    assert 5 * 1024 * 1024 <= AS._MAX_IMAGE_BYTES <= 200 * 1024 * 1024


def test_pexels_a_body_over_the_cap_is_discarded_and_the_next_hit_used(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 10)
    big = "https://videos.pexels.com/video-files/big.mp4"
    stream = _Stream({big: _Resp(chunks=(b"123456", b"789012")), PEXELS_OK: _Resp(chunks=(b"ok",))})
    videos = [_video(1, [_vf(1080, 1920, link=big)]), _video(2, [_vf(1080, 1920, link=PEXELS_OK)])]
    assert _pexels(tmp_path, videos, stream).source_ref == "2"
    assert [p.name for p in tmp_path.iterdir()] == ["pexels_2.mp4"]        # no partial, no .tmp


def test_pexels_stops_reading_at_the_cap_instead_of_draining_the_body(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 10)
    resp = _Resp(chunks=(b"123456", b"789012", b"never", b"never"))
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: resp})) is None
    assert resp.chunks_read == 2


def test_pexels_a_declared_content_length_over_the_cap_is_rejected_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 10)
    resp = _Resp(chunks=(b"x",), headers={"content-length": "11"})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: resp})) is None
    assert resp.chunks_read == 0


def test_pexels_a_body_exactly_at_the_cap_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 10)
    resp = _Resp(chunks=(b"12345", b"67890"), headers={"content-length": "10"})
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: resp}))
    assert result.local_path.read_bytes() == b"1234567890"


@pytest.mark.parametrize("length", ["", "abc", "-1", None])
def test_pexels_an_unusable_content_length_falls_back_to_counting_bytes(tmp_path, monkeypatch, length):
    monkeypatch.setattr(AS, "_MAX_VIDEO_BYTES", 10)
    headers = {} if length is None else {"content-length": length}
    ok = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])],
                 _Stream({PEXELS_OK: _Resp(chunks=(b"12345",), headers=headers)}))
    assert ok is not None
    big = _pexels(tmp_path / "b", [_video(2, [_vf(1080, 1920, link=PEXELS_OK)])],
                  _Stream({PEXELS_OK: _Resp(chunks=(b"12345678901",), headers=headers)}))
    assert big is None


# ── Wikipedia: host allowlist ────────────────────────────────────────────────

def test_wikipedia_downloads_from_upload_wikimedia_via_stream_without_following_redirects(tmp_path):
    stream = _Stream({WIKI_OK: _Resp(chunks=(b"or", b"ig"))})
    result = _wiki(tmp_path, _summary(), stream)
    assert result.local_path.read_bytes() == b"orig"
    (url, kw), = stream.requests
    assert url == WIKI_OK
    assert callable(kw.pop("extensions")["trace"])      # the watchdog's connect-time hook
    assert kw == {
        "headers": {"User-Agent": "reel-maker/1.0", "Accept-Encoding": "identity"},
        "timeout": 30.0, "follow_redirects": False,
    }


@pytest.mark.parametrize("bad", [
    "http://upload.wikimedia.org/wikipedia/commons/a/ab/Messi.jpg",
    "https://evil.example/Messi.jpg",
    "https://169.254.169.254/latest/meta-data/",
    "https://upload.wikimedia.org@evil.example/Messi.jpg",
    "file:///etc/passwd",
])
def test_wikipedia_never_requests_a_disallowed_original_and_uses_the_thumbnail(tmp_path, bad):
    stream = _Stream({WIKI_THUMB: _Resp(chunks=(b"thumb",))})
    result = _wiki(tmp_path, _summary(original=bad), stream)
    assert result.local_path.read_bytes() == b"thumb"
    assert stream.urls == [WIKI_THUMB]


def test_wikipedia_never_requests_a_disallowed_thumbnail(tmp_path):
    stream = _Stream({WIKI_OK: _Resp(status=500)})
    assert _wiki(tmp_path, _summary(thumbnail="https://evil.example/t.jpg"), stream) is None
    assert stream.urls == [WIKI_OK]


def test_wikipedia_with_both_urls_disallowed_requests_no_download_and_returns_none(tmp_path):
    stream = _Stream({})
    summary = _summary(original="https://evil.example/a.jpg", thumbnail="http://upload.wikimedia.org/t.jpg")
    assert _wiki(tmp_path, summary, stream) is None
    assert stream.requests == [] and list(tmp_path.iterdir()) == []


def test_wikipedia_a_file_already_on_disk_is_served_without_any_request(tmp_path):
    (tmp_path / "wiki_123.jpg").write_bytes(b"cached")
    stream = _Stream({})
    result = _wiki(tmp_path, _summary(original="https://evil.example/a.jpg", thumbnail=None), stream)
    assert result.local_path.read_bytes() == b"cached" and stream.requests == []


# ── Wikipedia: redirects, 429, size cap ──────────────────────────────────────

def test_wikipedia_follows_a_redirect_only_within_the_allowlist(tmp_path):
    moved = "https://upload.wikimedia.org/wikipedia/commons/z/zz/Messi.jpg"
    stream = _Stream({WIKI_OK: _redirect(moved), moved: _Resp(chunks=(b"moved",))})
    result = _wiki(tmp_path, _summary(thumbnail=None), stream)
    assert result.local_path.read_bytes() == b"moved"
    assert all(kw["follow_redirects"] is False for _, kw in stream.requests)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_wikipedia_follows_every_redirect_status(tmp_path, status):
    moved = "https://upload.wikimedia.org/wikipedia/commons/z/zz/Messi.jpg"
    stream = _Stream({WIKI_OK: _redirect(moved, status), moved: _Resp(chunks=(b"moved",))})
    result = _wiki(tmp_path, _summary(thumbnail=None), stream)
    assert stream.urls == [WIKI_OK, moved] and result.local_path.read_bytes() == b"moved"


def test_wikipedia_a_redirect_off_the_allowlist_falls_back_to_the_thumbnail(tmp_path):
    stream = _Stream({WIKI_OK: _redirect("https://evil.example/Messi.jpg"),
                      WIKI_THUMB: _Resp(chunks=(b"thumb",))})
    result = _wiki(tmp_path, _summary(), stream)
    assert result.local_path.read_bytes() == b"thumb"
    assert stream.urls == [WIKI_OK, WIKI_THUMB]


def test_wikipedia_429_is_still_retried_once_after_two_seconds_through_the_stream(tmp_path):
    sleeps: list[float] = []
    stream = _Stream({WIKI_OK: [_Resp(status=429), _Resp(chunks=(b"ok",))]})
    result = _wiki(tmp_path, _summary(thumbnail=None), stream, sleeps)
    assert result.local_path.read_bytes() == b"ok" and sleeps == [2.0]
    assert stream.urls == [WIKI_OK, WIKI_OK]


def test_wikipedia_429_twice_moves_on_to_the_thumbnail(tmp_path):
    sleeps: list[float] = []
    stream = _Stream({WIKI_OK: [_Resp(status=429), _Resp(status=429)], WIKI_THUMB: _Resp(chunks=(b"t",))})
    result = _wiki(tmp_path, _summary(), stream, sleeps)
    assert result.local_path.read_bytes() == b"t"
    assert sleeps == [2.0]                    # one pause between the two tries, none after the last


def test_wikipedia_an_oversized_original_falls_back_to_the_thumbnail_and_leaves_nothing_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", 10)
    stream = _Stream({WIKI_OK: _Resp(chunks=(b"123456", b"789012")), WIKI_THUMB: _Resp(chunks=(b"thumb",))})
    result = _wiki(tmp_path, _summary(), stream)
    assert result.local_path.name == "wiki_123.jpg" and result.local_path.read_bytes() == b"thumb"
    assert [p.name for p in tmp_path.iterdir()] == ["wiki_123.jpg"]


def test_wikipedia_a_declared_content_length_over_the_cap_is_rejected_before_reading(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", 10)
    resp = _Resp(chunks=(b"x",), headers={"content-length": "999"})
    assert _wiki(tmp_path, _summary(thumbnail=None), _Stream({WIKI_OK: resp})) is None
    assert resp.chunks_read == 0 and list(tmp_path.iterdir()) == []


def test_wikipedia_a_body_exactly_at_the_cap_is_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_MAX_IMAGE_BYTES", 10)
    result = _wiki(tmp_path, _summary(thumbnail=None), _Stream({WIKI_OK: _Resp(chunks=(b"1234567890",))}))
    assert result.local_path.read_bytes() == b"1234567890"


# ── password-only userinfo, whitespace/backslash/odd-host differentials with httpx ──────────

@pytest.mark.parametrize("url", ["https://:pw@videos.pexels.com/a.mp4", "https://:pw@upload.wikimedia.org/a.jpg"])
def test_password_only_userinfo_is_rejected(url):
    assert AS._pexels_url_ok(url) is False and AS._wikimedia_url_ok(url) is False


@pytest.mark.parametrize("url", [
    # urlsplit() silently strips tab/CR/LF, so these parsed as an allowed host while httpx refuses
    # (or re-reads) them: the guard must not rely on the two parsers agreeing
    "https://up\tload.wikimedia.org/x", "https://upload.wikimedia.org/x\n", "https://upload.wikimedia.org/\rx",
    " https://upload.wikimedia.org/x", "https://upload.wikimedia.org/x ", "https://upload.wikimedia.org/a b",
    "https://upload.wikimedia.org/a\\b", "https://upload.wikimedia.org\\@evil.com/x",
    "https://upload.wikimedia.org/\x00", "https://upload.wikimedia.org/\x7f",
])
def test_wikimedia_url_ok_rejects_whitespace_control_and_backslash_anywhere(url):
    assert AS._wikimedia_url_ok(url) is False


@pytest.mark.parametrize("url", [
    "https://evil.com\tvideos.pexels.com/x", "https://evil.com\\.pexels.com/x", "https://evil.com .pexels.com/x",
    "https://v%09ideos.pexels.com/x", "https://evil.com%2f.pexels.com/x", "https://v;ideos.pexels.com/x",
    "https://a b.pexels.com/x", "https://a_b.pexels.com/x", "https://a!b.pexels.com/x",
    "https://videos.pexels.com/x\ty", " https://videos.pexels.com/x",
])
def test_pexels_url_ok_rejects_hosts_httpx_would_read_differently(url):
    assert AS._pexels_url_ok(url) is False


def test_a_percent_encoded_path_is_still_fine():
    assert AS._wikimedia_url_ok("https://upload.wikimedia.org/wikipedia/commons/a/ab/Mes%C3%A9_%28x%29.jpg") is True
    assert AS._pexels_url_ok("https://videos.pexels.com/video-files/1/a%20b.mp4?x=1&y=2") is True


@pytest.mark.parametrize("url", [PEXELS_OK, "https://images.pexels.com/photos/1/a.jpeg", "https://a-b.c2.pexels.com/x"])
def test_the_guard_and_httpx_agree_on_the_host_of_every_accepted_pexels_url(url):
    assert AS._pexels_url_ok(url) and httpx.URL(url).host.endswith(".pexels.com")


# ── Accept-Encoding: identity, Content-Encoding rejected, decoded bytes ──────────────────────

@pytest.mark.parametrize("enc", ["gzip", "br", "deflate", "GZIP", "gzip, identity", "zstd", "compress"])
def test_a_compressed_response_is_rejected(enc):
    r = _Resp(chunks=(b"x",), headers={"content-encoding": enc})
    with pytest.raises(ValueError):
        list(AS._iter_capped(r, 10))
    assert r.chunks_read == 0


@pytest.mark.parametrize("headers", [{}, {"content-encoding": ""}, {"content-encoding": "identity"},
                                     {"content-encoding": " Identity "}])
def test_an_uncompressed_response_is_read(headers):
    assert list(AS._iter_capped(_Resp(chunks=(b"abc",), headers=headers), 10)) == [b"abc"]


def test_pexels_a_gzip_response_is_discarded_and_the_next_hit_used(tmp_path):
    bad = "https://videos.pexels.com/video-files/bomb.mp4"
    stream = _Stream({bad: _Resp(chunks=(b"x",), headers={"content-encoding": "gzip"}), PEXELS_OK: _Resp()})
    videos = [_video(1, [_vf(1080, 1920, link=bad)]), _video(2, [_vf(1080, 1920, link=PEXELS_OK)])]
    assert _pexels(tmp_path, videos, stream).source_ref == "2"


def test_wikipedia_a_gzip_original_falls_back_to_the_thumbnail(tmp_path):
    stream = _Stream({WIKI_OK: _Resp(chunks=(b"x",), headers={"content-encoding": "gzip"}),
                      WIKI_THUMB: _Resp(chunks=(b"thumb",))})
    assert _wiki(tmp_path, _summary(), stream).local_path.read_bytes() == b"thumb"


# ── _iter_capped directly ─────────────────────────────────────────────────────────────────────

def test_iter_capped_never_yields_a_chunk_past_the_cap():
    got = []
    with pytest.raises(ValueError):
        for c in AS._iter_capped(_Resp(chunks=(b"123456", b"789012")), 10):
            got.append(c)
    assert got == [b"123456"]


def test_iter_capped_does_not_ask_for_a_fixed_chunk_size():
    """A fixed size makes httpx buffer 64 KB before yielding, so a slow body is never checked."""
    seen = {}
    class R(_Resp):
        def iter_bytes(self, chunk_size=None):
            seen["n"] = chunk_size
            return iter([b"a"])
    list(AS._iter_capped(R(), 10))
    assert seen["n"] is None


def test_real_httpx_the_deadline_is_checked_on_every_network_read():
    """Real httpx buffers to `chunk_size` bytes if one is given: 50 bytes would arrive as ONE chunk."""
    def body():
        for _ in range(5):
            yield b"a" * 10
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=body()))) as c:
        with c.stream("GET", "https://videos.pexels.com/x.mp4") as r:
            clock = _fake_clock(1.0, 2.0, 999.0)
            AS._monotonic, saved = clock, AS._monotonic
            try:
                got = []
                with pytest.raises(ValueError, match="too slow|deadline"):
                    for chunk in AS._iter_capped(r, 1000, deadline_at=100.0):
                        got.append(chunk)
            finally:
                AS._monotonic = saved
    assert len(got) == 2


# ── a wall-clock deadline (httpx timeouts are per read, so a trickle never trips them) ────────

def _fake_clock(*ticks, after=999.0):
    """A clock returning `ticks` in order, then `after` forever (no zero-slack StopIteration).

    The tick lists in the tests below count every `_monotonic()` call the code makes (one per
    download for `deadline_at`, one per hop, one per body read): adding a call in the module means
    re-counting them, on purpose -- that is what pins the budget to be shared, not per call.
    """
    it = iter(ticks)
    return lambda: next(it, after)


def test_a_body_that_takes_too_long_is_cut_off_even_if_each_read_is_fast(monkeypatch):
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(5.0, 50.0, 500.0))
    got = []
    with pytest.raises(ValueError, match="too slow|deadline"):
        for c in AS._iter_capped(_Resp(chunks=(b"a", b"b", b"c")), 100, deadline_at=100.0):
            got.append(c)
    assert got == [b"a", b"b"]


def test_a_body_within_the_deadline_is_read_in_full(monkeypatch):
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(1.0, 2.0, 3.0))
    assert list(AS._iter_capped(_Resp(chunks=(b"a", b"b", b"c")), 100, deadline_at=100.0)) == [b"a", b"b", b"c"]


def test_reaching_the_deadline_exactly_is_still_allowed(monkeypatch):
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(100.0, 100.5))
    got = []
    with pytest.raises(ValueError):
        for c in AS._iter_capped(_Resp(chunks=(b"a", b"b")), 100, deadline_at=100.0):
            got.append(c)
    assert got == [b"a"]                                        # `>` not `>=`: the 100.0 tick passes


def test_the_budget_uses_a_monotonic_clock():
    assert AS._monotonic is time.monotonic          # a wall clock can jump; the budget must not


def test_the_deadlines_are_sane_numbers():
    assert 60 <= AS._IMAGE_DEADLINE_S < AS._VIDEO_DEADLINE_S <= 3600


def test_pexels_uses_the_video_deadline_and_not_the_image_one(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 10.0)
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 1e9)
    # readings: search start 0; this hit's deadline_at = 0 + 10; hop check 0; chunk a 1; chunk b 999 (> 10)
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 1.0, 999.0))
    stream = _Stream({PEXELS_OK: _Resp(chunks=(b"a", b"b"))})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert list(tmp_path.iterdir()) == []


def test_pexels_a_download_inside_the_video_deadline_is_kept_even_past_the_image_one(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 1e9)
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 10.0)
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 1e9)         # the search budget must not clip it
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 1.0, 999.0))
    stream = _Stream({PEXELS_OK: _Resp(chunks=(b"a", b"b"))})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream).local_path.read_bytes() == b"ab"


def test_wikipedia_uses_the_image_deadline_and_not_the_video_one(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 10.0)
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 1e9)
    # search start 0; original: _download reading 0 (deadline_at 10), hop 0, chunk a 1, chunk b 999 -> cut off.
    # thumbnail: _download reading 0 (deadline_at 10), hop 0, chunk 1 -> kept.
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 1.0, 999.0, 0.0, 0.0, 1.0))
    stream = _Stream({WIKI_OK: _Resp(chunks=(b"a", b"b")), WIKI_THUMB: _Resp(chunks=(b"t",))})
    result = _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK, WIKI_THUMB] and result.local_path.read_bytes() == b"t"


def test_wikipedia_a_download_inside_the_image_deadline_is_kept_even_past_the_video_one(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 1e9)
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 10.0)
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 1e9)           # the search budget must not clip it
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 1.0, 999.0))
    stream = _Stream({WIKI_OK: _Resp(chunks=(b"a", b"b"))})
    assert _wiki(tmp_path, _summary(thumbnail=None), stream).local_path.read_bytes() == b"ab"


def test_the_budget_covers_the_redirect_hops_too(tmp_path, monkeypatch):
    """A chain of slow-to-answer hops cannot each get a fresh budget."""
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 10.0)
    # search start 0; deadline_at 10; first hop at 0 (ok); second hop at 999 -> refused before it is requested
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 999.0))
    stream = _Stream({PEXELS_OK: _redirect(other), other: _Resp()})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert stream.urls == [PEXELS_OK]


def test_the_budget_covers_the_429_retry_too(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 10.0)
    # search start 0; _download reading 0 (deadline_at 10); hop 1 at 0; hop 2 (after the 429) at 999
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 999.0))
    stream = _Stream({WIKI_OK: [_Resp(status=429), _Resp(chunks=(b"ok",))]})
    assert _wiki(tmp_path, _summary(thumbnail=None), stream) is None
    assert stream.urls == [WIKI_OK]                             # the retry was refused: budget spent


# ── guard rejections are logged (host only), not silent ───────────────────────────────────────

def test_a_disallowed_url_is_logged_with_its_host_but_not_the_full_url(tmp_path, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    secret_url = "https://evil.example/secret/path?token=abc"
    _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=secret_url)])], _Stream({}))
    text = caplog.text
    assert "evil.example" in text and "token=abc" not in text and "secret/path" not in text


def test_a_redirect_off_the_allowlist_is_logged_with_its_host(tmp_path, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    stream = _Stream({PEXELS_OK: _redirect("https://169.254.169.254/latest/meta-data/")})
    _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream)
    assert "169.254.169.254" in caplog.text and "meta-data" not in caplog.text


# ── real httpx objects (the fakes above use plain dicts with lower-case keys) ────────────────

def _real_stream(handler, seen=None):
    @contextmanager
    def stream(method, url, **kw):
        if seen is not None:
            seen.append((url, kw))
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with client.stream(method, url, **kw) as r:
                yield r
    return stream


def test_real_httpx_redirect_header_name_is_case_insensitive_and_relative_urls_resolve(tmp_path):
    def handler(request):
        if request.url.path == "/video-files/1/a.mp4":
            return httpx.Response(302, headers={"Location": "/video-files/2/b.mp4"})
        return httpx.Response(200, content=b"final-bytes")
    seen = []
    src = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920, link="https://videos.pexels.com/video-files/1/a.mp4")])]})), \
            patch(f"{_MOD}._http_stream", _real_stream(handler, seen)):
        result = src.search("q", 1.0)
    assert result.local_path.read_bytes() == b"final-bytes"
    assert [u for u, _ in seen] == ["https://videos.pexels.com/video-files/1/a.mp4",
                                    "https://videos.pexels.com/video-files/2/b.mp4"]


def test_real_httpx_a_gzip_response_is_rejected_and_identity_was_requested(tmp_path):
    import gzip
    requested = []
    def handler(request):
        requested.append(request.headers.get("accept-encoding"))
        return httpx.Response(200, content=gzip.compress(b"\0" * 100000), headers={"content-encoding": "gzip"})
    src = PexelsVideoSource(api_key="k", store_dir=tmp_path)
    with patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])]})), \
            patch(f"{_MOD}._http_stream", _real_stream(handler)):
        assert src.search("q", 1.0) is None
    assert requested == ["identity"] and list(tmp_path.iterdir()) == []


# ── Wikipedia: malformed summary JSON degrades instead of raising ────────────────────────────

@pytest.mark.parametrize("summary", [
    [], "x", 7, None,
    {"originalimage": None, "pageid": 1},
    {"originalimage": "str", "pageid": 1},
    {"originalimage": {"source": 5}, "pageid": 1},
    {"originalimage": {"source": None}, "pageid": 1},
    {"originalimage": {"source": ["x"]}, "thumbnail": {"source": {"a": 1}}, "pageid": 1},
    {"thumbnail": [1], "pageid": 1},
    {"thumbnail": {"source": 5}, "pageid": 1},
    {"originalimage": {"source": ""}, "thumbnail": {"source": ""}, "pageid": 1},
], ids=lambda b: repr(b)[:50])
def test_wikipedia_a_malformed_summary_is_none_not_an_exception(tmp_path, summary):
    stream = _Stream({})
    assert _wiki(tmp_path, summary, stream) is None
    assert stream.requests == []


def test_wikipedia_a_malformed_original_does_not_stop_a_valid_thumbnail(tmp_path):
    summary = {"pageid": 4, "originalimage": {"source": 5}, "thumbnail": {"source": WIKI_THUMB}}
    result = _wiki(tmp_path, summary, _Stream({WIKI_THUMB: _Resp(chunks=(b"t",))}))
    assert result.local_path.read_bytes() == b"t"


def test_userinfo_in_a_rejected_url_is_never_logged(tmp_path, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link="https://user:secret@evil.example/a.mp4")])], _Stream({}))
    assert "(host evil.example)" in caplog.text
    assert "secret" not in caplog.text and "user:" not in caplog.text and "@" not in caplog.text


@pytest.mark.parametrize("url,expected", [
    ("https://evil.example/x?token=1", "evil.example"),
    ("https://EVIL.example/x", "evil.example"),
    ("https://evil\x1bc\x07.com/x", "?"),
    ("https://a.com\x85ERROR fake/x", "?"),
    ("https://" + "a" * 300 + ".com/x", "?"),
    ("https://[::1/x", "?"),
    ("not a url", "?"),
    ("", "?"),
    (5, "?"),
    (None, "?"),
])
def test_host_for_log_returns_a_short_plain_host_or_a_placeholder(url, expected):
    assert AS._host_for_log(url) == expected


def test_a_malformed_pexels_hit_is_logged_with_its_traceback(tmp_path, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    _pexels(tmp_path, [{"duration": 10, "video_files": [_vf(1080, 1920)]}], _Stream({}))      # no id
    rec = [r for r in caplog.records if "malformed Pexels" in r.getMessage()]
    assert len(rec) == 1 and rec[0].levelname == "WARNING" and rec[0].exc_info


def test_a_literal_non_ascii_path_character_is_accepted():
    assert AS._wikimedia_url_ok("https://upload.wikimedia.org/wikipedia/commons/a/ab/\u00c9.jpg") is True


def test_a_declared_content_length_over_the_cap_is_rejected_on_its_own():
    """The body is within the cap, so only the Content-Length check can reject this one."""
    r = httpx.Response(200, content=b"x" * 10, headers={"Content-Length": "11"})
    with pytest.raises(ValueError, match="Content-Length"):
        list(AS._iter_capped(r, 10))


# ── round 3 gaps ──────────────────────────────────────────────────────────────────────────────

def test_a_hostile_host_never_reaches_the_log_raw_through_the_download_path(tmp_path, caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    link = "https://evil\x1b[31m.example/a.mp4"
    _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=link)])], _Stream({}))
    assert "\x1b" not in caplog.text and "(host ?)" in caplog.text


def test_pexels_the_body_shares_the_budget_started_before_the_connection(tmp_path, monkeypatch):
    """Past the shared budget (10 s from t=0) but inside a fresh one started when the body begins."""
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 10.0)
    # search start 0; this hit's deadline_at = 0 + 10; hop check at 0 (ok); then every body read is at 11
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, after=11.0))
    stream = _Stream({PEXELS_OK: _Resp(chunks=(b"a", b"b"))})
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None


def test_the_pre_hop_check_allows_exactly_the_deadline(tmp_path, monkeypatch):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 10.0)
    # search start 0; deadline_at 10; hop 1 at 0; hop 2 at exactly 10 (allowed); body reads at 0
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0, 0.0, 0.0, 10.0, after=0.0))
    stream = _Stream({PEXELS_OK: _redirect(other), other: _Resp(chunks=(b"ok",))})
    result = _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream)
    assert stream.urls == [PEXELS_OK, other] and result is not None


def test_host_for_log_length_bound_is_the_dns_maximum():
    ok = ".".join(["a" * 63] * 3 + ["a" * 61])           # 63+1+63+1+63+1+61 = 253
    assert len(ok) == 253 and AS._host_for_log(f"https://{ok}/x") == ok
    assert AS._host_for_log(f"https://{ok}a/x") == "?"   # 254


@pytest.mark.parametrize("bad", [b"https://x.com", bytearray(b"x"), ("https://x.com",)])
def test_host_for_log_never_raises_on_non_str_input(bad):
    assert AS._host_for_log(bad) == "?"
