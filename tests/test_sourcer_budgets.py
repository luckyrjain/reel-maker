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


def _open(stream, *, timeout, deadline_at, clock=None):
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
