# Context Evaluation & Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pre-generation `enrich_context` Celery task that evaluates input context quality and silently enriches it via LLM before guide generation runs.

**Architecture:** New `enrich_context` Celery task runs before `generate_guide`. Rule-based `evaluate_context()` scores input on 5 engagement axes (0–100); if score < 60, `llm_enrich()` calls the enrichment LLM to add specificity, stakes, and a hook angle. Enriched context stored in `reel.enriched_context`; original preserved. `generate_guide` uses `enriched_context or context`. UI polls a new reel-level endpoint that tracks whichever job is currently active.

**Tech Stack:** Python, FastAPI, Celery/Redis, SQLAlchemy/Alembic, Jinja2/HTMX, PostgreSQL

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `engine/generation/context_enricher.py` | `evaluate_context()` rule scorer + `llm_enrich()` LLM call |
| Create | `worker/tasks/enrich_context.py` | Celery task: evaluate → enrich → enqueue generate |
| Create | `tests/test_context_enricher.py` | Unit tests for evaluator axes and llm_enrich |
| Create | `tests/test_enrich_context_task.py` | Task-level tests: enrichment gating, fallback, job chaining |
| Create | `migrations/versions/0003_context_enrichment.py` | Add `enriched_context` column; add `enriching`/`enrich` enum values |
| Create | `ui/templates/fragments/pipeline_status.html` | Reel-level polling fragment, phase-aware progress hints |
| Modify | `api/models.py` | Add `enriching` to `ReelStatus`; `enrich` to `JobType`; `enriched_context` column |
| Modify | `api/state.py` | Add `draft→enriching` and `enriching→{generating,failed}` transitions |
| Modify | `api/routers/reels.py` | `POST /api/reels` enqueues `enrich_context`; add active-job-fragment endpoint |
| Modify | `worker/celery_app.py` | Include `enrich_context` module; add task route |
| Modify | `worker/tasks/generate.py` | `effective_context = enriched_context or context` throughout |

---

## Task 1: DB Schema — Models + Migration

**Files:**
- Modify: `api/models.py`
- Create: `migrations/versions/0003_context_enrichment.py`

- [ ] **Step 1: Add `enriching` to `ReelStatus` and `enrich` to `JobType` in `api/models.py`**

```python
class ReelStatus(str, enum.Enum):
    draft = "draft"
    enriching = "enriching"      # ← new
    generating = "generating"
    guide_ready = "guide_ready"
    failed = "failed"


class JobType(str, enum.Enum):
    enrich = "enrich"            # ← new
    generate = "generate"
    render = "render"
    publish = "publish"
```

- [ ] **Step 2: Add `enriched_context` column to `Reel` in `api/models.py`**

Add after the `context` column:
```python
class Reel(Base):
    __tablename__ = "reels"

    id = Column(Integer, primary_key=True)
    context = Column(Text, nullable=False)
    enriched_context = Column(Text, nullable=True)   # ← new
    niche = Column(String(255))
    # ... rest unchanged
```

- [ ] **Step 3: Create migration `migrations/versions/0003_context_enrichment.py`**

```python
"""Add enriched_context column; add enriching/enrich enum values.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add enriched_context column to reels
    op.add_column("reels", sa.Column("enriched_context", sa.Text(), nullable=True))

    # PostgreSQL: add new values to existing enums
    # (enum value addition cannot be rolled back in downgrade)
    op.execute("ALTER TYPE reelstatus ADD VALUE IF NOT EXISTS 'enriching'")
    op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'enrich'")


def downgrade() -> None:
    # Column drop is reversible; enum value removal is not supported in PostgreSQL
    # without recreating the type — acceptable for dev environments only.
    op.drop_column("reels", "enriched_context")
```

- [ ] **Step 4: Run migration**

```bash
.venv/bin/alembic upgrade head
```

Expected output: `Running upgrade 0002 -> 0003, Add enriched_context column; add enriching/enrich enum values.`

- [ ] **Step 5: Verify schema**

```bash
.venv/bin/python -c "
from api.db import SessionLocal
from api.models import Reel, ReelStatus, JobType
db = SessionLocal()
print(ReelStatus.enriching)
print(JobType.enrich)
db.close()
print('OK')
"
```

