"""SoftTimeLimitExceeded must end the task from EVERY network call in the sourcers.

Celery raises it (an `Exception` subclass) inside the running task when the soft time limit hits. A
broad `except Exception` that treats it like an ordinary upstream failure eats the only signal the
task gets: the sourcer returns "nothing found" and the render carries on to the next beat, the next
HuggingFace tier or the next paid call, on a clock that has already run out. The download loops were
fixed first; these tests cover every other `except Exception` that wraps a network call:
the Wikipedia opensearch / summary / license lookups, the Pexels search call and both HuggingFace
sources. An ordinary failure at each site must still degrade to "no result".
"""
from unittest.mock import patch

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded

from engine.render.asset_sourcer import (
    HuggingFaceImageSource, HuggingFaceVideoSource, PexelsVideoSource, WikipediaImageSource,
)
from tests.test_sourcer_selection import _MOD, _json_resp, _summary

ORIG = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Messi.jpg"


def _wiki_get(*, raise_at=None, exc=None):
    """A Wikipedia httpx.get fake that raises `exc` at one stage ('opensearch'|'summary'|'license')."""
    def fake_get(url, **kw):
        action = (kw.get("params") or {}).get("action")
        stage = {"opensearch": "opensearch", "query": "license"}.get(action, "summary")
        if stage == raise_at:
            raise exc
        if stage == "opensearch":
            return _json_resp(["Lionel Messi", ["Lionel Messi"], [], []])
        if stage == "license":
            return _json_resp({"query": {"pages": {"1": {"imageinfo": [{"extmetadata": {}}]}}}})
        return _json_resp(_summary(original=ORIG, thumbnail=None))
    return fake_get


@pytest.mark.parametrize("stage", ["opensearch", "summary", "license"])
def test_wikipedia_a_soft_time_limit_propagates_from_every_lookup(tmp_path, stage):
    fake = _wiki_get(raise_at=stage, exc=SoftTimeLimitExceeded())
    with patch(f"{_MOD}.httpx.get", side_effect=fake), patch(f"{_MOD}.time.sleep"):
        with pytest.raises(SoftTimeLimitExceeded):
            WikipediaImageSource(tmp_path).search("Lionel Messi")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("stage,expect_none", [("opensearch", True), ("summary", True), ("license", False)])
def test_wikipedia_an_ordinary_error_at_each_lookup_still_degrades(tmp_path, stage, expect_none):
    """opensearch / summary failing means no result; a failing license lookup means 'unknown, unsafe'."""
    fake = _wiki_get(raise_at=stage, exc=httpx.ConnectError("down"))
    with patch(f"{_MOD}.httpx.get", side_effect=fake), patch(f"{_MOD}.time.sleep"), \
            patch(f"{_MOD}._http_stream", side_effect=httpx.ConnectError("down")):
        result = WikipediaImageSource(tmp_path).search("Lionel Messi")
    assert result is None                        # the download fails too, so every variant ends in None


def test_wikipedia_license_lookup_alone_degrades_to_unknown_and_unsafe():
    with patch(f"{_MOD}.httpx.get", side_effect=httpx.ConnectError("down")):
        info = WikipediaImageSource.__new__(WikipediaImageSource)._fetch_license("x.jpg")
    assert info["license"] == "unknown" and info["safe_to_publish"] is False


def test_wikipedia_license_lookup_alone_propagates_a_soft_limit():
    with patch(f"{_MOD}.httpx.get", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            WikipediaImageSource.__new__(WikipediaImageSource)._fetch_license("x.jpg")


def test_pexels_a_soft_time_limit_in_the_search_call_propagates(tmp_path):
    with patch(f"{_MOD}.httpx.get", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            PexelsVideoSource("k", tmp_path).search("q", 1.0)


def test_pexels_an_ordinary_error_in_the_search_call_still_returns_none(tmp_path):
    with patch(f"{_MOD}.httpx.get", side_effect=httpx.ConnectError("down")):
        assert PexelsVideoSource("k", tmp_path).search("q", 1.0) is None


@pytest.mark.parametrize("cls", [HuggingFaceImageSource, HuggingFaceVideoSource])
def test_huggingface_a_soft_time_limit_propagates_and_nothing_is_cached(tmp_path, cls):
    with patch(f"{_MOD}.httpx.post", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            cls("key", "org/model", tmp_path).generate("a prompt")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("cls", [HuggingFaceImageSource, HuggingFaceVideoSource])
def test_huggingface_an_ordinary_error_still_returns_none(tmp_path, cls):
    with patch(f"{_MOD}.httpx.post", side_effect=httpx.ConnectError("down")):
        assert cls("key", "org/model", tmp_path).generate("a prompt") is None


@pytest.mark.parametrize("cls", [HuggingFaceImageSource, HuggingFaceVideoSource])
def test_huggingface_a_soft_limit_does_not_leave_the_generated_flag_set(tmp_path, cls):
    src = cls("key", "org/model", tmp_path)
    with patch(f"{_MOD}.httpx.post", side_effect=SoftTimeLimitExceeded()):
        with pytest.raises(SoftTimeLimitExceeded):
            src.generate("p")
    assert src.last_call_was_generated is False
