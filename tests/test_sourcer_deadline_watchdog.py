"""Socket watchdog for the sourcers' per-download wall-clock budget (engine/render/asset_sourcer.py).

`deadline_at` is checked in `_open_download` before each hop and in `_iter_capped` after each network
read, but httpx yields no event while a server drips response headers or a chunked-encoding size line /
extension, and a single read can sit for its whole httpx timeout. `_DeadlineWatchdog` closes that: a
`threading.Timer` shuts down the connection's socket when the budget runs out, so the blocked read
returns at once and `_open_download` reports it as the same "too slow" ValueError the checks raise.

The `_raw_server` tests use real sockets on 127.0.0.1 (plain TCP, and TLS with a throwaway
self-signed cert) -- the property is about what the OS and h11 do, which a fake cannot show.
"""
import datetime
import ipaddress
import socket
import ssl
import threading
import time
from contextlib import contextmanager
from unittest.mock import MagicMock

import httpx
import pytest

from engine.render import asset_sourcer as AS
from tests.test_sourcer_download_guards import _Resp, _Stream, PEXELS_OK


def _allow_all(url):
    return True


# ── the watchdog object ──────────────────────────────────────────────────────

def _stream_with_socket(sock):
    stream = MagicMock()
    stream.get_extra_info.side_effect = lambda name: sock if name == "socket" else None
    return stream


def _connected(wd, sock, event="connection.connect_tcp.complete"):
    wd.trace(event, {"return_value": _stream_with_socket(sock)})


@pytest.fixture
def watchdog():
    made = []

    def make(remaining_s=3600.0):
        wd = AS._DeadlineWatchdog(remaining_s)
        made.append(wd)
        return wd

    yield make
    for wd in made:
        wd.cancel()


def test_firing_shuts_down_the_registered_socket_both_ways(watchdog):
    wd, sock = watchdog(), MagicMock()
    _connected(wd, sock)
    wd._fire()
    sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)
    assert wd.fired is True


def test_nothing_is_shut_down_before_the_deadline(watchdog):
    wd, sock = watchdog(), MagicMock()
    _connected(wd, sock)
    sock.shutdown.assert_not_called()
    assert wd.fired is False


@pytest.mark.parametrize("event", ["connection.connect_tcp.complete", "connection.start_tls.complete"])
def test_both_the_tcp_and_the_tls_stream_register_their_socket(watchdog, event):
    wd, sock = watchdog(), MagicMock()
    _connected(wd, sock, event)
    wd._fire()
    sock.shutdown.assert_called_once()


def test_the_tls_socket_replaces_the_tcp_one(watchdog):
    """start_tls wraps the TCP socket in a new object and detaches the old: only the new one counts."""
    wd, tcp, tls = watchdog(), MagicMock(), MagicMock()
    _connected(wd, tcp, "connection.connect_tcp.complete")
    _connected(wd, tls, "connection.start_tls.complete")
    wd._fire()
    tls.shutdown.assert_called_once()
    tcp.shutdown.assert_not_called()


@pytest.mark.parametrize("event", [
    "connection.connect_tcp.started", "connection.start_tls.started",
    "http11.send_request_headers.complete", "connection.close.complete",
])
def test_other_trace_events_are_ignored(watchdog, event):
    wd, sock = watchdog(), MagicMock()
    wd.trace(event, {"return_value": _stream_with_socket(sock)})
    wd._fire()
    sock.shutdown.assert_not_called()


def test_a_socket_registered_after_the_deadline_is_shut_down_at_once(watchdog):
    wd, sock = watchdog(), MagicMock()
    wd._fire()
    _connected(wd, sock)
    sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)


def test_a_disarmed_socket_is_left_alone(watchdog):
    """Between hops and on the way out of the response, so a late timer cannot hit a recycled fd."""
    wd, sock = watchdog(), MagicMock()
    _connected(wd, sock)
    wd.disarm()
    wd._fire()
    sock.shutdown.assert_not_called()
    assert wd.fired is True


