# implementation_plan — safe_to_publish staleness gate fix (v1)

## Payload

```yaml
implementation_plan:
  plan_set_id: PLANSET-58a834691f4fdee2
  plan_id: PLANSET-58a834691f4fdee2-01
  title: Publish gate must verify the video matches current pins (Medium-severity Open Issue fix)
  readiness: READY
  target_repo: https://github.com/luckyrjain/reel-maker
  executor: loop-task-implementer

  source_set:
    system_design_spec:
      path: docs/specs/2026-09-video-pins-staleness-gate-system-design.md
      sha256: fe042cb76534ef932805ca7304a0007205b3f5be291a105686c3727155f37241
      status: reviewed, revised, Ready for implementation
    architecture_review_report: null
      # Same scope call as the SRT caption export plan (PLANSET-2cc9dec2865f08d2-01) — additive
      # correctness fix onto an already-approved pipeline architecture, not a new architectural
      # decision. change_impact_report's own criticality (Medium, correctness-scoped, zero
      # impacted_services architectural boundary changes) supports this.
    change_impact_report:
      path: docs/specs/2026-09-video-pins-staleness-gate-change-impact-report.md
      sha256: 64eaf7aea170ce3ecedeb254e92420fbb31e984fd6d2d85901015f55579e5e73
      coverage_status: COMPLETE
      criticality: Medium
    specialist_reports: []
      # change_impact_report's 4 review_triggers (test-harness fixture fix, mutation-test of the
      # None-skip rollout property, the end-to-end regression test's non-optional status, finalize-
      # branch isolation) are folded into this plan's own tasks (T5, T6) rather than requiring a
      # separate specialist skill invocation.

  tasks:
    - id: T1
      title: Add Cut.rendered_pins_fingerprint column + migration
      dependencies: []
      target_paths:
        - api/models.py
        - migrations/versions/0012_rendered_pins_fingerprint.py
      action: >
        Add `rendered_pins_fingerprint = Column(String(64))` to the Cut model (api/models.py),
        placed after `black_frame_beat_indices` per the design's §5 precedent. Generate migration
        0012 (next free number — confirmed against migrations/versions/, current head is
        0011_subtitle_caption_export.py). Nullable, additive, no backfill (design §7's explicit,
        reviewed rollout decision — do not add a data migration/backfill step here).
      required_tests:
        - "alembic upgrade head / downgrade -1 / upgrade head round-trip against real Postgres"
      verification_commands:
        - ".venv/bin/alembic revision --autogenerate -m 'rendered pins fingerprint'"
        - ".venv/bin/alembic upgrade head"
        - ".venv/bin/alembic downgrade -1"
        - ".venv/bin/alembic upgrade head"
      estimate_loc: 15
      source_trace: [system_design_spec §5, change_impact_report impacted_data]

    - id: T2
      title: compute_pins_fingerprint() in asset_sourcer.py
      dependencies: []
      target_paths:
        - engine/render/asset_sourcer.py
        - tests/test_asset_sourcer.py
      action: >
        New function compute_pins_fingerprint(db, cut_id) -> str | None, colocated with _fp()
        (line ~561) and resolve_or_reuse() (line ~566) as their natural companion. Query
        CutAsset filtered by cut_id, ordered/sorted by (beat_index, order_in_beat, asset_id) —
        all three are plain ints (confirmed against api/models.py's CutAsset columns), so a tuple
        sort is well-defined and deterministic regardless of query result ordering. Hash the
        canonical joined string (e.g. "b:o:a|b:o:a|...") with hashlib.sha256, truncate to a
        stable length matching the new column's String(64). Return None when the cut has zero
        bound CutAsset rows (every beat black-framed, or nothing rendered yet) — do not return an
        empty-string hash, per the design's explicit "None means nothing to fingerprint yet"
        semantics (§4), consistent with this codebase's other nullable render-artifact columns.
      required_tests:
        - "Deterministic for a given pin set (same input always produces the same fingerprint)"
        - "Order-independent w.r.t. query result ordering (insert pins in different orders,
           confirm identical fingerprint)"
        - "None for zero pins"
        - "Changes when a beat's pin changes (different asset_id for a beat_index)"
        - "Unaffected when an untouched beat's pin stays the same (only the changed beat's
           contribution to the hash differs)"
      verification_commands:
        - ".venv/bin/pytest tests/test_asset_sourcer.py -v"
      estimate_loc: 40
      source_trace: [system_design_spec §3, §4, §10.2]

    - id: T3
      title: render_cut writes rendered_pins_fingerprint
      dependencies: [T1, T2]
      target_paths:
        - worker/tasks/render.py
        - tests/test_render_task.py
      action: >
        In worker/tasks/render.py, at the exact point video_path/thumbnail_path/duration_s are
        already assigned (lines ~146-151, after the db.refresh(cut)/platform_post_id re-check),
        also set cut.rendered_pins_fingerprint = compute_pins_fingerprint(db, cut.id). This point
        is reached only after every beat's resolve_or_reuse() call (and its commit) for this
        render has already landed — the loop that calls resolve_or_reuse() per beat fully
        completes before composite_cut() is even invoked (design §6 "Read timing"). This relies
        on the existing single-render-at-a-time invariant (CUT_TRANSITIONS blocks a second render
        while a cut is already `rendering`) — do not add new locking for this, the invariant
        already exists and is out of scope to touch.
      required_tests:
        - "Call-and-assign wiring ONLY — tests/test_render_task.py is 100%-MagicMock-based today
           (resolve_or_reuse itself is patched, no real CutAsset row ever exists in that file), so
           the honest test here patches compute_pins_fingerprint and asserts it is called with
           cut.id and its return value lands on cut.rendered_pins_fingerprint. Do NOT attempt to
           assert the fingerprint 'reflects real pins' in this file — that stronger property is
           T6's job, not this task's. (This is a design-review-corrected instruction — an earlier
           draft of this plan's source design overclaimed what this file's fixture style could
           prove.)"
      verification_commands:
        - ".venv/bin/pytest tests/test_render_task.py -v"
      estimate_loc: 10
      source_trace: [system_design_spec §3, §6, §10.3 (revised)]

    - id: T4
      title: assert_video_matches_pins() in gate.py
      dependencies: [T1, T2]
      target_paths:
        - engine/publish/gate.py
        - tests/test_publish_gate.py
      action: >
        New function assert_video_matches_pins(db, cut) -> None (takes a Cut object, not cut_id —
        deliberately asymmetric with assert_safe_to_publish(db, cut_id); justified in design §4,
        no real friction since publish_cut is the sole caller of both and already has the Cut
        object loaded). Computes current = compute_pins_fingerprint(db, cut.id); if
        cut.rendered_pins_fingerprint is None, return (no-op — the legacy/rollout-safety skip,
        design §7); otherwise if current != cut.rendered_pins_fingerprint, raise ValueError with a
        clear, actionable message telling the operator to re-render before publishing.
      required_tests:
        - "Match: fingerprints equal -> does not raise (mirror test_publish_gate.py's existing
           three-test fixture style, using db_session from tests/conftest.py)"
        - "Mismatch: fingerprints differ -> raises ValueError with an actionable message"
        - "CRITICAL, most important test in this task per the design's own emphasis:
           rendered_pins_fingerprint is None -> does NOT block, even when current pins compute to
           a real non-None value. Mutation-test this: temporarily make the check fire on None too,
           confirm this specific test fails, revert, confirm it passes again. This is the property
           the entire rollout/backward-compat argument (design §7) depends on."
      verification_commands:
        - ".venv/bin/pytest tests/test_publish_gate.py -v"
      estimate_loc: 30
      source_trace: [system_design_spec §4, §7, §10.4]

    - id: T5
      title: Wire assert_video_matches_pins into publish_cut + fix test_publish_task.py fixture
      dependencies: [T3, T4]
      target_paths:
        - worker/tasks/publish.py
        - tests/test_publish_task.py
      action: >
        In worker/tasks/publish.py's else: branch (line ~51, immediately alongside the existing
        assert_safe_to_publish(db, cut.id) call at line ~52 — same branch, same "before any
        credential lookup or upload" timing), add assert_video_matches_pins(db, cut). Do NOT add
        it to the if cut.platform_post_id: finalize branch (lines ~45-50) — that branch uploads
        nothing and gating it would leave a live post unrecorded with no operator way out, per the
        exact reasoning CLAUDE.md already documents for assert_safe_to_publish's identical scoping.
        MANDATORY PREREQUISITE, not optional cleanup: tests/test_publish_task.py's _cut() helper
        (line ~24) currently returns a bare MagicMock() with no rendered_pins_fingerprint set — on
        an unconfigured MagicMock that attribute is a truthy MagicMock instance, not None, so the
        moment this task's wiring lands, six existing tests that exercise the uploading branch
        (test_deterministic_publisher_failure_transitions_cut_to_failed,
        test_successful_publish_records_platform_post_id,
        test_a_transient_publisher_error_is_never_retried_automatically,
        test_post_id_is_committed_before_anything_that_can_fail_after_the_upload,
        test_missing_credential_fails_with_actionable_message, test_unsafe_asset_blocks_publish)
        will start failing from the NEW check firing on a None-vs-MagicMock mismatch, not from
        whatever each test actually means to exercise. Fix _cut()'s default to explicitly set
        cut.rendered_pins_fingerprint = None in the SAME commit as the publish_cut wiring — this
        is change_impact_report's review_triggers.1, flagged CRITICAL, will break CI if missed.
      required_tests:
        - "All 6 pre-existing uploading-branch tests still pass unmodified after the _cut() fixture
           fix (do not touch their own assertions — only the shared _cut() helper's default)"
        - "NEW test: a mismatch (rendered_pins_fingerprint set to a real value that differs from
           what compute_pins_fingerprint would return against the mocked db) blocks publish —
           publisher never reached, same assertion style already used for the existing
           test_unsafe_asset_blocks_publish. This new test needs its OWN distinct mock for
           whatever compute_pins_fingerprint reads, separate from unsafe_assets' existing
           db.query(...).join(...).filter(...).all() chain stub — do not assume the existing db
           mock configuration covers this new code path for free."
        - "Mutation-test pass confirming the new call is genuinely absent from the finalize branch:
           temporarily move it there, confirm test_an_already_posted_cut_is_finalized_without_uploading_again
           or test_an_already_posted_cut_is_finalized_even_if_an_asset_was_flagged_since fails,
           revert, confirm both pass again."
      verification_commands:
        - ".venv/bin/pytest tests/test_publish_task.py -v"
      estimate_loc: 45
      source_trace: [system_design_spec §6, §10.5 (revised), change_impact_report review_triggers.1, review_triggers.4]

    - id: T6
      title: End-to-end regression test reproducing the exact bug sequence
      dependencies: [T3, T4, T5]
      target_paths:
        - tests/test_publish_gate.py
      action: >
        THE SINGLE MOST IMPORTANT TEST IN THIS PLAN, per the design's own explicit emphasis (§10
        step 6) and change_impact_report's review_triggers.3 (not optional polish — its absence is
        a genuine gap). Using db_session (real SQLite, matching test_publish_gate.py's existing
        style), reproduce the exact failure sequence from the bug report: (1) render a cut with
        safe pins, set cut.video_path and cut.rendered_pins_fingerprint = compute_pins_fingerprint(
        db, cut.id) to simulate a successful render; (2) mutate a beat's CutAsset to a different
        asset_id (simulating resolve_or_reuse()'s re-pin — call resolve_or_reuse() directly if
        practical, or construct the CutAsset row change by hand if that's simpler given this
        file's existing fixture helpers) WITHOUT updating cut.video_path or
        cut.rendered_pins_fingerprint (simulating the second render failing after the re-pin
        commits but before video_path updates); (3) assert assert_video_matches_pins(db, cut) now
        raises for the still-stale video_path. This proves the FIX, not just that each new
        function behaves correctly in isolation against synthetic inputs — items T2/T4's own unit
        tests cannot substitute for this.
      required_tests: []
      verification_commands:
        - ".venv/bin/pytest tests/test_publish_gate.py -v -k staleness"
      estimate_loc: 35
      source_trace: [system_design_spec §10.6, change_impact_report required_tests.5, review_triggers.3]

    - id: T7
      title: Documentation updates
      dependencies: [T1, T2, T3, T4, T5, T6]
      target_paths:
        - CLAUDE.md
        - docs/roadmap.md
        - docs/data-model.md
      action: >
        CLAUDE.md: module-layout entries for asset_sourcer.py/gate.py/render.py's new functions/
        field, a Data model entry for Cut.rendered_pins_fingerprint, and a Key conventions entry
        explaining BOTH the fingerprint-vs-eager-clearing design choice (design §2 — why this
        wasn't done by just clearing video_path) AND the None-means-legacy rollout decision
        (design §7) — both are exactly the "non-obvious, would silently regress if changed
        carelessly" class of rule this file exists to preserve, matching the depth of its existing
        entries for e.g. the SRT caption export's offset-handling rule. docs/roadmap.md: mark the
        Open Issues table entry resolved with a pointer to this fix, at the same level of detail
        every other resolved item there has (see the existing "asset_sourcer degrades silently to
        black frames" row's "Fixed (Phase 7a)" treatment as the precedent to match).
        docs/data-model.md: append 0012 to the migration-list sentence near the top and add a
        rendered_pins_fingerprint row to the ### cuts table, following the black_frame_beat_indices
        row's exact precedent.
      required_tests: []
      verification_commands:
        - "grep -n rendered_pins_fingerprint docs/data-model.md CLAUDE.md docs/roadmap.md"
      estimate_loc: 35
      source_trace: [system_design_spec §10.7, change_impact_report impacted_data]

    - id: T8
      title: Independent adversarial review of the merged diff
      dependencies: [T1, T2, T3, T4, T5, T6, T7]
      target_paths: []
      action: >
        Dispatch an independent reviewer (isolated context) against the real merged diff, focused
        on change_impact_report's 4 review_triggers: (1) re-verify the test_publish_task.py
        fixture fix actually lands and all 6 pre-existing uploading-branch tests genuinely pass,
        not just that new tests were added; (2) re-run the None-skip mutation test for real,
        reading the actual test failure/pass output, not trusting a description of it; (3)
        confirm T6's end-to-end regression test exists, actually reproduces the bug sequence (not
        a weaker synthetic substitute), and actually fails against a reverted fix when mutation-
        tested; (4) confirm the finalize branch is genuinely untouched by re-running its mutation
        test. Also re-verify the full default suite (.venv/bin/pytest -q), golden test, ruff, and
        the migration round-trip against real Postgres one more time on the final merged state.
      required_tests: []
      verification_commands:
        - ".venv/bin/pytest -q"
        - ".venv/bin/pytest -m golden -v"
        - "ruff check --select F,E9 ."
      estimate_loc: 0
      source_trace: [change_impact_report review_triggers.1-4, criticality Medium rationale]

  execution_waves:
    - wave: 1
      tasks: [T1, T2]
    - wave: 2
      tasks: [T3, T4]
    - wave: 3
      tasks: [T5]
    - wave: 4
      tasks: [T6]
    - wave: 5
      tasks: [T7]
    - wave: 6
      tasks: [T8]

  traceability:
    T1: [system_design_spec §5, change_impact_report impacted_data]
    T2: [system_design_spec §3, §4, §10.2]
    T3: [system_design_spec §3, §6, §10.3]
    T4: [system_design_spec §4, §7, §10.4]
    T5: [system_design_spec §6, §10.5, change_impact_report review_triggers.1, review_triggers.4]
    T6: [system_design_spec §10.6, change_impact_report required_tests.5, review_triggers.3]
    T7: [system_design_spec §10.7, change_impact_report impacted_data]
    T8: [change_impact_report review_triggers.1-4, criticality]
```

