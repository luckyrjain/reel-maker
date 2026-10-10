"""The download watchdog and the Celery soft-limit pass-through (engine/render/asset_sourcer.py).

httpx timeouts are per read and `_iter_capped`'s deadline runs only when httpx yields a body chunk,
so a hostile host that drips response *headers*, or a chunked-encoding size line / extension, held
the (single) render worker indefinitely: no body event ever arrives for the deadline check to run on.
`_open_download` now arms a `_Watchdog` from httpcore's `connect_tcp.complete` trace event; when the
download's budget is spent it shuts the socket down, which makes the blocked read fail at once and
`_open_download` reports it as a `ValueError("download too slow ...")`.

These tests use REAL sockets (a raw-socket server on 127.0.0.1) and real httpx: the whole point is
that nothing in the fakes-only suites could notice a read that blocks. `url_ok` is passed as
`lambda u: True` for the loopback URL; the real allowlist is exercised elsewhere.
"""
import datetime
import ipaddress
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded

from engine.render import asset_sourcer as AS
from engine.render.asset_sourcer import PexelsVideoSource, WikipediaImageSource
from tests.test_sourcer_download_guards import (
    PEXELS_OK, WIKI_OK, WIKI_THUMB, _Resp, _Stream, _pexels, _wiki,
)
from tests.test_sourcer_selection import _MOD, _json_resp, _summary, _video, _vf


# ── a scripted raw-socket server ─────────────────────────────────────────────────────────────

def _drip(conn, stop, prefix: bytes, piece: bytes, every=0.05, limit=400):
    conn.sendall(prefix)
    for _ in range(limit):
        if stop.is_set():
            return
        conn.sendall(piece)
        time.sleep(every)


def _redirect_then_drip():
    """First connection: 302 to /y on the same server; later connections: dripped headers."""
    count = {"n": 0}
    lock = threading.Lock()

    def behavior(conn, stop):
        with lock:
            count["n"] += 1
            first = count["n"] == 1
        if first:
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /y.mp4\r\nContent-Length: 0\r\n\r\n")
        else:
            _drip(conn, stop, b"HTTP/1.1 200 OK\r\n", b"X-a: b\r\n")
    return behavior


