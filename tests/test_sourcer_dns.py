"""DNS resolution time of the sourcers' downloads (engine/render/asset_sourcer.py).

`socket.getaddrinfo` takes no timeout, so the per-hop httpx timeout (which bounds the TCP connect and
every read) does not bound the lookup, and the watchdog only exists once there is a connection. A
resolver that stalls held the render worker for as long as the OS resolver cared to (tens of seconds
per nameserver, retried). Before each hop `_dns_in_time` resolves the host on a daemon thread and
waits at most the hop's timeout; if the lookup is still pending the download fails like any other
"too slow" one. A resolution that FAILS is httpx's to report, so it passes. It is skipped for IP
literals and when an environment proxy does the resolving.

These tests carry the `real_dns` marker: every other test gets a no-op (tests/conftest.py), so no
test ever does a real lookup.
"""
import socket
import threading
import time
from unittest.mock import patch

import pytest

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import PexelsVideoSource, WikipediaImageSource
from tests.test_sourcer_download_guards import PEXELS_OK, WIKI_OK, _Resp, _Stream, _pexels, _redirect, _wiki
from tests.test_sourcer_selection import _summary, _video, _vf

pytestmark = pytest.mark.real_dns


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(k, raising=False)


class _Resolver:
    """A getaddrinfo stand-in that can stall until released."""

    def __init__(self, *, stall=False, error=None):
        self.stall, self.error, self.calls = stall, error, []
        self.release = threading.Event()

    def __call__(self, host, port, *a, **kw):
        self.calls.append((host, port))
        if self.stall:
            self.release.wait(10)
        if self.error:
            raise self.error
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.fixture
def resolver(monkeypatch):
    def install(**kw):
        r = _Resolver(**kw)
        monkeypatch.setattr(AS.socket, "getaddrinfo", r)
        return r
    made = []

    def factory(**kw):
        r = install(**kw)
        made.append(r)
        return r
    yield factory
    for r in made:
        r.release.set()                              # let any stalled daemon thread finish


# ── the check itself ─────────────────────────────────────────────────────────────────────────

def test_a_name_that_resolves_in_time_is_ok_and_the_lookup_gets_host_and_port(resolver):
    r = resolver()
    assert AS._dns_in_time("videos.pexels.com", 443, 5.0) is True
    assert r.calls == [("videos.pexels.com", 443)]


def test_a_stalled_lookup_is_reported_after_the_timeout_not_after_the_stall(resolver):
    resolver(stall=True)
    started = time.monotonic()
    assert AS._dns_in_time("videos.pexels.com", 443, 0.3) is False
    assert 0.25 <= time.monotonic() - started < 2.0


def test_the_stalled_lookup_runs_on_a_daemon_thread(resolver):
    resolver(stall=True)
    before = {t.ident for t in threading.enumerate()}
    AS._dns_in_time("videos.pexels.com", 443, 0.1)
    new = [t for t in threading.enumerate() if t.ident not in before]
    assert new and all(t.daemon for t in new)        # a hung getaddrinfo must not block interpreter exit


@pytest.mark.parametrize("error", [socket.gaierror(-2, "Name or service not known"), OSError("odd"), TimeoutError()])
def test_a_lookup_that_fails_is_httpxs_to_report_so_it_passes(resolver, error):
    resolver(error=error)
    assert AS._dns_in_time("nonexistent.pexels.com", 443, 5.0) is True


@pytest.mark.parametrize("host", ["127.0.0.1", "203.0.113.7", "::1", "2001:db8::1"])
def test_an_ip_literal_is_never_looked_up(resolver, host):
    r = resolver(stall=True)
    assert AS._dns_in_time(host, 443, 0.1) is True and r.calls == []


