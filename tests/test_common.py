"""Tests for worker/tasks/common.py — the retry-decision helpers."""
import subprocess

import httpx
import pytest

from worker.tasks.common import is_transient_error, should_retry


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.test/x")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@pytest.mark.parametrize(
    "exc, expected",
    [
        (httpx.ConnectTimeout("timed out"), True),
        (httpx.ReadTimeout("timed out"), True),
        (httpx.ConnectError("refused"), True),
        (TimeoutError("timed out"), True),
        (subprocess.TimeoutExpired(cmd="ffprobe", timeout=5), True),
        (ConnectionError("reset"), True),
        (_status_error(429), True),
        (_status_error(503), True),
        (_status_error(500), True),
        (_status_error(404), False),
        (_status_error(422), False),
        (ValueError("quality score 40/100"), False),
        (RuntimeError("FFmpeg drawtext failed (exit 1)"), False),
    ],
)
def test_is_transient_error(exc, expected):
    assert is_transient_error(exc) is expected


def test_should_retry_true_for_transient_below_limit():
    assert should_retry(httpx.ConnectTimeout("x"), retries=0, max_retries=2) is True
    assert should_retry(httpx.ConnectTimeout("x"), retries=1, max_retries=2) is True


def test_should_retry_false_at_retry_limit():
    """A transient error still stops retrying once the budget is spent."""
    assert should_retry(httpx.ConnectTimeout("x"), retries=2, max_retries=2) is False


def test_should_retry_false_for_deterministic_error():
    assert should_retry(ValueError("bad guide"), retries=0, max_retries=2) is False


def test_database_connection_errors_are_transient():
    """A failover or idle-timeout must retry, not fail a job that never ran."""
    from sqlalchemy import exc as sa_exc
    assert is_transient_error(sa_exc.OperationalError("SELECT 1", {}, Exception("server closed the connection")))
    assert is_transient_error(sa_exc.InterfaceError("SELECT 1", {}, Exception("connection already closed")))


def test_database_logic_errors_are_not_transient():
    from sqlalchemy import exc as sa_exc
    assert not is_transient_error(sa_exc.IntegrityError("INSERT", {}, Exception("duplicate key")))
    assert not is_transient_error(sa_exc.ProgrammingError("SELECT", {}, Exception("no such column")))
