"""The wall-clock budgets of the sourcers' downloads (engine/render/asset_sourcer.py).

* Per hop: httpx's own timeout is clamped to the time left in the download's budget (plus a small
  grace so the watchdog, which is exact, wins once the connection exists). That bounds the TCP
  connect and the TLS handshake -- the phase before the watchdog can arm -- and a single read that
  would otherwise outlive the budget by up to its full read timeout. DNS resolution
  (`getaddrinfo`) takes no timeout and is bounded only by the OS resolver.
* Per search: a Pexels search tries up to 15 hits and a Wikipedia search 2 candidates, each with its
  own download budget; one budget per search stops those adding up.
"""
import socket
from unittest.mock import patch

import httpx
import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import PexelsVideoSource, WikipediaImageSource
from tests.test_sourcer_download_guards import (
    PEXELS_OK, WIKI_OK, WIKI_THUMB, _Resp, _Stream, _fake_clock, _pexels, _wiki,
)
from tests.test_sourcer_selection import _json_resp, _summary, _video, _vf


# ── per-hop timeout clamp ─────────────────────────────────────────────────────────────────────

def test_the_grace_is_one_second():
    assert AS._HOP_TIMEOUT_GRACE_S == 1.0


def _open(stream, *, timeout, deadline_at):
    with patch.object(AS, "_http_stream", stream):
        with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=timeout, deadline_at=deadline_at):
            pass


def test_the_hop_timeout_is_clamped_to_the_time_left_plus_the_grace(monkeypatch):
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(4.0))          # the one per-hop reading
    stream = _Stream({PEXELS_OK: _Resp()})
    _open(stream, timeout=120.0, deadline_at=10.0)
    assert stream.requests[0][1]["timeout"] == pytest.approx(7.0)    # 10 - 4 + 1


def test_a_shorter_configured_timeout_is_kept(monkeypatch):
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(0.0))
    stream = _Stream({PEXELS_OK: _Resp()})
    _open(stream, timeout=30.0, deadline_at=600.0)
    assert stream.requests[0][1]["timeout"] == 30.0


def test_no_budget_leaves_the_timeout_alone():
    stream = _Stream({PEXELS_OK: _Resp()})
    _open(stream, timeout=5.0, deadline_at=None)
    assert stream.requests[0][1]["timeout"] == 5.0


def test_each_hop_is_clamped_to_what_is_left_at_that_hop(monkeypatch):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    from tests.test_sourcer_download_guards import _redirect
    monkeypatch.setattr(AS, "_monotonic", _fake_clock(1.0, 6.0))     # hop 1 at t=1, hop 2 at t=6
    stream = _Stream({PEXELS_OK: _redirect(other), other: _Resp()})
    _open(stream, timeout=120.0, deadline_at=10.0)
    assert [kw["timeout"] for _, kw in stream.requests] == [pytest.approx(10.0), pytest.approx(5.0)]


def test_the_clamp_costs_no_extra_clock_reading(monkeypatch):
    """The pre-hop deadline check and the clamp share one `_monotonic()` call per hop."""
    calls = []
    monkeypatch.setattr(AS, "_monotonic", lambda: calls.append(1) or 0.0)
    _open(_Stream({PEXELS_OK: _Resp()}), timeout=120.0, deadline_at=10.0)
    assert len(calls) == 1


def test_the_real_connect_timeout_is_the_clamped_one(monkeypatch):
    """Through the real httpx/httpcore stack: the timeout reaching socket.create_connection."""
    seen = {}

    def spy(address, timeout=None, source_address=None, **kw):
        seen["timeout"] = timeout
        raise OSError("blocked by the test")

    monkeypatch.setattr(socket, "create_connection", spy)
    deadline_at = AS._monotonic() + 2.0
    with pytest.raises(httpx.ConnectError):
        with AS._open_download("https://videos.pexels.com/x.mp4", AS._pexels_url_ok,
                               timeout=120.0, deadline_at=deadline_at):
            pass
    assert 2.0 < seen["timeout"] <= 3.0 + 1e-6                       # ~2 s left + 1 s grace, not 120