def test_cancel_stops_the_timer_and_disarms(watchdog):
    wd, sock = watchdog(0.05), MagicMock()
    _connected(wd, sock)
    wd.cancel()
    time.sleep(0.3)
    sock.shutdown.assert_not_called()
    assert wd.fired is False
    wd._fire()                                  # even a firing that still got through finds nothing armed
    sock.shutdown.assert_not_called()


def test_the_timer_fires_by_itself_after_the_remaining_time(watchdog):
    wd, sock = watchdog(0.05), MagicMock()
    _connected(wd, sock)
    deadline = time.monotonic() + 3
    while not wd.fired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert wd.fired is True
    sock.shutdown.assert_called_once()


def test_an_already_spent_budget_fires_immediately(watchdog):
    wd = watchdog(-5.0)
    deadline = time.monotonic() + 3
    while not wd.fired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert wd.fired is True


def test_a_socket_that_is_already_closed_does_not_raise(watchdog):
    wd, sock = watchdog(), MagicMock()
    sock.shutdown.side_effect = OSError(9, "Bad file descriptor")
    _connected(wd, sock)
    wd._fire()                                  # must not raise (it runs on the timer thread)
    assert wd.fired is True


@pytest.mark.parametrize("info", [{}, {"return_value": None}, {"return_value": object()}])
def test_a_trace_event_without_a_usable_stream_is_ignored(watchdog, info):
    wd = watchdog()
    wd.trace("connection.connect_tcp.complete", info)       # must not raise into httpcore
    wd._fire()


def test_a_stream_without_a_socket_is_ignored(watchdog):
    wd = watchdog()
    stream = MagicMock()
    stream.get_extra_info.return_value = None
    wd.trace("connection.connect_tcp.complete", {"return_value": stream})
    wd._fire()


def test_a_stream_without_a_socket_does_not_unregister_the_earlier_one(watchdog):
    wd, sock = watchdog(), MagicMock()
    _connected(wd, sock)
    bare = MagicMock()
    bare.get_extra_info.return_value = None
    wd.trace("connection.start_tls.complete", {"return_value": bare})
    wd._fire()
    sock.shutdown.assert_called_once()


def test_a_stream_whose_get_extra_info_raises_is_ignored(watchdog):
    wd = watchdog()
    stream = MagicMock()
    stream.get_extra_info.side_effect = RuntimeError("boom")
    wd.trace("connection.connect_tcp.complete", {"return_value": stream})
    wd._fire()


def test_the_timer_thread_is_a_daemon(watchdog):
    """A watchdog must never keep a worker process alive."""
    assert watchdog()._timer.daemon is True


# ── wiring into _open_download (fake httpx.stream) ───────────────────────────

def test_open_download_passes_the_trace_hook_to_httpx(monkeypatch):
    stream = _Stream({PEXELS_OK: _Resp()})
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 60):
        pass
    assert callable(stream.requests[0][1]["extensions"]["trace"])


def test_no_deadline_means_no_watchdog_and_no_trace_hook(monkeypatch):
    stream = _Stream({PEXELS_OK: _Resp()})
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    before = threading.active_count()
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0):
        assert threading.active_count() == before
    assert "extensions" not in stream.requests[0][1]


def test_the_timer_is_gone_once_the_download_is_over(monkeypatch):
    monkeypatch.setattr(AS, "_httpx_stream", _Stream({PEXELS_OK: _Resp()}))
    before = threading.active_count()
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
        pass
    time.sleep(0.05)
    assert threading.active_count() == before


def test_the_timer_is_gone_when_the_body_raises(monkeypatch):
    monkeypatch.setattr(AS, "_httpx_stream", _Stream({PEXELS_OK: _Resp()}))
    before = threading.active_count()
    with pytest.raises(RuntimeError):
        with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
            raise RuntimeError("caller failed")
    time.sleep(0.05)
    assert threading.active_count() == before


