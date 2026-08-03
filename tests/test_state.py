"""Tests for the state machine transition guard."""
import pytest
from api.state import REEL_TRANSITIONS, CUT_TRANSITIONS, transition


class _FakeObj:
    class _Status:
        def __init__(self, v):
            self.value = v
    def __init__(self, status):
        self.status = self._Status(status)


def test_valid_reel_transition():
    obj = _FakeObj("draft")
    transition(obj, "generating", REEL_TRANSITIONS)
    assert obj.status == "generating"


def test_invalid_reel_transition_raises():
    obj = _FakeObj("draft")
    with pytest.raises(ValueError):
        transition(obj, "guide_ready", REEL_TRANSITIONS)
    # Status must not change on failure
    assert obj.status.value == "draft"


def test_cut_draft_to_rendering():
    obj = _FakeObj("draft")
    transition(obj, "rendering", CUT_TRANSITIONS)
    assert obj.status == "rendering"


def test_cut_cannot_skip_to_approved():
    obj = _FakeObj("draft")
    with pytest.raises(ValueError):
        transition(obj, "approved", CUT_TRANSITIONS)


def test_cut_in_review_can_rerender():
    obj = _FakeObj("in_review")
    transition(obj, "rendering", CUT_TRANSITIONS)
    assert obj.status == "rendering"


def test_cut_failed_reverts_to_draft():
    obj = _FakeObj("failed")
    transition(obj, "draft", CUT_TRANSITIONS)
    assert obj.status == "draft"


def test_cut_failed_publish_can_retry_from_approved():
    """A publish failure (bad credentials, network error) shouldn't force a re-render."""
    obj = _FakeObj("failed")
    transition(obj, "approved", CUT_TRANSITIONS)
    assert obj.status == "approved"


def test_cut_approved_can_go_to_publishing():
    obj = _FakeObj("approved")
    transition(obj, "publishing", CUT_TRANSITIONS)
    assert obj.status == "publishing"


def test_cut_publishing_can_fail():
    obj = _FakeObj("publishing")
    transition(obj, "failed", CUT_TRANSITIONS)
    assert obj.status == "failed"


def test_cut_publishing_can_succeed():
    obj = _FakeObj("publishing")
    transition(obj, "published", CUT_TRANSITIONS)
    assert obj.status == "published"


def test_reel_failed_reverts_to_draft():
    obj = _FakeObj("failed")
    transition(obj, "draft", REEL_TRANSITIONS)
    assert obj.status == "draft"


def test_draft_can_transition_to_enriching():
    class Obj:
        status = type("S", (), {"value": "draft"})()
    obj = Obj()
    transition(obj, "enriching", REEL_TRANSITIONS)
    assert obj.status == "enriching"


def test_enriching_can_transition_to_generating():
    class Obj:
        status = type("S", (), {"value": "enriching"})()
    obj = Obj()
    transition(obj, "generating", REEL_TRANSITIONS)
    assert obj.status == "generating"


def test_enriching_can_transition_to_failed():
    class Obj:
        status = type("S", (), {"value": "enriching"})()
    obj = Obj()
    transition(obj, "failed", REEL_TRANSITIONS)
    assert obj.status == "failed"


def test_enriching_cannot_transition_to_guide_ready():
    class Obj:
        status = type("S", (), {"value": "enriching"})()
    obj = Obj()
    with pytest.raises(ValueError):
        transition(obj, "guide_ready", REEL_TRANSITIONS)
