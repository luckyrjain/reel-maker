"""One place for platform API calls (Google OAuth/YouTube, Meta Graph).

Thin ``get``/``post``/``put`` wrappers over ``httpx`` that every platform call goes through. Each applies
the same decisions the call sites used to repeat by hand: a default timeout, ``raise_for_status()``
straight after the call, an ``Authorization: Bearer`` header built from ``bearer=``, and one rule that
used to live only in comments: **a secret never goes in a URL**. A secret-named query key (see
``SECRET_QUERY_NAMES``), userinfo in the URL or a bearer sent over a non-https URL raises
``ValueError`` before any request is made, because ``httpx.HTTPStatusError.__str__`` includes the URL
and that text reaches ``job.error`` and the credentials router's 502 body. One exception:
``put(..., server_supplied_url=True)``, for a URL the platform chose (the YouTube upload session),
skips the scan of that URL's query; userinfo is still rejected, and the call may carry no ``params``,
no ``bearer``, no ``data``/``json`` (the body goes in ``content``) and only
``Content-Type``/``Content-Length`` headers.

The response is returned untouched; the exception type is still ``httpx.HTTPStatusError``, so
``worker/tasks/common.py::is_transient_error`` and the credentials router's error text are unchanged.

Only the arguments a caller supplied are forwarded to ``httpx`` (never ``params=None`` and so on), plus
``timeout``, which is always sent. The URL is passed positionally. ``urllib.parse`` and ``httpx`` are always called as module attributes
(never through a ``from ... import``) so a test's patch of them is seen.

No message raised here contains caller-supplied text: only the matched secret name taken from the
constant and fixed wording.

Design: ``docs/specs/2026-11-platform-http-module-design.md``.
"""
from __future__ import annotations

import re
import urllib.parse
from collections.abc import Mapping

import httpx
from celery.exceptions import SoftTimeLimitExceeded

DEFAULT_TIMEOUT_S = 30.0  # every platform call except the video upload
UPLOAD_TIMEOUT_S = 300.0  # the YouTube video PUT only

# NORMALISED names (lower case, letters and digits only; see _normalise). Pinned as a literal by a test.
SECRET_QUERY_NAMES = frozenset({
    "accesstoken", "refreshtoken", "idtoken", "clientsecret", "secret", "fbexchangetoken",
    "apikey", "xgoogapikey", "key", "token", "code", "password", "auth", "authorization", "bearer",
    "signature", "sig",                                                          # 17
    "oauthtoken", "apitoken", "authtoken", "sessiontoken", "securitytoken", "accesskey",
    "secretkey", "privatekey", "accesssecret", "jwt", "passwd", "pwd",           # + 12 = 29
    "appsecret", "apisecret", "sessionid", "credential", "assertion", "clientassertion",
    "authorizationcode", "authkey",                                              # + 8 = 37
})

_UNSET = object()

_MAX_TIMEOUT_S = 3600
_MAX_DECODE_PASSES = 8
_UNSTABLE = "\x00unstable"  # what _normalise returns for a name still changing after the pass cap

_BEARER_RE = r"[\x21-\x7e]+"
_HEADER_NAME_RE = r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+"  # an RFC 7230 token
_HEADER_VALUE_RE = r"[\x21-\x7e]([\x20-\x7e\t]*[\x21-\x7e])?"
_SERVER_URL_HEADERS = frozenset({"content-type", "content-length"})
# httpx turns a bytes key into its repr, "b'name'", which would not match a secret name
_BYTES_KEY_RE = r"""b(['"]).*\1"""

_HINT = "send it with bearer= or in the request body"
_MSG_BAD_URL = "platform_http: unparseable URL"
_MSG_BAD_PARAMS = "platform_http: unparseable params"
_MSG_USERINFO = "platform_http: credentials (userinfo) are not allowed in a URL; use bearer= or the request body"
_MSG_UNSTABLE = f"platform_http: a query parameter name is encoded too many times; {_HINT}"
_MSG_SERVER_PARAMS = "platform_http: params not allowed with a server-supplied URL"
_MSG_SERVER_HEADER = "platform_http: header not allowed with a server-supplied URL"
_MSG_SERVER_BODY = "platform_http: only content= may carry the body with a server-supplied URL"