def test_the_timer_is_gone_when_a_hop_is_refused(monkeypatch):
    monkeypatch.setattr(AS, "_httpx_stream", _Stream({PEXELS_OK: _Resp(status=302, headers={"location": "https://evil.example/x"})}))
    before = threading.active_count()
    with pytest.raises(ValueError):
        with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
            pass
    time.sleep(0.05)
    assert threading.active_count() == before


def test_the_httpx_timeout_is_clamped_to_the_time_left(monkeypatch):
    stream = _Stream({PEXELS_OK: _Resp()})
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    monkeypatch.setattr(AS, "_monotonic", lambda: 95.0)
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=100.0):
        pass
    assert stream.requests[0][1]["timeout"] == 5.0


def test_the_clamped_timeout_never_drops_below_a_floor(monkeypatch):
    stream = _Stream({PEXELS_OK: _Resp()})
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    monkeypatch.setattr(AS, "_monotonic", lambda: 99.99)
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=100.0):
        pass
    assert stream.requests[0][1]["timeout"] == 1.0


def test_a_generous_budget_leaves_the_timeout_alone(monkeypatch):
    stream = _Stream({PEXELS_OK: _Resp()})
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    monkeypatch.setattr(AS, "_monotonic", lambda: 0.0)
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=600.0):
        pass
    assert stream.requests[0][1]["timeout"] == 120.0


def _hooked_stream(script, socks):
    """A fake stream that, like httpcore, reports each connection's socket through the trace hook, and
    that lets a late timer fire at the moment its response is being closed (after the caller is done)."""
    socks = iter(socks)

    class _Registers(_Stream):
        @contextmanager
        def __call__(self, method, url, **kw):
            hook = kw["extensions"]["trace"]
            self.watchdog = hook.__self__
            hook("connection.connect_tcp.complete", {"return_value": _stream_with_socket(next(socks))})
            try:
                with _Stream.__call__(self, method, url, **kw) as r:
                    yield r
            finally:
                self.watchdog._fire()           # the timer firing while the connection is being closed

    return _Registers(script)


def test_the_socket_is_disarmed_before_the_response_is_closed(monkeypatch):
    """After the with block the fd may be recycled: a late timer must find nothing to shut down."""
    sock = MagicMock()
    monkeypatch.setattr(AS, "_httpx_stream", _hooked_stream({PEXELS_OK: _Resp()}, [sock]))
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
        pass
    sock.shutdown.assert_not_called()


def test_the_socket_is_disarmed_even_when_the_body_raises(monkeypatch):
    sock = MagicMock()
    monkeypatch.setattr(AS, "_httpx_stream", _hooked_stream({PEXELS_OK: _Resp()}, [sock]))
    with pytest.raises(RuntimeError):
        with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
            raise RuntimeError("caller failed")
    sock.shutdown.assert_not_called()


def test_the_socket_of_a_redirect_hop_is_disarmed_before_it_is_closed(monkeypatch):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    first, second = MagicMock(), MagicMock()
    stream = _hooked_stream({PEXELS_OK: _Resp(status=302, headers={"location": other}), other: _Resp()}, [first, second])
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
        stream.watchdog._fire()                         # mid-body of the second hop
    assert second.shutdown.called                      # (registered after the hop-1 close firing: shut at once)
    first.shutdown.assert_not_called()                  # the redirect's connection was disarmed before it closed


def test_one_watchdog_serves_every_hop_of_a_download(monkeypatch):
    other = "https://videos.pexels.com/video-files/2/b.mp4"
    stream = _Stream({PEXELS_OK: _Resp(status=302, headers={"location": other}), other: _Resp()})
    monkeypatch.setattr(AS, "_httpx_stream", stream)
    with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
        pass
    hooks = [kw["extensions"]["trace"] for _, kw in stream.requests]
    assert len(hooks) == 2 and hooks[0] == hooks[1]


