# implementation_plan — reaper resumes killed jobs (v1)

## Payload

```yaml
implementation_plan:
  plan_set_id: PLANSET-9c488325df41226b
  plan_id: PLANSET-9c488325df41226b-01
  title: Reaper resumes killed jobs for idempotent job types (Medium-severity Open Issue, High-criticality change)
  readiness: READY
  target_repo: https://github.com/luckyrjain/reel-maker
  executor: loop-task-implementer

  source_set:
    system_design_spec:
      path: docs/specs/2026-09-reaper-resume-killed-jobs-system-design.md
      sha256: 660120d232ba3cd26161ec2b84d20f785a2436684a928b88d7b5da22c62c7d1a
      status: reviewed twice, revised each round, Ready for implementation
    architecture_review_report: null
      # Same scope call as this pipeline's prior two plans — see "Architecture review scope
      # note" below for why this is deliberate, not a missing artifact, even at High criticality.
    change_impact_report:
      path: docs/specs/2026-09-reaper-resume-killed-jobs-change-impact-report.md
      sha256: c9442be1c8dd67d2bb0c4b89435b49aff3e1749962a831719ea9195daee63c63
      coverage_status: COMPLETE
      criticality: High
    specialist_reports: []
      # change_impact_report's 5 review_triggers are folded into T2/T3/T4's own required_tests
      # and T6's terminal review task, not routed to a separate specialist skill.

  tasks:
    - id: T1
      title: Add Job.reaper_resumes + Job.claim_token columns
      dependencies: []
      target_paths:
        - api/models.py
        - migrations/versions/0013_reaper_resume_and_claim_token.py
      action: >
        Add reaper_resumes = Column(Integer, default=0, nullable=False, server_default="0") and
        claim_token = Column(Integer, default=0, nullable=False, server_default="0") to the Job
        model (api/models.py), placed after attempts. Generate migration 0013 (next free number —
        confirmed against migrations/versions/, current head is 0012_rendered_pins_fingerprint.py).
        Both columns single-step nullable=False + server_default against the live jobs table —
        directly precedented by 0002_improvements.py's identical treatment of
        cut_assets.beat_index/order_in_beat and assets.safe_to_publish (verify this precedent
        yourself by reading that migration, not just citing it). No separate backfill step.
      required_tests:
        - "alembic upgrade head / downgrade -1 / upgrade head round-trip against real Postgres"
      verification_commands:
        - ".venv/bin/alembic revision --autogenerate -m 'reaper resume and claim token'"
        - ".venv/bin/alembic upgrade head"
        - ".venv/bin/alembic downgrade -1"
        - ".venv/bin/alembic upgrade head"
      estimate_loc: 20
      source_trace: [system_design_spec §5, change_impact_report impacted_data]

    - id: T2
      title: Fencing-token mechanism in worker/tasks/common.py (5 checkpoints)
      dependencies: [T1]
      target_paths:
        - worker/tasks/common.py
        - tests/test_job_lifecycle.py
      action: >
        THE HIGHEST-STAKES TASK IN THIS PLAN — worker/tasks/common.py is the most heavily-tested
        file in this repository (122 existing tests in tests/test_job_lifecycle.py, hardened
        across multiple documented mutation-testing rounds per CLAUDE.md's own history). Read the
        design doc's §2 and §4 in full, twice, before touching any code — it documents TWO rounds
        of adversarial review that each found a real correctness gap in this exact mechanism
        before any code existed; do not re-derive a simpler version that reopens either gap.

        Add token: int | None = None to _advance()'s signature, added to its WHERE clause only
        when not None. Thread token=job.claim_token into EXACTLY these five call sites (not more,
        not fewer — see design §2's "what does NOT need to change" for why _fail_job_keep_owner
        and the reaper's own resume-CAS are correctly excluded):
        1. heartbeat()
        2. lock_job()
        3. the done-stamp CAS inside job_task's run()
        4. _settle_failure's retry-reset CAS (the inline _advance() call in its
           `not committed and retriable` branch) — requires adding token: int | None = None to
           _settle_failure's own signature, forwarded from run()'s except Exception handler as
           token=(job.claim_token if owned else None)
        5. _fail_job (as called from _settle_failure's non-retriable branch) — requires adding
           token: int | None = None to _fail_job's own signature too; every OTHER existing caller
           of _fail_job (the reaper's own fail path via a different function, _fail_interrupted,
           _fail_rejected_retry) keeps passing no token, unaffected — do not add a token parameter
           to those call sites, only the one inside _settle_failure's non-retriable branch.

        Additionally, _heartbeat_loop gains a required claim_token: int parameter (this DOES
        change its signature — update its one caller, the thread-start line inside run(), in the
        same commit), threaded into its own periodic _advance() call.

        The initial claim step (pending->running) bumps claim_token via a SQL-side
        Job.claim_token + 1 expression in the same _advance() UPDATE, then loads the real
        post-increment value onto the in-memory job object — either via db.refresh(job)
        immediately after (works correctly with expire_on_commit=False, which only suppresses
        AUTOMATIC expiration on commit and has no effect on an explicit refresh()) or via
        SQLAlchemy 2.0's .returning() in one round trip instead of two (this repo runs 2.0.52,
        confirmed available — prefer this if implementation time allows, it's the cleaner
        pattern the design's own review round two flagged as worth adopting).
      required_tests:
        - "A claim bumps claim_token, and the bumped value is what subsequent heartbeat()/
           lock_job() calls within that same run use."
        - "THE CORE REGRESSION THIS DESIGN EXISTS FOR: claim a job capturing token T1, externally
           force a second claim bumping to token T2 on the same row (simulating a reaper resume +
           a second worker's claim), then call heartbeat()/lock_job()/the done-stamp using the
           stale T1 — assert each is correctly fenced (JobLost, or the done-stamp's existing
           'reaped mid-run' discard). Mutation-test by temporarily removing the token predicate
           from _advance() and confirming this exact test fails."
        - "ROUND-TWO GAP #1, must be its own explicit test, not just re-derived from the test
           above: after a second claim has landed (token T2), simulate the ORIGINAL (token T1)
           run raising a transient exception and reaching _settle_failure's retry-reset branch
           with token=T1 — assert the CAS does NOT match (0 rows updated), so the RESUMED run's
           row is left untouched rather than being bounced back to pending out from under it.
           Repeat for the non-retriable path through _fail_job. Mutation-test both by temporarily
           dropping the token argument at each call site and confirming these specific tests fail."
        - "ROUND-TWO GAP #2, must be its own explicit test: after a second claim has landed
           (token T2), invoke _heartbeat_loop's underlying _advance() call (or one iteration of
           the loop itself) with the now-stale claim_token T1 — assert heartbeat_at is NOT
           advanced. Mutation-test by temporarily omitting the token argument there too."
        - "ALL 122 pre-existing tests in this file must pass UNMODIFIED — if any existing test
           needed a change to keep passing, that itself is a signal the token addition touched
           behavior it should not have. Do not adjust an existing test's assertions to accommodate
           this change; if one breaks, the implementation has a bug, not the test."
      verification_commands:
        - ".venv/bin/pytest tests/test_job_lifecycle.py -v"
      estimate_loc: 180
      source_trace: [system_design_spec §2 "second revision note", §3, §4, §9.2, change_impact_report review_triggers.1, review_triggers.2]

    - id: T3
      title: Resume branch in worker/tasks/maintenance.py with differentiated budgets
      dependencies: [T1, T2]
      target_paths:
        - worker/tasks/maintenance.py
        - tests/test_maintenance.py
      action: >
        New _RESUMABLE_TASKS: dict[str, tuple[Callable, int]] mapping enrich -> (enrich_context, 2),
        render -> (render_cut, 2), generate -> (generate_guide, 1) — generate's budget is
        DELIBERATELY 1, not 2, per the design's §7 cost-multiplication arithmetic; do not simplify
        to a single shared constant. publish is deliberately absent from this dict; add a
        module-level assert "publish" not in _RESUMABLE_TASKS as a second, structural safeguard
        beyond the dict's own omission (per design §4/§9's "publish must never be auto-resumed"
        requirement, which the change-impact report flags as the one hard-safety negative case
        in this whole plan). Thread job_type through reap_stuck_jobs's existing candidate-building
        loop into _reap_one (snapshotted as a plain string, j.type.value, at the same point
        id/status are already snapshotted — for the same "session expires on commit" reason the
        existing code already documents for status). _reap_one gains a resume branch: for a
        running-stale candidate of a resumable type under its type-specific budget, CAS
        running->pending with reaper_resumes+1 (WHERE also requires reaper_resumes < max_resumes),
        commit, THEN call task.delay(job_id) (commit-before-enqueue ordering is load-bearing —
        see design §6). Every other candidate (pending-stale, done-orphan, publish-typed, or a
        resumable type past its budget) falls through to the existing fail-CAS unchanged.
      required_tests:
        - "A stale running enrich/render job under its budget (2) resumes: status->pending,
           reaper_resumes incremented, the correct task's .delay() called with the job id."
        - "generate resumes only once (budget 1), then fails normally on a second stale detection
           — a distinct test from enrich/render's budget-2 case, not a parametrized variant that
           could hide the differentiated-budget requirement being silently dropped."
        - "A stale running publish job is NEVER resumed regardless of reaper_resumes's value —
           the hard-safety negative case. Also confirm the module-level assert actually fires if
           publish were (hypothetically) added to the dict, by testing it directly if practical."
        - "pending-stale and done-orphan candidates are completely unaffected — existing tests for
           both must pass unmodified."
        - "Commit-before-enqueue ordering: assert the CAS is durably committed (query the DB
           directly) before the mocked .delay() is invoked."
      verification_commands:
        - ".venv/bin/pytest tests/test_maintenance.py -v"
      estimate_loc: 90
      source_trace: [system_design_spec §3, §4, §6, §7, §9.3, change_impact_report review_triggers.3, review_triggers.4]

    - id: T4
      title: Fix job.meta staleness leak in generate_guide
      dependencies: []
      target_paths:
        - worker/tasks/generate.py
        - tests/test_generate_task.py
      action: >
        At the very top of generate_guide, before the structured-path branch runs, strip
        {"structured_fallback", "structured_score", "path"} from whatever job.meta already holds
        (from a prior killed or retried attempt of this same Job row) — do not rely on additive
        merge to self-correct. Confirmed exact write site this leak comes from:
        worker/tasks/generate.py:408 (`job.meta = {**(job.meta or {}), "structured_score":
        last_score, "structured_fallback": True}`, inside the structured-path quality-gate-failed
        branch). Do NOT strip context_score, performance_note_ids, or any other legitimately-
        persisted key — only the three fallback-tracking keys named above. This task has no
        dependency on T1-T3 (a standalone bug fix, reachable via ordinary task-level retry today,
        not just via the new resume mechanism) and can be implemented in parallel with them.
      required_tests:
        - "A killed/retried attempt's committed structured_fallback=True / structured_score do
           NOT leak into a SUBSEQUENT clean structured-path success's final job.meta — this repo's
           established mutation-testing convention applies here too: write the naive (buggy)
           version first if unsure, confirm a new test fails against it, then apply the fix and
           confirm it passes."
        - "Confirm engine/generation/estimate.py::estimate_generation()'s existing
           structured_fallback-exclusion logic is unaffected by this fix for the case where
           structured_fallback really IS True (a genuine, non-leaked fallback) — the strip-then-
           rebuild must not accidentally suppress a real fallback signal, only a stale one."
      verification_commands:
        - ".venv/bin/pytest tests/test_generate_task.py -v"
      estimate_loc: 25
      source_trace: [system_design_spec §7 "job.meta staleness", change_impact_report review_triggers.5]

    - id: T5
      title: Documentation updates
      dependencies: [T1, T2, T3, T4]
      target_paths:
        - CLAUDE.md
        - docs/roadmap.md
      action: >
        CLAUDE.md: a new Key conventions entry explaining the fencing-token mechanism (§2's full
        reasoning — why status alone stops being a reliable ownership signal once resume exists,
        why five checkpoints need the token and two specific ones (_fail_job_keep_owner, the
        reaper's resume-CAS) provably don't, and the two round-two gaps this exists to prevent
        reintroducing if a future change touches this file carelessly), a module-layout entry for
        maintenance.py's _RESUMABLE_TASKS/differentiated budgets, and a Data model entry for both
        new Job columns. Match the depth of this file's existing entries for comparably subtle
        rules (e.g. the SRT caption export's offset-handling entry, or the pins-staleness gate's
        None-means-legacy entry) — this is exactly the same class of "would silently reintroduce a
        real bug if simplified carelessly" documentation this file exists to preserve.
        docs/roadmap.md: mark the "Reaper does not resume killed jobs" Open Issues row resolved,
        including a note that this design went through two review rounds before any code was
        written, at the same level of detail the two prior resolved rows in this table have.
      required_tests: []
      verification_commands:
        - "grep -n claim_token docs/roadmap.md CLAUDE.md"
        - "grep -n reaper_resumes docs/roadmap.md CLAUDE.md"
      estimate_loc: 45
      source_trace: [system_design_spec §9.5]

    - id: T6
      title: Independent adversarial review of the merged diff
      dependencies: [T1, T2, T3, T4, T5]
      target_paths: []
      action: >
        Dispatch an independent reviewer (isolated context, no access to this plan's own
        reasoning) against the real merged diff. Given criticality High and this design's own
        two-round review history, this review must be at least as rigorous as the design-stage
        reviews, not a lighter pass because "the design was already reviewed twice." Focus
        specifically on change_impact_report's 5 review_triggers: (1) independently re-derive that
        BOTH round-two fencing gaps are actually closed in the merged code (read
        _settle_failure/_fail_job/_heartbeat_loop directly, do not trust the design doc's
        description of what the diff does), and independently re-run both new mutation tests
        rather than trusting they were run; (2) confirm all 122+23+11 pre-existing tests across
        the three affected files pass completely unmodified; (3) confirm the rolling-deploy
        None-token-is-safe assumption actually holds for all five modified call sites by reading
        each one; (4) confirm generate's resume budget is genuinely 1, not simplified to match
        enrich/render's 2; (5) confirm the job.meta strip clears exactly the three fallback-
        tracking keys and nothing else. Also re-verify the full default test suite
        (.venv/bin/pytest -q), the golden test, ruff, and the migration round-trip against real
        Postgres one more time on the final merged state.
      required_tests: []
      verification_commands:
        - ".venv/bin/pytest -q"
        - ".venv/bin/pytest -m golden -v"
        - "ruff check --select F,E9 ."
      estimate_loc: 0
      source_trace: [change_impact_report review_triggers.1-5, criticality High rationale]

  execution_waves:
    - wave: 1
      tasks: [T1, T4]
    - wave: 2
      tasks: [T2]
    - wave: 3
      tasks: [T3]
    - wave: 4
      tasks: [T5]
    - wave: 5
      tasks: [T6]

  traceability:
    T1: [system_design_spec §5, change_impact_report impacted_data]
    T2: [system_design_spec §2, §3, §4, §9.2, change_impact_report review_triggers.1-2]
    T3: [system_design_spec §3, §4, §6, §7, §9.3, change_impact_report review_triggers.3-4]
    T4: [system_design_spec §7, change_impact_report review_triggers.5]
    T5: [system_design_spec §9.5]
    T6: [change_impact_report review_triggers.1-5, criticality]
```