BEHAVIORS = {
    # response headers that never finish
    "header_drip": lambda c, s: _drip(c, s, b"HTTP/1.1 200 OK\r\n", b"X-a: b\r\n"),
    # a chunked body whose first size line (with an extension) never finishes: no body event ever
    "chunk_line_drip": lambda c, s: _drip(
        c, s, b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;", b"a"),
    # a body that arrives one byte at a time
    "body_trickle": lambda c, s: _drip(
        c, s, b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n", b"x"),
    # accepts and says nothing
    "silent": lambda c, s: s.wait(10),
    "fast_ok": lambda c, s: c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello"),
    # no Content-Length, no chunking: the body ends when the server closes. EOF is a VALID end of body,
    # so a watchdog shutdown looks like completion to h11 -- it must still never count as a success
    "close_delimited_drip": lambda c, s: _drip(
        c, s, b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n", b"x"),
    "close_delimited_ok": lambda c, s: c.sendall(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nhello"),
    "redirect_then_drip": _redirect_then_drip(),
}


class _Server:
    def __init__(self, behavior, tls_ctx=None):
        self.behavior = behavior
        self.tls_ctx = tls_ctx
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.requests = []                              # raw request heads, in arrival order
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self):
        scheme = "https" if self.tls_ctx else "http"
        return f"{scheme}://127.0.0.1:{self.port}/x.mp4"

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.settimeout(3)
            if self.tls_ctx is not None:
                if self.behavior is HANDSHAKE_HANG:
                    self.stop.wait(10)                 # accept the TCP connection, never speak TLS
                    return
                conn = self.tls_ctx.wrap_socket(conn, server_side=True)
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                buf += chunk
            self.requests.append(buf)
            self.behavior(conn, self.stop)
        except OSError:
            pass                                   # the client closed on us: exactly what we want
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


def HANDSHAKE_HANG(conn, stop):                        # sentinel behavior: see _Server._handle
    raise AssertionError("never called")


@contextmanager
def _serve(name, tls_ctx=None):
    server = _Server(HANDSHAKE_HANG if name == "handshake_hang" else BEHAVIORS[name], tls_ctx)
    try:
        yield server
    finally:
        server.close()


def _fetch(url, deadline_s, *, timeout=30.0):
    """(result_or_exception, elapsed_s) for one guarded download of `url`."""
    started = time.monotonic()
    deadline_at = AS._monotonic() + deadline_s
    try:
        with AS._open_download(url, lambda u: True, timeout=timeout, deadline_at=deadline_at) as r:
            r.raise_for_status()
            out = b"".join(AS._iter_capped(r, 10**6, deadline_at))
    except Exception as exc:                       # noqa: BLE001 - the exception IS the result here
        return exc, time.monotonic() - started
    return out, time.monotonic() - started


# ── the watchdog cuts off what httpx's per-read timeout and the per-chunk check cannot ───────

@pytest.mark.parametrize("behavior", ["header_drip", "chunk_line_drip", "body_trickle", "silent"])
def test_a_hostile_host_is_cut_off_near_the_deadline(behavior):
    with _serve(behavior) as server:
        result, elapsed = _fetch(server.url, 0.6)
    assert isinstance(result, ValueError) and "too slow" in str(result), repr(result)
    assert 0.5 <= elapsed < 3.0, elapsed           # near the 0.6 s budget, not the 30 s read timeout


def _live_timers():
    return [t for t in threading.enumerate() if isinstance(t, threading.Timer)]


def test_a_fast_download_is_unaffected_and_leaves_no_timer_thread_behind():
    with _serve("fast_ok") as server:
        result, elapsed = _fetch(server.url, 30.0)
        assert result == b"hello" and elapsed < 2.0
        for _ in range(60):                         # cancel() wakes the Timer thread; let it exit
            if not _live_timers():
                break
            time.sleep(0.05)
    assert _live_timers() == []                     # a 30 s timer left running would show up here


def test_no_budget_means_no_watchdog():
    with patch.object(AS.threading, "Timer") as timer, _serve("fast_ok") as server:
        with AS._open_download(server.url, lambda u: True, timeout=5.0) as r:
            assert b"".join(AS._iter_capped(r, 1000)) == b"hello"
    timer.assert_not_called()


def test_the_watchdog_is_armed_with_the_remaining_budget_and_cancelled_on_exit():
    timers = []
    real = threading.Timer

    class Spy(real):
        def __init__(self, interval, *a, **kw):
            super().__init__(interval, *a, **kw)
            self.armed_for = interval
            timers.append(self)

    with patch.object(AS.threading, "Timer", Spy), _serve("fast_ok") as server:
        result, _ = _fetch(server.url, 30.0)
    assert result == b"hello" and len(timers) == 1
    assert 25.0 < timers[0].armed_for <= 30.0 and timers[0].daemon is True
    assert timers[0].finished.is_set()              # cancel() sets `finished`


def test_two_downloads_on_one_budget_arm_their_timers_for_what_remains():
    timers = []
    real = threading.Timer

    class Spy(real):
        def __init__(self, interval, *a, **kw):
            super().__init__(interval, *a, **kw)
            timers.append(interval)

    with patch.object(AS.threading, "Timer", Spy), _serve("fast_ok") as server:
        deadline_at = AS._monotonic() + 30.0
        for _ in range(2):
            time.sleep(0.05)
            with AS._open_download(server.url, lambda u: True, timeout=5.0, deadline_at=deadline_at) as r:
                list(AS._iter_capped(r, 1000, deadline_at))
    assert len(timers) == 2 and timers[1] < timers[0]       # armed for the remaining budget, not a fresh one


def test_a_fired_watchdog_is_logged_with_the_host_only(caplog):
    caplog.set_level("WARNING", logger=AS.__name__)
    with _serve("header_drip") as server:
        _fetch(server.url + "?token=secret", 0.5)
    assert "127.0.0.1" in caplog.text and "token=secret" not in caplog.text and "deadline" in caplog.text


def test_an_exception_that_is_not_the_watchdogs_passes_through_unchanged():
    with _serve("fast_ok") as server:
        with pytest.raises(KeyError):
            with AS._open_download(server.url, lambda u: True, timeout=5.0, deadline_at=AS._monotonic() + 30):
                raise KeyError("mine")


# ── the real search() paths, end to end ───────────────────────────────────────────────────────

@pytest.mark.parametrize("behavior", ["header_drip", "chunk_line_drip", "body_trickle"])
def test_pexels_search_gives_up_on_a_hostile_host_and_leaves_nothing_behind(tmp_path, monkeypatch, behavior):
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 0.6)
    monkeypatch.setattr(AS, "_pexels_url_ok", lambda u: True)
    started = time.monotonic()
    with _serve(behavior) as server, \
            patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920, link=server.url)])]})):
        result = PexelsVideoSource("k", tmp_path).search("q", 1.0)
    assert result is None and time.monotonic() - started < 4.0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("behavior", ["header_drip", "chunk_line_drip", "body_trickle"])