def test_the_real_connect_timeout_is_untouched_without_a_budget(monkeypatch):
    seen = {}

    def spy(address, timeout=None, source_address=None, **kw):
        seen["timeout"] = timeout
        raise OSError("blocked by the test")

    monkeypatch.setattr(socket, "create_connection", spy)
    with pytest.raises(httpx.ConnectError):
        with AS._open_download("https://videos.pexels.com/x.mp4", AS._pexels_url_ok, timeout=7.0):
            pass
    assert seen["timeout"] == 7.0


# ── one budget per search (earlier hits' time counts against later ones) ─────────────────────

class _Ticking:
    """Wraps a _Stream so every request advances a shared fake clock by `step` seconds."""

    def __init__(self, stream, clock, step):
        self.stream, self.clock, self.step = stream, clock, step

    def __call__(self, method, url, **kw):
        self.clock["t"] += self.step
        return self.stream(method, url, **kw)

    @property
    def requests(self):
        return self.stream.requests

    @property
    def urls(self):
        return self.stream.urls


@pytest.fixture
def clock(monkeypatch):
    state = {"t": 0.0}
    monkeypatch.setattr(AS, "_monotonic", lambda: state["t"])
    return state


def _failing(*urls):
    return _Stream({u: _Resp(status=500) for u in urls})


def test_the_search_budgets_are_sane_numbers():
    assert AS._VIDEO_DEADLINE_S < AS._PEXELS_SEARCH_BUDGET_S <= 3600
    assert AS._IMAGE_DEADLINE_S < AS._WIKI_SEARCH_BUDGET_S <= 3600


def _links(n):
    return [f"https://videos.pexels.com/video-files/{i}/a.mp4" for i in range(n)]


def _pexels_hits(links):
    return [_video(i + 1, [_vf(1080, 1920, link=link)]) for i, link in enumerate(links)]


def test_pexels_stops_trying_hits_once_the_search_budget_is_spent(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 25.0)
    links = _links(5)
    stream = _Ticking(_failing(*links), clock, step=10.0)
    assert _pexels(tmp_path, _pexels_hits(links), stream) is None
    assert stream.urls == links[:3]                 # requests at t=0, 10, 20; at t=30 the budget (25) is gone


def test_pexels_a_hit_that_would_start_exactly_when_the_budget_ends_is_not_started(tmp_path, clock, monkeypatch):
    """Without the explicit check the pre-hop test (`now > deadline_at`) would let it through, with a 1 s timeout."""
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 25.0)
    links = _links(5)
    stream = _Ticking(_failing(*links), clock, step=12.5)
    assert _pexels(tmp_path, _pexels_hits(links), stream) is None
    assert stream.urls == links[:2]                 # t=0 and t=12.5; the third would start at t=25 == the end


def test_pexels_without_a_tight_budget_tries_every_hit(tmp_path, clock):
    links = _links(5)
    stream = _Ticking(_failing(*links), clock, step=10.0)
    assert _pexels(tmp_path, _pexels_hits(links), stream) is None
    assert stream.urls == links


def test_pexels_the_last_hit_is_clipped_to_what_is_left_of_the_search_budget(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 25.0)
    links = _links(5)
    stream = _Ticking(_failing(*links), clock, step=10.0)
    _pexels(tmp_path, _pexels_hits(links), stream)
    # a request at now=t gets deadline_at=min(t+600, 25), hence a hop timeout of min(120, 25-t+1):
    # the search budget clips even the first hit
    timeouts = [kw["timeout"] for _, kw in stream.requests]
    assert timeouts == [pytest.approx(26.0), pytest.approx(16.0), pytest.approx(6.0)]


def test_pexels_a_cached_hit_is_still_served_after_the_budget_is_spent(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 5.0)
    (tmp_path / "pexels_2.mp4").write_bytes(b"cached")
    links = _links(2)
    stream = _Ticking(_failing(links[0]), clock, step=100.0)
    result = _pexels(tmp_path, _pexels_hits(links), stream)
    assert result.local_path.read_bytes() == b"cached" and stream.urls == [links[0]]


