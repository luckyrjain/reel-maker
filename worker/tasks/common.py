"""Shared helpers for Celery tasks.

is_transient_error / should_retry decide whether a failure is worth another
attempt. Kept as pure functions (no Celery, no db) so the decision is
unit-testable without a broker or a task context.
"""
import subprocess

import httpx

_TRANSIENT_TYPES = (
    httpx.TransportError,      # connect/read/write timeouts, connection errors
    TimeoutError,
    subprocess.TimeoutExpired,
    ConnectionError,
)
_TRANSIENT_STATUS = {429, 500, 502, 503, 504}


def is_transient_error(exc: BaseException) -> bool:
    """True when re-running the same input could plausibly succeed."""
    if isinstance(exc, _TRANSIENT_TYPES):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    return False


def should_retry(exc: BaseException, retries: int, max_retries: int) -> bool:
    """Retry decision, split out from the Celery glue so it is unit-testable."""
    return retries < max_retries and is_transient_error(exc)