def test_wikipedia_search_gives_up_on_a_hostile_host_and_leaves_nothing_behind(tmp_path, monkeypatch, behavior):
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 0.6)
    monkeypatch.setattr(AS, "_wikimedia_url_ok", lambda u: True)
    started = time.monotonic()
    with _serve(behavior) as server:
        summary = _summary(original=server.url.replace(".mp4", ".jpg"), thumbnail=None)

        def fake_get(url, **kw):
            action = (kw.get("params") or {}).get("action")
            if action == "opensearch":
                return _json_resp(["Lionel Messi", ["Lionel Messi"], [], []])
            if action == "query":
                return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {}}]}}}})
            return _json_resp(summary)

        with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
            result = WikipediaImageSource(tmp_path).search("Lionel Messi")
    assert result is None and time.monotonic() - started < 4.0
    assert list(tmp_path.iterdir()) == []


# ── SoftTimeLimitExceeded must end the task, not be swallowed by the loops' broad except ──────

def test_pexels_a_soft_time_limit_mid_download_propagates_and_cleans_up(tmp_path):
    stream = _Stream({PEXELS_OK: SoftTimeLimitExceeded()})
    videos = [_video(1, [_vf(1080, 1920, link=PEXELS_OK)]), _video(2, [_vf(1080, 1920, link=PEXELS_OK)])]
    with pytest.raises(SoftTimeLimitExceeded):
        _pexels(tmp_path, videos, stream)
    assert len(stream.requests) == 1                   # the next hit was NOT started
    assert list(tmp_path.iterdir()) == []


def test_pexels_a_soft_time_limit_raised_while_writing_removes_the_partial_file(tmp_path):
    class Boom(_Resp):
        def iter_bytes(self, chunk_size=None):
            yield b"partial"
            raise SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        _pexels(tmp_path, [_video(1, [_vf(1080, 1920, link=PEXELS_OK)])], _Stream({PEXELS_OK: Boom()}))
    assert list(tmp_path.iterdir()) == []


def test_wikipedia_a_soft_time_limit_mid_download_propagates_without_trying_the_thumbnail(tmp_path):
    stream = _Stream({WIKI_OK: SoftTimeLimitExceeded(), WIKI_THUMB: _Resp(chunks=(b"t",))})
    with pytest.raises(SoftTimeLimitExceeded):
        _wiki(tmp_path, _summary(), stream)
    assert stream.urls == [WIKI_OK] and list(tmp_path.iterdir()) == []


def test_wikipedia_a_soft_time_limit_raised_while_reading_propagates(tmp_path):
    class Boom(_Resp):
        def iter_bytes(self, chunk_size=None):
            yield b"partial"
            raise SoftTimeLimitExceeded()
    with pytest.raises(SoftTimeLimitExceeded):
        _wiki(tmp_path, _summary(), _Stream({WIKI_OK: Boom(), WIKI_THUMB: _Resp(chunks=(b"t",))}))
    assert list(tmp_path.iterdir()) == []