## Architecture review scope note

Same rationale as this pipeline's prior two plans: this change adds two nullable-turned-not-null
columns and a bounded set of new parameters onto an already-shipped, already-approved job-lifecycle
architecture — it does not introduce a new architectural decision. The **High** criticality (higher
than either prior fix) reflects blast radius and the track record of this specific mechanism being
easy to get subtly wrong (two design-stage review rounds each found a real gap before any code
existed), not architectural novelty — `job_task`'s own shape, the CAS-based ownership model, and the
reaper's candidate-then-CAS pattern are all unchanged; this design adds one new dimension (a token)
to an existing, well-understood mechanism. `change_impact_report`'s own `impacted_contracts` section
confirms every changed function signature is either additive-optional or has exactly one, already-
identified caller to update in the same commit — there is no ambiguity about blast radius that would
need a separate architectural soundness review to resolve. `T6`'s terminal review is scoped precisely
to re-verifying this specific mechanism's correctness at merge time, which is the actual risk here,
not a system-wide architecture question `architecture-review` would otherwise exist to answer.

## Readiness

**READY.** Every task has grounded target paths (verified against the real repository, with exact
current file:line evidence for every function this design touches), a valid acyclic dependency DAG,
deterministic execution waves, complete traceability, and named verification commands. T2 and T3
explicitly carry forward every concrete requirement from both of the design's review rounds as
first-class, individually-named task instructions — including the two specific round-two
regression tests — rather than leaving them for the Builder to rediscover or approximate during
implementation.
