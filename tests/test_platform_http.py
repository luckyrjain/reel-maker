"""Behavior of `api/platform_http.py`: the one place platform API calls go through.

Covers rules 1-7 of the design (docs/specs/2026-11-platform-http-module-design.md, section 4.2): status
handling, `bearer=`, header and type validation, forwarding of only what was supplied, timeouts, the
secret-in-the-URL guard (with every spelling and input form), the unparseable-input paths, the
`server_supplied_url` allowlist (including the real site-9 publish), no echo of caller text, and that
`httpx` is resolved at call time.

Conventions that keep the tests honest:

* Cases are parametrized over LITERAL lists in this file, never over the imported constant, so deleting a
  secret name from the module deletes the guard, not the case (`SECRET_NAMES` below is the pin).
* Two recorders: the bind-only `BindRecorder` (never answers through a real client, so it can see every
  keyword exactly as the wrapper forwarded it, including object identity) and the Client-answering
  `Recorder` of `tests/_platform_http_helpers.py` where a real response is needed.
* Patch targets are literal (`tests/test_module_seams.py` G1 walks every test file).
"""
from __future__ import annotations

import ast
import copy
import dataclasses
import functools
import inspect
import re
import socket
import urllib.parse
from unittest.mock import MagicMock

import httpx
import pytest

from api import platform_http
from engine.publish.youtube import YouTubePublisher
from tests import _seam_ast as S
from tests._platform_http_helpers import Recorder, network_guard
from tests._platform_http_helpers import platform_network_guard  # noqa: F401  (autouse fixture)

VERBS = ["get", "post", "put"]
URL = "https://api.example/x"

# (normalised name, a natural spelling of it): the literal pin of SECRET_QUERY_NAMES, 37 names.
NATURAL = [
    ("accesstoken", "access_token"), ("refreshtoken", "refresh_token"), ("idtoken", "id_token"),
    ("clientsecret", "client_secret"), ("secret", "secret"), ("fbexchangetoken", "fb_exchange_token"),
    ("apikey", "api_key"), ("xgoogapikey", "x-goog-api-key"), ("key", "key"), ("token", "token"),
    ("code", "code"), ("password", "password"), ("auth", "auth"), ("authorization", "authorization"),
    ("bearer", "bearer"), ("signature", "signature"), ("sig", "sig"),
    ("oauthtoken", "oauth_token"), ("apitoken", "api_token"), ("authtoken", "auth_token"),
    ("sessiontoken", "session_token"), ("securitytoken", "security_token"), ("accesskey", "access_key"),
    ("secretkey", "secret_key"), ("privatekey", "private_key"), ("accesssecret", "access_secret"),
    ("jwt", "jwt"), ("passwd", "passwd"), ("pwd", "pwd"),
    ("appsecret", "app_secret"), ("apisecret", "api_secret"), ("sessionid", "session_id"),
    ("credential", "credential"), ("assertion", "assertion"), ("clientassertion", "client_assertion"),
    ("authorizationcode", "authorization_code"), ("authkey", "auth_key"),
]
SECRET_NAMES = [name for name, _ in NATURAL]


# ── helpers ────────────────────────────────────────────────────────────────────────────────────────

_REAL_SIGNATURES = {name: inspect.signature(getattr(httpx, name)) for name in VERBS}


class BindRecorder:
    """Replaces the bare httpx.get/post/put: checks each call against the real signature, records
    `(verb, args, kwargs)` untouched (so identity of every forwarded object is visible) and answers 200."""

    def __init__(self, monkeypatch, status: int = 200):
        self.calls: list[tuple] = []
        self.status = status
        for name in VERBS:
            monkeypatch.setattr(httpx, name, self._make(name))

    def _make(self, name):
        signature = _REAL_SIGNATURES[name]

        def fake(*args, **kwargs):
            signature.bind(*args, **kwargs)
            self.calls.append((name, args, kwargs))
            return httpx.Response(self.status, request=httpx.Request(name.upper(), args[0]))

        return fake

    @property
    def kwargs(self) -> dict:
        assert len(self.calls) == 1, self.calls
        return self.calls[0][2]


@pytest.fixture
def bind(monkeypatch):
    return BindRecorder(monkeypatch)


def call(verb, url=URL, **kwargs):
    return getattr(platform_http, verb)(url, **kwargs)


def reject(bind, verb, exc_type, url=URL, **kwargs):
    """Call and expect `exc_type` before any request; returns the exception."""
    with pytest.raises(exc_type) as info:
        call(verb, url, **kwargs)
    assert bind.calls == [], f"{verb} reached httpx despite the rejection"
    return info.value


def assert_secret_rejection(exc, name, context=""):
    message = str(exc)
    assert isinstance(exc, ValueError), context
    assert name in message and "bearer=" in message, f"{context}: {message}"


def layered(natural: str, passes: int) -> str:
    """`natural` percent-encoded so that exactly `passes` decode passes are needed to reveal it."""
    text = f"%{ord(natural[0]):02x}" + natural[1:]
    for _ in range(passes - 1):
        text = text.replace("%", "%25")
    return text


def camel(natural: str) -> str:
    first, *rest = re.split(r"[_-]", natural)
    return first + "".join(part.title() for part in rest)


SPELLINGS = {
    "natural": lambda n: n,
    "upper": lambda n: n.upper(),
    "swapcase": lambda n: n.swapcase(),
    "hyphen": lambda n: n.replace("_", "-"),
    "camel": camel,
    "squashed": lambda n: n.replace("_", "").replace("-", ""),
    "brackets": lambda n: n + "[]",
    "trailing space": lambda n: n + " ",
    "leading space": lambda n: " " + n,
    "plus": lambda n: n.replace("_", "+"),
    "percent first char": lambda n: f"%{ord(n[0]):02x}" + n[1:],
    "percent separators": lambda n: n.replace("_", "%5F").replace("-", "%2D"),
    "double percent": lambda n: layered(n, 2),
    "eight layers": lambda n: layered(n, 8),
}

PARAM_FORMS = {
    "dict": lambda k: {k: "v"},
    "dict key not first": lambda k: {"part": "x", k: "v"},
    "dict None value": lambda k: {k: None},
    "dict list value": lambda k: {k: ["a", "b"]},
    "list of pairs": lambda k: [(k, "v")],
    "tuple of pairs, key not first": lambda k: (("part", "x"), (k, "v")),
    "QueryParams": lambda k: httpx.QueryParams([("part", "x"), (k, "v")]),
    "str": lambda k: f"{k}=v",
    "str key not first": lambda k: f"part=x&{k}=v",
    "str bare key": lambda k: k,
    "str question mark prefix": lambda k: f"?{k}=v",
    "str semicolon": lambda k: f"part=x;{k}=v",
    "str blank value": lambda k: f"{k}=",
    "bytes": lambda k: f"{k}=v".encode(),
    "bytes key not first": lambda k: f"part=x&{k}=v".encode(),
}

URL_FORMS = {
    "key=value": lambda k: f"{URL}?{k}=v",
    "key not first": lambda k: f"{URL}?part=x&{k}=v",
    "bare key": lambda k: f"{URL}?{k}",
    "semicolon": lambda k: f"{URL}?part=x;{k}=v",
    "key first then semicolon": lambda k: f"{URL}?{k}=v;part=x",
    "blank value": lambda k: f"{URL}?{k}=",
    "before a fragment": lambda k: f"{URL}?part=x&{k}=v#frag",
}


# ── the pin of the secret names ────────────────────────────────────────────────────────────────────

def test_secret_names_literal_pin():
    assert len(NATURAL) == 37 and len(set(SECRET_NAMES)) == 37
    assert platform_http.SECRET_QUERY_NAMES == frozenset(SECRET_NAMES)
    assert isinstance(platform_http.SECRET_QUERY_NAMES, frozenset)
    for name, natural in NATURAL:
        assert platform_http._normalise(natural) == name, natural


def test_public_surface_constants():
    assert platform_http.DEFAULT_TIMEOUT_S == 30.0 and type(platform_http.DEFAULT_TIMEOUT_S) is float
    assert platform_http.UPLOAD_TIMEOUT_S == 300.0 and type(platform_http.UPLOAD_TIMEOUT_S) is float


# ── status handling (rule 4) and the response (rules 5, 6) ─────────────────────────────────────────

@pytest.fixture
def mp():
    patch = pytest.MonkeyPatch()
    yield patch
    patch.undo()


@pytest.mark.parametrize("verb", VERBS)
def test_raise_for_status_per_verb(verb, mp):
    rec = Recorder(lambda request: httpx.Response(500, content=b"not json")).install(mp)
    with pytest.raises(httpx.HTTPStatusError) as info:
        call(verb)
    assert info.value.response.status_code == 500
    assert len(rec.calls) == 1


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503, 301, 302, 307])
def test_status_variants(verb, status, mp):
    headers = {"Location": "https://other.example/"} if 300 <= status < 400 else {}
    rec = Recorder(lambda request: httpx.Response(status, headers=headers, content=b"<html>x</html>")).install(mp)
    with pytest.raises(httpx.HTTPStatusError) as info:
        call(verb)
    assert len(rec.requests) == 1, "a redirect must not be followed"
    # the text is exactly what httpx itself raises for that response (unchanged for is_transient_error,
    # job.error and the credentials router)
    probe = httpx.Response(status, headers=headers, request=httpx.Request(verb.upper(), URL))
    with pytest.raises(httpx.HTTPStatusError) as expected:
        probe.raise_for_status()
    assert str(info.value) == str(expected.value)
    assert info.value.response.status_code == status
    assert URL in str(info.value)


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("status", [200, 201, 204])
def test_success_statuses_return_the_response(verb, status, mp):
    Recorder(lambda request: httpx.Response(status, json={"ok": True})).install(mp)
    resp = call(verb)
    assert isinstance(resp, httpx.Response) and resp.status_code == status


class _StandIn:
    """A response whose raise_for_status() returns None (as older httpx did), to tell `return resp`
    from `return resp.raise_for_status()` whatever httpx version is installed."""

    def __init__(self):
        self.raised = 0

    def raise_for_status(self):
        self.raised += 1
        return None


@pytest.mark.parametrize("verb", VERBS)
def test_returns_resp_not_raise_result(verb, monkeypatch):
    stand_in = _StandIn()
    monkeypatch.setattr(httpx, verb, lambda *a, **k: stand_in)
    assert call(verb) is stand_in
    assert stand_in.raised == 1


@pytest.mark.parametrize("verb", VERBS)
def test_uses_httpx_verb_directly(verb, bind):
    call(verb)
    assert [c[0] for c in bind.calls] == [verb]       # exactly the same-named httpx function, once
    # httpx.request is blocked by the autouse network guard (an AssertionError would surface here)


@pytest.mark.parametrize("verb", VERBS)
def test_transport_errors_not_wrapped(verb, monkeypatch):
    for error in (httpx.ConnectError("SENTINEL-connect"), httpx.ReadTimeout("t"), OSError("o")):
        def boom(*args, _error=error, **kwargs):
            raise _error

        monkeypatch.setattr(httpx, verb, boom)
        with pytest.raises(type(error)) as info:
            call(verb)
        assert info.value is error


@pytest.mark.parametrize("verb", VERBS)
def test_no_import_time_capture(verb, monkeypatch):
    seen = []

    def late(*args, **kwargs):
        seen.append(args)
        return httpx.Response(200, request=httpx.Request(verb.upper(), URL))

    monkeypatch.setattr(httpx, verb, late)          # patched long after platform_http was imported
    call(verb)
    assert seen == [(URL,)]


# ── bearer (rule 2) ────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verb", VERBS)
def test_bearer_format_and_first_header(verb, bind):
    call(verb, bearer="tok-123_ABC.xyz~+/=", headers={"Content-Type": "x/y", "X-A": "1"})
    headers = bind.kwargs["headers"]
    assert list(headers.items()) == [
        ("Authorization", "Bearer tok-123_ABC.xyz~+/="), ("Content-Type", "x/y"), ("X-A", "1"),
    ]


@pytest.mark.parametrize("verb", VERBS)
def test_bearer_alone_builds_the_only_header(verb, bind):
    call(verb, bearer="tok")
    assert bind.kwargs["headers"] == {"Authorization": "Bearer tok"}


@pytest.mark.parametrize("verb", VERBS)
def test_bearer_omitted_sends_no_authorization(verb, bind):
    call(verb)
    assert "headers" not in bind.kwargs
    bind.calls.clear()
    headers = {"X-A": "1"}
    call(verb, headers=headers)
    assert bind.kwargs["headers"] is headers and "Authorization" not in bind.kwargs["headers"]


BAD_BEARERS = [
    None, "", " ", "   ", "\t", "\n", 123, 1.5, True, b"tok", ["tok"], object(),
    "a b", "tok en", "tok\n", "tok\r\n", "tok\r\nX-Evil: 1", "\ntok", "tok\x00", "tok\x7f", "tok\x0b",
    "t\u00f6k", "tok\u00a0", "tok\u2028", " tok", "tok ", "\ttok", "tok\ttok",
]


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("bad", BAD_BEARERS, ids=[repr(b)[:20] for b in BAD_BEARERS])
def test_bearer_rejected_before_any_request(verb, bad, bind):
    exc = reject(bind, verb, ValueError, bearer=bad)
    assert str(exc) == "bearer must be a non-empty str"


@pytest.mark.parametrize("verb", VERBS)
def test_bearer_message_never_echoes_the_value(verb, bind):
    for index, bad in enumerate(["SENTINEL-B1 x", "SENTINEL-B2\n", "SENTINEL-B3\x00", "SENTINEL-b\u00e9"]):
        exc = reject(bind, verb, ValueError, bearer=bad)
        assert f"SENTINEL-B{index + 1}" not in str(exc) and "SENTINEL-b\u00e9" not in str(exc)


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("url", ["http://api.example/x", "ftp://api.example/x", "api.example/x", "//api.example/x",
                                 "http://SENTINEL-host.example/x", "httpsx://api.example/x", "https:/api.example"])
def test_bearer_requires_https(verb, url, bind):
    exc = reject(bind, verb, ValueError, url=url, bearer="tok")
    assert str(exc) == "bearer requires an https URL"
    assert "SENTINEL" not in str(exc)


@pytest.mark.parametrize("verb", VERBS)
def test_bearer_not_required_for_http_without_a_bearer(verb, bind):
    call(verb, "http://api.example/x")                 # no bearer: a plain http URL is not this rule's business
    assert bind.calls[0][1] == ("http://api.example/x",)


@pytest.mark.parametrize("verb", VERBS)
def test_bearer_header_collision_any_case(verb, bind):
    for name in ["Authorization", "authorization", "AUTHORIZATION", "aUtHoRiZaTiOn"]:
        reject(bind, verb, ValueError, bearer="tok", headers={"X-A": "1", name: "Bearer other"})
    # `Proxy-Authorization` is a different header, not a collision
    call(verb, bearer="tok", headers={"Proxy-Authorization": "x"})
    assert list(bind.kwargs["headers"]) == ["Authorization", "Proxy-Authorization"]


@pytest.mark.parametrize("verb", VERBS)
def test_bearer_collision_message_does_not_echo(verb, bind):
    exc = reject(bind, verb, ValueError, bearer="tok-SENTINEL-1", headers={"Authorization": "Bearer SENTINEL-2"})
    assert "SENTINEL" not in str(exc)


# ── headers (rule 7, R5) ───────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verb", VERBS)
def test_headers_with_bearer_are_a_new_dict_and_the_caller_dict_is_untouched(verb, bind):
    headers = {"content-TYPE": "x/y", "X-Z": "1"}
    snapshot = copy.deepcopy(headers)
    call(verb, bearer="tok", headers=headers)
    sent = bind.kwargs["headers"]
    assert headers == snapshot and sent is not headers
    assert list(sent) == ["Authorization", "content-TYPE", "X-Z"]       # key case preserved, Authorization first
    assert sent["content-TYPE"] == "x/y"


@pytest.mark.parametrize("verb", VERBS)
def test_headers_without_bearer_are_forwarded_as_the_same_object(verb, bind):
    headers = {"x-lower": "1", "X-Upper": "2"}
    call(verb, headers=headers)
    assert bind.kwargs["headers"] is headers
    assert list(headers) == ["x-lower", "X-Upper"]


@pytest.mark.parametrize("verb", VERBS)
def test_headers_authorization_without_bearer_is_accepted_but_still_validated(verb, bind):
    headers = {"Authorization": "Bearer from-the-caller"}
    call(verb, headers=headers)
    assert bind.kwargs["headers"] is headers
    bind.calls.clear()
    reject(bind, verb, ValueError, headers={"Authorization": "Bearer bad\nvalue"})


@pytest.mark.parametrize("verb", VERBS)
def test_headers_must_be_a_mapping_of_str(verb, bind):
    for bad in ([("A", "b")], "A: b", b"A: b", ("A", "b"), 5, object(), {1: "x"}, {"X": 1}, {"X": b"v"}, {"X": None},
                {b"X": "v"}, {"X": ["v"]}):
        reject(bind, verb, TypeError, headers=bad)
    call(verb, headers=httpx.Headers({"A": "b"}))        # an httpx.Headers is a Mapping of str


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("name", ["", " ", "A B", "A:B", "A\nB", "A\r\nB", "\u00c4", "A(B)", "A,B", "A;B", "A/B", "A@B",
                                  "A\x00", "A\x7f", "A\t", "\tA", " A", "A ", "A=B", "A[B]", "A<B>", 'A"B', "A{B}"])