Expected: `ReelStatus.enriching` and `JobType.enrich` printed without error.

- [ ] **Step 6: Commit**

```bash
git add api/models.py migrations/versions/0003_context_enrichment.py
git commit -m "feat: add enriched_context column and enriching/enrich enum values"
```

---

## Task 2: State Machine

**Files:**
- Modify: `api/state.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_state.py`:
```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_state.py -v
```

Expected: 4 new tests FAIL.

- [ ] **Step 3: Update `api/state.py`**

```python
REEL_TRANSITIONS: dict[str, set[str]] = {
    "draft":      {"generating", "enriching"},
    "enriching":  {"generating", "failed"},
    "generating": {"guide_ready", "failed"},
    "guide_ready": {"failed"},
    "failed":     {"draft"},
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_state.py -v
```

Expected: all state tests PASS.

- [ ] **Step 5: Commit**

```bash
git add api/state.py tests/test_state.py
git commit -m "feat: add enriching reel state and transitions"
```

---

## Task 3: Context Enricher Module

**Files:**
- Create: `engine/generation/context_enricher.py`
- Create: `tests/test_context_enricher.py`

- [ ] **Step 1: Write the failing tests in `tests/test_context_enricher.py`**

```python
"""Tests for context_enricher — evaluate_context axis scoring."""
import pytest
from engine.generation.context_enricher import evaluate_context

LONG_NEUTRAL = " ".join(["the team played and worked together"] * 8)


def test_score_bounded_zero_to_100():
    score, _ = evaluate_context("")
    assert 0 <= score <= 100


def test_empty_context_scores_zero():
    score, issues = evaluate_context("")
    assert score == 0
    assert "context_too_short" in issues


def test_short_context_scores_low_on_length():
    score, issues = evaluate_context("Argentina won.")
    assert "context_too_short" in issues


def test_long_context_clears_length_axis():
    text = " ".join(["word"] * 110)
    _, issues = evaluate_context(text)
    assert "context_too_short" not in issues


def test_named_entities_clear_specificity_axis():
    text = (
        "Lionel Messi scored 7 goals in 2022. "
        "Emiliano Martinez saved three penalties against France. "
        "The match ended 3-3 before penalties."
    )
    _, issues = evaluate_context(text)
    assert "lacks_specific_details" not in issues


def test_no_entities_fails_specificity_axis():
    _, issues = evaluate_context(LONG_NEUTRAL)
    assert "lacks_specific_details" in issues


def test_tension_words_clear_stakes_axis():
    text = LONG_NEUTRAL + " however there is a real risk of collapse despite the pressure"
    _, issues = evaluate_context(text)
    assert "no_conflict_or_tension" not in issues


def test_no_tension_fails_stakes_axis():
    _, issues = evaluate_context(LONG_NEUTRAL)
    assert "no_conflict_or_tension" in issues


def test_question_hook_clears_hook_axis():
    text = "Was this the greatest final ever? " + LONG_NEUTRAL
    _, issues = evaluate_context(text)
    assert "weak_hook_potential" not in issues


def test_direct_address_hook_clears_hook_axis():
    text = "Imagine watching the best match in history. " + LONG_NEUTRAL
    _, issues = evaluate_context(text)
    assert "weak_hook_potential" not in issues


def test_weak_opener_fails_hook_axis():
    text = "The team had a performance during the tournament this year. " + LONG_NEUTRAL
    _, issues = evaluate_context(text)
    assert "weak_hook_potential" in issues


def test_rich_context_scores_above_threshold():
    context = (
        "Was this the greatest World Cup final ever? "
        "Argentina's Lionel Messi scored 7 goals in 2022 to lead his nation. "
        "But France pushed back — Kylian Mbappé scored a hat-trick. "
        "Emiliano Martinez saved two penalties however the pressure never eased. "
        "Despite the lead, Argentina nearly collapsed before winning 4-2. "
        "The question is whether this squad can repeat the feat in 2026."
    )
    score, issues = evaluate_context(context)
    assert score >= 60, f"Expected >= 60, got {score}. Issues: {issues}"


def test_discourse_connectors_help_narrative_axis():
    text = (
        "First the team set up their formation. "
        "Then they pressed high. "
        "However the opposition countered. "
        "Finally they found a breakthrough. " * 2
    )
    _, issues = evaluate_context(text)
    assert "weak_narrative_structure" not in issues
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_context_enricher.py -v
```

