"""One place for platform API calls (Google OAuth/YouTube, Meta Graph).

Thin ``get``/``post``/``put`` wrappers over ``httpx`` that every platform call goes through. Each applies
the same three decisions the call sites used to repeat by hand: a default timeout, ``raise_for_status()``
straight after the call, and an ``Authorization: Bearer`` header built from ``bearer=``. The response
is returned untouched; the exception type is still ``httpx.HTTPStatusError``, so
``worker/tasks/common.py::is_transient_error`` and the credentials router's error text are unchanged.

Only the arguments a caller supplied are forwarded to ``httpx`` (never ``params=None`` and so on), and
the URL is passed positionally.

Design: ``docs/specs/2026-11-platform-http-module-design.md``.
"""
from __future__ import annotations

import httpx

DEFAULT_TIMEOUT_S = 30.0  # every platform call except the video upload
UPLOAD_TIMEOUT_S = 300.0  # the YouTube video PUT only

_UNSET = object()


def _prepare(bearer, headers, params, data, json, content, timeout) -> dict:
    kwargs: dict = {}
    if bearer is not _UNSET:
        merged = {"Authorization": f"Bearer {bearer}"}
        if headers:
            merged.update(headers)
        kwargs["headers"] = merged
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
    kwargs["timeout"] = timeout
    return kwargs


def get(url, *, bearer=_UNSET, headers=None, params=None, timeout=DEFAULT_TIMEOUT_S) -> httpx.Response:
    resp = httpx.get(url, **_prepare(bearer, headers, params, None, None, None, timeout))
    resp.raise_for_status()
    return resp


def post(
    url, *, bearer=_UNSET, headers=None, params=None, data=None, json=None, content=None,
    timeout=DEFAULT_TIMEOUT_S,
) -> httpx.Response:
    resp = httpx.post(url, **_prepare(bearer, headers, params, data, json, content, timeout))
    resp.raise_for_status()
    return resp


def put(
    url, *, bearer=_UNSET, headers=None, params=None, data=None, json=None, content=None,
    timeout=DEFAULT_TIMEOUT_S,
) -> httpx.Response:
    resp = httpx.put(url, **_prepare(bearer, headers, params, data, json, content, timeout))
    resp.raise_for_status()
    return resp