@pytest.mark.parametrize("var", ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"])
def test_an_environment_proxy_does_the_resolving_so_the_gate_is_skipped(resolver, monkeypatch, var):
    r = resolver(stall=True)
    monkeypatch.setenv(var, "http://proxy.internal:3128")
    assert AS._dns_in_time("videos.pexels.com", 443, 0.1) is True and r.calls == []


def test_an_unrelated_no_proxy_variable_does_not_skip_the_gate(resolver, monkeypatch):
    r = resolver()
    monkeypatch.setenv("NO_PROXY", "localhost")
    AS._dns_in_time("videos.pexels.com", 443, 1.0)
    assert r.calls == [("videos.pexels.com", 443)]


# ── wired into _open_download ────────────────────────────────────────────────────────────────

def _open(url, **kw):
    with AS._open_download(url, AS._pexels_url_ok, **kw):
        pass


def test_a_stalled_lookup_fails_the_hop_before_any_request_is_made(resolver, caplog):
    resolver(stall=True)
    caplog.set_level("WARNING", logger=AS.__name__)
    stream = _Stream({})
    started = time.monotonic()
    with patch.object(AS, "_http_stream", stream), pytest.raises(ValueError, match="too slow.*DNS"):
        _open(PEXELS_OK + "?token=secret", timeout=0.3, deadline_at=AS._monotonic() + 30)
    assert time.monotonic() - started < 2.0 and stream.requests == []
    assert "videos.pexels.com" in caplog.text and "token=secret" not in caplog.text


def test_the_lookup_gets_the_hop_timeout_not_the_configured_one(resolver, monkeypatch):
    seen = {}
    monkeypatch.setattr(AS, "_dns_in_time", lambda host, port, timeout: seen.update(t=timeout) or True)
    monkeypatch.setattr(AS, "_monotonic", lambda: 0.0)
    with patch.object(AS, "_http_stream", _Stream({PEXELS_OK: _Resp()})):
        _open(PEXELS_OK, timeout=120.0, deadline_at=10.0)
    assert seen["t"] == pytest.approx(11.0)           # min(120, 10 - 0 + 1)


def test_without_a_budget_there_is_no_lookup(resolver):
    r = resolver(stall=True)
    with patch.object(AS, "_http_stream", _Stream({PEXELS_OK: _Resp()})):
        _open(PEXELS_OK, timeout=5.0)
    assert r.calls == []


def test_every_redirect_hop_resolves_its_own_host(resolver):
    r = resolver()
    other = "https://images.pexels.com/video-files/2/b.mp4"
    with patch.object(AS, "_http_stream", _Stream({PEXELS_OK: _redirect(other), other: _Resp()})):
        _open(PEXELS_OK, timeout=5.0, deadline_at=AS._monotonic() + 30)
    assert r.calls == [("videos.pexels.com", 443), ("images.pexels.com", 443)]


def test_a_redirect_to_a_host_whose_lookup_stalls_fails_that_hop(resolver, monkeypatch):
    answers = {"videos.pexels.com": True, "images.pexels.com": False}
    monkeypatch.setattr(AS, "_dns_in_time", lambda host, port, timeout: answers[host])
    other = "https://images.pexels.com/video-files/2/b.mp4"
    stream = _Stream({PEXELS_OK: _redirect(other), other: _Resp()})
    with patch.object(AS, "_http_stream", stream), pytest.raises(ValueError, match="DNS"):
        _open(PEXELS_OK, timeout=5.0, deadline_at=AS._monotonic() + 30)
    assert stream.urls == [PEXELS_OK]                 # the stalled hop was never requested


def test_a_disallowed_url_is_refused_before_any_lookup(resolver):
    r = resolver()
    with patch.object(AS, "_http_stream", _Stream({})), pytest.raises(ValueError, match="not allowed"):
        _open("https://evil.example/a.mp4", timeout=5.0, deadline_at=AS._monotonic() + 30)
    assert r.calls == []                              # never resolve a host we would not fetch from


# ── through the real search paths ────────────────────────────────────────────────────────────

def test_pexels_a_stalled_lookup_gives_up_on_the_hit_and_caches_nothing(resolver, tmp_path, monkeypatch):
    resolver(stall=True)
    monkeypatch.setattr(AS, "_HOP_TIMEOUT_GRACE_S", 0.2)
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 0.3)
    stream = _Stream({})
    started = time.monotonic()
    assert _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], stream) is None
    assert time.monotonic() - started < 3.0 and stream.requests == [] and list(tmp_path.iterdir()) == []


def test_wikipedia_a_stalled_lookup_gives_up_on_the_candidate(resolver, tmp_path, monkeypatch):
    resolver(stall=True)
    monkeypatch.setattr(AS, "_HOP_TIMEOUT_GRACE_S", 0.2)
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 0.3)
    stream = _Stream({})
    # the opensearch/summary calls go through the (patched) httpx.get, which does not touch DNS here
    assert _wiki(tmp_path, _summary(thumbnail=None), stream) is None
    assert stream.requests == [] and list(tmp_path.iterdir()) == []