def _msg_secret(name: str) -> str:
    return f"platform_http: secret-named query parameter ({name}) is not allowed in a URL; {_HINT}"


def _normalise(key: str) -> str:
    """The comparison form of a query key: percent-decoded to a fixed point (at most
    ``_MAX_DECODE_PASSES`` passes; a key still changing after that returns ``_UNSTABLE``, fail closed),
    stripped, lower-cased, every non-alphanumeric character removed."""
    text = key
    for _ in range(_MAX_DECODE_PASSES):
        decoded = urllib.parse.unquote_plus(text)
        if decoded == text:
            break
        text = decoded
    else:
        if urllib.parse.unquote_plus(text) != text:
            return _UNSTABLE
    return re.sub(r"[^a-z0-9]", "", text.strip().lower())


def _secret_message(keys) -> str | None:
    """The guard's message for the first key that normalises into the secret names, else None."""
    for key in keys:
        name = _normalise(key)
        if name == _UNSTABLE:
            return _MSG_UNSTABLE
        for known in SECRET_QUERY_NAMES:
            if known == name:
                return _msg_secret(known)  # the name from the constant, never the caller's key
    return None


def _split_query(query: str) -> list[str]:
    """The keys of a query string, split on both '&' and ';', blank values kept (a bare key counts)."""
    return [pair.partition("=")[0] for pair in query.replace(";", "&").split("&")]


def _url_problem(url: str, server_supplied_url: bool) -> str | None:
    if url != url.strip() or url[:1].isspace() or url[-1:].isspace():
        return _MSG_BAD_URL  # httpx would parse it differently from urlsplit
    parts = urllib.parse.urlsplit(url)
    if "@" in parts.netloc:
        return _MSG_USERINFO
    if server_supplied_url:
        return None
    return _secret_message(_split_query(parts.query))


def _params_problem(params) -> str | None:
    if params is None:
        return None
    if isinstance(params, (str, bytes)):
        text = params.decode("ascii") if isinstance(params, bytes) else params
        if text.startswith("?"):
            text = text[1:]
        keys = _split_query(text)
    else:
        keys = list(httpx.QueryParams(params).keys())
        if any(re.fullmatch(_BYTES_KEY_RE, key) for key in keys):
            return _MSG_BAD_PARAMS
    return _secret_message(keys)


def _guard(url: str, params, server_supplied_url: bool) -> None:
    """Raise ValueError when a secret could travel in the URL. Any failure while computing the answer
    is caught, remembered, and raised as a fixed message AFTER the except block, so the original
    error (which can quote part of the URL) is neither the cause nor the context. Celery's soft time
    limit is the one exception let through: the worker must see it to report the timeout."""
    problem = None
    try:
        problem = _url_problem(url, server_supplied_url)
    except SoftTimeLimitExceeded:
        raise
    except Exception:
        problem = _MSG_BAD_URL
    if problem is None:
        try:
            problem = _params_problem(params)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            problem = _MSG_BAD_PARAMS
    if problem is not None:
        raise ValueError(problem)


def _check_types(url, headers, params, timeout) -> None:
    if not isinstance(url, str):
        raise TypeError("platform_http: url must be a str")
    if headers is not None:
        if not isinstance(headers, Mapping):
            raise TypeError("platform_http: headers must be a mapping of str to str")
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise TypeError("platform_http: headers must be a mapping of str to str")
            if not re.fullmatch(_HEADER_NAME_RE, name):
                raise ValueError("platform_http: invalid header name")
            if not re.fullmatch(_HEADER_VALUE_RE, value):
                raise ValueError("platform_http: invalid header value")
    if params is not None and not isinstance(params, (Mapping, list, tuple, str, bytes)):
        raise TypeError("platform_http: params has an unsupported type")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise TypeError("platform_http: timeout must be a number")
    # The range is checked BEFORE any float(): nan, inf, zero, negatives and 10**400 all fail the
    # comparison, so no raw OverflowError can escape.
    if not (0 < timeout <= _MAX_TIMEOUT_S):
        raise ValueError(f"platform_http: timeout must be greater than 0 and at most {_MAX_TIMEOUT_S} seconds")