def test_an_ordinary_error_is_still_swallowed_and_the_next_candidate_tried(tmp_path):
    stream = _Stream({WIKI_OK: RuntimeError("boom"), WIKI_THUMB: _Resp(chunks=(b"t",))})
    assert _wiki(tmp_path, _summary(), stream).local_path.read_bytes() == b"t"


def test_a_soft_time_limit_is_not_turned_into_a_deadline_error_by_a_fired_watchdog():
    class Fired(AS._Watchdog):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.fired = True
    with patch.object(AS, "_Watchdog", Fired), patch(f"{_MOD}._http_stream", _Stream({WIKI_OK: _Resp()})):
        with pytest.raises(SoftTimeLimitExceeded):
            with AS._open_download(WIKI_OK, AS._wikimedia_url_ok, timeout=5.0, deadline_at=AS._monotonic() + 30):
                raise SoftTimeLimitExceeded()


# ── the watchdog's trace hook, directly ───────────────────────────────────────────────────────

class _FakeNetworkStream:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, name):
        return self._sock if name == "socket" else None


def test_the_trace_hook_arms_one_timer_per_watchdog_even_if_it_sees_two_connects():
    a, b = socket.socketpair()
    timers = []
    real = threading.Timer

    class Spy(real):
        def __init__(self, *args, **kw):
            super().__init__(*args, **kw)
            timers.append(self)

    try:
        with patch.object(AS.threading, "Timer", Spy):
            guard = AS._Watchdog(AS._monotonic() + 30.0)
            guard.trace("connection.connect_tcp.complete", {"return_value": _FakeNetworkStream(a)})
            guard.trace("connection.connect_tcp.complete", {"return_value": _FakeNetworkStream(b)})
            guard.cancel()
        assert len(timers) == 1
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("event", ["connection.connect_tcp.started", "connection.start_tls.complete",
                                   "http11.send_request_headers.started", ""])
def test_the_trace_hook_ignores_every_other_event(event):
    a, b = socket.socketpair()
    try:
        with patch.object(AS.threading, "Timer") as timer:
            AS._Watchdog(AS._monotonic() + 30.0).trace(event, {"return_value": _FakeNetworkStream(a)})
        timer.assert_not_called()
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("info", [{}, {"return_value": None}, {"return_value": _FakeNetworkStream(None)},
                                  {"return_value": object()}])
def test_the_trace_hook_never_raises_and_arms_nothing_without_a_real_socket(info):
    with patch.object(AS.threading, "Timer") as timer:
        AS._Watchdog(AS._monotonic() + 30.0).trace("connection.connect_tcp.complete", info)
    timer.assert_not_called()


# ── round 4: HTTPS (every real host), close-delimited bodies, redirect hops, the seam itself ───