Expected: all 12 tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `engine/generation/context_enricher.py`**

```python
"""
Pre-generation context quality evaluation and LLM enrichment.

evaluate_context() — rule-based scorer (5 axes × 20 pts = 100 max).
llm_enrich()       — LLM call to improve thin context; returns enriched string or None.

Threshold: score < 60 triggers enrichment.
"""
import logging
import re

_log = logging.getLogger(__name__)

ENRICH_THRESHOLD = 60

# ── Axis helpers ─────────────────────────────────────────────────────────────

_NAMED_ENTITY_RE = re.compile(
    r'\b[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{1,})+\b'
)
_NUMBER_RE = re.compile(r'\b\d+(?:[.,]\d+)?%?\b')

_TENSION_WORDS = {
    "but", "however", "despite", "risk", "challenge", "collapse",
    "concern", "pressure", "threat", "weakness", "problem",
    "controversy", "doubt", "debate", "question", "exposed",
    "fragile", "vulnerable", "worry", "danger",
}

_DISCOURSE_CONNECTORS = {
    "first", "then", "however", "finally", "but", "because",
    "which means", "therefore", "meanwhile", "although", "despite",
    "nevertheless", "yet", "whereas",
}

_HOOK_ADDRESS_RE = re.compile(
    r'^(imagine|you |here\'s|this is|what if|was this|is this|did you|'
    r'why does|how does|the real|meet )',
    re.IGNORECASE,
)


def _score_length(words: list[str]) -> tuple[int, list[str]]:
    n = len(words)
    if n < 50:
        return 0, ["context_too_short"]
    if n < 100:
        return 10, []
    return 20, []


def _score_specificity(text: str) -> tuple[int, list[str]]:
    names = _NAMED_ENTITY_RE.findall(text)
    numbers = _NUMBER_RE.findall(text)
    count = len(set(names)) + len(set(numbers))
    if count == 0:
        return 0, ["lacks_specific_details"]
    if count <= 2:
        return 10, []
    return 20, []


def _score_stakes(text: str) -> tuple[int, list[str]]:
    lower = text.lower()
    found = sum(1 for w in _TENSION_WORDS if w in lower)
    if found == 0:
        return 0, ["no_conflict_or_tension"]
    if found == 1:
        return 10, []
    return 20, []


def _score_narrative(text: str) -> tuple[int, list[str]]:
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    lower = text.lower()
    connector_count = sum(1 for c in _DISCOURSE_CONNECTORS if c in lower)
    if connector_count >= 2 and len(sentences) >= 4:
        return 20, []
    if connector_count >= 1 or len(sentences) >= 3:
        return 10, []
    return 0, ["weak_narrative_structure"]


def _score_hook(text: str) -> tuple[int, list[str]]:
    sentences = [s.strip() for s in re.split(r'[.!?]+', text) if s.strip()]
    if not sentences:
        return 0, ["weak_hook_potential"]
    first = sentences[0]
    # Direct question
    if "?" in first:
        return 20, []
    # Direct address or bold opener
    if _HOOK_ADDRESS_RE.match(first.strip()):
        return 20, []
    # Number + superlative/comparative
    if _NUMBER_RE.search(first) and re.search(
        r'\b(most|best|greatest|worst|ever|never|only|first|last)\b', first, re.IGNORECASE
    ):
        return 20, []
    # Short punchy opener is still ok
    if len(first.split()) <= 12:
        return 10, []
    return 0, ["weak_hook_potential"]


def evaluate_context(context: str) -> tuple[int, list[str]]:
    """Score the input context for video engagement potential (0–100).

    Returns (score, issues) where issues is a list of axis-failure labels.
    score < ENRICH_THRESHOLD (60) means the context should be enriched.
    """
    if not context or not context.strip():
        return 0, [
            "context_too_short", "lacks_specific_details",
            "no_conflict_or_tension", "weak_narrative_structure", "weak_hook_potential",
        ]

    words = context.split()
    s1, i1 = _score_length(words)
    s2, i2 = _score_specificity(context)
    s3, i3 = _score_stakes(context)
    s4, i4 = _score_narrative(context)
    s5, i5 = _score_hook(context)

    total = s1 + s2 + s3 + s4 + s5
    issues = i1 + i2 + i3 + i4 + i5
    return total, issues


def llm_enrich(context: str, niche: str, llm) -> str | None:
    """Call the LLM to enrich a thin context for video engagement.

    Returns the enriched context string, or None on any failure.
    The caller is responsible for wrapping this in record_stage().
    """
    messages = [
        {
            "role": "system",
            "content": (
                "You are a video content strategist. "
                "You improve topic descriptions to maximise short-form video engagement. "
                "Return the improved context only — no explanation, no markdown."
            ),
        },
        {
            "role": "user",
            "content": (
                "Improve the following context for a short-form video script.\n\n"
                "Requirements:\n"
                "- Add specific details (names, numbers, dates, events) if mentioned but vague\n"
                "- Add at least one clear conflict, risk, or open question that creates tension\n"
                "- Make the first sentence a strong hook: a direct question, bold claim, "
                "or direct address to the viewer\n"
                "- Preserve all original facts — do not invent statistics or events\n"
                "- Max 1200 words\n\n"
                f"NICHE: {niche or 'general'}\n\n"
                f"CONTEXT:\n{context}"
            ),
        },
    ]
    try:
        result = llm.complete(messages)
        if result and result.strip():
            return result.strip()
        return None
    except Exception as exc:
        _log.warning("llm_enrich failed: %s", exc)
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
.venv/bin/pytest tests/test_context_enricher.py -v
```

