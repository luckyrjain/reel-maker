"""The hardened fetch layer: bounded, deadline-driven access to remote assets and APIs.

`open_download` enforces https on the default port, re-checks every redirect hop (httpx is never asked to
follow redirects), applies per-hop and wall-clock deadlines (a `_Watchdog` shuts the connection down at the
deadline, a DNS gate bounds the lookup) and refuses encoded bodies; `iter_capped` caps the size by
Content-Length and by bytes read. It does NOT decide which hosts are acceptable: the host allowlist is the
caller's `url_ok`, applied to the first URL and to every redirect target. `api_get` / `api_post` are the same
machinery for small JSON calls and default to `url_ok=_any_https_host`, which means "any https host except
IP literals and localhost" and is NOT an allowlist.

The clock (`now`, which reads `_monotonic`, the one patch point) and the render-wide asset budget
(`asset_budget`, `clip_to_asset_budget`, `asset_budget_spent`) live here too, so every deadline follows the
same clock.

It knows nothing about Pexels, Wikipedia or Hugging Face, and nothing about what a downloaded file contains
(see `engine.render.media_policy`).
"""

import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
import zlib
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
from celery.exceptions import SoftTimeLimitExceeded

_log = logging.getLogger(__name__)


# ── guarded downloads ────────────────────────────────────────────────────────
# Download URLs (Pexels `link`, Wikipedia `originalimage`/`thumbnail` `source`) come straight from
# remote JSON, so a tampered or compromised upstream response could otherwise point the worker at
# any URL (blind SSRF) or hand it an unbounded body. Every download is https-only to hosts the
# caller's `url_ok` allows, re-checked on each redirect hop (httpx is never asked to follow redirects
# itself), and size-capped.
MAX_API_JSON_BYTES = 8 * 1024 * 1024   # Pexels search / Wikipedia lookups are a few KB; refuse to parse more
_MAX_REDIRECTS = 3
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# httpx timeouts are per read, so a server that trickles never trips them: each download also gets a
# wall-clock budget, shared by its redirect hops / 429 retry. It is enforced in three places: before
# every hop (`open_download`), after every network read (`iter_capped`), and by a `_Watchdog` that
# shuts the socket down when the budget runs out -- the only thing that can interrupt a read blocked
# on a server dripping response headers or a chunked-encoding size line, where httpx yields nothing for
# the other two to check. Each hop's httpx timeout is also clamped to the time left (see
# `_HOP_TIMEOUT_GRACE_S`), which bounds the TCP connect / TLS handshake before the watchdog exists; the
# adapters choose their own per-search budgets (and pass the resulting deadline in); and the host's DNS
# lookup is given at most that long (`_dns_in_time`; getaddrinfo takes no timeout). The API calls are
# streamed and capped while they are read (`_api_call`), and a render's total is bounded by
# `asset_budget()` (below). SoftTimeLimitExceeded raised in ANY network call of this fetch layer is
# re-raised (it ends the task), never swallowed as a failed download; the adapters keep their own
# `except SoftTimeLimitExceeded` clauses for the calls they make themselves.
_monotonic = time.monotonic     # indirection so tests can drive a fake clock


def now() -> float:
    return _monotonic()


# One budget for a whole render's asset sourcing. The adapters' per-search budgets can still add up across a
# render's beats unless something bounds the sum: `asset_budget()` sets a deadline that every search()
# inside it is clipped to, after which no download starts and no HuggingFace generation is requested
# (a cached file, Pexels / Wikipedia / HuggingFace alike, is still served). It gates the START of
# downloads and paid calls only: the small API calls (search / summary / license lookups, the 429 retry
# sleep) and anything already in flight are not interrupted. `render_cut` wraps each beat's asset
# resolution in it, with the time left out of `RENDER_ASSET_BUDGET_S`. Unset (the default) changes nothing.
RENDER_ASSET_BUDGET_S = 1200.0
_asset_deadline_at: ContextVar["float | None"] = ContextVar("asset_deadline_at", default=None)


@contextmanager
def asset_budget(seconds: float):
    """Bound the asset sourcing done inside this block to `seconds` of wall clock (nesting only tightens)."""
    deadline = _monotonic() + seconds
    outer = _asset_deadline_at.get()
    token = _asset_deadline_at.set(deadline if outer is None else min(deadline, outer))
    try:
        yield
    finally:
        _asset_deadline_at.reset(token)