@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    """A self-signed cert for 127.0.0.1: (server SSLContext, path of the cert for SSL_CERT_FILE)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    d = tmp_path_factory.mktemp("tls")
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = d / "cert.pem", d / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx, str(cert_path)


@pytest.fixture
def trust_tls(tls, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", tls[1])        # httpx's default verify honours it
    return tls[0]


def test_tls_a_fast_download_works_through_the_watchdog(trust_tls):
    with _serve("fast_ok", trust_tls) as server:
        result, elapsed = _fetch(server.url, 30.0)
    assert result == b"hello" and elapsed < 3.0


@pytest.mark.parametrize("behavior", ["header_drip", "chunk_line_drip", "silent", "handshake_hang"])
def test_tls_a_hostile_host_is_cut_off_near_the_deadline(trust_tls, behavior):
    """Every real host (Pexels, Wikimedia) is HTTPS. The socket captured at connect time is detached
    by the TLS wrap, so a watchdog holding that object shut down a closed fd and did nothing."""
    with _serve(behavior, trust_tls) as server:
        result, elapsed = _fetch(server.url, 0.6)
    assert isinstance(result, ValueError) and "too slow" in str(result), repr(result)
    assert 0.5 <= elapsed < 3.0, elapsed


@pytest.mark.parametrize("behavior", ["close_delimited_drip"])
@pytest.mark.parametrize("tls_on", [False, True])
def test_a_close_delimited_body_cut_by_the_watchdog_is_never_a_success(trust_tls, behavior, tls_on):
    """EOF is a valid end of a close-delimited body, so h11 reports the shutdown as completion."""
    with _serve(behavior, trust_tls if tls_on else None) as server:
        result, elapsed = _fetch(server.url, 0.6)
    assert isinstance(result, ValueError) and "too slow" in str(result), repr(result)
    assert elapsed < 3.0


def test_a_close_delimited_body_that_finishes_in_time_is_a_success():
    with _serve("close_delimited_ok") as server:
        result, _ = _fetch(server.url, 30.0)
    assert result == b"hello"


def test_pexels_search_never_caches_a_truncated_close_delimited_video(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_VIDEO_DEADLINE_S", 0.6)
    monkeypatch.setattr(AS, "_pexels_url_ok", lambda u: True)
    with _serve("close_delimited_drip") as server, \
            patch(f"{_MOD}.httpx.get", return_value=_json_resp({"videos": [_video(1, [_vf(1080, 1920, link=server.url)])]})):
        result = PexelsVideoSource("k", tmp_path).search("q", 1.0)
    assert result is None and list(tmp_path.iterdir()) == []


def test_wikipedia_search_never_caches_a_truncated_close_delimited_image(tmp_path, monkeypatch):
    monkeypatch.setattr(AS, "_IMAGE_DEADLINE_S", 0.6)
    monkeypatch.setattr(AS, "_wikimedia_url_ok", lambda u: True)
    with _serve("close_delimited_drip") as server:
        summary = _summary(original=server.url.replace(".mp4", ".jpg"), thumbnail=None)

        def fake_get(url, **kw):
            action = (kw.get("params") or {}).get("action")
            if action == "opensearch":
                return _json_resp(["Lionel Messi", ["Lionel Messi"], [], []])
            if action == "query":
                return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {}}]}}}})
            return _json_resp(summary)

        with patch(f"{_MOD}.httpx.get", side_effect=fake_get):
            result = WikipediaImageSource(tmp_path).search("Lionel Messi")
    assert result is None and list(tmp_path.iterdir()) == []


def test_a_hostile_redirect_target_gets_its_own_watchdog():
    """A shared guard would stay armed on the first hop's (finished) connection and never cover this one."""
    armed = []
    real = threading.Timer

    class Spy(real):
        def __init__(self, interval, *a, **kw):
            super().__init__(interval, *a, **kw)
            armed.append(interval)

    with patch.object(AS.threading, "Timer", Spy), _serve("redirect_then_drip") as server:
        result, elapsed = _fetch(server.url, 0.8)
    assert isinstance(result, ValueError) and "too slow" in str(result), repr(result)
    assert elapsed < 3.0 and len(armed) == 2 and armed[1] < armed[0]


def test_a_redirect_over_tls_still_arms_per_hop(trust_tls):
    with _serve("redirect_then_drip", trust_tls) as server:
        result, elapsed = _fetch(server.url, 0.8)
    assert isinstance(result, ValueError) and "too slow" in str(result) and elapsed < 3.0


# ── _Watchdog internals ───────────────────────────────────────────────────────────────────────

class _FakeSock:
    def __init__(self, on_shutdown=None, boom=False):
        self.shutdowns, self.closed, self.on_shutdown, self.boom = [], False, on_shutdown, boom

    def shutdown(self, how):
        if self.on_shutdown:
            self.on_shutdown()
        if self.boom:
            raise OSError("already closed")
        self.shutdowns.append(how)

    def close(self):
        self.closed = True


def _armed(sock):
    guard = AS._Watchdog(AS._monotonic() + 30.0)
    guard._sock = sock                              # what trace() stores: a dup of the connection
    return guard