## Architecture review scope note

Same rationale as `PLANSET-2cc9dec2865f08d2-01` (the SRT caption export plan): this change adds one
nullable column and two small, single-responsibility functions onto an already-shipped, already-
approved pipeline architecture. It does not introduce, revise, or challenge any architectural
decision — the risk profile here is narrower still than the caption-export change, since nothing
here alters an existing function's public signature (`change_impact_report`'s own
`impacted_contracts` section confirms every new surface is additive). `criticality: Medium` reflects
the *safety property* being protected (an unlicensed asset must never ship), not architectural
complexity — the two are independent axes, and this plan's `T8` review task is scoped precisely to
the correctness risk that actually exists here (rollout-safety of the `None`-skip decision, and
whether the fix's own tests genuinely prove it works) rather than a system-wide soundness question
`architecture-review` would otherwise exist to answer.

## Readiness

**READY.** Every task has grounded target paths (verified against the real repository during
change-impact analysis, with exact file:line evidence), a valid acyclic dependency DAG, deterministic
execution waves, complete traceability, and named verification commands. The plan's T5 and T6
explicitly carry forward the two concrete, reviewer-confirmed hazards from the design's own revision
history (the `test_publish_task.py` fixture break and the non-optional end-to-end regression test) as
first-class task instructions, not left implicit for the Builder to rediscover mid-implementation.