def _check_bearer(url: str, bearer, headers, server_supplied_url: bool) -> None:
    if not isinstance(bearer, str) or not re.fullmatch(_BEARER_RE, bearer):
        raise ValueError("bearer must be a non-empty str")
    if server_supplied_url:
        raise ValueError("bearer is not allowed with a server-supplied URL")
    if url[:8].lower() != "https://":
        raise ValueError("bearer requires an https URL")
    if headers and any(name.lower() == "authorization" for name in headers):
        raise ValueError("platform_http: headers already carry Authorization; remove it when using bearer=")


def _check_server_supplied(headers, params, data, json) -> None:
    """With a server-supplied URL only Content-Type / Content-Length may be sent, no params, and the body
    only as content= (a form or JSON body is where the other calls carry credentials)."""
    if params is not None:
        raise ValueError(_MSG_SERVER_PARAMS)
    if data is not None or json is not None:
        raise ValueError(_MSG_SERVER_BODY)
    seen = set()
    for name in headers or {}:
        lowered = name.lower()
        if lowered not in _SERVER_URL_HEADERS or lowered in seen:
            raise ValueError(_MSG_SERVER_HEADER)
        seen.add(lowered)


def _prepare(url, bearer, headers, params, data, json, content, timeout, server_supplied_url) -> dict:
    """Validate, run the guard and build the keyword set for httpx (only what was supplied)."""
    _check_types(url, headers, params, timeout)
    if not isinstance(server_supplied_url, bool):
        raise TypeError("platform_http: server_supplied_url must be a bool")
    if bearer is not _UNSET:
        _check_bearer(url, bearer, headers, server_supplied_url)
    if server_supplied_url:
        _check_server_supplied(headers, params, data, json)
    _guard(url, params, server_supplied_url)

    kwargs: dict = {}
    if bearer is not _UNSET:
        kwargs["headers"] = {"Authorization": f"Bearer {bearer}", **(headers or {})}
    elif headers is not None:
        kwargs["headers"] = headers
    if params is not None:
        kwargs["params"] = params
    if data is not None:
        kwargs["data"] = data
    if json is not None:
        kwargs["json"] = json
    if content is not None:
        kwargs["content"] = content
    kwargs["timeout"] = float(timeout)
    return kwargs


def get(url, *, bearer=_UNSET, headers=None, params=None, timeout=DEFAULT_TIMEOUT_S) -> httpx.Response:
    kwargs = _prepare(url, bearer, headers, params, None, None, None, timeout, False)
    resp = httpx.get(url, **kwargs)
    resp.raise_for_status()
    return resp


def post(
    url, *, bearer=_UNSET, headers=None, params=None, data=None, json=None, content=None,
    timeout=DEFAULT_TIMEOUT_S,
) -> httpx.Response:
    kwargs = _prepare(url, bearer, headers, params, data, json, content, timeout, False)
    resp = httpx.post(url, **kwargs)
    resp.raise_for_status()
    return resp


def put(
    url, *, bearer=_UNSET, headers=None, params=None, data=None, json=None, content=None,
    timeout=DEFAULT_TIMEOUT_S, server_supplied_url=False,
) -> httpx.Response:
    kwargs = _prepare(url, bearer, headers, params, data, json, content, timeout, server_supplied_url)
    resp = httpx.put(url, **kwargs)
    resp.raise_for_status()
    return resp
