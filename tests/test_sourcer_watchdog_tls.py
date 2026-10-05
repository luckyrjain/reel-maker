"""The download watchdog must work over TLS (every real Pexels / Wikimedia download is https).

httpcore reports the raw TCP socket at `connection.connect_tcp.complete`; `start_tls` then wraps it in
a NEW `SSLSocket` and detaches the original, so a watchdog that only remembers the TCP socket calls
`shutdown` on a dead object and the blocked read is never interrupted. These tests use real sockets on
127.0.0.1 with a throwaway self-signed certificate (trusted through `SSL_CERT_FILE`).
"""
import datetime
import ipaddress
import socket
import ssl
import threading
import time
from contextlib import contextmanager

import pytest

from engine.render import asset_sourcer as AS


def _allow_all(url):
    return True


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


def _slow_download(url, *, budget_s, read_timeout=5.0, consume=True):
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


def test_real_tls_a_fast_download_inside_the_budget_is_untouched(tmp_path, monkeypatch):
    cert_path, key_path = _self_signed_cert(tmp_path)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))
    body = b"hello" * 1000

    def ok(conn, stop):
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body) + body)

    with _raw_server(ok, tls_context=server_ctx) as port:
        with AS._open_download(f"https://127.0.0.1:{port}/x", _allow_all, timeout=5.0,
                               deadline_at=AS._monotonic() + 30) as r:
            got = b"".join(AS._iter_capped(r, 1 << 20, AS._monotonic() + 30))
    assert got == body


# ── the re-targeting itself (no network) ─────────────────────────────────────

class _RecordingSocket(socket.socket):
    def __init__(self):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self.shutdowns = []

    def shutdown(self, how):
        self.shutdowns.append(how)
        super().shutdown(how)       # an unconnected socket raises OSError, which the watchdog swallows


class _Net:
    def __init__(self, sock):
        self._sock = sock

    def get_extra_info(self, name):
        return self._sock if name == "socket" else None


def _watchdog_with_tcp_socket():
    tcp = _RecordingSocket()
    wd = AS._Watchdog(AS._monotonic() + 3600)
    wd.trace("connection.connect_tcp.complete", {"return_value": _Net(tcp)})
    return wd, tcp


def test_the_tls_socket_replaces_the_tcp_socket_as_the_shutdown_target():
    wd, tcp = _watchdog_with_tcp_socket()
    tls = _RecordingSocket()
    try:
        wd.trace("connection.start_tls.complete", {"return_value": _Net(tls)})
        wd._fire()
        assert tls.shutdowns == [socket.SHUT_RDWR] and tcp.shutdowns == []
    finally:
        wd.cancel()
        tcp.close()
        tls.close()


def test_a_tls_socket_that_appears_after_the_deadline_is_shut_down_at_once():
    wd, tcp = _watchdog_with_tcp_socket()
    tls = _RecordingSocket()
    try:
        wd._fire()
        wd.trace("connection.start_tls.complete", {"return_value": _Net(tls)})
        assert tls.shutdowns == [socket.SHUT_RDWR]
    finally:
        wd.cancel()
        tcp.close()
        tls.close()


def test_a_tls_event_without_a_usable_socket_keeps_the_tcp_target():
    wd, tcp = _watchdog_with_tcp_socket()
    try:
        wd.trace("connection.start_tls.complete", {"return_value": _Net(None)})
        wd.trace("connection.start_tls.complete", {})
        wd._fire()
        assert tcp.shutdowns == [socket.SHUT_RDWR]
    finally:
        wd.cancel()
        tcp.close()