def clip_to_asset_budget(deadline_at: float) -> float:
    """`deadline_at`, or the active asset budget's deadline if that is sooner (no clock reading either way)."""
    budget = _asset_deadline_at.get()
    return deadline_at if budget is None else min(deadline_at, budget)


def asset_budget_spent() -> bool:
    budget = _asset_deadline_at.get()
    return budget is not None and _monotonic() >= budget


# Per hop, httpx's own timeout is clamped to the budget that is left plus this grace: it bounds the TCP
# connect and TLS handshake (before the watchdog exists) and one read that would outlive the budget,
# while the watchdog -- exact, but only armed once connected -- still wins when the connection is up.
# DNS is gated separately (`_dns_in_time`), with this same hop timeout.
_HOP_TIMEOUT_GRACE_S = 1.0
_HOST_RE = re.compile(r"[a-z0-9.-]+")


def https_host(url) -> "str | None":
    """Lowercased host of an https URL on the default port with no userinfo, else None.

    Deliberately stricter than urlsplit: it silently strips tab/CR/LF and accepts hosts that httpx
    (the parser that actually connects) rejects or reads differently, so a URL with any whitespace,
    control character or backslash is refused outright and the host must be plain `[a-z0-9.-]`.
    """
    if not isinstance(url, str) or any(c.isspace() or c == "\\" or ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "https" or parts.username is not None or parts.password is not None:
        return None
    if port not in (None, 443):
        return None
    if not parts.netloc.isascii():      # hostname.lower() would map e.g. U+212A (Kelvin) to "k"
        return None
    host = (parts.hostname or "").lower()
    return host if _HOST_RE.fullmatch(host) else None


def _host_for_log(url) -> str:
    """Host of `url` for a log line: a short plain `[a-z0-9.-]` host, else "?".

    Never the path, query or userinfo (tokens), and never raw control characters or an unbounded
    string from a hostile upstream.
    """
    if not isinstance(url, str):
        return "?"
    try:
        host = urllib.parse.urlsplit(url).hostname or ""       # already lower-cased by urlsplit
    except ValueError:
        return "?"
    return host if len(host) <= 253 and _HOST_RE.fullmatch(host) else "?"


@contextmanager
def _http_stream(method, url, *, extensions=None, **kwargs):
    """`httpx.stream()` plus the `extensions` parameter it lacks (needed for the connect-time trace).

    A module-level seam so tests can substitute the transport; same call shape as `httpx.stream`.
    """
    with httpx.Client() as client:
        with client.stream(method, url, extensions=extensions, **kwargs) as response:
            yield response


class _Watchdog:
    """Shuts a download's connection down once its wall-clock budget is spent.

    httpx timeouts are per read, and the deadline checks in `open_download`/`iter_capped` only run
    when httpx hands back a response or a body chunk; a server dripping response headers, a chunked
    size line / extension, or a TLS handshake produces none of those, so nothing else can interrupt
    that blocked read. `trace` (the request's `trace` extension) arms it on httpcore's
    `connection.connect_tcp.complete` event: the raw socket exists before any response does, and
    a `dup()` of it is kept, not the socket itself -- for HTTPS (every allowlisted host) httpcore
    next wraps the socket in TLS, which *detaches* the original object (fileno -1), so shutting that
    down would silently do nothing. `shutdown()` acts on the connection, not the descriptor, so the
    dup still breaks a blocked read in any phase. `cancel()` it when the hop ends. When it fires,
    `fired` is True: the caller must treat the download as failed even if the read ended *cleanly*
    (a close-delimited body reads a shutdown as a valid EOF).
    """

    def __init__(self, deadline_at: "float | None"):
        self._deadline_at = deadline_at
        self._lock = threading.Lock()
        self._timer: "threading.Timer | None" = None
        self._sock: "socket.socket | None" = None
        self.fired = False

    def trace(self, event_name: str, info: dict) -> None:
        if self._deadline_at is None or self._timer is not None:
            return
        if event_name != "connection.connect_tcp.complete":
            return
        try:
            raw = info["return_value"].get_extra_info("socket")
            if not isinstance(raw, socket.socket):
                return
            dup = raw.dup()
        except SoftTimeLimitExceeded:
            raise                                   # a few microseconds wide, but the task is out of time
        except Exception:                                   # noqa: BLE001 - never break the request
            return
        timer = threading.Timer(max(self._deadline_at - _monotonic(), 0.0), self._fire)
        timer.daemon = True
        with self._lock:
            self._sock, self._timer = dup, timer
        timer.start()

    def _fire(self) -> None:
        with self._lock:                                    # cancel() closes the dup under this lock
            if self._sock is None:
                return
            self.fired = True                               # before the shutdown: the read thread may
            try:                                            # see the error the instant it returns
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                self.fired = False                          # nothing was closed; do not claim it was

    def cancel(self) -> None:
        with self._lock:
            timer, sock, self._sock = self._timer, self._sock, None
        if timer is not None:
            timer.cancel()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def _dns_in_time(host: str, port: int, timeout: float) -> bool:
    """False only if resolving `host` is still pending after `timeout` seconds.

    `socket.getaddrinfo` takes no timeout, so neither httpx's connect/read timeouts nor the watchdog
    (which needs a connection) bound a stalled resolver. The lookup runs on a daemon thread (a hung
    getaddrinfo cannot be cancelled, so it is simply abandoned and must not block interpreter exit)
    and is waited on for at most `timeout`. A resolution that FAILS counts as in time: reporting it is
    httpx's job. Skipped (True) for IP literals and when an environment proxy is configured, because
    the proxy then resolves the name. This is a gate, not a guarantee: httpx resolves again when it
    connects (normally an OS-cache hit); a resolver that answers once and then stalls is not covered.
    """
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    env = urllib.request.getproxies_environment()
    if any(env.get(k) for k in ("all", "https", "http")):
        return True
    done = threading.Event()

    def resolve():
        try:
            socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except Exception:                                   # noqa: BLE001 - httpx reports a failed lookup
            pass
        finally:
            done.set()

    threading.Thread(target=resolve, daemon=True, name="sourcer-dns").start()
    return done.wait(timeout)


@contextmanager
def open_download(url: str, url_ok, *, timeout: float, headers: dict | None = None,
                   deadline_at: "float | None" = None, method: str = "GET", params=None, json=None,
                   follow_redirects: bool = True):
    """Stream a request to `url` (GET by default), following redirects by hand so every hop is checked by `url_ok`.

    `method`/`params`/`json` are for the API calls (`_api_call`); `follow_redirects=False` yields a
    redirect response to the caller as it is (the API calls never follow one).

    Raises ValueError for a disallowed URL (first or redirected), a redirect with no Location, or
    more than `_MAX_REDIRECTS` hops, if the wall-clock `deadline_at` has passed before a hop, or if a
    `_Watchdog` had to close the connection at the deadline. Yields the final (non-redirect) response;
    the caller still owns `raise_for_status()` and the status handling. With a `deadline_at`, each hop's
    httpx timeout is clamped to the time left (+ `_HOP_TIMEOUT_GRACE_S`) and its host's DNS lookup is
    given at most that long (`_dns_in_time`). `Accept-Encoding: identity` is always sent
    (media is not worth compressing, and a gzip body would inflate far past its Content-Length
    before the size cap could see it); `iter_capped` rejects a response that is encoded anyway --
    except for `_api_call`, which reads gzip / deflate RAW and inflates it under its own cap.
    """
    kwargs = {
        "follow_redirects": False,
        "timeout": timeout,
        "headers": {**(headers or {}), "Accept-Encoding": "identity"},
    }
    if params is not None:
        kwargs["params"] = params
    if json is not None:
        kwargs["json"] = json
    for _ in range(_MAX_REDIRECTS + 1):
        hop_kwargs = kwargs
        if deadline_at is not None:
            now = _monotonic()                              # one reading: the check and the clamp
            if now > deadline_at:
                raise ValueError("download too slow: deadline exceeded before the request")
            hop_kwargs = {**kwargs, "timeout": min(timeout, deadline_at - now + _HOP_TIMEOUT_GRACE_S)}
        if not url_ok(url):
            _log.warning("Refusing download from a disallowed URL (host %s)", _host_for_log(url))
            raise ValueError(f"download URL not allowed: {url!r}")
        if deadline_at is not None and not _dns_in_time(
                urllib.parse.urlsplit(url).hostname or "", 443, hop_kwargs["timeout"]):
            _log.warning("DNS resolution for %s did not finish in time; giving up on the download",
                         _host_for_log(url))
            raise ValueError("download too slow: DNS resolution did not finish in time")
        guard = _Watchdog(deadline_at)
        try:
            with _http_stream(method, url, extensions={"trace": guard.trace}, **hop_kwargs) as r:
                if not follow_redirects or r.status_code not in _REDIRECT_STATUSES:
                    yield r
                    if guard.fired:
                        # the body ended "cleanly" only because we shut the connection down (a
                        # close-delimited body reads that as EOF): it is truncated, never a success
                        raise ValueError("download too slow: deadline exceeded (connection closed)")
                    return
                location = r.headers.get("location")
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            if guard.fired:
                _log.warning("Download from %s exceeded its deadline; connection closed", _host_for_log(url))
                raise ValueError("download too slow: deadline exceeded (connection closed)") from exc
            raise
        finally:
            guard.cancel()
        if not location or not isinstance(location, str):
            raise ValueError(f"redirect without a Location from {url!r}")
        url = urllib.parse.urljoin(url, location)
    raise ValueError("too many redirects")


_API_MIN_DEADLINE_S = 30.0
_API_DECODABLE_ENCODINGS = ("gzip", "x-gzip", "deflate")
_API_DROPPED_HEADERS = ("content-encoding", "content-length", "transfer-encoding")


def _content_encoding(headers) -> str:
    """The lower-cased Content-Encoding header, or "" (a list like "gzip, gzip" stays as written)."""
    value = headers.get("content-encoding")
    return value.strip().lower() if isinstance(value, str) else ""


def _decode_bounded(raw: bytes, encoding: str, limit: int) -> bytes:
    """Inflate a gzip / deflate body, refusing (`TooLarge`) as soon as the OUTPUT passes `limit`.

    httpx's own decoder would inflate the whole thing before any cap could look at it (a few KB of gzip
    is gigabytes of zeros), so the body is read raw and inflated here in bounded steps. `deflate` is
    zlib-wrapped in practice but raw deflate exists in the wild, so both are tried. A corrupt or
    truncated stream raises ValueError.
    """
    if not raw:
        return b""                      # 204 / 304 / empty 200 with an encoding header: nothing to inflate
    wbits_options = (47,) if encoding in ("gzip", "x-gzip") else (zlib.MAX_WBITS, -zlib.MAX_WBITS)
    last: Exception | None = None
    for wbits in wbits_options:
        d = zlib.decompressobj(wbits)
        out: list[bytes] = []
        total = 0
        data = raw
        try:
            while data:
                chunk = d.decompress(data, 1 << 20)
                total += len(chunk)
                if total > limit:
                    raise TooLarge(f"download too large: more than {limit} bytes once decoded")
                out.append(chunk)
                data = d.unconsumed_tail
            if not d.eof:
                raise ValueError("compressed response body is truncated")
        except zlib.error as exc:
            last = exc
            continue
        return b"".join(out)
    raise ValueError(f"compressed response body is corrupt: {last}")


def _any_https_host(url) -> bool:
    """URL check for the API calls: https, default port, a plain host. The API hosts are fixed by the
    callers (they are not remote-controlled), so unlike the download hosts this is not an allowlist: any
    https host passes. An IP literal or `localhost` is refused all the same: no API host is one, and it
    keeps a future variable URL from reaching a metadata endpoint."""
    host = https_host(url)
    if host is not None:
        host = host.rstrip(".")                 # "localhost." is localhost
    if not host or host == "localhost" or host.endswith(".localhost"):
        return False
    last = host.rsplit(".", 1)[-1]
    if last.isdigit() or last.startswith("0x"):
        return False                            # 2130706433, 0x7f.1, 127.1, 017700000001: inet_aton IPv4 forms
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return True
    return False


def _api_call(method: str, url: str, limit: int, *, headers=None, timeout: float, params=None, json=None,
              deadline_s: "float | None" = None, url_ok=_any_https_host):
    """One API request, streamed through `open_download` and read at most `limit` bytes.

    `httpx.get` / `httpx.post` read the WHOLE body before returning, so a hostile or broken upstream
    could make the worker buffer gigabytes before any size check ran. Here `Accept-Encoding: identity`
    is sent; a host that ignores it and answers gzip / deflate anyway is still served -- the body is
    read raw (capped as sent) and inflated under the same cap (`_decode_bounded`) -- while any other
    encoding (br, zstd, a stack of them) is refused. No redirect is followed (same as the httpx functions), reading stops with `TooLarge` as soon as
    `limit` is crossed, and what was read is returned as a real `httpx.Response` (`.json()`,
    `.content`, `.headers`, `.raise_for_status()` work as call sites expect).

    The call also has a wall-clock budget (`deadline_s`, default `max(30 s, 2 x timeout)`), because
    httpx's timeout is per read: a host dripping headers, a chunked size line or the body would
    otherwise hold the worker for as long as it likes. It gets the same guards as a download -- the
    per-hop timeout clamp, the DNS gate and the connection `_Watchdog` -- and a spent budget raises
    `ValueError("download too slow ...")`, which every caller treats like any other failed request.
    It is NOT clipped to `asset_budget()`: the searches must still run after it is spent so that cached
    files can be found and served.
    """
    if deadline_s is None:
        deadline_s = max(_API_MIN_DEADLINE_S, 2 * timeout)
    deadline_at = _monotonic() + deadline_s
    with open_download(url, url_ok, timeout=timeout, headers=headers, deadline_at=deadline_at,
                        method=method, params=params, json=json, follow_redirects=False) as r:
        if _content_encoding(r.headers) in _API_DECODABLE_ENCODINGS:
            body = _decode_bounded(b"".join(iter_capped(r, limit, deadline_at, raw=True)),
                                   _content_encoding(r.headers), limit)
        else:
            body = b"".join(iter_capped(r, limit, deadline_at))
        headers = {k: v for k, v in r.headers.items() if k.lower() not in _API_DROPPED_HEADERS}
        return httpx.Response(r.status_code, headers=headers, content=body,
                              request=httpx.Request(method, url))


def api_get(url: str, *, limit: int | None = None, **kw):
    """`httpx.get(url, **kw)` (params/headers/timeout), streamed and capped at `limit` (default 8 MB).

    The default `url_ok` (`_any_https_host`) is not an allowlist: any https host except an IP literal or
    `localhost` passes, so the caller must pass a fixed URL, never one taken from a response.
    """
    return _api_call("GET", url, MAX_API_JSON_BYTES if limit is None else limit, **kw)


def api_post(url: str, *, limit: int | None = None, **kw):
    """`httpx.post(url, **kw)` (headers/json/timeout), streamed and capped at `limit` (default 8 MB).

    The default `url_ok` (`_any_https_host`) is not an allowlist: any https host except an IP literal or
    `localhost` passes, so the caller must pass a fixed URL, never one taken from a response.
    """
    return _api_call("POST", url, MAX_API_JSON_BYTES if limit is None else limit, **kw)


def body_too_large(resp, limit: int) -> bool:
    """True if `resp` declares (Content-Length) or actually holds (decoded) more than `limit` bytes.

    Defense in depth behind `_api_call`, which already stops reading at the limit: this keeps an
    oversized body from being handed to `.json()` (parse time/memory amplification) or written to the
    asset cache (disk) if a response reaches the caller some other way. A header that understates
    (gzip) is caught by the decoded length; a missing or unparsable header falls back to it.
    """
    try:
        declared = int(resp.headers.get("content-length"))
    except (TypeError, ValueError, AttributeError):
        declared = None
    if declared is not None and declared > limit:
        return True
    content = getattr(resp, "content", None)
    return isinstance(content, (bytes, bytearray)) and len(content) > limit


class TooLarge(ValueError):
    """A response body exceeded its size limit (declared Content-Length, or bytes actually read)."""


def iter_capped(r, limit: int, deadline_at: "float | None" = None, *, raw: bool = False):
    """Yield `r`'s body in chunks; `TooLarge` (a ValueError) once it exceeds `limit` bytes, plain ValueError past `deadline_at` or for an encoded body.

    Also refuses a response with a non-identity Content-Encoding, whose decoded size is not what
    Content-Length (or the network read) says -- unless `raw=True`, which reads the UNDECODED stream
    (`iter_raw()`) so the limit and the deadline apply to the bytes on the wire and the caller decodes
    them itself under its own cap (`_decode_bounded`). `iter_bytes()` is called without a chunk size on
    purpose: with one, httpx buffers that many bytes before yielding anything, so a slow body would
    never reach the deadline check; without it, every network read does.
    """
    encoding = r.headers.get("content-encoding")
    if not raw and isinstance(encoding, str) and encoding.strip().lower() not in ("", "identity"):
        raise ValueError(f"download is Content-Encoding {encoding!r}, expected identity")
    try:
        declared = int(r.headers.get("content-length"))
    except (TypeError, ValueError):
        declared = None
    if declared is not None and declared > limit:
        raise TooLarge(f"download too large: Content-Length {declared} > {limit}")
    total = 0
    for chunk in (r.iter_raw() if raw else r.iter_bytes()):
        total += len(chunk)
        if total > limit:
            raise TooLarge(f"download too large: more than {limit} bytes")
        if deadline_at is not None and _monotonic() > deadline_at:
            raise ValueError("download too slow: deadline exceeded")
        yield chunk