Expected: all 12 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
.venv/bin/pytest -q
```

Expected: 80 passed (68 existing + 12 new).

- [ ] **Step 6: Commit**

```bash
git add engine/generation/context_enricher.py tests/test_context_enricher.py
git commit -m "feat: add context evaluator and LLM enricher"
```

---

## Task 4: Enrich Context Celery Task

**Files:**
- Create: `worker/tasks/enrich_context.py`
- Modify: `worker/celery_app.py`

- [ ] **Step 1: Write failing tests in `tests/test_enrich_context_task.py`**

```python
"""Tests for enrich_context task logic — mocks DB and LLM."""
import pytest
from unittest.mock import MagicMock, patch, call


def _make_job(status="pending", meta=None):
    job = MagicMock()
    job.id = 1
    job.reel_id = 10
    job.status = MagicMock()
    job.status.value = status
    job.meta = meta or {"generation_path": "auto"}
    job.attempts = 0
    job.progress = 0
    return job


def _make_reel(context="Some context about a topic.", enriched_context=None):
    reel = MagicMock()
    reel.id = 10
    reel.context = context
    reel.enriched_context = enriched_context
    reel.niche = "sports"
    reel.status = MagicMock()
    reel.status.value = "enriching"
    return reel


def test_idempotency_guard_done_job():
    """Task returns immediately if job is already done."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job(status="done")
    db = MagicMock()
    db.get.return_value = job
    with patch("worker.tasks.enrich_context.SessionLocal", return_value=db):
        enrich_context(1)
    # generate_guide should never be enqueued
    db.add.assert_not_called()


def test_idempotency_guard_running_job():
    """Task returns immediately if job is already running."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job(status="running")
    db = MagicMock()
    db.get.return_value = job
    with patch("worker.tasks.enrich_context.SessionLocal", return_value=db):
        enrich_context(1)
    db.add.assert_not_called()


def test_enrichment_runs_when_score_below_threshold():
    """When evaluate_context returns score < 60, llm_enrich is called."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.enrich_context.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(40, ["context_too_short"])) as mock_eval,
        patch("worker.tasks.enrich_context.llm_enrich", return_value="Enriched context.") as mock_enrich,
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_eval.assert_called_once_with(reel.context)
    mock_enrich.assert_called_once()
    assert reel.enriched_context == "Enriched context."


def test_enrichment_skipped_when_score_above_threshold():
    """When evaluate_context returns score >= 60, llm_enrich is NOT called."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.enrich_context.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(75, [])),
        patch("worker.tasks.enrich_context.llm_enrich") as mock_enrich,
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_enrich.assert_not_called()
    assert reel.enriched_context is None  # original _make_reel has None