def test_a_transport_error_after_the_deadline_fired_is_reported_as_too_slow(monkeypatch):
    seen = {}

    class _Dies(_Stream):
        def __call__(self, method, url, **kw):
            seen["hook"] = kw["extensions"]["trace"]
            return super().__call__(method, url, **kw)

    monkeypatch.setattr(AS, "_httpx_stream", _Dies({PEXELS_OK: _Resp()}))
    with pytest.raises(ValueError, match="too slow|deadline"):
        with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
            # what the socket shutdown looks like from inside the body read
            gc_watchdog = seen["hook"].__self__
            gc_watchdog._fire()
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")


def test_a_transport_error_before_the_deadline_is_not_relabelled(monkeypatch):
    monkeypatch.setattr(AS, "_httpx_stream", _Stream({PEXELS_OK: _Resp()}))
    with pytest.raises(httpx.ReadError):
        with AS._open_download(PEXELS_OK, AS._pexels_url_ok, timeout=120.0, deadline_at=time.monotonic() + 600):
            raise httpx.ReadError("connection reset")


# ── real sockets ─────────────────────────────────────────────────────────────

@contextmanager
def _raw_server(handler, *, tls_context=None):
    """A thread-per-connection TCP (or TLS) server on 127.0.0.1; yields its port."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.1)
    stop = threading.Event()
    conns = []

    def serve(conn):
        try:
            if tls_context is not None:
                conn = tls_context.wrap_socket(conn, server_side=True)
            conns.append(conn)
            buf = b""
            while b"\r\n\r\n" not in buf:
                data = conn.recv(4096)
                if not data:
                    return
                buf += data
            handler(conn, stop)
        except OSError:
            pass

    def accept_loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conns.append(conn)
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    t = threading.Thread(target=accept_loop, daemon=True)
    t.start()
    try:
        yield srv.getsockname()[1]
    finally:
        stop.set()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
        srv.close()
        t.join(2)


def _drip(conn, stop, payload, *, every=0.1, for_s=8.0):
    """Send `payload` one byte per `every` seconds until `for_s` has passed (or the client is gone)."""
    end = time.monotonic() + for_s
    for byte in payload:
        if stop.is_set() or time.monotonic() > end:
            return
        conn.sendall(bytes([byte]))
        time.sleep(every)


def _forever(chars):
    while True:
        yield from chars


def _header_drip(conn, stop):
    conn.sendall(b"HTTP/1.1 200 OK\r\n")
    _drip(conn, stop, b"".join(b"X-Drip-%d: a\r\n" % i for i in range(10_000)))


def _chunk_line_drip(conn, stop):
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5;")
    _drip(conn, stop, b"e" * 10_000)                    # an endless chunk extension


def _body_stall(conn, stop):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\nx")
    stop.wait(8.0)                                      # then silence: each read waits its full timeout


def _body_trickle(conn, stop):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100000\r\n\r\n")
    _drip(conn, stop, b"a" * 100_000)


def _slow_download(url, *, budget_s, read_timeout=30.0, consume=True):
    """(elapsed seconds, exception or None) for one guarded GET against `url`."""
    started = time.monotonic()
    exc = None
    try:
        with AS._open_download(url, _allow_all, timeout=read_timeout,
                               deadline_at=AS._monotonic() + budget_s) as r:
            if consume:
                for _ in AS._iter_capped(r, 10 * 1024 * 1024, AS._monotonic() + budget_s):
                    pass
    except Exception as e:      # noqa: BLE001 -- the test inspects it
        exc = e
    return time.monotonic() - started, exc


@pytest.mark.parametrize("handler,name", [
    (_header_drip, "response headers"),
    (_chunk_line_drip, "a chunked-encoding size line / extension"),
    (_body_stall, "a stalled body (one read waits its whole timeout)"),
])
def test_real_sockets_a_dripping_server_is_cut_off_at_the_budget(handler, name):
    with _raw_server(handler) as port:
        elapsed, exc = _slow_download(f"http://127.0.0.1:{port}/x", budget_s=0.6)
    assert isinstance(exc, ValueError) and "too slow" in str(exc), f"{name}: got {exc!r}"
    assert elapsed < 3.0, f"{name}: took {elapsed:.1f}s for a 0.6 s budget"


def test_real_sockets_a_body_trickle_is_still_cut_off(tmp_path):
    with _raw_server(_body_trickle) as port:
        elapsed, exc = _slow_download(f"http://127.0.0.1:{port}/x", budget_s=0.6)
    assert isinstance(exc, ValueError) and elapsed < 3.0


def test_real_sockets_a_header_drip_in_the_redirect_target_is_cut_off_too():
    """The budget is shared across hops: the second hop gets whatever the first left."""
    def redirect_then_drip(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        _drip(conn, stop, b"".join(b"X-Drip-%d: a\r\n" % i for i in range(10_000)))

    def redirect(conn, stop):
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: %s\r\nContent-Length: 0\r\n\r\n" % redirect.target)

    with _raw_server(redirect_then_drip) as port_b, _raw_server(redirect) as port_a:
        redirect.target = f"http://127.0.0.1:{port_b}/y".encode()
        elapsed, exc = _slow_download(f"http://127.0.0.1:{port_a}/x", budget_s=0.6)
    assert isinstance(exc, ValueError) and "too slow" in str(exc)
    assert elapsed < 3.0


def test_real_sockets_a_fast_download_inside_the_budget_is_untouched():
    body = b"hello" * 1000

    def ok(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)

    with _raw_server(ok) as port:
        got = []
        with AS._open_download(f"http://127.0.0.1:{port}/x", _allow_all, timeout=5.0,
                               deadline_at=AS._monotonic() + 30) as r:
            got = b"".join(AS._iter_capped(r, 1 << 20, AS._monotonic() + 30))
    assert got == body


def test_real_sockets_the_watchdog_does_not_fire_after_a_finished_download():
    """A completed download's socket must not be shut down later by a timer that was not cancelled."""
    body = b"abc"

    def ok(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\n\r\n" + body)

    with _raw_server(ok) as port:
        with AS._open_download(f"http://127.0.0.1:{port}/x", _allow_all, timeout=5.0,
                               deadline_at=AS._monotonic() + 0.3) as r:
            assert b"".join(AS._iter_capped(r, 100, AS._monotonic() + 30)) == body
        hook_owner = threading.enumerate()
        time.sleep(0.6)                                 # past the (cancelled) deadline
    assert not [t for t in threading.enumerate() if isinstance(t, threading.Timer)], hook_owner


# ── TLS: the socket handed out by httpcore is an SSLSocket ───────────────────

def _self_signed_cert(tmp_path):
    cryptography = pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return cert_path, key_path


@pytest.mark.parametrize("handler", [_header_drip, _chunk_line_drip, _body_stall])
def test_real_tls_a_dripping_server_is_cut_off_at_the_budget(tmp_path, monkeypatch, handler):
    cert_path, key_path = _self_signed_cert(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))      # httpx trusts this cert and nothing else
    with _raw_server(handler, tls_context=server_ctx) as port:
        elapsed, exc = _slow_download(f"https://127.0.0.1:{port}/x", budget_s=0.8)
    assert isinstance(exc, ValueError) and "too slow" in str(exc), repr(exc)
    assert elapsed < 4.0


def test_httpx_stream_does_not_follow_redirects_and_passes_the_trace_hook():
    """The real `_httpx_stream` against a real socket: the 302 itself comes back, and the hook is called."""
    def redirect(conn, stop):
        conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/never\r\nContent-Length: 0\r\n\r\n")

    events = []
    with _raw_server(redirect) as port:
        with AS._httpx_stream("GET", f"http://127.0.0.1:{port}/x", timeout=5.0,
                              extensions={"trace": lambda name, info: events.append(name)}) as r:
            assert r.status_code == 302
    assert "connection.connect_tcp.complete" in events