def test_pexels_a_cached_hit_later_in_the_list_is_served_after_the_budget_is_spent(tmp_path, clock, monkeypatch):
    """The cached hit is free; `break` on the first over-budget hit would never reach it."""
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 5.0)
    (tmp_path / "pexels_3.mp4").write_bytes(b"cached")
    links = _links(3)
    stream = _Ticking(_failing(links[0]), clock, step=100.0)
    result = _pexels(tmp_path, _pexels_hits(links), stream)
    assert result.source_ref == "3" and stream.urls == [links[0]]     # hit 2 skipped (no budget), hit 3 served


def test_pexels_each_search_gets_its_own_budget(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_PEXELS_SEARCH_BUDGET_S", 25.0)
    links = _links(3)
    first = _Ticking(_failing(*links), clock, step=10.0)
    _pexels(tmp_path, _pexels_hits(links), first)
    clock["t"] += 1000.0                            # a later search, long after the first one's budget
    second = _Ticking(_failing(*links), clock, step=10.0)
    _pexels(tmp_path, _pexels_hits(links), second)
    assert second.urls == links[:3]


def test_wikipedia_the_second_candidate_is_skipped_once_the_search_budget_is_spent(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 150.0)
    stream = _Ticking(_failing(WIKI_OK, WIKI_THUMB), clock, step=160.0)
    assert _wiki(tmp_path, _summary(), stream) is None
    assert stream.urls == [WIKI_OK]                 # t=160 after the original: 150 s budget gone


def test_wikipedia_a_candidate_that_would_start_exactly_when_the_budget_ends_is_not_started(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 150.0)
    stream = _Ticking(_failing(WIKI_OK, WIKI_THUMB), clock, step=150.0)
    assert _wiki(tmp_path, _summary(), stream) is None
    assert stream.urls == [WIKI_OK]                 # the thumbnail would start at t=150 == the end


def test_wikipedia_the_second_candidate_is_still_tried_while_there_is_budget(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 150.0)
    stream = _Ticking(_failing(WIKI_OK, WIKI_THUMB), clock, step=100.0)
    assert _wiki(tmp_path, _summary(), stream) is None
    assert stream.urls == [WIKI_OK, WIKI_THUMB]                         # t=100 < 150: the thumbnail is tried
    assert [kw["timeout"] for _, kw in stream.requests] == [pytest.approx(30.0), pytest.approx(30.0)]


def test_wikipedia_a_cached_candidate_is_served_after_the_budget_is_spent(tmp_path, clock, monkeypatch):
    """The thumbnail has its own file name (.png): cached, so free even though the budget is gone."""
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 150.0)
    thumb_png = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Messi.jpg/320px-Messi.png"
    (tmp_path / "wiki_123.png").write_bytes(b"cached")
    stream = _Ticking(_failing(WIKI_OK), clock, step=200.0)
    result = _wiki(tmp_path, _summary(thumbnail=thumb_png), stream)
    assert result.local_path.read_bytes() == b"cached" and stream.urls == [WIKI_OK]


def test_wikipedia_a_tight_remaining_budget_shrinks_the_second_download_timeout(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(AS, "_WIKI_SEARCH_BUDGET_S", 150.0)
    stream = _Ticking(_failing(WIKI_OK, WIKI_THUMB), clock, step=130.0)
    _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK, WIKI_THUMB]
    assert stream.requests[1][1]["timeout"] == pytest.approx(21.0)      # min(30, 150-130+1)


def test_wikipedia_a_fast_original_never_touches_the_budget(tmp_path, clock):
    stream = _Ticking(_Stream({WIKI_OK: _Resp(chunks=(b"orig",))}), clock, step=1.0)
    assert _wiki(tmp_path, _summary(thumbnail=None), stream).local_path.read_bytes() == b"orig"


def test_media_sniffing_is_permissive_in_unmarked_tests():
    """tests/conftest.py's autouse fixture: only tests marked real_media_sniffing get the real check."""
    assert AS._sniff_ok("image", b"placeholder") is True and AS._sniff_ok("video", b"") is True