def test_generate_guide_always_enqueued_on_success():
    """generate_guide.delay() is called regardless of whether enrichment ran."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.enrich_context.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(80, [])),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    mock_gen.delay.assert_called_once()


def test_llm_failure_is_nonfatal_and_generate_still_enqueued():
    """If llm_enrich returns None, the task continues and enqueues generate_guide."""
    from worker.tasks.enrich_context import enrich_context
    job = _make_job()
    reel = _make_reel()
    db = MagicMock()
    db.get.side_effect = lambda model, id_: job if id_ == 1 else reel

    with (
        patch("worker.tasks.enrich_context.SessionLocal", return_value=db),
        patch("worker.tasks.enrich_context.evaluate_context", return_value=(30, ["context_too_short"])),
        patch("worker.tasks.enrich_context.llm_enrich", return_value=None),
        patch("worker.tasks.enrich_context.get_enrichment_provider", return_value=MagicMock()),
        patch("worker.tasks.enrich_context.generate_guide") as mock_gen,
        patch("worker.tasks.enrich_context.transition"),
        patch("worker.tasks.enrich_context.record_stage"),
    ):
        enrich_context(1)

    assert reel.enriched_context is None  # no enrichment applied
    mock_gen.delay.assert_called_once()   # generate still runs
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
.venv/bin/pytest tests/test_enrich_context_task.py -v
```

Expected: all 5 tests FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Create `worker/tasks/enrich_context.py`**

```python
import logging
from datetime import datetime, timezone

from api import models
from api.config import settings
from api.db import SessionLocal
from api.state import REEL_TRANSITIONS, transition
from engine.generation.context_enricher import evaluate_context, llm_enrich, ENRICH_THRESHOLD
from engine.generation.llm import get_enrichment_provider
from engine.observability import record_stage
from worker.celery_app import celery_app
from worker.tasks.generate import generate_guide

_log = logging.getLogger(__name__)


def _heartbeat(db, job, progress: int) -> None:
    job.progress = progress
    job.heartbeat_at = datetime.now(timezone.utc)
    db.commit()


@celery_app.task(bind=True, max_retries=0)
def enrich_context(self, job_id: int):
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            return
        if job.status in (models.JobStatus.done, models.JobStatus.running):
            return

        reel = db.get(models.Reel, job.reel_id)

        job.status = models.JobStatus.running
        job.started_at = datetime.now(timezone.utc)
        job.heartbeat_at = job.started_at
        job.attempts = (job.attempts or 0) + 1
        job.progress = 10
        db.commit()

        # ── Step 1: Evaluate context quality ─────────────────────────────────
        score, issues = evaluate_context(reel.context)
        job.meta = {**(job.meta or {}), "context_score": score, "context_issues": issues}
        _heartbeat(db, job, 30)

        # ── Step 2: Enrich if below threshold ────────────────────────────────
        if score < ENRICH_THRESHOLD:
            _heartbeat(db, job, 40)
            llm = get_enrichment_provider()
            enrich_provider = "nvidia" if settings.nvidia_api_key else "ollama"
            with record_stage(db, reel.id, "context_enrich", provider=enrich_provider) as ev:
                enriched = llm_enrich(reel.context, reel.niche or "", llm)
                ev.detail["score_before"] = score
                ev.detail["issues"] = issues

            if enriched:
                reel.enriched_context = enriched
                job.meta = {**(job.meta or {}), "enriched": True}
                _log.info("reel_id=%s: context enriched (score was %d)", reel.id, score)
            else:
                job.meta = {**(job.meta or {}), "enriched": False, "enrich_failed": True}
                _log.warning(
                    "reel_id=%s: llm_enrich returned None (score %d), proceeding with original",
                    reel.id, score,
                )
        else:
            job.meta = {**(job.meta or {}), "enriched": False}
            _log.info("reel_id=%s: context score %d >= threshold, skipping enrichment", reel.id, score)

        _heartbeat(db, job, 80)

        # ── Step 3: Transition reel + create and enqueue generate job ─────────
        transition(reel, "generating", REEL_TRANSITIONS)

        generation_path = (job.meta or {}).get("generation_path", "auto")
        generate_job = models.Job(
            type=models.JobType.generate,
            reel_id=reel.id,
            status=models.JobStatus.pending,
            progress=0,
            meta={"generation_path": generation_path, "context_score": score},
        )
        db.add(generate_job)

        job.status = models.JobStatus.done
        job.progress = 100
        job.heartbeat_at = datetime.now(timezone.utc)
        job.error = None
        db.commit()
        db.refresh(generate_job)

        generate_guide.delay(generate_job.id)

    except Exception as exc:
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = models.JobStatus.failed
            job.error = str(exc)[:2000]
            reel = db.get(models.Reel, job.reel_id) if job.reel_id else None
            if reel and reel.status.value == "enriching":
                try:
                    transition(reel, "failed", REEL_TRANSITIONS)
                except ValueError:
                    pass
            db.commit()
        raise
    finally:
        db.close()
```

- [ ] **Step 4: Add `enrich_context` to `worker/celery_app.py`**

```python
celery_app = Celery(
    "reel_maker",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "worker.tasks.noop",
        "worker.tasks.generate",
        "worker.tasks.render",
        "worker.tasks.maintenance",
        "worker.tasks.enrich_context",   # ← add this line
    ],
)
```

And in `task_routes`:
```python
task_routes={
    "worker.tasks.render.render_cut":                  {"queue": "rendering"},
    "worker.tasks.generate.generate_guide":            {"queue": "generation"},
    "worker.tasks.noop.noop_job":                      {"queue": "generation"},
    "worker.tasks.enrich_context.enrich_context":      {"queue": "generation"},   # ← add
},
```

- [ ] **Step 5: Run task tests to verify they pass**

```bash
.venv/bin/pytest tests/test_enrich_context_task.py -v
```

Expected: all 5 tests PASS.

- [ ] **Step 6: Run full test suite**

```bash
.venv/bin/pytest -q
```

Expected: 85 passed.

- [ ] **Step 7: Commit**

```bash
git add worker/tasks/enrich_context.py worker/celery_app.py tests/test_enrich_context_task.py
git commit -m "feat: add enrich_context Celery task and queue routing"
```

---

## Task 5: Router Changes + Active-Job-Fragment Endpoint

**Files:**
- Modify: `api/routers/reels.py`

- [ ] **Step 1: Update `POST /api/reels` to enqueue `enrich_context`**

Replace the current import and handler in `api/routers/reels.py`:

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from typing import Annotated, Optional

from api.db import get_db
from api import models
from api.state import transition, REEL_TRANSITIONS
from worker.tasks.enrich_context import enrich_context

router = APIRouter()
templates = Jinja2Templates(directory="ui/templates")


@router.post("/reels", response_class=HTMLResponse)
def create_reel(
    request: Request,
    context: Annotated[str, Form()],
    niche: Annotated[Optional[str], Form()] = None,
    voiceover_mode: Annotated[str, Form()] = "voiceover",
    target_length_s: Annotated[float, Form()] = 45.0,
    generation_path: Annotated[str, Form()] = "auto",
    db: Session = Depends(get_db),
):
    reel = models.Reel(
        context=context,
        niche=niche,
        voiceover_mode=voiceover_mode,
        status=models.ReelStatus.draft,
    )
    db.add(reel)
    db.flush()

    transition(reel, "enriching", REEL_TRANSITIONS)

    for platform in [models.CutPlatform.youtube_shorts, models.CutPlatform.instagram_reels]:
        cut = models.Cut(
            reel_id=reel.id,
            platform=platform,
            target_length_s=target_length_s,
            status=models.CutStatus.draft,
        )
        db.add(cut)

    job = models.Job(
        type=models.JobType.enrich,
        reel_id=reel.id,
        status=models.JobStatus.pending,
        progress=0,
        meta={"generation_path": generation_path},
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    enrich_context.delay(job.id)

    return templates.TemplateResponse(
        request, "fragments/pipeline_status.html",
        {"job": job, "reel": reel,
         "poll_url": f"/api/reels/{reel.id}/active-job-fragment"},
    )


@router.get("/reels/{reel_id}/active-job-fragment", response_class=HTMLResponse)
def active_job_fragment(reel_id: int, request: Request, db: Session = Depends(get_db)):
    reel = db.get(models.Reel, reel_id)
    if not reel:
        raise HTTPException(status_code=404, detail="Reel not found")

    # Return the currently active job (pending/running), falling back to latest
    active_job = (
        db.query(models.Job)
        .filter(
            models.Job.reel_id == reel_id,
            models.Job.status.in_([models.JobStatus.pending, models.JobStatus.running]),
        )
        .order_by(models.Job.created_at.desc())
        .first()
    )
    job = active_job or (
        db.query(models.Job)
        .filter(models.Job.reel_id == reel_id)
        .order_by(models.Job.created_at.desc())
        .first()
    )
    if not job:
        raise HTTPException(status_code=404, detail="No jobs for reel")

    poll_url = f"/api/reels/{reel_id}/active-job-fragment"
    return templates.TemplateResponse(
        request, "fragments/pipeline_status.html",
        {"job": job, "reel": reel, "poll_url": poll_url},
    )


@router.get("/reels/{reel_id}", response_class=HTMLResponse)
def reel_detail(reel_id: int, request: Request, db: Session = Depends(get_db)):
    reel = db.get(models.Reel, reel_id)
    if not reel:
        raise HTTPException(status_code=404, detail="Reel not found")
    cuts = db.query(models.Cut).filter(models.Cut.reel_id == reel_id).all()
    return templates.TemplateResponse(
        request, "reel.html",
        {"reel": reel, "cuts": cuts},
    )
```

- [ ] **Step 2: Run full test suite to check nothing broke**

```bash
.venv/bin/pytest -q
```

Expected: 85 passed.

- [ ] **Step 3: Commit**

```bash
git add api/routers/reels.py
git commit -m "feat: route POST /api/reels through enrich_context; add active-job-fragment endpoint"
```

---

## Task 6: Pipeline Status Template

**Files:**
- Create: `ui/templates/fragments/pipeline_status.html`

- [ ] **Step 1: Create `ui/templates/fragments/pipeline_status.html`**

```html
{% set status = job.status.value %}
{% set job_type = job.type.value %}
{% set reel_done = reel.status.value in ("guide_ready", "failed") %}
{% set is_final = reel_done or (status == "failed") %}

<div
  {% if not is_final %}
    hx-get="{{ poll_url }}"
    hx-trigger="load delay:2s"
    hx-swap="outerHTML"
  {% endif %}
  class="job-status"
>
    <span class="badge badge-{{ status }}">{{ status }}</span>

    {% if job_type == "enrich" %}
    <span class="badge badge-ok" title="Evaluating and enriching input context">context prep</span>
    {% elif job_type == "generate" %}
        {% set path = job.meta.path if job.meta and job.meta.path else None %}
        {% if path == "structured" %}
        <span class="badge badge-ok" title="Structured-script path — fast (~60–120 s)">structured · {{ job.meta.stub_count if job.meta else "?" }} beats</span>
        {% elif path == "standard" %}
        <span class="badge badge-warn" title="Standard LLM path — ~2–5 min, up to 3 retries">standard LLM</span>
        {% endif %}
    {% endif %}

    {% if status != "pending" %}
    <div class="progress-bar">
        <div class="progress-fill{% if status == 'running' %} progress-fill--animated{% endif %}" style="width: {{ job.progress }}%"></div>
    </div>
    <span class="progress-label">{{ job.progress }}%</span>
    {% endif %}

    {% if status == "running" %}
    <p class="progress-hint">
        {% if job_type == "enrich" %}
            {% if job.progress <= 30 %}Analysing context quality…
            {% else %}Enriching context with AI…
            {% endif %}
        {% else %}
            {% if job.progress <= 20 %}Generating guide with AI — takes 1–3 min…
            {% elif job.progress <= 50 %}Retrying AI generation…
            {% else %}Saving guide…
            {% endif %}
        {% endif %}
    </p>
    {% endif %}

    {% if job_type == "enrich" and status == "done" %}
    <p class="progress-hint">Context ready — starting guide generation…</p>
    {% endif %}

    {% if job.error and status == "failed" %}
    <p class="error">{{ job.error }}</p>
    {% endif %}

    {% if job_type == "generate" and status == "done" and job.reel_id %}
    <p class="success">
        Guide ready
        {% if job.meta and job.meta.quality_score %}
            <span style="color:#6b7280;font-weight:400;"> · quality score {{ job.meta.quality_score }}/100</span>
        {% endif %}
        {% if job.meta and job.meta.context_score is defined %}
            <span style="color:#6b7280;font-weight:400;"> · context score {{ job.meta.context_score }}/100</span>
        {% endif %}
        — <a href="/api/reels/{{ job.reel_id }}" style="color:#065f46;font-weight:600;">View guide →</a>
    </p>
    {% endif %}
</div>
```

- [ ] **Step 2: Run full test suite**

```bash
.venv/bin/pytest -q
```

Expected: 85 passed.

- [ ] **Step 3: Commit**

```bash
git add ui/templates/fragments/pipeline_status.html
git commit -m "feat: add pipeline_status.html template for reel-level job polling"
```

---

## Task 7: Generate Task — Use Effective Context

**Files:**
- Modify: `worker/tasks/generate.py`

- [ ] **Step 1: Update `_generate_from_structured_script` signature to accept `context` param**

Change the function signature and replace `reel.context` usages inside it:

```python
def _generate_from_structured_script(
    reel, cuts, llm, db, target_lengths: dict, stubs: list[BeatStub], context: str
) -> MasterGuide:
```

Inside the function, replace both occurrences of `reel.context` with `context`:

Line that calls `_enrich_with_insight`:
```python
_enrich_with_insight(stubs, context, enrichment_llm)
```

Line that calls `_make_conflict_stub`:
```python
result = _make_conflict_stub(context, enrichment_llm, index=len(stubs) - 1)
```

- [ ] **Step 2: Update `generate_guide` task to compute `effective_context` and use it everywhere**

After loading `reel` (immediately after `db.get(models.Reel, job.reel_id)`), add:

```python
reel = db.get(models.Reel, job.reel_id)
effective_context = reel.enriched_context or reel.context
```

Then replace every `reel.context` in `generate_guide` with `effective_context`:

1. `stubs = script_parser.parse(effective_context)`
2. Call to `_generate_from_structured_script` — add `context=effective_context`:
   ```python
   guide = _generate_from_structured_script(
       reel, cuts, llm, db, target_lengths, stubs, context=effective_context
   )
   ```
3. `rule_s, rule_i = score_guide(effective_context, guide, max_target)` (structured path)
4. `last_score, last_issues = _combined_score(rule_s, rule_i, guide, effective_context, ...)` (structured path)
5. `build_messages(context=effective_context, ...)` (standard path)
6. `score_guide(effective_context, candidate, max_target)` (standard path)
7. `_combined_score(rule_s, rule_i, candidate, effective_context, ...)` (standard path)
8. `_enrich_standard_path_guide(candidate, effective_context)` (standard path)

- [ ] **Step 3: Run full test suite**

```bash
.venv/bin/pytest -q
```

Expected: 85 passed.

- [ ] **Step 4: Commit**

```bash
git add worker/tasks/generate.py
git commit -m "feat: generate_guide uses enriched_context when available"
```

---

## Task 8: Final Verification

- [ ] **Step 1: Run complete test suite one final time**

```bash
.venv/bin/pytest -v
```

Expected: 85 passed, 0 failed.

- [ ] **Step 2: Verify migration is clean**

```bash
.venv/bin/alembic current
```

Expected: `0003 (head)`

- [ ] **Step 3: Smoke-test import of all modified modules**

```bash
.venv/bin/python -c "
from engine.generation.context_enricher import evaluate_context, llm_enrich, ENRICH_THRESHOLD
from worker.tasks.enrich_context import enrich_context
from api.routers.reels import router
from api.models import ReelStatus, JobType
assert ReelStatus.enriching
assert JobType.enrich
assert ENRICH_THRESHOLD == 60
print('All imports OK')
"
```

Expected: `All imports OK`

- [ ] **Step 4: Final commit if any loose ends**

```bash
git status
# commit anything uncommitted
```