def test_headers_name_must_be_an_rfc7230_token(verb, name, bind):
    exc = reject(bind, verb, ValueError, headers={name: "v"})
    assert str(exc) == "platform_http: invalid header name"


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("name", ["X-Foo", "x_foo", "X.Foo", "X~Foo", "X!#$%&'*+-^_`|~", "X%41", "X_Foo", "a1", "A" * 300])
def test_headers_name_token_characters_are_accepted(verb, name, bind):
    call(verb, headers={name: "v"})
    assert bind.kwargs["headers"] == {name: "v"}


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("value", [
    "", " ", "\t", "a\r\nX-Evil: 1", "a\nb", "a\rb", "a\x00b", "a\x0bb", "a\x0cb", "\x7f", "a\x7fb", "\u00e9", "caf\u00e9.mp4",
    " lead", "trail ", "\tlead", "trail\t", "a\u00a0", "\u2028", "a\x01b", "a\x1fb",
])
def test_headers_value_must_fullmatch_printable_ascii(verb, value, bind):
    exc = reject(bind, verb, ValueError, headers={"X-A": value})
    assert str(exc) == "platform_http: invalid header value"


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("value", [
    "a", "a b", "a\tb", "video/mp4", "multipart/related; boundary=0123456789abcdef", "application/json; charset=UTF-8",
    "1234", "a  b", "!", "~", "a" * 5000,
])
def test_headers_value_accepted(verb, value, bind):
    call(verb, headers={"X-A": value})
    assert bind.kwargs["headers"] == {"X-A": value}


@pytest.mark.parametrize("verb", VERBS)
def test_headers_empty_mapping_is_forwarded(verb, bind):
    headers = {}
    call(verb, headers=headers)
    assert bind.kwargs["headers"] is headers


@pytest.mark.parametrize("verb", VERBS)
def test_headers_messages_do_not_echo(verb, bind):
    for index, headers in enumerate([{"SENTINEL-HN1 x": "v"}, {"X": "SENTINEL-HV2\n"}, {"X": "SENTINEL-HV3\u00e9"}]):
        exc = reject(bind, verb, ValueError, headers=headers)
        assert f"SENTINEL-HN{index + 1}" not in str(exc) and f"SENTINEL-HV{index + 1}" not in str(exc)


# ── what is forwarded (rule 1) ─────────────────────────────────────────────────────────────────────

FORWARDABLE = {
    "get": {"headers": {"X-A": "1"}, "params": {"fields": "id"}},
    "post": {"headers": {"X-A": "1"}, "params": {"fields": "id"}, "data": {"a": "b"}, "json": {"a": [1]},
             "content": b"raw"},
    "put": {"headers": {"X-A": "1"}, "params": {"fields": "id"}, "data": {"a": "b"}, "json": {"a": [1]},
            "content": b"raw"},
}


@pytest.mark.parametrize("verb", VERBS)
def test_forwards_only_supplied(verb, bind):
    call(verb)
    assert set(bind.kwargs) == {"timeout"}
    for name, value in FORWARDABLE[verb].items():
        bind.calls.clear()
        call(verb, **{name: value})
        assert set(bind.kwargs) == {name, "timeout"}, name
        assert bind.kwargs[name] == value, name
    bind.calls.clear()
    call(verb, **FORWARDABLE[verb])
    assert set(bind.kwargs) == set(FORWARDABLE[verb]) | {"timeout"}
    for name, value in FORWARDABLE[verb].items():
        assert bind.kwargs[name] == value, name


@pytest.mark.parametrize("verb", VERBS)
def test_forwards_a_falsy_value_that_is_not_none(verb, bind):
    call(verb, params={}, headers={})
    assert bind.kwargs["params"] == {} and bind.kwargs["headers"] == {}
    if verb != "get":
        bind.calls.clear()
        call(verb, data={}, content=b"", json=[])
        assert bind.kwargs["data"] == {} and bind.kwargs["content"] == b"" and bind.kwargs["json"] == []


@pytest.mark.parametrize("verb", ["post", "put"])
def test_explicit_none_is_never_forwarded(verb, bind):
    call(verb, headers=None, params=None, data=None, json=None, content=None)
    assert set(bind.kwargs) == {"timeout"}


@pytest.mark.parametrize("verb", VERBS)
def test_url_positional(verb, bind):
    call(verb, bearer="tok", params={"a": "b"}, headers={"X": "y"})
    (name, args, kwargs), = bind.calls
    assert args == (URL,) and "url" not in kwargs


@pytest.mark.parametrize("verb", VERBS)
def test_never_forwards_httpx_defaults(verb, bind):
    call(verb, bearer="tok", **{k: v for k, v in FORWARDABLE[verb].items() if k != "headers"})
    for name in ("follow_redirects", "verify", "trust_env", "auth", "cookies", "proxy", "files", "extensions"):
        assert name not in bind.kwargs
    assert set(bind.kwargs) <= {"headers", "params", "data", "json", "content", "timeout"}
    for forbidden in ("auth", "cookies", "follow_redirects", "verify", "trust_env", "proxy", "files"):
        with pytest.raises(TypeError):
            call(verb, **{forbidden: None})
    assert len(bind.calls) == 1


def test_get_takes_no_body_keywords(bind):
    for name in ("data", "json", "content", "server_supplied_url"):
        with pytest.raises(TypeError):
            platform_http.get(URL, **{name: None if name != "server_supplied_url" else True})
    with pytest.raises(TypeError):
        platform_http.post(URL, server_supplied_url=True)
    assert bind.calls == []


@pytest.mark.parametrize("verb", VERBS)
def test_url_and_options_signature(verb):
    parameters = inspect.signature(getattr(platform_http, verb)).parameters
    assert next(iter(parameters)) == "url"
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in parameters.items() if name != "url")
    with pytest.raises(TypeError):
        getattr(platform_http, verb)(URL, "tok")          # nothing but the URL is positional


@pytest.mark.parametrize("verb", VERBS)
def test_params_same_object(verb, bind):
    forms = [
        {"fields": "id", "part": ["a", "b"]}, [("fields", "id")], (("fields", "id"),),
        httpx.QueryParams({"fields": "id"}), "fields=id", b"fields=id",
    ]
    for params in forms:
        bind.calls.clear()
        snapshot = copy.deepcopy(params)
        call(verb, params=params)
        assert bind.kwargs["params"] is params, params
        assert params == snapshot, params          # never mutated


def test_put_flag_is_never_forwarded(bind):
    call("put", headers={"Content-Type": "video/mp4"}, content=b"x", server_supplied_url=True)
    assert set(bind.kwargs) == {"headers", "content", "timeout"}


# ── timeouts (R8) ──────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verb", VERBS)
def test_timeout_default_per_verb(verb, bind):
    call(verb)
    timeout = bind.kwargs["timeout"]
    assert type(timeout) is float and timeout == 30.0


def test_timeout_constants_and_defaults_by_value():
    assert platform_http.DEFAULT_TIMEOUT_S == 30.0 and platform_http.UPLOAD_TIMEOUT_S == 300.0
    for verb in VERBS:
        default = inspect.signature(getattr(platform_http, verb)).parameters["timeout"].default
        assert default == 30.0 and type(default) is float, verb


@pytest.mark.parametrize("verb", VERBS)
def test_timeout_override_is_forwarded_as_a_float(verb, bind):
    for given, expected in [(12.5, 12.5), (5, 5.0), (300.0, 300.0), (3600, 3600.0), (3600.0, 3600.0),
                            (0.001, 0.001), (1, 1.0), (platform_http.UPLOAD_TIMEOUT_S, 300.0)]:
        bind.calls.clear()
        call(verb, timeout=given)
        sent = bind.kwargs["timeout"]
        assert type(sent) is float and sent == expected, given


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("bad", [None, True, False, "30", b"30", [30], (30,), httpx.Timeout(5.0), object(), 3 + 0j])
def test_timeout_type_rejected(verb, bad, bind):
    exc = reject(bind, verb, TypeError, timeout=bad)
    assert str(exc) == "platform_http: timeout must be a number"


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 10**400, -(10**400), 0, 0.0, -0.0, -1, -1.5,
                                 3600.001, 3601, 3600.0000001, 10**6, 1e300])
def test_timeout_range_rejected_before_any_float_conversion(verb, bad, bind):
    exc = reject(bind, verb, ValueError, timeout=bad)          # an OverflowError is not a ValueError
    assert type(exc) is ValueError


def test_timeout_messages_do_not_echo(bind):
    for bad in ("SENTINEL-T1", 1234567.891, 10**30):
        for verb in VERBS:
            with pytest.raises((TypeError, ValueError)) as info:
                call(verb, timeout=bad)
            assert "SENTINEL-T1" not in str(info.value) and "1234567" not in str(info.value)
            assert "1000000000" not in str(info.value)


# ── the secret guard (rule 3) ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verb", VERBS)
def test_guard_params_forms(verb, bind):
    for name, natural in NATURAL:
        for form_name, make in PARAM_FORMS.items():
            if form_name == "str bare key" and natural.endswith(" "):
                continue
            exc = reject(bind, verb, ValueError, params=make(natural))
            assert_secret_rejection(exc, name, f"{verb} {natural} {form_name}")


@pytest.mark.parametrize("verb", VERBS)
def test_guard_params_spellings(verb, bind):
    for name, natural in NATURAL:
        for spelling_name, spell in SPELLINGS.items():
            key = spell(natural)
            for form_name in ("dict", "list of pairs", "str key not first", "bytes key not first", "dict key not first"):
                exc = reject(bind, verb, ValueError, params=PARAM_FORMS[form_name](key))
                assert_secret_rejection(exc, name, f"{verb} {natural} {spelling_name} {form_name}")


@pytest.mark.parametrize("verb", VERBS)
def test_guard_url_query_forms(verb, bind):
    for name, natural in NATURAL:
        for form_name, make in URL_FORMS.items():
            exc = reject(bind, verb, ValueError, url=make(natural))
            assert_secret_rejection(exc, name, f"{verb} {natural} {form_name}")


@pytest.mark.parametrize("verb", VERBS)
def test_guard_url_query_spellings(verb, bind):
    for name, natural in NATURAL:
        for spelling_name, spell in SPELLINGS.items():
            key = spell(natural)
            for form_name in ("key=value", "key not first", "semicolon"):
                exc = reject(bind, verb, ValueError, url=URL_FORMS[form_name](key))
                assert_secret_rejection(exc, name, f"{verb} {natural} {spelling_name} {form_name}")


@pytest.mark.parametrize("verb", VERBS)
def test_guard_variants(verb, bind):
    # the access_token spellings of the design, in both places
    for key in ["access-token", "accessToken", "access_token[]", "%61ccess_token", "%2561ccess_token",
                "access_token ", "ACCESS_TOKEN", "Access-Token", "access+token", "access.token", "access%20token",
                "a-c-c-e-s-s_t-o-k-e-n", "ACCESS%5FTOKEN", "access__token", "\u212aey"]:
        want = "key" if key == "\u212aey" else "accesstoken"       # the Kelvin sign lower-cases to 'k'
        assert_secret_rejection(reject(bind, verb, ValueError, params={key: "v"}), want, key)
        assert_secret_rejection(reject(bind, verb, ValueError, url=f"{URL}?x=1&{key}=v"), want, key)
    # any decode depth up to the cap of 8 is decoded and named
    for layers in range(1, 9):
        key = layered("access_token", layers)
        assert_secret_rejection(reject(bind, verb, ValueError, params={key: "v"}), "accesstoken", str(layers))
        assert_secret_rejection(reject(bind, verb, ValueError, url=f"{URL}?{key}=v"), "accesstoken", str(layers))
    # a name still changing after the cap is rejected too (fail closed), with the same hint
    for layers in (9, 10, 12, 30):
        key = layered("access_token", layers)
        for exc in (reject(bind, verb, ValueError, params={key: "v"}), reject(bind, verb, ValueError, url=f"{URL}?{key}=v"),
                    reject(bind, verb, ValueError, params=f"{key}=v"), reject(bind, verb, ValueError, params=[(key, "v")])):
            assert "bearer=" in str(exc), layers
    # an innocent key that merely encodes a harmless name is still accepted at a depth the cap allows
    bind.calls.clear()
    call(verb, params={layered("fields", 3): "v"})
    assert len(bind.calls) == 1


@pytest.mark.parametrize("verb", VERBS)
def test_guard_accepts_the_real_call_params_and_near_misses(verb, bind):
    accepted = [
        {"part": "snippet,status", "id": "abc", "fields": "id", "metric": "plays,likes", "uploadType": "resumable"},
        {"uploadType": "multipart", "part": "snippet"},
        {"fields": "access_token"},                                # a secret-looking name only as a VALUE
        {"fields": "id,access_token", "part": "bearer"},
        {k: "v" for k in ["keyword", "monkey", "tokens", "encoded", "code_challenge", "pagetoken", "nextpagetoken",
                          "keys", "mykey", "tokenizer", "authority", "codes", "secrets", "passwords", "signatures",
                          "sigma", "authx", "xauth", "bearers", "accesstokens", "access_tokens", "token_type",
                          "key_id", "client_id", "client", "refresh", "access", "idtokens", "api", "apikeys"]},
        {1: "v", 2: "w"},                                         # non-str keys are stringified, as httpx does
        {"a": True, "b": None, "c": ["x", "y"]},
        [("part", "x"), ("id", "y")], (("fields", "id"),), httpx.QueryParams({"fields": "id"}),
        "part=x&id=y", b"part=x&id=y", "?part=x", "fields=access_token", "", b"", {}, [],
    ]
    for params in accepted:
        bind.calls.clear()
        call(verb, params=params)
        assert bind.kwargs["params"] is params
    for query in ["x=1", "part=snippet%2Cstatus&id=1", "keyword=1", "monkey", "fields=access_token", "x=1/media", "?x=1",
                  "", "a=1&&b=2", "&", ";", "=access_token", "x=1;y=2"]:
        bind.calls.clear()
        call(verb, f"{URL}?{query}")
        assert bind.calls[0][1] == (f"{URL}?{query}",), query
    for url in ["https://h/a?x=1/media", "https://h/a@b?x=y@z", "https://h/a/@/b", "https://h/v1/SECRET-TOKEN/media",
                "https://h/a#access_token=1", "https://h/a#?access_token", "https://h/path;access_token=1"]:
        bind.calls.clear()
        call(verb, url)
        assert bind.calls[0][1] == (url,), url


@pytest.mark.parametrize("verb", ["post", "put"])
def test_guard_does_not_inspect_bodies_or_headers(verb, bind):
    call(verb, data={"access_token": "t", "client_secret": "s", "code": "c"})
    bind.calls.clear()
    call(verb, json={"access_token": "t", "refresh_token": "r"})
    bind.calls.clear()
    call(verb, content=b"access_token=t&client_secret=s")
    bind.calls.clear()
    call(verb, headers={"X-Access-Token": "t", "Cookie": "a=b", "X-Goog-Api-Key": "k"})
    bind.calls.clear()
    call(verb, bearer="access_token", data={"access_token": "t"})
    assert bind.kwargs["data"] == {"access_token": "t"}


@pytest.mark.parametrize("verb", VERBS)
def test_guard_known_gaps_are_pinned_as_accepted(verb, bind):
    call(verb, "https://api.example/v1/SECRET-IN-THE-PATH/media")              # secret in the URL path
    bind.calls.clear()
    call(verb, params={"x": "a-token-under-an-innocent-key"})                  # secret under an innocent key
    bind.calls.clear()
    call(verb, f"{URL}?x=a-token-under-an-innocent-key")


@pytest.mark.parametrize("verb", VERBS)
def test_guard_userinfo(verb, bind):
    for url in ["https://user:pw@api.example/x", "https://user@api.example/x", "https://:pw@api.example/x",
                "https://@api.example/x", "https://a:b@c:443/x?y=1", "http://user:pw@api.example/x",
                "//user:pw@api.example/x", "https://api.example@evil.example/x"]:
        exc = reject(bind, verb, ValueError, url=url)
        assert "bearer=" in str(exc), url
    exc = reject(bind, verb, ValueError, url="https://SENTINEL-U1:SENTINEL-P1@api.example/x")
    assert "SENTINEL" not in str(exc)


@pytest.mark.parametrize("verb", VERBS)
@pytest.mark.parametrize("url", [
    " " + URL, URL + " ", "\n" + URL, URL + "\n", "\t" + URL, URL + "\t", "\r\n" + URL, URL + "\r\n", "\u00a0" + URL,
    URL + "\u00a0", "\u3000" + URL, URL + "\u2003", "\x0b" + URL, URL + "\x0c", "\x1f" + URL, URL + "\x1c",
    f"{URL}?a=1 ", f"{URL}?a=1\n", f"{URL}#frag ", "   ",
])
def test_guard_whitespace_around_the_url_is_unparseable(verb, url, bind):
    exc = reject(bind, verb, ValueError, url=url)
    assert str(exc) == "platform_http: unparseable URL"
    assert exc.__context__ is None and exc.__cause__ is None


@pytest.mark.parametrize("verb", VERBS)
def test_guard_message(verb, bind):
    for name, natural in NATURAL:
        exc = reject(bind, verb, ValueError, params={natural + "-" * 200: "SENTINEL-VALUE-1"})
        message = str(exc)
        assert name in message and "bearer=" in message
        assert "-" * 5 not in message and len(message) < 300           # never the raw key, bounded
        assert "SENTINEL-VALUE-1" not in message
        exc = reject(bind, verb, ValueError, url=f"https://SENTINEL-HOST-2.example/p?x=SENTINEL-Q-3&{natural}=SENTINEL-V-4")
        message = str(exc)
        assert name in message and "bearer=" in message
        for sentinel in ("SENTINEL-HOST-2", "SENTINEL-Q-3", "SENTINEL-V-4", "p?x"):
            assert sentinel not in message
    # the message names the matched constant, not the spelling the caller used
    exc = reject(bind, verb, ValueError, params={"%61CCESS-Token[]": "v"})
    assert "accesstoken" in str(exc) and "%61" not in str(exc) and "CCESS" not in str(exc)
    exc = reject(bind, verb, ValueError, params={"api_key": "v", "client_secret": "w"})
    assert "apikey" in str(exc) and "clientsecret" not in str(exc)          # the first offender is named


