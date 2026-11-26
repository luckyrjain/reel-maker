"""Shared helpers for the platform-call tests (a helper module, not a test file).

Two things live here:

* `Recorder`: replaces the BARE `httpx.get` / `httpx.post` / `httpx.put` for one test. Each call is
  checked against the real function's signature (`inspect.signature(real).bind(url, **kw)`, so an
  unknown keyword or `get(data=...)` fails), recorded as `(method, url, kwargs)` in call order with
  the URL positional, and then answered through a real `httpx.Client` over an `httpx.MockTransport`
  using the same keywords. The wire request is therefore real and the response is a real
  `httpx.Response` with its request attached.
* `network_guard()`: makes every other way out to the network (`httpx.request`, a socket connect,
  a name lookup) raise `AssertionError` and records the attempt, so code that swallows the
  `AssertionError` (a best-effort `except Exception`) still fails the test at teardown.

The autouse fixture `platform_network_guard` is imported by the test files that need it with
`from tests._platform_http_helpers import platform_network_guard  # noqa: F401`, because a fixture
is autouse only in the namespace that holds it.
"""
from __future__ import annotations

import inspect
import socket
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx
import pytest

# Captured at import, before any test patches the module: the real functions whose signatures the
# fake checks (a second capture inside a patched test would grab the fake itself).
_REAL = {"get": httpx.get, "post": httpx.post, "put": httpx.put}
_SIGNATURES = {name: inspect.signature(fn) for name, fn in _REAL.items()}


@dataclass
class Call:
    method: str
    url: str
    kwargs: dict


@dataclass
class Recorder:
    """Records every call to the patched bare `httpx.get/post/put`; answers them with `handler`."""

    handler: Callable[[httpx.Request], httpx.Response]
    calls: list[Call] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def _wire(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def _make(self, name: str):
        signature = _SIGNATURES[name]

        def fake(*args, **kwargs):
            if len(args) != 1 or "url" in kwargs:
                raise AssertionError("the URL must be passed as the one positional argument")
            signature.bind(*args, **kwargs)
            (url,) = args
            self.calls.append(Call(name.upper(), url, dict(kwargs)))
            with httpx.Client(transport=httpx.MockTransport(self._wire), trust_env=False) as client:
                return client.request(name.upper(), url, **kwargs)

        return fake

    def install(self, monkeypatch) -> "Recorder":
        for name in ("get", "post", "put"):
            monkeypatch.setattr(httpx, name, self._make(name))
        return self


def _restorable(owner, name):
    """(value, was_in_own_dict): enough to put an attribute back exactly as it was."""
    return getattr(owner, name), name in vars(owner)


def network_guard() -> Iterator[list]:
    """Block every route to the network except the patched `httpx.get/post/put` fakes.

    A plain generator: installs the patches, yields the list of blocked attempts, and on
    resumption restores the originals in a `finally` and then asserts the list is empty. The
    patches raise and record to THEIR OWN list and never chain to an outer guard, so a control
    test can nest this under the autouse one.
    """
    blocked: list = []

    def blocker(label: str):
        def _blocked(*args, **kwargs):
            blocked.append(label)
            raise AssertionError(f"blocked network call: {label}")

        return _blocked

    targets = [
        (httpx, "request", "httpx.request"),
        (socket.socket, "connect", "socket.socket.connect"),
        (socket.socket, "connect_ex", "socket.socket.connect_ex"),
        (socket, "getaddrinfo", "socket.getaddrinfo"),
    ]
    saved = [(owner, name, *_restorable(owner, name)) for owner, name, _ in targets]
    try:
        for owner, name, label in targets:
            setattr(owner, name, blocker(label))
        yield blocked
    finally:
        for owner, name, value, in_own_dict in saved:
            if in_own_dict:
                setattr(owner, name, value)
            else:
                delattr(owner, name)
    assert not blocked, f"code reached the network outside the patched httpx functions: {blocked}"


@pytest.fixture(autouse=True)
def platform_network_guard():
    yield from network_guard()