def test_fire_marks_the_watchdog_fired_before_the_shutdown_is_attempted():
    """The read thread can see the error the instant shutdown() returns; `fired` must already be set."""
    holder = {}
    sock = _FakeSock(on_shutdown=lambda: holder.__setitem__("fired_inside", holder["guard"].fired))
    holder["guard"] = guard = _armed(sock)
    guard._fire()
    assert holder["fired_inside"] is True and guard.fired is True and sock.shutdowns == [socket.SHUT_RDWR]


def test_fire_does_not_claim_a_shutdown_that_failed():
    guard = _armed(_FakeSock(boom=True))
    guard._fire()                                   # must not raise out of the Timer thread either
    assert guard.fired is False


def test_fire_after_cancel_touches_nothing_and_cancel_closes_the_dup():
    sock = _FakeSock()
    guard = _armed(sock)
    guard.cancel()
    guard._fire()
    assert sock.shutdowns == [] and sock.closed is True and guard.fired is False


def test_cancel_is_idempotent_and_safe_before_anything_was_armed():
    guard = AS._Watchdog(AS._monotonic() + 30.0)
    guard.cancel()
    guard.cancel()


def test_the_watchdog_shuts_down_a_dup_so_the_tls_wrap_cannot_orphan_it():
    a, b = socket.socketpair()
    try:
        guard = AS._Watchdog(AS._monotonic() + 30.0)
        guard.trace("connection.connect_tcp.complete", {"return_value": _FakeNetworkStream(a)})
        assert guard._sock is not a and guard._sock.fileno() != a.fileno()
        a.detach()                                  # what ssl wrap_socket does to the original object
        guard._fire()                               # still shuts the connection down, through the dup
        assert guard.fired is True and b.recv(10) == b""
    finally:
        guard.cancel()
        b.close()


def test_a_non_socket_object_never_arms_a_timer():
    with patch.object(AS.threading, "Timer") as timer:
        AS._Watchdog(AS._monotonic() + 30.0).trace(
            "connection.connect_tcp.complete", {"return_value": _FakeNetworkStream("not-a-socket")})
    timer.assert_not_called()


# ── the real _http_stream seam ────────────────────────────────────────────────────────────────

def test_http_stream_forwards_its_arguments_and_closes_the_client(monkeypatch):
    seen = {}
    real_client = httpx.Client

    class SpyClient(real_client):
        def stream(self, method, url, **kw):
            seen.update(method=method, url=url, **kw)
            seen["client"] = self
            return super().stream(method, url, **kw)

    monkeypatch.setattr(AS.httpx, "Client", SpyClient)
    hook = lambda *a: None                          # noqa: E731
    with _serve("fast_ok") as server:
        with AS._http_stream("GET", server.url, extensions={"trace": hook}, timeout=7.0,
                             headers={"X-t": "1"}, follow_redirects=False) as r:
            assert r.status_code == 200 and not seen["client"].is_closed
    assert seen["client"].is_closed
    assert seen["method"] == "GET" and seen["timeout"] == 7.0 and seen["follow_redirects"] is False
    assert seen["headers"] == {"X-t": "1"} and seen["extensions"] == {"trace": hook}


def test_http_stream_closes_the_client_when_the_body_raises(monkeypatch):
    seen = {}
    real_client = httpx.Client

    class SpyClient(real_client):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["client"] = self

    monkeypatch.setattr(AS.httpx, "Client", SpyClient)
    with _serve("fast_ok") as server:
        with pytest.raises(KeyError):
            with AS._http_stream("GET", server.url, timeout=5.0, follow_redirects=False):
                raise KeyError("mine")
    assert seen["client"].is_closed


def test_http_stream_sends_the_request_headers_and_does_not_follow_a_redirect():
    def redirect(conn, stop):
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: /elsewhere\r\nContent-Length: 0\r\n\r\n")

    server = _Server(redirect)
    try:
        with AS._http_stream("GET", server.url, timeout=5.0, headers={"X-t": "1"},
                             follow_redirects=False) as r:
            assert r.status_code == 302 and r.headers["location"] == "/elsewhere"
        assert b"x-t: 1" in server.requests[0].lower()
        assert len(server.requests) == 1                # the 302 was not followed
    finally:
        server.close()