@pytest.mark.parametrize("verb", VERBS)
def test_guard_runs_before_the_request_and_checks_every_pair(verb, bind):
    reject(bind, verb, ValueError, url=f"{URL}?a=1&b=2&c=3&d=4&e=5&access_token=v")
    reject(bind, verb, ValueError, params={f"k{i}": "v" for i in range(30)} | {"code": "v"})
    reject(bind, verb, ValueError, params="a=1&b=2&c=3&d=4&e=5&f=6&code=v")
    # a clean call goes through exactly once
    call(verb, f"{URL}?a=1", params={"b": "2"})
    assert len(bind.calls) == 1


def test_normalise_fixpoint():
    for raw, expected in [
        ("access_token", "accesstoken"), ("Access-Token", "accesstoken"), ("accessToken", "accesstoken"),
        ("access_token[]", "accesstoken"), ("access_token ", "accesstoken"), (" access_token", "accesstoken"),
        ("%61ccess_token", "accesstoken"), ("%2561ccess_token", "accesstoken"), ("access+token", "accesstoken"),
        ("a%2Bb", "ab"), ("", ""), ("---", ""), ("100%", "100"), ("%zz", "zz"), ("x-goog-api-key", "xgoogapikey"),
        ("A1_b2", "a1b2"), ("a%20b%20c", "abc"),
    ]:
        assert platform_http._normalise(raw) == expected, raw
    for layers in range(1, 9):
        assert platform_http._normalise(layered("access_token", layers)) == "accesstoken", layers
    # beyond the cap the result is NOT a plain name (fail closed in the guard: see test_guard_variants)
    for layers in (9, 10, 25):
        assert platform_http._normalise(layered("access_token", layers)) != "accesstoken", layers


# ── unparseable inputs (rule 3), deterministically ─────────────────────────────────────────────────

URL_MESSAGE = "platform_http: unparseable URL"
PARAMS_MESSAGE = "platform_http: unparseable params"


def _assert_replacement(exc, message):
    assert type(exc) is ValueError
    assert str(exc) == message
    assert exc.__context__ is None and exc.__cause__ is None
    assert exc.__suppress_context__ is False or exc.__context__ is None


@pytest.mark.parametrize("verb", VERBS)
def test_unparseable_url(verb, bind, monkeypatch):
    def boom(url, *args, **kwargs):
        raise ValueError("SENTINEL-URLSPLIT netloc 'x' contains invalid characters")

    monkeypatch.setattr(urllib.parse, "urlsplit", boom)
    for url in (URL, f"{URL}?a=1", "https://h/x"):
        exc = reject(bind, verb, ValueError, url=url)
        _assert_replacement(exc, URL_MESSAGE)
        assert "SENTINEL" not in str(exc)
    # the same failure with another exception class and with a bearer present
    monkeypatch.setattr(urllib.parse, "urlsplit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("SENTINEL-2")))
    exc = reject(bind, verb, ValueError, bearer="tok")
    _assert_replacement(exc, URL_MESSAGE)


@pytest.mark.parametrize("verb", VERBS)
def test_unparseable_url_real_smoke(verb, bind):
    url = "https://h\u2100/x"
    try:
        urllib.parse.urlsplit(url)
    except ValueError:
        pass
    else:
        pytest.skip("this Python's urlsplit accepts the netloc")
    exc = reject(bind, verb, ValueError, url=url)
    _assert_replacement(exc, URL_MESSAGE)


@pytest.mark.parametrize("verb", VERBS)
def test_unparseable_url_when_the_decoder_fails_on_the_query(verb, bind, monkeypatch):
    def boom(text, *args, **kwargs):
        raise RuntimeError("SENTINEL-UNQUOTE")

    monkeypatch.setattr(urllib.parse, "unquote_plus", boom)
    _assert_replacement(reject(bind, verb, ValueError, url=f"{URL}?a=1"), URL_MESSAGE)


class _RaisingQueryParams(httpx.QueryParams):
    def __init__(self, *args, **kwargs):
        raise RuntimeError("SENTINEL-QUERYPARAMS")


@pytest.mark.parametrize("verb", VERBS)
def test_unparseable_params(verb, bind, monkeypatch):
    monkeypatch.setattr(httpx, "QueryParams", _RaisingQueryParams)
    for params in ({"a": "1"}, {"access_token": "t"}, [("a", "1")], {}):
        exc = reject(bind, verb, ValueError, params=params)
        _assert_replacement(exc, PARAMS_MESSAGE)
        assert "SENTINEL" not in str(exc)
    # a str / bytes query string is split by the module itself and never reaches QueryParams
    call(verb, params="a=1")
    bind.calls.clear()
    call(verb, params=b"a=1")


class _BadKey:
    def __str__(self):
        raise RuntimeError("SENTINEL-STR")

    __repr__ = __str__

    def __hash__(self):
        return 1

    def __eq__(self, other):
        return self is other


@pytest.mark.parametrize("verb", VERBS)
def test_unparseable_params_real_smokes(verb, bind):
    for params in ([("a",)], {_BadKey(): "v"}, [(_BadKey(), "v")], b"\xff=1", b"a=\xc3\xa9", b"\x80"):
        exc = reject(bind, verb, ValueError, params=params)
        _assert_replacement(exc, PARAMS_MESSAGE)


# ── type checks, nothing echoed (rule 7) ───────────────────────────────────────────────────────────

class _Loud:
    def __init__(self, tag):
        self.tag = tag

    def __repr__(self):
        return self.tag

    __str__ = __repr__


@pytest.mark.parametrize("verb", VERBS)
def test_type_checks_no_echo(verb, bind):
    cases = [
        ("url", dict(url=_Loud("SENTINEL-TU1"))),
        ("url", dict(url=b"https://SENTINEL-TU2@h/x")),
        ("url", dict(url=httpx.URL("https://SENTINEL-TU3:pw@h/x"))),
        ("url", dict(url=5)),
        ("url", dict(url=None)),
        ("headers", dict(headers=_Loud("SENTINEL-TH1"))),
        ("headers", dict(headers=[("SENTINEL-TH2", "v")])),
        ("headers", dict(headers={"X": _Loud("SENTINEL-TH3")})),
        ("headers", dict(headers={_Loud("SENTINEL-TH4"): "v"})),
        ("headers", dict(headers="SENTINEL-TH5")),
        ("params", dict(params=_Loud("SENTINEL-TP1"))),
        ("params", dict(params={"SENTINEL-TP2"})),
        ("params", dict(params=5)),
        ("params", dict(params=1.5)),
        ("params", dict(params=range(3))),
        ("timeout", dict(timeout="SENTINEL-TT1")),
        ("timeout", dict(timeout=_Loud("SENTINEL-TT2"))),
        ("timeout", dict(timeout=None)),
        ("timeout", dict(timeout=True)),
    ]
    for kind, kwargs in cases:
        tag = re.search(r"SENTINEL-\w+", repr(kwargs))
        exc = reject(bind, verb, TypeError, **kwargs)
        if tag:
            assert tag.group(0) not in str(exc), kwargs
        assert str(exc).startswith("platform_http: ") and kind in str(exc), kwargs


@pytest.mark.parametrize("verb", VERBS)
def test_non_str_url_is_a_type_error_not_a_guard_error(verb, bind):
    # without the type check these would fall into the guard's `unparseable URL` ValueError (or worse)
    for url in (b"https://api.example/x", httpx.URL(URL), 5, None, ["https://api.example/x"]):
        reject(bind, verb, TypeError, url=url)
        assert bind.calls == []


@pytest.mark.parametrize("verb", VERBS)
def test_no_message_echoes_a_caller_value(verb, bind):
    cases = [
        dict(bearer=_Loud("SENTINEL-M1")),
        dict(bearer="SENTINEL-M2\n"),
        dict(bearer="tok", url="http://SENTINEL-M3.example/x"),
        dict(bearer="tok", headers={"AUTHORIZATION": "Bearer SENTINEL-M4"}),
        dict(url="https://SENTINEL-M5:SENTINEL-M5b@h/x"),
        dict(headers={"SENTINEL-M6 x": "v"}),
        dict(headers={"X": "SENTINEL-M7\r\nInjected: 1"}),
        dict(params={"access_token": "SENTINEL-M8"}),
        dict(params={"SENTINEL-M9": "v", "access_token": "w"}),
        dict(params="SENTINEL-M10=v&access_token=w"),
        dict(url="https://h/x?SENTINEL-M11=1&access_token=w"),
        dict(url="https://h/x?access_token=SENTINEL-M12"),
        dict(timeout=1.2345678e9),
        dict(timeout=-98765.4321),
    ]
    for case in cases:
        kwargs = dict(case)
        url = kwargs.pop("url", URL)
        tag = re.search(r"SENTINEL-M\d+\w?", repr(case))
        with pytest.raises((TypeError, ValueError)) as info:
            call(verb, url, **kwargs)
        assert bind.calls == []
        if tag:
            assert tag.group(0) not in str(info.value), case
        assert "1234567" not in str(info.value) and "98765" not in str(info.value)


# ── server_supplied_url (R4) ───────────────────────────────────────────────────────────────────────

SESSION_URL = "https://upload.example.com/s?upload_id=1&key=2"
UPLOAD_HEADERS = {"Content-Type": "video/mp4", "Content-Length": "3"}


def _put(rec=None, url=SESSION_URL, **kwargs):
    kwargs.setdefault("headers", UPLOAD_HEADERS)
    kwargs.setdefault("content", b"abc")
    kwargs.setdefault("server_supplied_url", True)
    return platform_http.put(url, **kwargs)


def test_server_supplied_url_skips_only_the_url_query_scan(mp):
    rec = Recorder(lambda request: httpx.Response(200, json={"id": "v"})).install(mp)
    resp = _put()
    assert resp.json() == {"id": "v"}
    assert rec.calls[0].url == SESSION_URL and rec.calls[0].kwargs["content"] == b"abc"
    assert rec.requests[0].url.params["key"] == "2" and rec.requests[0].url.params["upload_id"] == "1"
    # every secret-named pair in a server-supplied URL's own query is accepted
    for name, natural in NATURAL:
        rec.calls.clear()
        _put(url=f"https://upload.example.com/s?{natural}=v&x=1;{natural}=w")
        assert len(rec.calls) == 1, natural


def test_server_supplied_url_default_off_still_scans_the_query(bind):
    with pytest.raises(ValueError):
        platform_http.put(SESSION_URL, headers=UPLOAD_HEADERS, content=b"abc")
    with pytest.raises(ValueError):
        platform_http.put(SESSION_URL, headers=UPLOAD_HEADERS, content=b"abc", server_supplied_url=False)
    assert bind.calls == []
    assert inspect.signature(platform_http.put).parameters["server_supplied_url"].default is False


def test_server_supplied_url_accepted_header_spellings(bind):
    for headers in (
        {"Content-Type": "video/mp4", "Content-Length": "3"}, {"content-type": "video/mp4", "CONTENT-LENGTH": "3"},
        {"CONTENT-TYPE": "x/y"}, {"Content-Type": "video/mp4"}, {"content-length": "3"}, {}, None,
    ):
        bind.calls.clear()
        _put(headers=headers)
        assert len(bind.calls) == 1, headers
    for params in (None,):
        bind.calls.clear()
        _put(params=params)
        assert "params" not in bind.kwargs and "server_supplied_url" not in bind.kwargs


@pytest.mark.parametrize("params", [{}, {"part": "x"}, [], [("a", "b")], "", "a=1", b"", httpx.QueryParams()])
def test_server_supplied_url_rejects_any_params(params, bind):
    exc = reject(bind, "put", ValueError, url=SESSION_URL, headers=UPLOAD_HEADERS, content=b"abc", params=params,
                 server_supplied_url=True)
    assert str(exc) == "platform_http: params not allowed with a server-supplied URL"


def test_server_supplied_url_still_rejects_userinfo(bind):
    for url in ("https://u:p@upload.example.com/s", "https://u@upload.example.com/s?key=2"):
        exc = reject(bind, "put", ValueError, url=url, headers=UPLOAD_HEADERS, server_supplied_url=True)
        assert "bearer=" in str(exc)
    assert "@" not in str(exc)


def test_server_supplied_url_rejects_a_bearer(bind):
    for bearer in ("tok", "tok-123"):
        exc = reject(bind, "put", ValueError, url=SESSION_URL, headers=UPLOAD_HEADERS, bearer=bearer, server_supplied_url=True)
        assert str(exc) == "bearer is not allowed with a server-supplied URL"
    # an Authorization HEADER is not allowed either (see the allowlist below)
    exc = reject(bind, "put", ValueError, url=SESSION_URL, headers={"Authorization": "Bearer t"}, server_supplied_url=True)
    assert str(exc) == "platform_http: header not allowed with a server-supplied URL"


SERVER_ALLOWLIST_MESSAGE = "platform_http: header not allowed with a server-supplied URL"


@pytest.mark.parametrize("name", [
    # credential-like
    "Authorization", "authorization", "AUTHORIZATION", "Proxy-Authorization", "Cookie", "cookie", "X-Goog-Api-Key",
    "X_Goog_Api_Key", "X-Api-Key", "X-Access-Token", "Set-Cookie", "Api-Key",
    # benign, so the rule is an allowlist and not a denylist
    "Accept", "X-Upload-Content-Type", "X-Upload-Content-Length", "X-Foo", "X_Foo", "User-Agent", "Content-Encoding",
    "Content-Disposition", "Range", "Host", "Transfer-Encoding",
    # normalised spellings of the two allowed names are NOT the allowed names
    "ContentType", "Content_Type", "Content%2DType", "ContentLength", "Content_Length", "Content.Type", "Content-Type-",
    "Content-Type2", "-Content-Type", "Content--Type", "Content-Typ", "ontent-Type",
])
def test_server_supplied_url_header_allowlist(name, bind):
    exc = reject(bind, "put", ValueError, url=SESSION_URL, headers={"Content-Type": "video/mp4", name: "v"},
                 server_supplied_url=True)
    assert str(exc) == SERVER_ALLOWLIST_MESSAGE, name
    exc = reject(bind, "put", ValueError, url=SESSION_URL, headers={name: "v"}, server_supplied_url=True)
    assert str(exc) == SERVER_ALLOWLIST_MESSAGE, name


def test_server_supplied_url_duplicate_names_after_case_folding(bind):
    for headers in ({"Content-Type": "a", "content-type": "b"}, {"CONTENT-LENGTH": "1", "Content-Length": "2"},
                    {"Content-Type": "a", "Content-Length": "1", "content-TYPE": "b"}):
        exc = reject(bind, "put", ValueError, url=SESSION_URL, headers=headers, server_supplied_url=True)
        assert str(exc) == SERVER_ALLOWLIST_MESSAGE


def test_server_supplied_url_rule7_still_applies_and_is_not_the_allowlist(bind):
    # spellings that rule 7 accepts are accepted WITHOUT the flag, so the allowlist message above is the
    # allowlist's own and cannot be a rule-7 rejection in disguise
    for name in ("X_Foo", "X%41", "ContentType", "Content_Type"):
        bind.calls.clear()
        platform_http.put(URL, headers={name: "v"}, content=b"x")
        assert bind.kwargs["headers"] == {name: "v"}
    bind.calls.clear()
    exc = reject(bind, "put", ValueError, url=SESSION_URL, headers={"X bad": "v"}, server_supplied_url=True)
    assert str(exc) == "platform_http: invalid header name"
    exc = reject(bind, "put", ValueError, url=SESSION_URL, headers={"Content-Type": "bad\nvalue"}, server_supplied_url=True)
    assert str(exc) == "platform_http: invalid header value"


def test_server_supplied_url_must_be_a_bool(bind):
    for flag in (1, "yes", object(), [True]):
        reject(bind, "put", TypeError, url=SESSION_URL, headers=UPLOAD_HEADERS, server_supplied_url=flag)


def test_server_supplied_url_other_rules_still_apply(bind):
    reject(bind, "put", ValueError, url=" " + SESSION_URL, headers=UPLOAD_HEADERS, server_supplied_url=True)
    reject(bind, "put", ValueError, url=SESSION_URL, headers=UPLOAD_HEADERS, timeout=0, server_supplied_url=True)
    reject(bind, "put", TypeError, url=5, server_supplied_url=True)
    _put(timeout=300.0)
    assert bind.kwargs["timeout"] == 300.0


# the real site 9: a session URL carrying `?key=` must still publish

def _drive_publish(tmp_path, monkeypatch, session_url):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"VID")
    cut = MagicMock()
    cut.id, cut.reel_id, cut.video_path, cut.hashtags, cut.subtitle_path = 1, 2, str(video), None, None
    credential = MagicMock()
    credential.token_blob, credential.refresh_token_blob, credential.expires_at = "tok-site9", "unused", None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, headers={"Location": session_url})
        return httpx.Response(200, json={"id": "vid-9"})

    rec = Recorder(handler).install(monkeypatch)
    result = YouTubePublisher().publish(cut, credential, MagicMock(), caption="cap")
    return rec, result


@pytest.mark.parametrize("session_url", [
    "https://upload.example.com/s/abc?upload_id=U1&key=K2&upload_protocol=resumable",
    "https://upload.example.com/s/abc?access_token=NOT-REALLY&sig=1;code=2",
    "https://upload.example.com/s/abc",
])
def test_server_supplied_url_site9_publishes_with_a_secret_looking_session_query(tmp_path, monkeypatch, session_url):
    rec, result = _drive_publish(tmp_path, monkeypatch, session_url)
    assert result.platform_post_id == "vid-9"
    assert [c.method for c in rec.calls] == ["POST", "PUT"]
    put = rec.calls[1]
    assert put.url == session_url and put.kwargs["timeout"] == 300.0
    assert set(put.kwargs) == {"headers", "content", "timeout"}
    assert "authorization" not in {name.lower() for name in put.kwargs["headers"]}
    assert "authorization" not in rec.requests[1].headers
    assert rec.requests[1].url.query == urllib.parse.urlsplit(session_url).query.encode()


def test_server_supplied_url_site9_still_refuses_a_session_url_with_userinfo(tmp_path, monkeypatch):
    with pytest.raises(ValueError):
        _drive_publish(tmp_path, monkeypatch, "https://user:pw@upload.example.com/s/abc")


# ═══════════════════════════════════════════════════════════════════════════════════════════════════
# The static guards PH-G1 .. PH-G5 (spec 4.5) and the control of the network guard (spec 6.3)
#
# Every checker below takes SOURCE TEXT plus a repo-relative path, so the very same function runs on the
# real tree (api/ engine/ worker/) and on small synthetic snippets: each rule has a snippet that must fire
# and an exclusion that must not. They are AST tripwires, not proofs (they do not see getattr(httpx, ...),
# importlib or a dynamically built module name; spec section 7).
#
#   PH-G1  raw httpx request calls, `.raise_for_status` and other HTTP clients stay in a short allowlist.
#   PH-G2  `api.platform_http` is imported as a module and its functions are only ever CALLED as attributes;
#          `server_supplied_url=True` exists once, in one function, with exactly the two upload headers.
#   PH-G3  (the runtime secret guard) lives above in this file.
#   PH-G4  every `platform_http.get/post/put` call is statically resolved: the URL text, `params=`,
#          `bearer=`, the call shape. Anything it cannot resolve fails closed.
#   PH-G5  the module's imports and public names.
# ═══════════════════════════════════════════════════════════════════════════════════════════════════

PLATFORM_HTTP = "api.platform_http"
PH_PATH = "api/platform_http.py"
OAUTH_PATH = "api/oauth.py"
YOUTUBE_PATH = "engine/publish/youtube.py"
INSTAGRAM_PATH = "engine/publish/instagram.py"
METRICS_PATH = "engine/publish/metrics.py"
LLM_PATH = "engine/generation/llm.py"
SAFE_FETCH_PATH = "engine/render/safe_fetch.py"
ASSET_SOURCER_PATH = "engine/render/asset_sourcer.py"
PINNED_CALLERS = sorted([OAUTH_PATH, YOUTUBE_PATH, INSTAGRAM_PATH, METRICS_PATH])
PH_FUNCS = ("get", "post", "put")
PH_CALLEES = frozenset(f"{PLATFORM_HTTP}.{name}" for name in PH_FUNCS)


@functools.lru_cache(maxsize=None)
def _sources() -> dict:
    return S.production_sources()


def _qualname(node) -> str:
    """`Class.method` (outer to inner) of the classes and functions that enclose `node`."""
    parts = []
    cur = getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(cur.name)
        cur = getattr(cur, "_parent", None)
    return ".".join(reversed(parts))


def _in_function_body(node) -> bool:
    """True when the nearest enclosing function (or lambda) runs `node` at call time: inside its BODY, not
    in a default, a decorator or an annotation (those run when the function is defined)."""
    child, cur = node, getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return child._field == "body"
        child, cur = cur, getattr(cur, "_parent", None)
    return False


def _platform_http_imports(tree, path):
    """(node, form) for every import that reaches api.platform_http: form 'module' (the module itself) or
    'names' (a from-import of something inside it)."""
    package = S.package_of(path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PLATFORM_HTTP or alias.name.startswith(PLATFORM_HTTP + "."):
                    found.append((node, "module"))
        elif isinstance(node, ast.ImportFrom):
            base = S._from_base(node, package)
            if base is None:
                continue
            if base == PLATFORM_HTTP or base.startswith(PLATFORM_HTTP + "."):
                found.append((node, "names"))
            elif base == "api" and any(alias.name == "platform_http" for alias in node.names):
                found.append((node, "module"))
    return found


def _is_module_expr(node, bindings) -> bool:
    """`node` (a Name or Attribute chain read) stands for the module api.platform_http itself."""
    if not isinstance(node, (ast.Name, ast.Attribute)):
        return False
    if not isinstance(node.ctx, ast.Load):
        return False
    return PLATFORM_HTTP in S.dotted(node, bindings)


def derive_callers(sources) -> list:
    """Every production file that imports api.platform_http (PH-G2's import walk), sorted. The list that
    PH-G4 checks is THIS one, never a hand-written one, so a new caller is checked from its first commit."""
    callers = []
    for path, source in sorted(sources.items()):
        if path == PH_PATH or "platform_http" not in source:
            continue
        if _platform_http_imports(S.parse(source, path), path):
            callers.append(path)
    return callers


def caller_pin_problems(sources) -> list:
    derived = derive_callers(sources)
    if derived != PINNED_CALLERS:
        return [S.Problem("<tree>", 0, "PH-G4", f"callers of api.platform_http are {derived}, pinned: {PINNED_CALLERS}")]
    return []


# ── PH-G1 confinement ──────────────────────────────────────────────────────────────────────────────

NON_REQUEST = frozenset({
    "QueryParams", "HTTPStatusError", "HTTPError", "TransportError", "TimeoutException", "InvalidURL",
    "Response", "URL", "Headers",
})
ALLOWED_REQUEST = frozenset({PH_PATH, LLM_PATH, SAFE_FETCH_PATH})
ALLOWED_RAISE = ALLOWED_REQUEST | {ASSET_SOURCER_PATH}
HTTP_CLIENT_MODULES = ("requests", "aiohttp", "urllib3", "http.client", "urllib.request")


def httpx_names(source, path) -> list:
    """(name, line) of every `httpx.<name>` reached through any alias (`import httpx as h`, `x = httpx`) and
    of every name from-imported out of httpx (`from httpx import post as p`)."""
    tree = S.parse(source, path)
    bindings = S.import_bindings(tree, path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            for dotted_path in S.dotted(node, bindings):
                parts = dotted_path.split(".")
                if len(parts) == 2 and parts[0] == "httpx":
                    found.append((parts[1], node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "httpx":
                found.extend((alias.name, node.lineno) for alias in node.names)
            elif node.module.startswith("httpx."):
                found.extend((f"{node.module[len('httpx.'):]}.{alias.name}", node.lineno) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend((alias.name[len("httpx."):], node.lineno) for alias in node.names
                         if alias.name.startswith("httpx."))
    return found


def http_client_imports(source, path) -> list:
    """(module, line) for every import of another HTTP client, in every spelling."""
    tree = S.parse(source, path)
    package = S.package_of(path)
    found = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = S._from_base(node, package)
            if base is not None:
                names = [base] + [f"{base}.{alias.name}" for alias in node.names if alias.name != "*"]
        for name in names:
            if any(name == module or name.startswith(module + ".") for module in HTTP_CLIENT_MODULES):
                found.append((name, node.lineno))
    return found


def raise_for_status_lines(source, path) -> list:
    tree = S.parse(source, path)
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "raise_for_status"]


def g1_problems(source, path) -> list:
    problems = []
    if path not in ALLOWED_REQUEST:
        for name, line in httpx_names(source, path):
            if name not in NON_REQUEST:
                problems.append(S.Problem(path, line, "PH-G1(a)", f"httpx.{name} is not on the non-request allowlist"))
        for module, line in http_client_imports(source, path):
            problems.append(S.Problem(path, line, "PH-G1(c)", f"import of the HTTP client {module}"))
    if path not in ALLOWED_RAISE:
        for line in raise_for_status_lines(source, path):
            problems.append(S.Problem(path, line, "PH-G1(b)", ".raise_for_status outside the request modules"))
    return problems


def g1_tree(sources) -> list:
    problems = []
    for path, source in sorted(sources.items()):
        problems.extend(g1_problems(source, path))
    return problems


def test_ph_g1_real_tree_is_confined():
    sources = _sources()
    assert g1_tree(sources) == []


def test_ph_g1_real_tree_non_vacuity():
    sources = _sources()
    for path in (PH_PATH, LLM_PATH, SAFE_FETCH_PATH, ASSET_SOURCER_PATH, *PINNED_CALLERS, "worker/tasks/common.py"):
        assert path in sources, f"the walk does not cover {path}"
    assert len(sources) > 50
    # outside the three allowed files the only httpx names in use are the non-request ones
    used_outside = {}
    for path, source in sources.items():
        if path in ALLOWED_REQUEST:
            continue
        names = sorted({name for name, _ in httpx_names(source, path)})
        if names:
            used_outside[path] = names
    assert used_outside == {
        OAUTH_PATH: ["QueryParams"],
        "worker/tasks/common.py": ["HTTPStatusError", "TransportError"],
    }
    for path in (YOUTUBE_PATH, INSTAGRAM_PATH, METRICS_PATH):
        assert httpx_names(sources[path], path) == [], path
    # ... and the three allowed files really do use request-capable names (the walk sees them)
    for path in sorted(ALLOWED_REQUEST):
        request_names = {name for name, _ in httpx_names(sources[path], path)} - NON_REQUEST
        assert request_names, f"the PH-G1 walk found no request-capable httpx name in {path}"
    assert {"get", "post", "put"} <= {name for name, _ in httpx_names(sources[PH_PATH], PH_PATH)}
    # raise_for_status: only in the request modules and the sourcers that sit on safe_fetch
    seen = {path for path, source in sources.items() if raise_for_status_lines(source, path)}
    assert seen == {PH_PATH, LLM_PATH, ASSET_SOURCER_PATH}
    # the one other HTTP client in the tree is urllib.request in safe_fetch (proxy detection)
    clients = {path: sorted({name for name, _ in http_client_imports(source, path)})
               for path, source in sources.items() if http_client_imports(source, path)}
    assert clients == {SAFE_FETCH_PATH: ["urllib.request"]}


_G1_HTTPX_NAMES_THAT_MUST_FIRE = [
    "Client", "AsyncClient", "request", "HTTPTransport", "Request", "stream", "get", "post", "put", "delete",
    "patch", "head", "options", "Timeout", "Limits", "MockTransport", "BaseTransport", "AsyncHTTPTransport",
    "Proxy", "Auth", "BasicAuth", "ConnectError", "ReadTimeout", "Cookies", "codes", "something_new",
]


@pytest.mark.parametrize("name", _G1_HTTPX_NAMES_THAT_MUST_FIRE)
def test_ph_g1_control_unknown_httpx_name_fires_in_every_form(name):
    forms = {
        "attribute": f"import httpx\nhttpx.{name}(u)\n",
        "import as": f"import httpx as h\nh.{name}(u)\n",
        "from-import": f"from httpx import {name}\n{name}(u)\n",
        "from-import as": f"from httpx import {name} as p\np(u)\n",
        "simple alias": f"import httpx\nx = httpx\nx.{name}(u)\n",
        "nested use": f"import httpx\nvalue = httpx.{name}.default\n",
        "inside a function": f"import httpx\ndef f(u):\n    return httpx.{name}(u)\n",
    }
    for form, source in forms.items():
        problems = g1_problems(source, "engine/publish/youtube.py")
        assert problems and all(p.rule == "PH-G1(a)" for p in problems), (form, problems)


def test_ph_g1_control_star_and_submodule_imports_fire():
    for source in ("from httpx import *\n", "import httpx._client\n", "from httpx._client import Client\n",
                   "import httpx.something\n"):
        assert g1_problems(source, "engine/foo.py"), source


@pytest.mark.parametrize("name", sorted(NON_REQUEST))
def test_ph_g1_control_non_request_names_pass_in_every_form(name):
    for source in (
        f"import httpx\nvalue = httpx.{name}\n",
        f"import httpx as h\nvalue = h.{name}\n",
        f"from httpx import {name}\n",
        f"from httpx import {name} as other\n",
        f"import httpx\ntry:\n    pass\nexcept httpx.{name}:\n    pass\n",
    ):
        assert g1_problems(source, "engine/publish/youtube.py") == [], source
    assert g1_problems("import httpx\n", "engine/foo.py") == []


def test_ph_g1_control_the_allowlist_is_exact():
    assert NON_REQUEST == {"QueryParams", "HTTPStatusError", "HTTPError", "TransportError", "TimeoutException",
                           "InvalidURL", "Response", "URL", "Headers"}
    for banned in ("Client", "AsyncClient", "request"):
        assert banned not in NON_REQUEST
    assert ALLOWED_REQUEST == {PH_PATH, LLM_PATH, SAFE_FETCH_PATH}
    assert ALLOWED_RAISE == ALLOWED_REQUEST | {ASSET_SOURCER_PATH}


def test_ph_g1_control_allowed_files_may_use_request_names_but_others_may_not():
    raw = "import httpx\nhttpx.post(u)\nhttpx.Client()\n"
    for path in sorted(ALLOWED_REQUEST):
        assert g1_problems(raw, path) == [], path
    for path in (ASSET_SOURCER_PATH, YOUTUBE_PATH, INSTAGRAM_PATH, METRICS_PATH, OAUTH_PATH, "worker/tasks/common.py",
                 "engine/new_module.py"):
        assert g1_problems(raw, path), path


def test_ph_g1_control_raise_for_status_only_in_the_request_modules():
    source = "def f(resp):\n    resp.raise_for_status()\n"
    for path in sorted(ALLOWED_RAISE):
        assert g1_problems(source, path) == [], path
    for path in (YOUTUBE_PATH, INSTAGRAM_PATH, METRICS_PATH, OAUTH_PATH, "worker/tasks/common.py", "api/routers/x.py"):
        problems = g1_problems(source, path)
        assert [p.rule for p in problems] == ["PH-G1(b)"], path
    # a bare reference, a call through another name and getattr-free attribute use are all the same attribute
    assert g1_problems("check = resp.raise_for_status\n", YOUTUBE_PATH)
    # asset_sourcer is only allowed the status check, not a raw request
    assert [p.rule for p in g1_problems("import httpx\nhttpx.get(u)\nr.raise_for_status()\n", ASSET_SOURCER_PATH)] == ["PH-G1(a)"]


@pytest.mark.parametrize("source", [
    "import requests", "import requests as r", "import requests.adapters", "from requests import get",
    "from requests import *", "import aiohttp", "import aiohttp as a", "from aiohttp import ClientSession",
    "import urllib3", "from urllib3 import PoolManager", "import http.client", "import http.client as hc",
    "from http import client", "from http import client as hc", "from http.client import HTTPSConnection",
    "import urllib.request", "from urllib import request", "from urllib import request as rq",
    "from urllib.request import urlopen", "import urllib.request as ur",
    "def f():\n    import requests\n", "try:\n    import requests\nexcept ImportError:\n    pass",
])
def test_ph_g1_control_other_http_clients_fire_in_every_form(source):
    problems = g1_problems(source + "\n", "engine/publish/youtube.py")
    assert problems and all(p.rule == "PH-G1(c)" for p in problems), (source, problems)
    assert g1_problems(source + "\n", SAFE_FETCH_PATH) == []


@pytest.mark.parametrize("source", [
    "import urllib.parse", "from urllib import parse", "from urllib.parse import quote", "import http",
    "from http import HTTPStatus", "import requestsx", "import urllib", "from urllib import error",
    "import http.cookies", "from . import requests", "import httpx",
])
def test_ph_g1_control_unrelated_imports_pass(source):
    assert g1_problems(source + "\n", "engine/publish/youtube.py") == [], source


# ── PH-G2 module-attribute access ──────────────────────────────────────────────────────────────────

G2_CONSTANTS = ("DEFAULT_TIMEOUT_S", "UPLOAD_TIMEOUT_S")
G2_OTHER_PUBLIC = ("SECRET_QUERY_NAMES",)
FLAG_SITE = (YOUTUBE_PATH, "YouTubePublisher.publish")


@dataclasses.dataclass
class G2Result:
    problems: list
    calls: list          # (path, qualname, fn, line) of every `platform_http.<fn>(...)` call
    flag_calls: list     # (path, qualname, line) of every call carrying a server_supplied_url keyword
    upload_reads: list   # (path, qualname, line) of every UPLOAD_TIMEOUT_S read
    imports_module: bool


def _flag_call_problems(call, path, bindings) -> list:
    out = []

    def bad(message):
        out.append(S.Problem(path, call.lineno, "PH-G2", message))

    flag = S._kw(call, "server_supplied_url")
    if not (isinstance(flag, ast.Constant) and flag.value is True):
        bad("server_supplied_url must be the literal True")
    func = call.func
    if not (isinstance(func, ast.Attribute) and func.attr == "put" and _is_module_expr(func.value, bindings)):
        bad("server_supplied_url only on a platform_http.put call")
    if any(keyword.arg is None for keyword in call.keywords):
        bad("a ** spread next to server_supplied_url")
    for name in ("bearer", "params"):
        if S._kw(call, name) is not None:
            bad(f"{name}= next to server_supplied_url")
    headers = S._kw(call, "headers")
    if not isinstance(headers, ast.Dict):
        bad("headers= must be a dict literal next to server_supplied_url")
    else:
        keys = [k.value if isinstance(k, ast.Constant) and isinstance(k.value, str) else None for k in headers.keys]
        if None in keys or sorted(keys) != ["Content-Length", "Content-Type"]:
            bad("headers= must have exactly the constant keys Content-Type and Content-Length")
    if (path, _qualname(call)) != FLAG_SITE:
        bad(f"server_supplied_url outside {FLAG_SITE[0]}::{FLAG_SITE[1]}")
    return out


def ph_g2_walk(source, path) -> G2Result:
    tree = S.parse(source, path)
    bindings = S.import_bindings(tree, path)
    result = G2Result([], [], [], [], False)
    imports = _platform_http_imports(tree, path)
    result.imports_module = bool(imports)
    for node, form in imports:
        if form == "names":
            result.problems.append(S.Problem(path, node.lineno, "PH-G2",
                                             "from-import of names out of api.platform_http: import the module"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and S._kw(node, "server_supplied_url") is not None:
            result.flag_calls.append((path, _qualname(node), node.lineno))
            result.problems.extend(_flag_call_problems(node, path, bindings))
        if not _is_module_expr(node, bindings):
            continue
        parent = node._parent
        if not (isinstance(parent, ast.Attribute) and node._field == "value"):
            result.problems.append(S.Problem(path, node.lineno, "PH-G2",
                                             "the module api.platform_http used as a value, not as `platform_http.<name>`"))
            continue
        attr = parent.attr
        if attr in PH_FUNCS:
            call = parent._parent
            if isinstance(call, ast.Call) and parent._field == "func":
                result.calls.append((path, _qualname(parent), attr, parent.lineno))
            else:
                result.problems.append(S.Problem(path, parent.lineno, "PH-G2",
                                                 f"platform_http.{attr} captured instead of called"))
        elif attr in G2_CONSTANTS:
            if not (isinstance(parent.ctx, ast.Load) and _in_function_body(parent)):
                result.problems.append(S.Problem(path, parent.lineno, "PH-G2",
                                                 f"platform_http.{attr} is only a read inside a function body"))
            if attr == "UPLOAD_TIMEOUT_S":
                result.upload_reads.append((path, _qualname(parent), parent.lineno))
        elif attr not in G2_OTHER_PUBLIC:
            result.problems.append(S.Problem(path, parent.lineno, "PH-G2", f"platform_http.{attr} is not public API"))
    return result


@functools.lru_cache(maxsize=None)
def _real_g2() -> dict:
    return {path: ph_g2_walk(source, path) for path, source in _sources().items() if path != PH_PATH}


def test_ph_g2_real_tree_is_clean():
    problems = [p for result in _real_g2().values() for p in result.problems]
    assert problems == []


def test_ph_g2_real_tree_non_vacuity():
    results = _real_g2()
    per_file = {}
    for path, result in results.items():
        for call in result.calls:
            per_file[path] = per_file.get(path, 0) + 1
    assert per_file == {OAUTH_PATH: 6, YOUTUBE_PATH: 3, INSTAGRAM_PATH: 3, METRICS_PATH: 2}
    assert sum(per_file.values()) == 14
    # exactly one server_supplied_url, at its one allowlisted function, and exactly one upload-constant read
    flags = [call for result in results.values() for call in result.flag_calls]
    assert [(p, q) for p, q, _ in flags] == [FLAG_SITE]
    reads = [read for result in results.values() for read in result.upload_reads]
    assert [(p, q) for p, q, _ in reads] == [FLAG_SITE]
    # the callers are exactly the files whose walk saw an import
    assert sorted(path for path, result in results.items() if result.imports_module) == PINNED_CALLERS
    # which verbs: 8 post, 4 get ... pinned by function and file
    verbs = sorted((path, fn) for result in results.values() for path, _, fn, _ in result.calls)
    assert verbs == sorted([
        (OAUTH_PATH, "post")] * 4 + [(OAUTH_PATH, "get")] * 2
        + [(YOUTUBE_PATH, "post")] * 2 + [(YOUTUBE_PATH, "put")]
        + [(INSTAGRAM_PATH, "post")] * 2 + [(INSTAGRAM_PATH, "get")]
        + [(METRICS_PATH, "get")] * 2)


_G2_IMPORT = "from api import platform_http\n"


def _g2_flag_site(call_source, prefix=_G2_IMPORT):
    return (prefix + "class YouTubePublisher:\n    def publish(self, u, c):\n        " + call_source + "\n")


_G2_OK_CALL = ('platform_http.put(u, headers={"Content-Type": "video/mp4", "Content-Length": "3"}, content=c, '
               "server_supplied_url=True)")


def test_ph_g2_control_the_valid_site_passes():
    result = ph_g2_walk(_g2_flag_site(_G2_OK_CALL), YOUTUBE_PATH)
    assert result.problems == []
    assert [(c[2]) for c in result.calls] == ["put"]
    assert [(p, q) for p, q, _ in result.flag_calls] == [FLAG_SITE]
    # key order and case of the dict do not matter; the timeout read inside the function is fine
    reordered = ('platform_http.put(u, headers={"Content-Length": "3", "Content-Type": "x"}, content=c, '
                 "timeout=platform_http.UPLOAD_TIMEOUT_S, server_supplied_url=True)")
    result = ph_g2_walk(_g2_flag_site(reordered), YOUTUBE_PATH)
    assert result.problems == [] and len(result.upload_reads) == 1


@pytest.mark.parametrize("source, path", [
    ("import api.platform_http as ph\ndef f(u):\n    return ph.get(u)\n", OAUTH_PATH),
    ("import api.platform_http\ndef f(u):\n    return api.platform_http.post(u)\n", OAUTH_PATH),
    ("from api import platform_http as ph\ndef f(u):\n    return ph.put(u)\n", YOUTUBE_PATH),
    ("from api import platform_http\ndef f(u):\n    return platform_http.get(u)\n", METRICS_PATH),
    ("from . import platform_http\ndef f(u):\n    return platform_http.get(u)\n", OAUTH_PATH),
    ("from api import platform_http, config\ndef f(u):\n    return platform_http.get(u)\n", OAUTH_PATH),
    ("from api import platform_http\ndef f():\n    return platform_http.DEFAULT_TIMEOUT_S\n", OAUTH_PATH),
    ("from api import platform_http\nf = lambda: platform_http.UPLOAD_TIMEOUT_S\n", OAUTH_PATH),
    ("from api import platform_http\ndef f():\n    return platform_http.SECRET_QUERY_NAMES\n", OAUTH_PATH),
    ("from api import platform_http\nclass C:\n    def m(self, u):\n        return platform_http.post(u)\n", OAUTH_PATH),
])
def test_ph_g2_control_allowed_forms_pass(source, path):
    result = ph_g2_walk(source, path)
    assert result.problems == [], result.problems
    assert result.imports_module


@pytest.mark.parametrize("source", [
    "from api.platform_http import get\n",
    "from api.platform_http import get as g\n",
    "from api.platform_http import post, put\n",
    "from api.platform_http import *\n",
    "from api.platform_http import DEFAULT_TIMEOUT_S\n",
    "from api.platform_http import SECRET_QUERY_NAMES\n",
    "from api.platform_http import _normalise\n",
    "from .platform_http import get\n",
    "def f():\n    from api.platform_http import get\n",
])
def test_ph_g2_control_every_forbidden_import_form_fires(source):
    result = ph_g2_walk(source, OAUTH_PATH)
    assert result.problems and result.imports_module, source


@pytest.mark.parametrize("body", [
    "x = platform_http.get",
    "a, b = platform_http.get, platform_http.post",
    "f = platform_http.get if c else platform_http.post",
    "f = c and platform_http.get",
    "h = [platform_http.get]",
    "h = {'g': platform_http.get}",
    "return platform_http.post",
    "functools.partial(platform_http.get, u)",
    "m = platform_http",
    "g = getattr(platform_http, 'get')",
    "h = platform_http.get.__call__",
    "platform_http()",
    "x = platform_http._normalise('a')",
    "x = platform_http.__dict__",
    "x = platform_http.something_new",
    "platform_http.DEFAULT_TIMEOUT_S = 5",
    "platform_http.get = c",
    "del platform_http.put",
])
def test_ph_g2_control_captures_and_non_callee_uses_fire(body):
    source = _G2_IMPORT + "def f(u, c):\n    " + body + "\n"
    assert ph_g2_walk(source, OAUTH_PATH).problems, body
    # the same line with an `import ... as` alias
    source = "import api.platform_http as ph\n" + "def f(u, c):\n    " + body.replace("platform_http", "ph") + "\n"
    assert ph_g2_walk(source, OAUTH_PATH).problems, body


def test_ph_g2_control_a_default_argument_and_a_decorator_are_captures():
    for source in (
        _G2_IMPORT + "def f(g=platform_http.get):\n    pass\n",
        _G2_IMPORT + "@platform_http.get\ndef f():\n    pass\n",
        _G2_IMPORT + "class C(platform_http.get):\n    pass\n",
    ):
        assert ph_g2_walk(source, OAUTH_PATH).problems, source


@pytest.mark.parametrize("source", [
    _G2_IMPORT + "T = platform_http.UPLOAD_TIMEOUT_S\n",
    _G2_IMPORT + "T = platform_http.DEFAULT_TIMEOUT_S\n",
    _G2_IMPORT + "def f(t=platform_http.UPLOAD_TIMEOUT_S):\n    pass\n",
    _G2_IMPORT + "class C:\n    t = platform_http.UPLOAD_TIMEOUT_S\n",
    _G2_IMPORT + "def f(t: platform_http.UPLOAD_TIMEOUT_S):\n    pass\n",
    _G2_IMPORT + "@dec(platform_http.UPLOAD_TIMEOUT_S)\ndef f():\n    pass\n",
    _G2_IMPORT + "def f():\n    platform_http.UPLOAD_TIMEOUT_S = 5\n",
    _G2_IMPORT + "def f():\n    del platform_http.UPLOAD_TIMEOUT_S\n",
])
def test_ph_g2_control_the_constants_are_reads_inside_a_function_only(source):
    assert ph_g2_walk(source, OAUTH_PATH).problems, source


@pytest.mark.parametrize("call, why", [
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=flag)', "a variable"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=1)', "=1"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=False)', "False"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url="yes")', "str"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=not False)', "expr"),
    ('platform_http.get(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "get"),
    ('platform_http.post(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "post"),
    ('platform_http.put(u, bearer=t, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "bearer"),
    ('platform_http.put(u, params={}, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "params"),
    ('platform_http.put(u, params={"part": "x"}, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "params2"),
    ('platform_http.put(u, headers={"Authorization": "x", "Content-Length": "1"}, server_supplied_url=True)', "Authorization"),
    ('platform_http.put(u, headers={"Cookie": "a"}, server_supplied_url=True)', "Cookie"),
    ('platform_http.put(u, headers={"Accept": "a"}, server_supplied_url=True)', "Accept"),
    ('platform_http.put(u, headers=hdrs, server_supplied_url=True)', "headers=hdrs"),
    ('platform_http.put(u, server_supplied_url=True)', "no headers"),
    ('platform_http.put(u, headers=None, server_supplied_url=True)', "headers=None"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1", "X-Foo": "b"}, server_supplied_url=True)', "extra benign key"),
    ('platform_http.put(u, headers={"Content-Type": "a"}, server_supplied_url=True)', "only Content-Type"),
    ('platform_http.put(u, headers={"Content-Length": "1"}, server_supplied_url=True)', "only Content-Length"),
    ('platform_http.put(u, headers={**hdrs, "Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "spread in dict"),
    ('platform_http.put(u, headers={k: "a", "Content-Length": "1"}, server_supplied_url=True)', "non-constant key"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Type": "b"}, server_supplied_url=True)', "duplicate key"),
    ('platform_http.put(u, headers={"content-type": "a", "content-length": "1"}, server_supplied_url=True)', "lower case keys"),
    ('platform_http.put(u, headers={b"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "bytes key"),
    ('platform_http.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True, **opts)', "call spread"),
    ('httpx.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "other callee"),
    ('other.put(u, headers={"Content-Type": "a", "Content-Length": "1"}, server_supplied_url=True)', "other module"),
])
def test_ph_g2_control_every_bad_server_supplied_url_shape_fires(call, why):
    result = ph_g2_walk(_g2_flag_site(call), YOUTUBE_PATH)
    assert result.problems, why
    assert len(result.flag_calls) == 1


def test_ph_g2_control_a_valid_shape_in_the_wrong_place_fires():
    # the exact allowed call, but in another function / another class / another file
    other_function = (_G2_IMPORT + "class YouTubePublisher:\n    def other(self, u, c):\n        " + _G2_OK_CALL + "\n")
    other_class = (_G2_IMPORT + "class Other:\n    def publish(self, u, c):\n        " + _G2_OK_CALL + "\n")
    module_level = _G2_IMPORT + "def publish(u, c):\n    " + _G2_OK_CALL + "\n"
    nested = (_G2_IMPORT + "class YouTubePublisher:\n    def publish(self, u, c):\n        def inner():\n            "
              + _G2_OK_CALL + "\n")
    for source in (other_function, other_class, module_level, nested):
        assert ph_g2_walk(source, YOUTUBE_PATH).problems, source
    for path in (OAUTH_PATH, INSTAGRAM_PATH, METRICS_PATH, "engine/publish/other.py"):
        assert ph_g2_walk(_g2_flag_site(_G2_OK_CALL), path).problems, path
    # a second call with the flag in the allowlisted function: each is judged, the count is pinned elsewhere
    twice = (_G2_IMPORT + "class YouTubePublisher:\n    def publish(self, u, c):\n        " + _G2_OK_CALL
             + "\n        " + _G2_OK_CALL + "\n")
    assert ph_g2_walk(twice, YOUTUBE_PATH).problems == []
    assert len(ph_g2_walk(twice, YOUTUBE_PATH).flag_calls) == 2


def test_ph_g2_control_the_flag_in_a_non_call_position_cannot_hide():
    # a keyword named server_supplied_url on ANY call is judged, not only on platform_http calls
    for source in ("def f(u):\n    return anything(u, server_supplied_url=True)\n",
                   "class A:\n    def m(self):\n        self.helper(server_supplied_url=True)\n"):
        assert ph_g2_walk(source, "engine/x.py").problems, source


# ── PH-G4 static call-site check ───────────────────────────────────────────────────────────────────
#
# Written new (no precedent in tests/_seam_ast.py): the module-binding multiplicity counter
# (`_file_binding_count`), the class-attribute resolver (`_G4File.self_attr`), the JoinedStr flattener
# (`_G4File.flatten`), the authority splitter (`_url_text_problems`), the call-shape checker and the
# per-callee `upload_url` allowlist (`_G4File.resolve_url`). Only `_normalise` is imported from the module
# under test (through `platform_http`), for the `params=` keys; the secret NAMES stay the literal list
# `SECRET_NAMES` of this file.

_ID = "\x00"                      # stands for an allowed id hole inside a flattened URL
_SECRET_SET = frozenset(SECRET_NAMES)
_TYPE_PARAMS = tuple(getattr(ast, name) for name in ("TypeVar", "ParamSpec", "TypeVarTuple") if hasattr(ast, name))


def _id_dump(expression: str) -> str:
    return ast.dump(ast.parse(expression, mode="eval").body)


# (file, function) -> the exact expressions that may fill an `{...}` hole of an f-string URL, as ast.dump
PINNED_IDS = {
    (OAUTH_PATH, "InstagramOAuth.discover_account"): frozenset({_id_dump("page_id")}),
    (INSTAGRAM_PATH, "InstagramPublisher._create_container"): frozenset({_id_dump("ig_user_id")}),
    (INSTAGRAM_PATH, "InstagramPublisher._publish_container"): frozenset({_id_dump("ig_user_id")}),
    (INSTAGRAM_PATH, "InstagramPublisher._wait_until_ready"): frozenset({_id_dump("creation_id")}),
    (METRICS_PATH, "InstagramMetricsFetcher.fetch"): frozenset({_id_dump("cut.platform_post_id")}),
}
# (file, function, callee): the one place a bare `upload_url` Name may be the URL (Google's session URL)
UPLOAD_URL_SITE = (YOUTUBE_PATH, "YouTubePublisher.publish", "put")


class _Unresolvable(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class CallInfo:
    path: str
    qualname: str
    fn: str
    line: int
    url_kind: object          # constant | fstring | self_attr | name | upload_url | None (unresolved)
    params_dict: bool
    bearer: bool


@dataclasses.dataclass
class G4Result:
    problems: list
    calls: list


def _bindings_in_node(node, name, annotation_only) -> int:
    """How many times `node` itself binds `name` (every binding form of spec 4.5)."""
    if isinstance(node, ast.Name):
        stores = isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in annotation_only
        return int(node.id == name and stores)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return int(node.name == name)
    if isinstance(node, ast.arg):
        return int(node.arg == name)
    if isinstance(node, ast.ExceptHandler):
        return int(node.name == name)
    if isinstance(node, (ast.MatchAs, ast.MatchStar)):
        return int(node.name == name)
    if isinstance(node, ast.MatchMapping):
        return int(node.rest == name)
    if isinstance(node, ast.alias):
        return int((node.asname or node.name.split(".")[0]) == name)
    if _TYPE_PARAMS and isinstance(node, _TYPE_PARAMS):
        return int(node.name == name)
    return 0


def _annotation_only_targets(tree) -> set:
    return {id(node.target) for node in ast.walk(tree) if isinstance(node, ast.AnnAssign) and node.value is None}


def _file_binding_count(tree, name) -> int:
    """Every binding of `name` anywhere in the file, in any scope."""
    annotation_only = _annotation_only_targets(tree)
    return sum(_bindings_in_node(node, name, annotation_only) for node in ast.walk(tree))


def _class_binding_count(classdef, name) -> int:
    """Bindings of `name` in the class body's own scope (nested functions, classes and lambdas are not entered,
    but their names count; annotation-only does not)."""
    annotation_only = _annotation_only_targets(classdef)
    count = 0
    stack = list(classdef.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            count += int(node.name == name)
            continue
        if isinstance(node, ast.Lambda):
            continue
        count += _bindings_in_node(node, name, annotation_only)
        stack.extend(ast.iter_child_nodes(node))
    return count


def _is_str_constant(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def _assigned_string(statement, name):
    """The string constant `statement` binds to `name` (a plain `name = "..."` / `name: str = "..."`), else None."""
    if (isinstance(statement, ast.Assign) and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == name
            and _is_str_constant(statement.value)):
        return statement.value.value
    if (isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name)
            and statement.target.id == name and statement.value is not None and _is_str_constant(statement.value)):
        return statement.value.value
    return None


def _attribute_store_problem(tree, attr) -> bool:
    """A store through ANY receiver (`self.`, `cls.`, `ClassName.`, ...) or a setattr on this attribute."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == attr and isinstance(node.ctx, (ast.Store, ast.Del)):
            return True
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in ("setattr", "delattr") and len(node.args) >= 2):
            name_arg = node.args[1]
            if not _is_str_constant(name_arg) or name_arg.value == attr:
                return True
    return False


def _url_text_problems(text) -> list:
    out = []
    if not text.startswith("https://"):
        out.append("the URL does not start with https://")
    for char in "?;#":
        if char in text:
            out.append(f"the URL text contains {char!r} (a query is only ever added through params=)")
    head = text.split(_ID)[0]
    rest = head[len("https://"):] if head.startswith("https://") else head
    if _ID in text and "/" not in rest:
        out.append("an id hole comes before the '/' that ends the authority: it could extend the host")
    if "@" in rest.split("/", 1)[0]:
        out.append("userinfo ('@') in the authority")
    return out


class _G4File:
    def __init__(self, source, path):
        self.path = path
        self.tree = S.parse(source, path)
        self.bindings = S.import_bindings(self.tree, path)
        self.star = [node for node in ast.walk(self.tree)
                     if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)]

    def callee(self, call):
        for dotted_path in S.dotted(call.func, self.bindings):
            if dotted_path in PH_CALLEES:
                return dotted_path.rsplit(".", 1)[1]
        return None

    def module_string(self, name) -> str:
        """The text of the module-level string constant `name`: exactly ONE binding in the whole file."""
        if self.star:
            raise _Unresolvable("a star import in this file could bind any name")
        if _file_binding_count(self.tree, name) != 1:
            raise _Unresolvable(f"{name} is not bound exactly once in the file")
        for statement in self.tree.body:
            value = _assigned_string(statement, name)
            if value is not None:
                return value
        raise _Unresolvable(f"{name} is not a module-level string constant")

    def self_attr(self, node, call) -> str:
        """`self.<attr>` resolved to the ENCLOSING CLASS BODY only (bases are not consulted)."""
        if not (isinstance(node.value, ast.Name) and node.value.id == "self"):
            raise _Unresolvable("only self.<attr> is resolved")
        classdef = S._class_of(call)
        if classdef is None:
            raise _Unresolvable("self.<attr> outside a class")
        if _attribute_store_problem(self.tree, node.attr):
            raise _Unresolvable(f"{node.attr} is stored through an attribute or setattr in this file")
        if _class_binding_count(classdef, node.attr) != 1:
            raise _Unresolvable(f"{node.attr} is not bound exactly once in the class body")
        for statement in classdef.body:
            value = _assigned_string(statement, node.attr)
            if value is not None:
                return value
        raise _Unresolvable(f"{node.attr} is not a string constant in the class body")

    def flatten(self, node, qualname) -> str:
        pinned = PINNED_IDS.get((self.path, qualname), frozenset())
        text = ""
        for part in node.values:
            if isinstance(part, ast.Constant):
                text += part.value
            elif isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    raise _Unresolvable("a hole with a conversion or a format spec")
                if ast.dump(part.value) in pinned:
                    text += _ID
                elif isinstance(part.value, ast.Name):
                    text += self.module_string(part.value.id)
                else:
                    raise _Unresolvable(f"the hole {ast.unparse(part.value)!r} is neither a module constant nor a pinned id")
            else:
                raise _Unresolvable("unexpected part of an f-string")
        return text

    def resolve_url(self, node, call, qualname, fn):
        """(text or None, kind). Anything else is `_Unresolvable`: fail closed."""
        if _is_str_constant(node):
            return node.value, "constant"
        if isinstance(node, ast.JoinedStr):
            return self.flatten(node, qualname), "fstring"
        if isinstance(node, ast.Name):
            if node.id == "upload_url" and (self.path, qualname, fn) == UPLOAD_URL_SITE:
                return None, "upload_url"
            return self.module_string(node.id), "name"
        if isinstance(node, ast.Attribute):
            return self.self_attr(node, call), "self_attr"
        raise _Unresolvable(f"cannot resolve a {type(node).__name__} URL")


def _params_problems(node) -> list:
    """What is wrong with a `params=` expression (empty list: nothing). Only literal shapes are accepted."""
    keys = []
    if isinstance(node, ast.Dict):
        for key in node.keys:
            if key is None:
                return ["params= has a ** spread"]
            keys.append(key)
    elif isinstance(node, (ast.List, ast.Tuple)):
        for element in node.elts:
            if not (isinstance(element, (ast.Tuple, ast.List)) and len(element.elts) == 2):
                return ["params= is a sequence whose items are not literal pairs"]
            keys.append(element.elts[0])
    elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict"
          and not node.args and all(keyword.arg is not None for keyword in node.keywords)):
        keys = [ast.Constant(value=keyword.arg) for keyword in node.keywords]
    else:
        return [f"params= is not a literal dict / list of pairs / dict(k=v): {ast.unparse(node)!r}"]
    out = []
    for key in keys:
        if not _is_str_constant(key):
            out.append("params= has a key that is not a string constant")
        elif platform_http._normalise(key.value) in _SECRET_SET:
            out.append(f"params= has the secret-named key {key.value!r}")
    return out


def ph_g4_check(source, path) -> G4Result:
    f = _G4File(source, path)
    problems, calls = [], []

    def fail(node, rule, message):
        problems.append(S.Problem(path, node.lineno, rule, message))

    for node in f.star:
        fail(node, "PH-G4/star", "a star import in a platform_http caller: names cannot be resolved")
    for call in ast.walk(f.tree):
        if not isinstance(call, ast.Call):
            continue
        fn = f.callee(call)
        if fn is None:
            continue
        qualname = _qualname(call)
        # call shape: the URL is the one positional argument or the url= keyword; no *args, no **kwargs
        if any(isinstance(arg, ast.Starred) for arg in call.args):
            fail(call, "PH-G4/shape", "a *args spread")
        if any(keyword.arg is None for keyword in call.keywords):
            fail(call, "PH-G4/shape", "a **kwargs spread")
        if len(call.args) > 1:
            fail(call, "PH-G4/shape", "more than one positional argument")
        url_keyword = S._kw(call, "url")
        if call.args and url_keyword is not None:
            fail(call, "PH-G4/shape", "the URL is given twice (positional and url=)")
        url_node = call.args[0] if call.args else url_keyword
        text, kind = None, None
        if url_node is None:
            fail(call, "PH-G4/shape", "no URL argument")
        else:
            try:
                text, kind = f.resolve_url(url_node, call, qualname, fn)
            except _Unresolvable as exc:
                fail(call, "PH-G4/url", f"unresolvable URL: {exc}")
            else:
                if text is not None:
                    for message in _url_text_problems(text):
                        fail(call, "PH-G4/url", message)
        bearer = S._kw(call, "bearer") is not None
        if bearer:
            if text is None or not text.startswith("https://"):
                fail(call, "PH-G4/bearer", "bearer= needs a URL that statically resolves to https://")
            headers = S._kw(call, "headers")
            if isinstance(headers, ast.Dict) and any(
                    _is_str_constant(key) and key.value.lower() == "authorization" for key in headers.keys):
                fail(call, "PH-G4/bearer", "an Authorization header next to bearer=")
        params = S._kw(call, "params")
        if params is not None:
            for message in _params_problems(params):
                fail(call, "PH-G4/params", message)
        calls.append(CallInfo(path, qualname, fn, call.lineno, kind, isinstance(params, ast.Dict), bearer))
    return G4Result(problems, calls)


def g4_tree(sources) -> G4Result:
    result = G4Result([], [])
    for path in derive_callers(sources):
        one = ph_g4_check(sources[path], path)
        result.problems.extend(one.problems)
        result.calls.extend(one.calls)
    return result


@functools.lru_cache(maxsize=None)
def _real_g4() -> G4Result:
    return g4_tree(_sources())


def test_ph_g4_caller_list_is_derived_and_pinned():
    sources = _sources()
    assert derive_callers(sources) == PINNED_CALLERS
    assert caller_pin_problems(sources) == []
    # a fifth module that imports it: found by the derivation, and the pin fails until it is added on purpose
    extra = dict(sources)
    extra["engine/publish/tiktok_new.py"] = 'from api import platform_http\nplatform_http.get(u, params={"access_token": t})\n'
    assert derive_callers(extra) == sorted(PINNED_CALLERS + ["engine/publish/tiktok_new.py"])
    assert caller_pin_problems(extra)
    # ... and it is CHECKED from its first commit (not only rejected by the pin)
    assert any(p.path == "engine/publish/tiktok_new.py" for p in g4_tree(extra).problems)
    # every import spelling makes a caller
    for spelling in ("import api.platform_http as ph\n", "from api import platform_http as ph\n",
                     "import api.platform_http\n", "from api.platform_http import get\n"):
        assert derive_callers({"engine/new.py": spelling}) == ["engine/new.py"], spelling
    assert derive_callers({"engine/new.py": "import httpx\n", "api/platform_http.py": "import httpx\n"}) == []


def test_ph_g4_real_tree_is_clean():
    assert _real_g4().problems == []


def test_ph_g4_real_tree_non_vacuity_by_kind():
    calls = _real_g4().calls
    assert len(calls) == 14
    kinds = {}
    for call in calls:
        kinds[call.url_kind] = kinds.get(call.url_kind, 0) + 1
    # sites 5 and 13 / 6, 10, 11, 12 and 14 / 1-4 / 7 and 8 / 9
    assert kinds == {"constant": 2, "fstring": 5, "self_attr": 4, "name": 2, "upload_url": 1}
    assert sum(1 for call in calls if call.params_dict) == 6          # sites 6, 7, 8, 11, 13, 14
    assert sum(1 for call in calls if call.bearer) == 7               # sites 5, 6, 7, 8, 11, 13, 14
    by_file = {}
    for call in calls:
        by_file[call.path] = by_file.get(call.path, 0) + 1
    assert by_file == {OAUTH_PATH: 6, YOUTUBE_PATH: 3, INSTAGRAM_PATH: 3, METRICS_PATH: 2}
    assert sorted((c.path, c.qualname) for c in calls if c.url_kind == "self_attr") == sorted([
        (OAUTH_PATH, "YouTubeOAuth.exchange_code"), (OAUTH_PATH, "YouTubeOAuth.refresh"),
        (OAUTH_PATH, "InstagramOAuth.exchange_code"), (OAUTH_PATH, "InstagramOAuth.exchange_long_lived_token")])
    assert [(c.path, c.qualname, c.fn) for c in calls if c.url_kind == "upload_url"] == [
        (YOUTUBE_PATH, "YouTubePublisher.publish", "put")]
    assert sorted(c.qualname for c in calls if c.url_kind == "fstring") == sorted([
        "InstagramOAuth.discover_account", "InstagramPublisher._create_container",
        "InstagramPublisher._wait_until_ready", "InstagramPublisher._publish_container",
        "InstagramMetricsFetcher.fetch"])


# the synthetic harness ------------------------------------------------------------------------------

_G4_PRELUDE = (
    "from api import platform_http\n"
    '_GRAPH = "https://graph.facebook.com/v19.0"\n'
    '_HOST = "https://graph.facebook.com"\n'
)
_G4_PARAMS = "self, ig_user_id, creation_id, access_token, page_id, t, u, k, q, x, b, h, p, base, args, opts, other, cut"


def g4_in_method(body, *, prelude="", cls="InstagramPublisher", fn="_create_container"):
    indented = "\n".join("        " + line for line in body.splitlines())
    return f"{_G4_PRELUDE}{prelude}class {cls}:\n    def {fn}({_G4_PARAMS}):\n{indented}\n"


def g4_rules(source, path=INSTAGRAM_PATH):
    return sorted({p.rule for p in ph_g4_check(source, path).problems})


def _fires(source, path=INSTAGRAM_PATH, rule=None, text=None):
    problems = ph_g4_check(source, path).problems
    assert problems, f"expected PH-G4 to fire on:\n{source}"
    if rule is not None:
        assert rule in {p.rule for p in problems}, (rule, problems)
    if text is not None:
        assert any(text in p.message for p in problems), (text, problems)
    return problems


def _passes(source, path=INSTAGRAM_PATH):
    result = ph_g4_check(source, path)
    assert result.problems == [], result.problems
    return result


def test_ph_g4_control_clean_baselines_pass():
    for body in (
        'platform_http.post(f"{_GRAPH}/{ig_user_id}/media", data={"access_token": t, "caption": q})',
        'platform_http.get(f"{_GRAPH}/{ig_user_id}", params={"fields": "access_token"}, bearer=t)',
        'platform_http.get("https://graph.facebook.com/v19.0/me/accounts", bearer=t)',
        'platform_http.get(url="https://graph.facebook.com/v19.0/me/accounts", bearer=t)',
        'platform_http.get(_GRAPH, params={"part": "x", "id": q})',
        'platform_http.get(_GRAPH, params=[("part", "x"), ("id", q)])',
        'platform_http.get(_GRAPH, params=(("part", "x"),))',
        'platform_http.get(_GRAPH, params=dict(part="x", id=q))',
        'platform_http.get(f"{_HOST}/v19.0/{ig_user_id}")',
        'platform_http.post(f"{_GRAPH}/{ig_user_id}/media_publish", data={"creation_id": q}, json={"access_token": t})',
        'platform_http.put(_GRAPH, headers={"Authorization": "x"}, content=b)',     # no bearer: the runtime rule 7 only
    ):
        result = _passes(g4_in_method(body))
        assert len(result.calls) == 1, body


@pytest.mark.parametrize("params", [
    '{"access_token": t}', '{"part": "x", "access_token": t}', '{"accessToken": t}', '{"Access-Token": t}',
    '{"%61ccess_token": t}', '{"%2561ccess_token": t}', '{"access_token ": t}', '{"access_token[]": t}',
    '{"client_secret": t}', '{"x-goog-api-key": t}', '{"key": t}', '{"code": t}', '{"sig": t}', '{"jwt": t}',
    '[("access_token", t)]', '(("part", "x"), ("refresh_token", t))', '[["access_token", t]]',
    "dict(access_token=t)", "dict(part='x', client_secret=t)", "dict(**x)", "dict(**x, part='x')", "dict(pairs)",
    "dict([('part', 'x')])", "{**base, 'part': 'x'}", "{**base}", "{k: t}", "{'part': 'x', k: t}", "[(k, t)]",
    "[k]", "[('a', 'b', 'c')]", "p", "base", "x.params", "{'a': 1}.copy()", "f(p)", "dict", "'access_token=1'",
    "b'part=x'", "{1: t}", "[(1, t)]", "[('part', 'x'), p]", "list(p)",
])
def test_ph_g4_control_bad_params_fire(params):
    _fires(g4_in_method(f"platform_http.get(_GRAPH, params={params})"), rule="PH-G4/params")


def test_ph_g4_control_data_headers_json_and_secret_values_are_not_params():
    _passes(g4_in_method('platform_http.post(_GRAPH, data={"access_token": t, "client_secret": q}, '
                         'json={"refresh_token": t}, content=b, headers={"X-Api-Key": t})'))
    _passes(g4_in_method('platform_http.get(_GRAPH, params={"fields": "access_token", "part": "bearer"})'))


@pytest.mark.parametrize("url, text", [
    ('f"{u}?access_token={t}"', None),
    ('f"{b}?{k}={t}"', None),
    ('f"{u}?access_{x}=1"', None),
    ('f"{base}access_token=1"', None),
    ('f"https://{h}/x"', None),
    ('f"{q}"', None),
    ('f"{_GRAPH}/{x}"', None),
    ('f"https://graph.facebook.com{page_id}/x"', None),
    ('f"https://graph.facebook.com{ig_user_id}/x"', "authority"),
    ('f"{_HOST}{ig_user_id}/x"', "authority"),
    ('f"{_HOST}{ig_user_id}"', "authority"),
    ('f"{_GRAPH}/{page_id!r}"', None),
    ('f"{_GRAPH}/{ig_user_id!r}"', "conversion"),
    ('f"{_GRAPH}/{ig_user_id!s}"', "conversion"),
    ('f"{_GRAPH}/{ig_user_id!a}"', "conversion"),
    ('f"{_GRAPH}/{page_id:{q}}"', None),
    ('f"{_GRAPH}/{ig_user_id:{q}}"', "format spec"),
    ('f"{_GRAPH}/{ig_user_id:?<40}"', "format spec"),
    ('f"{_GRAPH}/{ig_user_id:>10}"', "format spec"),
    ('f"{_GRAPH!r}/{ig_user_id}"', "conversion"),
    ('f"{_GRAPH:>5}/{ig_user_id}"', "format spec"),
    ('f"{_GRAPH}/{other.platform_post_id}"', None),
    ('f"{_GRAPH}/{cut.reel_id}"', None),
    ('f"{_GRAPH}/{cut.platform_post_id}"', None),               # pinned only in InstagramMetricsFetcher.fetch
    ('f"{_GRAPH}/{page_id}"', None),                            # pinned only in InstagramOAuth.discover_account
    ('f"{_GRAPH}/{creation_id}"', None),                        # pinned only in _wait_until_ready
    ('f"{_GRAPH}/{page_id.strip()}"', None),
    ('f"{_GRAPH}/{ig_user_id.strip()}"', None),
    ('f"{_GRAPH}/{ig_user_id + q}"', None),
    ('f"{_GRAPH}/{ig_user_id}{q}"', None),
    ('f"{_GRAPH}/{ig_user_id}/{x}"', None),
    ('f"{_GRAPH}/{str(ig_user_id)}"', None),
    ('f"{_GRAPH}/{ig_user_id[0]}"', None),
    ('f"{_GRAPH}/{self.ig_user_id}"', None),
    ('f"{_GRAPH}/{ig_user_id}?access_token={t}"', None),
    ('f"{_GRAPH}/{ig_user_id}?{q}"', None),
    ('f"{_GRAPH}/{ig_user_id}?x=1"', "'?'"),
    ('"https://graph.facebook.com/x?accessToken=1"', "'?'"),
    ('"https://graph.facebook.com/x?"', "'?'"),
    ('"https://graph.facebook.com/x;key=1"', "';'"),
    ('"https://graph.facebook.com/x#x"', "'#'"),
    ('"https://graph.facebook.com/x#"', "'#'"),
    ('"https://u:p@graph.facebook.com/x"', "userinfo"),
    ('"https://u@graph.facebook.com/x"', "userinfo"),
    ('"https://graph.facebook.com@evil.example/x"', "userinfo"),
    ('"https://@graph.facebook.com"', "userinfo"),
    ('f"https://u:p@graph.facebook.com/{ig_user_id}"', "userinfo"),
    ('"http://graph.facebook.com/x"', "https://"),
    ('"ftp://graph.facebook.com/x"', "https://"),
    ('"graph.facebook.com/x"', "https://"),
    ('"//graph.facebook.com/x"', "https://"),
    ('"HTTPS://graph.facebook.com/x"', "https://"),
    ('""', "https://"),
    ('"https:/graph.facebook.com/x"', "https://"),
    ("u", None),
    ("x.y", None),
    ("self.nothing", None),
    ("cls.token_url", None),
    ("url_from_somewhere()", None),
    ('"https://graph.facebook.com/x" + q', None),
    ('"https://graph.facebook.com/%s" % q', None),
    ('"https://graph.facebook.com/{}".format(q)', None),
    ('_GRAPH + "/x"', None),
    ("(q or _GRAPH)", None),
    ('(_GRAPH if q else _HOST)', None),
    ("_MISSING", None),
])
def test_ph_g4_control_bad_urls_fire(url, text):
    _fires(g4_in_method(f"platform_http.get({url})"), text=text)
    _fires(g4_in_method(f"platform_http.get({url}, bearer=t)"))
    _fires(g4_in_method(f"platform_http.post(url={url}, data=b)"))


def test_ph_g4_control_a_url_built_in_a_variable_fires():
    _fires(g4_in_method('u = f"{x}?access_token={t}"\nplatform_http.get(u)'), rule="PH-G4/url")
    _fires(g4_in_method('u = "https://graph.facebook.com/x?access_token=1"\nplatform_http.get(u, bearer=t)'))
    _fires(g4_in_method('u = f"{_GRAPH}/{ig_user_id}/media"\nplatform_http.get(u)'))      # even a clean one: not resolved


@pytest.mark.parametrize("call, text", [
    ("platform_http.get(u, b)", "more than one positional"),
    ("platform_http.get(_GRAPH, b)", "more than one positional"),
    ("platform_http.get(_GRAPH, _HOST, _HOST)", "more than one positional"),
    ("platform_http.get(*args)", "*args"),
    ("platform_http.get(_GRAPH, *args)", "*args"),
    ("platform_http.get(*[_GRAPH])", "*args"),
    ("platform_http.get(_GRAPH, **opts)", "**kwargs"),
    ("platform_http.get(_GRAPH, bearer=t, **opts)", "**kwargs"),
    ("platform_http.post(_GRAPH, data=b, **{'params': p})", "**kwargs"),
    ("platform_http.get(**opts)", "**kwargs"),
    ("platform_http.get(_GRAPH, url=_GRAPH)", "twice"),
    ("platform_http.get()", "no URL"),
    ("platform_http.get(bearer=t)", "no URL"),
    ("platform_http.get(params={'part': 'x'})", "no URL"),
])
def test_ph_g4_control_call_shape_fires(call, text):
    _fires(g4_in_method(call), rule="PH-G4/shape", text=text)


def test_ph_g4_control_url_keyword_is_checked_not_skipped():
    _fires(g4_in_method('platform_http.get(url=f"{u}?access_token={t}")'))
    _fires(g4_in_method('platform_http.get(url="https://graph.facebook.com/x?access_token=1")'))
    _fires(g4_in_method('platform_http.get(url="http://graph.facebook.com/x")'))
    _fires(g4_in_method("platform_http.get(url=u)"))
    _passes(g4_in_method('platform_http.get(url=f"{_GRAPH}/{ig_user_id}/media")'))


def test_ph_g4_control_aliased_callees_are_counted_and_checked():
    bad = 'f"{u}?access_token={t}"'
    for source in (
        g4_in_method(f"x = platform_http.get\nx({bad})"),
        g4_in_method(f"import api.platform_http as ph\nph.get({bad})"),
        f"import api.platform_http as ph\ndef f(u, t):\n    ph.get({bad})\n",
        f"import api.platform_http\ndef f(u, t):\n    api.platform_http.get({bad})\n",
        f"from api import platform_http as ph\ndef f(u, t):\n    ph.post({bad})\n",
        f"from api import platform_http as ph\nm = ph\ndef f(u, t):\n    m.get({bad})\n",
        f"from api.platform_http import get\ndef f(u, t):\n    get({bad})\n",
        f"from api.platform_http import get as g\ndef f(u, t):\n    g({bad})\n",
        f"from api.platform_http import put\ndef f(u, t):\n    put({bad})\n",
    ):
        result = ph_g4_check(source, INSTAGRAM_PATH)
        assert result.problems, source
        assert result.calls, f"the aliased call was not counted:\n{source}"
    # an unrelated function called `get` is not a platform call
    assert ph_g4_check("def get(u):\n    pass\nget(f'{u}?access_token=1')\n", INSTAGRAM_PATH).calls == []


# the upload_url allowlist: by file, function AND callee ----------------------------------------------

_UPLOAD_CALL = 'put(upload_url, headers={"Content-Type": "a", "Content-Length": "1"}, content=c, server_supplied_url=True)'


def test_ph_g4_control_upload_url_is_allowlisted_only_for_put_in_publish():
    def publish(call, cls="YouTubePublisher", fn="publish"):
        return ("from api import platform_http\n"
                f"class {cls}:\n    def {fn}(self, upload_url, c, t):\n        platform_http.{call}\n")

    result = _passes(publish(_UPLOAD_CALL), YOUTUBE_PATH)
    assert [(c.url_kind, c.fn) for c in result.calls] == [("upload_url", "put")]
    _fires(publish(_UPLOAD_CALL.replace("put(", "post(", 1)), YOUTUBE_PATH)                       # the verb
    _fires(publish(_UPLOAD_CALL.replace("put(", "get(", 1)), YOUTUBE_PATH)
    _fires(publish(_UPLOAD_CALL, fn="other"), YOUTUBE_PATH)                                       # the function
    _fires(publish(_UPLOAD_CALL, cls="Other"), YOUTUBE_PATH)
    _fires(publish(_UPLOAD_CALL, cls="Other", fn="publish"), YOUTUBE_PATH)
    for path in (OAUTH_PATH, INSTAGRAM_PATH, METRICS_PATH, "engine/publish/other.py"):            # the file
        _fires(publish(_UPLOAD_CALL), path)
    _fires(publish(_UPLOAD_CALL.replace("upload_url", "session_url")), YOUTUBE_PATH)              # the name
    # `url=upload_url` is the same URL expression, so it stays allowed in the one place
    _passes(publish(_UPLOAD_CALL.replace("put(upload_url,", "put(url=upload_url,")), YOUTUBE_PATH)
    # a bearer next to the allowlisted URL cannot be shown to be https: it fires on the bearer rule
    _fires(publish(_UPLOAD_CALL.replace("content=c,", "content=c, bearer=t,")), YOUTUBE_PATH, rule="PH-G4/bearer")


# bearer= ----------------------------------------------------------------------------------------------

def test_ph_g4_control_bearer_needs_an_https_url_and_no_authorization_header():
    _passes(g4_in_method('platform_http.get(f"{_GRAPH}/{ig_user_id}", bearer=t)'))
    _fires(g4_in_method('platform_http.get("http://graph.facebook.com/x", bearer=t)'), text="https://")
    _fires(g4_in_method('platform_http.get(u, bearer=t)'))
    for key in ("Authorization", "authorization", "AUTHORIZATION", "aUtHoRiZaTiOn"):
        source = g4_in_method(f'platform_http.get(_GRAPH, bearer=t, headers={{"{key}": "x"}})')
        _fires(source, rule="PH-G4/bearer", text="Authorization")
        source = g4_in_method(f'platform_http.get(_GRAPH, bearer=t, headers={{"X-A": "1", "{key}": "x"}})')
        _fires(source, rule="PH-G4/bearer", text="Authorization")
    _passes(g4_in_method('platform_http.get(_GRAPH, bearer=t, headers={"Content-Type": "x", "Proxy-Authorization": "y"})'))
    _passes(g4_in_method('platform_http.get(_GRAPH, bearer=t, headers={"Content-Type": "x"})'))


# module `Name` binding multiplicity -----------------------------------------------------------------

MODULE_BINDING_FORMS = [
    ("assignment", '_GRAPH = "https://other.example/v1"'),
    ("augmented assignment", '_GRAPH += "/extra"'),
    ("annotated assignment with a value", '_GRAPH: str = "https://other.example/v1"'),
    ("def", "def _GRAPH():\n    pass"),
    ("async def", "async def _GRAPH():\n    pass"),
    ("class", "class _GRAPH:\n    pass"),
    ("for", 'for _GRAPH in ("a", "b"):\n    pass'),
    ("with", 'with open("f") as _GRAPH:\n    pass'),
    ("import as", "import os as _GRAPH"),
    ("import itself", "import _GRAPH"),
    ("from-import", "from os import path as _GRAPH"),
    ("from-import name", "from os import _GRAPH"),
    ("del", "del _GRAPH"),
    ("walrus", '(_GRAPH := "https://other.example/v1")'),
    ("tuple unpacking", '_GRAPH, _other = "a", "b"'),
    ("star unpacking", '*_GRAPH, _other = "a", "b"'),
    ("type statement", "type _GRAPH = int"),
    ("function parameter", "def _f(_GRAPH):\n    pass"),
    ("keyword-only parameter", "def _f(*, _GRAPH):\n    pass"),
    ("*args parameter", "def _f(*_GRAPH):\n    pass"),
    ("**kwargs parameter", "def _f(**_GRAPH):\n    pass"),
    ("lambda parameter", "_f = lambda _GRAPH: _GRAPH"),
    ("except as", "try:\n    pass\nexcept Exception as _GRAPH:\n    pass"),
    ("comprehension target", "_c = [_GRAPH for _GRAPH in 'ab']"),
    ("match capture", "match 1:\n    case _GRAPH:\n        pass"),
    ("match star capture", "match [1]:\n    case [*_GRAPH]:\n        pass"),
    ("match mapping rest", "match {}:\n    case {**_GRAPH}:\n        pass"),
    ("global write", 'def _w():\n    global _GRAPH\n    _GRAPH = "x"'),
    ("nonlocal write", "def _outer():\n    _GRAPH = 1\n    def _inner():\n        nonlocal _GRAPH\n        _GRAPH = 2"),
    ("local shadow in a function", 'def _g():\n    _GRAPH = "https://other.example"'),
    ("type parameter", "def _f[_GRAPH]():\n    pass"),
]
CLASS_BINDING_FORMS = [
    ("assignment", 'token_url = "https://other.example/t"'),
    ("augmented assignment", 'token_url += "/extra"'),
    ("annotated assignment with a value", 'token_url: str = "https://other.example/t"'),
    ("def", "def token_url(self):\n    pass"),
    ("class", "class token_url:\n    pass"),
    ("for", 'for token_url in ("a", "b"):\n    pass'),
    ("with", 'with open("f") as token_url:\n    pass'),
    ("import as", "import os as token_url"),
    ("from-import", "from os import path as token_url"),
    ("del", "del token_url"),
    ("walrus", '(token_url := "https://other.example/t")'),
    ("tuple unpacking", 'token_url, _other = "a", "b"'),
    ("type statement", "type token_url = int"),
    ("comprehension target", "_c = [token_url for token_url in 'ab']"),
    ("match capture", "match 1:\n    case token_url:\n        pass"),
    ("except as", "try:\n    pass\nexcept Exception as token_url:\n    pass"),
]


def _module_url_source(extra, position):
    call = {
        "url": "platform_http.get(_GRAPH, bearer=t)",
        "hole": 'platform_http.get(f"{_GRAPH}/{ig_user_id}/media", bearer=t)',
    }[position]
    return g4_in_method(call, prelude=extra + "\n")


def test_ph_g4_control_module_name_baselines_pass():
    for position in ("url", "hole"):
        _passes(_module_url_source("", position))
    # an annotation without a value is not a binding; reads and attribute stores of other names do not count
    _passes(_module_url_source("_GRAPH: str\n_other = 5\n_other += 1", "url"))
    _passes(_module_url_source("print(_GRAPH)\nvalue = _GRAPH + '/x'\nobj = type('o', (), {})\nobj._GRAPH = 1", "hole"))


@pytest.mark.parametrize("position", ["url", "hole"])
@pytest.mark.parametrize("form, extra", MODULE_BINDING_FORMS, ids=[name for name, _ in MODULE_BINDING_FORMS])
def test_ph_g4_control_a_second_binding_of_a_module_name_fires(form, extra, position):
    _fires(_module_url_source(extra, position), rule="PH-G4/url")


def test_ph_g4_control_a_module_name_must_be_a_module_level_string_constant():
    for prelude in ('_GRAPH = "x" + "y"', "_GRAPH = compute()", "_GRAPH = 5", "_GRAPH = b'https://x/y'",
                    '_GRAPH = f"https://x/{a}"', "_GRAPH, _y = 'https://x/', 'y'", '_GRAPH = _x = "https://x/"'):
        source = (f"from api import platform_http\n{prelude}\nclass InstagramPublisher:\n"
                  "    def _create_container(self, ig_user_id, t):\n        platform_http.get(_GRAPH, bearer=t)\n")
        assert ph_g4_check(source, INSTAGRAM_PATH).problems, prelude
    # the single binding inside a function is not a module name
    source = ("from api import platform_http\nclass InstagramPublisher:\n    def _create_container(self, t):\n"
              '        _GRAPH = "https://graph.facebook.com/v19.0"\n        platform_http.get(_GRAPH, bearer=t)\n')
    assert ph_g4_check(source, INSTAGRAM_PATH).problems
    # a module constant that itself carries a "?" is rejected: the substituted text is judged too
    _fires(g4_in_method("platform_http.get(_GRAPH, bearer=t)").replace('v19.0"', 'v19.0?x=1"', 1), text="'?'")


def test_ph_g4_control_a_star_import_fails_closed():
    clean = _module_url_source("", "url")
    _passes(clean)
    starred = clean.replace("from api import platform_http\n", "from api import platform_http\nfrom os.path import *\n")
    problems = _fires(starred, rule="PH-G4/star")
    assert {p.rule for p in problems} >= {"PH-G4/star"}
    # even a call that would otherwise resolve through a constant URL is flagged, once per star import
    only_constant = ('from api import platform_http\nfrom os.path import *\n'
                     'def f(t):\n    platform_http.get("https://graph.facebook.com/x", bearer=t)\n')
    _fires(only_constant, rule="PH-G4/star")
    # and module Names stop resolving
    _fires(starred, rule="PH-G4/url")


# class attributes: the enclosing class body only ----------------------------------------------------

def _class_source(extra="", *, value='token_url = "https://oauth2.googleapis.com/token"', call="platform_http.post(self.token_url, data={'code': code})",
                  prelude="", method="exchange_code", cls="YouTubeOAuth", base=""):
    body = "\n".join("    " + line for line in (value + "\n" + extra).strip("\n").splitlines())
    return (f"from api import platform_http\n{prelude}class {cls}{base}:\n{body}\n"
            f"    def {method}(self, code):\n        {call}\n")


def test_ph_g4_control_class_attribute_baselines_pass():
    result = _passes(_class_source(), OAUTH_PATH)
    assert [c.url_kind for c in result.calls] == ["self_attr"]
    # the real shape: a base class with an annotation only, two subclasses with one value each
    two_subclasses = (
        "from api import platform_http\n"
        "class OAuthProvider:\n    token_url: str\n    def exchange_code(self, code):\n        raise NotImplementedError\n"
        "class YouTubeOAuth(OAuthProvider):\n    token_url = \"https://oauth2.googleapis.com/token\"\n"
        "    def exchange_code(self, code):\n        platform_http.post(self.token_url, data={'code': code})\n"
        "class InstagramOAuth(OAuthProvider):\n    token_url = \"https://graph.facebook.com/v19.0/oauth/access_token\"\n"
        "    def exchange_code(self, code):\n        platform_http.post(self.token_url, data={'code': code})\n")
    assert [c.url_kind for c in _passes(two_subclasses, OAUTH_PATH).calls] == ["self_attr", "self_attr"]
    # an annotation without a value next to the one value does not count
    _passes(_class_source("token_url: str"), OAUTH_PATH)
    _passes(_class_source(extra="other = 5\nother += 1\nname = 'youtube'"), OAUTH_PATH)
    # an attribute named like it on another receiver is only read
    _passes(_class_source(call="platform_http.post(self.token_url, data={'u': other.token_url})"), OAUTH_PATH)


@pytest.mark.parametrize("form, extra", CLASS_BINDING_FORMS, ids=[name for name, _ in CLASS_BINDING_FORMS])
def test_ph_g4_control_a_second_binding_in_the_class_body_fires(form, extra):
    _fires(_class_source(extra), OAUTH_PATH, rule="PH-G4/url")


def test_ph_g4_control_class_attribute_other_failures_fire():
    # no value at all in the enclosing class: the base class is NOT consulted
    no_value = _class_source(value="token_url: str")
    _fires(no_value, OAUTH_PATH, rule="PH-G4/url")
    inherited = ("from api import platform_http\nclass Base:\n    token_url = 'https://x.example/t'\n"
                 "class Child(Base):\n    def exchange_code(self, code):\n        platform_http.post(self.token_url, data={})\n")
    _fires(inherited, OAUTH_PATH, rule="PH-G4/url")
    # one class defining it twice fires; so does a value that is not a string constant
    _fires(_class_source('token_url = "https://x.example/other"'), OAUTH_PATH)
    _fires(_class_source(value="token_url = compute()"), OAUTH_PATH)
    _fires(_class_source(value="token_url = 5"), OAUTH_PATH)
    _fires(_class_source(value="token_url = b'https://x.example/t'"), OAUTH_PATH)
    _fires(_class_source(value="token_url, other = 'https://x.example/t', 1"), OAUTH_PATH)
    # self.<attr> where the class has the attribute only in a nested class or a method does not count
    _fires(_class_source(value="class Inner:\n    token_url = 'https://x.example/t'"), OAUTH_PATH)
    _fires(_class_source(value="def helper(self):\n    token_url = 'https://x.example/t'"), OAUTH_PATH)
    # not self., or not in a class
    _fires(_class_source(call="platform_http.post(cls.token_url, data={})"), OAUTH_PATH)
    _fires(_class_source(call="platform_http.post(YouTubeOAuth.token_url, data={})"), OAUTH_PATH)
    _fires(_class_source(call="platform_http.post(other.token_url, data={})"), OAUTH_PATH)
    _fires("from api import platform_http\ndef f(self):\n    platform_http.post(self.token_url, data={})\n", OAUTH_PATH)
    # a class-level value that fails the URL text rules
    for url in ("https://x.example/y?client_secret=z", "https://x.example/y?x=1", "https://x.example/y;a=1",
                "https://x.example/y#f", "http://x.example/y", "https://u:p@x.example/y", "x.example/y"):
        _fires(_class_source(value=f'token_url = "{url}"'), OAUTH_PATH)


@pytest.mark.parametrize("store", [
    "self.token_url = 'https://other.example/t'",
    "self.token_url += '/x'",
    "cls.token_url = 'https://other.example/t'",
    "YouTubeOAuth.token_url = 'https://other.example/t'",
    "other.token_url = 'https://other.example/t'",
    "(self.token_url, other) = ('https://other.example/t', 1)",
    "for self.token_url in ('a',):\n    pass",
    "with open('f') as self.token_url:\n    pass",
    "del self.token_url",
    "setattr(self, 'token_url', 'https://other.example/t')",
    "setattr(YouTubeOAuth, 'token_url', 'https://other.example/t')",
    "setattr(cls, 'token_url', 'https://other.example/t')",
    "setattr(self, name, 'https://other.example/t')",
    "delattr(self, 'token_url')",
])
def test_ph_g4_control_stores_through_self_cls_classname_or_setattr_fire(store):
    method = "    def configure(self, cls=None, other=None, name=None):\n" + "\n".join(
        "        " + line for line in store.splitlines()) + "\n"
    source = _class_source().rstrip("\n") + "\n" + method
    _fires(source, OAUTH_PATH, rule="PH-G4/url")
    # the same store at module level, outside the class
    module_level = _class_source() + "\n" + store.replace("self", "YouTubeOAuth") + "\n"
    if "for " not in store and "with " not in store:
        _fires(module_level, OAUTH_PATH, rule="PH-G4/url")


def test_ph_g4_control_a_store_of_another_attribute_does_not_fire():
    source = _class_source().rstrip("\n") + "\n    def configure(self, other):\n        self.client_id = 'a'\n        other.token = 'b'\n"
    _passes(source, OAUTH_PATH)


# the pinned id holes ---------------------------------------------------------------------------------

def test_ph_g4_control_each_pinned_hole_passes_only_in_its_own_function_and_file():
    cases = [
        (OAUTH_PATH, "InstagramOAuth", "discover_account", "page_id"),
        (INSTAGRAM_PATH, "InstagramPublisher", "_create_container", "ig_user_id"),
        (INSTAGRAM_PATH, "InstagramPublisher", "_publish_container", "ig_user_id"),
        (INSTAGRAM_PATH, "InstagramPublisher", "_wait_until_ready", "creation_id"),
        (METRICS_PATH, "InstagramMetricsFetcher", "fetch", "cut.platform_post_id"),
    ]
    holes = {"page_id", "ig_user_id", "creation_id", "cut.platform_post_id"}
    for path, cls, fn, expression in cases:
        call = f'platform_http.get(f"{{_GRAPH}}/{{{expression}}}/insights", bearer=t)'
        _passes(g4_in_method(call, cls=cls, fn=fn), path)
        for other_hole in sorted(holes - {expression}):
            other_call = f'platform_http.get(f"{{_GRAPH}}/{{{other_hole}}}/insights", bearer=t)'
            _fires(g4_in_method(other_call, cls=cls, fn=fn), path, rule="PH-G4/url")
        # the same hole in a different function, class or file
        _fires(g4_in_method(call, cls=cls, fn="other"), path)
        _fires(g4_in_method(call, cls="Other", fn=fn), path)
        for other_path in (OAUTH_PATH, INSTAGRAM_PATH, METRICS_PATH, YOUTUBE_PATH):
            if other_path != path and (other_path, f"{cls}.{fn}") not in PINNED_IDS:
                _fires(g4_in_method(call, cls=cls, fn=fn), other_path)
    # attribute name alone is not enough, nor the receiver alone
    for expression in ("other.platform_post_id", "cut.reel_id", "cut.platform_post_id.strip()", "cuts.platform_post_id",
                       "cut.id", "cut", "platform_post_id"):
        call = f'platform_http.get(f"{{_GRAPH}}/{{{expression}}}/insights", bearer=t)'
        _fires(g4_in_method(call, cls="InstagramMetricsFetcher", fn="fetch"), METRICS_PATH)
    # two pinned holes in one URL are fine when both are pinned for the function
    both = 'platform_http.get(f"{_GRAPH}/{ig_user_id}/x/{ig_user_id}", bearer=t)'
    _passes(g4_in_method(both, cls="InstagramPublisher", fn="_create_container"), INSTAGRAM_PATH)


def test_ph_g4_control_page_id_holes_in_discover_account():
    """The spec's own page_id controls, in the one function where page_id is pinned (so only the rule under test
    can fire): the hole itself is fine, an id hole in the authority, a conversion, a nested format spec and a
    method call or concatenation on the pinned name are not."""
    def discover(url):
        return g4_in_method(f"platform_http.get({url}, bearer=t)", cls="InstagramOAuth", fn="discover_account")

    _passes(discover('f"{_GRAPH}/{page_id}"'), OAUTH_PATH)
    _passes(discover('f"{_GRAPH}/{page_id}/accounts"'), OAUTH_PATH)
    _fires(discover('f"https://graph.facebook.com{page_id}/x"'), OAUTH_PATH, text="authority")
    _fires(discover('f"{_HOST}{page_id}/x"'), OAUTH_PATH, text="authority")
    _fires(discover('f"{_GRAPH}/{page_id!r}"'), OAUTH_PATH, text="conversion")
    _fires(discover('f"{_GRAPH}/{page_id:{q}}"'), OAUTH_PATH, text="format spec")
    _fires(discover('f"{_GRAPH}/{page_id:?<40}"'), OAUTH_PATH, text="format spec")
    _fires(discover('f"{_GRAPH}/{page_id.strip()}"'), OAUTH_PATH, text="neither a module constant nor a pinned id")
    _fires(discover('f"{_GRAPH}/{page_id + q}"'), OAUTH_PATH, text="neither a module constant nor a pinned id")
    _fires(discover('f"{_GRAPH}/{other.platform_post_id}"'), OAUTH_PATH, text="neither a module constant nor a pinned id")
    _fires(discover('f"{_GRAPH}/{ig_user_id}"'), OAUTH_PATH)                        # pinned elsewhere, not here


def test_ph_g4_control_a_parameter_that_shadows_a_module_name_fires():
    """`def f(_GRAPH): platform_http.get(f"{_GRAPH}/{page_id}")`: the name is bound twice (module and parameter)."""
    shadow = ("from api import platform_http\n_GRAPH = \"https://graph.facebook.com/v19.0\"\n"
              "class InstagramOAuth:\n    def discover_account(self, _GRAPH, page_id, t):\n"
              "        platform_http.get(f\"{_GRAPH}/{page_id}\", bearer=t)\n")
    _fires(shadow, OAUTH_PATH, rule="PH-G4/url")
    clean = shadow.replace("self, _GRAPH, page_id", "self, page_id")
    _passes(clean, OAUTH_PATH)
    in_url_position = shadow.replace('f"{_GRAPH}/{page_id}"', "_GRAPH")
    _fires(in_url_position, OAUTH_PATH, rule="PH-G4/url")
    _passes(clean.replace('f"{_GRAPH}/{page_id}"', "_GRAPH"), OAUTH_PATH)
    # a local assignment inside the function is the same thing
    local = clean.replace("        platform_http.get", "        _GRAPH = t\n        platform_http.get")
    _fires(local, OAUTH_PATH, rule="PH-G4/url")


def test_ph_g4_control_an_id_hole_needs_the_slash_of_the_authority_before_it():
    ok = 'platform_http.get(f"{_GRAPH}/{ig_user_id}", bearer=t)'
    _passes(g4_in_method(ok))
    for call in ('platform_http.get(f"https://graph.facebook.com{ig_user_id}/x", bearer=t)',
                 'platform_http.get(f"{_HOST}{ig_user_id}", bearer=t)',
                 'platform_http.get(f"{_HOST}{ig_user_id}/x", bearer=t)',
                 'platform_http.get(f"https:{ig_user_id}", bearer=t)',
                 'platform_http.get(f"https://{ig_user_id}/x", bearer=t)',
                 'platform_http.get(f"https://graph.facebook.com.{ig_user_id}/x", bearer=t)'):
        _fires(g4_in_method(call), text="authority")
    # a "/" only AFTER the hole does not help
    _fires(g4_in_method('platform_http.get(f"{_HOST}{ig_user_id}/path/more", bearer=t)'), text="authority")
    # and an "@" before the first "/" is userinfo
    _fires(g4_in_method('platform_http.get(f"https://u@graph.facebook.com/{ig_user_id}", bearer=t)'), text="userinfo")


def test_ph_g4_control_non_resolving_inputs_do_not_count_as_calls_to_something_else():
    # a call on another object named like a platform function is not a platform call
    assert ph_g4_check("def f(client):\n    client.get(f'{u}?access_token=1')\n", INSTAGRAM_PATH).calls == []
    assert ph_g4_check("import httpx\ndef f():\n    httpx.get(f'{u}?access_token=1')\n", INSTAGRAM_PATH).calls == []
    # a platform_http call is judged wherever it sits: lambda, comprehension, nested function, module level
    for source in (
        "from api import platform_http\nf = lambda u: platform_http.get(u)\n",
        "from api import platform_http\nr = [platform_http.get(u) for u in us]\n",
        "from api import platform_http\ndef f():\n    def g(u):\n        return platform_http.get(u)\n    return g\n",
        "from api import platform_http\nplatform_http.get(u)\n",
        "from api import platform_http\nasync def f(u):\n    return platform_http.get(u)\n",
        "from api import platform_http\ntry:\n    platform_http.get(u)\nexcept Exception:\n    pass\n",
    ):
        assert ph_g4_check(source, INSTAGRAM_PATH).problems, source


# ── PH-G5 module hygiene ───────────────────────────────────────────────────────────────────────────

G5_ALLOWED_MODULES = frozenset({"httpx", "re", "urllib.parse", "collections.abc", "__future__"})
G5_FORBIDDEN_FROM = frozenset({"httpx", "urllib.parse"})
G5_PUBLIC = frozenset({"get", "post", "put", "DEFAULT_TIMEOUT_S", "UPLOAD_TIMEOUT_S", "SECRET_QUERY_NAMES"})


def _top_level_names(tree) -> set:
    names = set()
    stack = list(tree.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return names


def g5_problems(source, path=PH_PATH) -> list:
    tree = S.parse(source, path)
    problems = []

    def bad(node, message):
        problems.append(S.Problem(path, node.lineno, "PH-G5", message))

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "json":
                    bad(node, "import json: the json= parameter shadows it inside post/put (json shadows)")
                elif alias.name not in G5_ALLOWED_MODULES:
                    bad(node, f"import of {alias.name}: not one of {sorted(G5_ALLOWED_MODULES)}")
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            if module == "json":
                bad(node, "from json import ...: the json= parameter shadows it inside post/put (json shadows)")
            elif module in G5_FORBIDDEN_FROM:
                bad(node, f"from {module} import ...: call it as a module attribute so a test's patch is seen")
            elif module not in G5_ALLOWED_MODULES:
                bad(node, f"from {module} import ...: not one of {sorted(G5_ALLOWED_MODULES)}")
    public = {name for name in _top_level_names(tree) if not name.startswith("_")}
    if public != G5_PUBLIC:
        problems.append(S.Problem(path, 1, "PH-G5", f"public names are {sorted(public)}, expected {sorted(G5_PUBLIC)}"))
    return problems


def test_ph_g5_the_real_module_is_clean():
    assert g5_problems(_sources()[PH_PATH]) == []
    # the file the guard reads is the module under test
    assert _sources()[PH_PATH] == inspect.getsource(platform_http)


def _g5_real_with(line_before="", line_after=""):
    return line_before + _sources()[PH_PATH] + line_after


@pytest.mark.parametrize("addition, why", [
    ("import json\n", "import json"),
    ("import json as j\n", "import json as"),
    ("from json import dumps\n", "from json"),
    ("import os\n", "an extra import"),
    ("import urllib\n", "import urllib"),
    ("import urllib.request\n", "urllib.request"),
    ("import json, os\n", "two"),
    ("from urllib.parse import urlsplit\n", "from urllib.parse"),
    ("from urllib.parse import unquote_plus as u\n", "from urllib.parse as"),
    ("from urllib import parse\n", "from urllib"),
    ("from httpx import QueryParams\n", "from httpx"),
    ("from httpx import QueryParams as Q\n", "from httpx as"),
    ("from httpx._client import Client\n", "from httpx submodule"),
    ("import requests\n", "requests"),
    ("from . import other\n", "relative"),
    ("def f():\n    import json\n", "import inside a function"),
    ("def f():\n    from httpx import get\n", "from httpx inside a function"),
])
def test_ph_g5_control_imports_fire(addition, why):
    problems = g5_problems(_g5_real_with(line_before=addition))
    assert problems, why
    assert any("import" in p.message for p in problems), why
    if "json" in addition:
        # the dedicated rule (not only the generic allowlist) names the shadowing reason
        assert any("json shadows" in p.message for p in problems), (why, problems)


@pytest.mark.parametrize("addition, why", [
    ("def helper():\n    pass\n", "a new function"),
    ("class Helper:\n    pass\n", "a new class"),
    ("RETRIES = 3\n", "a new constant"),
    ("A, B = 1, 2\n", "tuple assignment"),
    ("COUNT: int = 3\n", "annotated"),
    ("async def run():\n    pass\n", "async def"),
    ("if True:\n    EXTRA = 1\n", "in an if"),
    ("try:\n    EXTRA = 1\nexcept Exception:\n    pass\n", "in a try"),
    ("for LOOPVAR in (1,):\n    pass\n", "a loop variable"),
])
def test_ph_g5_control_a_new_public_name_fires(addition, why):
    problems = g5_problems(_g5_real_with(line_after="\n" + addition))
    assert problems and any("public names" in p.message for p in problems), why


def test_ph_g5_control_private_names_and_allowed_imports_pass_and_a_missing_name_fires():
    source = _g5_real_with(line_after="\n_PRIVATE = 1\ndef _helper():\n    pass\nclass _Hidden:\n    pass\n")
    assert g5_problems(source) == []
    for allowed in ("import re\n", "import httpx\n", "import urllib.parse\n", "from collections.abc import Mapping\n",
                    "from collections.abc import Callable\n", "from __future__ import annotations\n", "from re import fullmatch\n"):
        assert g5_problems(_g5_real_with(line_before=allowed)) == [], allowed
    removed = _sources()[PH_PATH].replace("def put(", "def _put(")
    assert removed != _sources()[PH_PATH]
    assert any("public names" in p.message for p in g5_problems(removed))
    assert any("public names" in p.message for p in g5_problems(_sources()[PH_PATH].replace("UPLOAD_TIMEOUT_S = ", "_UPLOAD = ", 1)))


# ── the network guard itself (spec 6.3) ────────────────────────────────────────────────────────────

def test_network_guard_control():
    before = (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, httpx.request)
    gen = network_guard()
    try:
        blocked = next(gen)
        assert blocked == []
        # while the inner guard is active every route is a different object (the inner patches never chain)
        assert socket.getaddrinfo is not before[0] and httpx.request is not before[3]
        with pytest.raises(AssertionError, match="blocked network call"):
            socket.getaddrinfo("x", 1)
        with pytest.raises(AssertionError, match="blocked network call"):
            httpx.request("GET", "https://example.invalid/")
        probe = socket.socket()
        try:
            for attempt in (lambda: probe.connect(("127.0.0.1", 9)), lambda: probe.connect_ex(("127.0.0.1", 9))):
                with pytest.raises(AssertionError, match="blocked network call"):
                    attempt()
        finally:
            probe.close()
        # production code that swallows the AssertionError (a best-effort `except Exception`) still leaves a record
        try:
            socket.getaddrinfo("x", 1)
        except Exception:
            pass
        assert blocked == ["socket.getaddrinfo", "httpx.request", "socket.socket.connect", "socket.socket.connect_ex",
                           "socket.getaddrinfo"]
        # on resumption the teardown reports the recorded attempts ...
        with pytest.raises(AssertionError, match="outside the patched httpx functions"):
            next(gen)
        # ... and everything is back exactly as it was before the first next(gen)
        assert socket.getaddrinfo is before[0]
        assert socket.socket.connect is before[1]
        assert socket.socket.connect_ex is before[2]
        assert httpx.request is before[3]
    finally:
        gen.close()
    assert (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, httpx.request) == before
    assert socket.getaddrinfo is before[0] and httpx.request is before[3]


def test_network_guard_control_restores_after_an_abandoned_guard_and_a_clean_run_is_silent():
    before = (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, httpx.request)
    gen = network_guard()
    next(gen)
    gen.close()                                   # abandoned mid-way: the finally restores the patches
    assert (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, httpx.request) == before
    assert all(a is b for a, b in zip((socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex,
                                        httpx.request), before))
    # nothing blocked: the teardown is silent and the generator simply ends
    gen = network_guard()
    assert next(gen) == []
    with pytest.raises(StopIteration):
        next(gen)
    assert socket.getaddrinfo is before[0] and httpx.request is before[3]
