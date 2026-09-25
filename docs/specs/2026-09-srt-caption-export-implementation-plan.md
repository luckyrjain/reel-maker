# implementation_plan — SRT caption export (v1)

## Payload

```yaml
implementation_plan:
  plan_set_id: PLANSET-2cc9dec2865f08d2
  plan_id: PLANSET-2cc9dec2865f08d2-01
  title: SRT caption export (roadmap Phase 5, item 5d)
  readiness: READY
  target_repo: https://github.com/luckyrjain/reel-maker
  executor: loop-task-implementer

  source_set:
    system_design_spec:
      path: docs/specs/2026-09-srt-caption-export-system-design.md
      sha256: e17443c08553aa7632d170084bf032ff177e2d2a84afabff1c6b27957c113e94
      status: reviewed, revised, Ready for implementation
    architecture_review_report: null
      # Deliberately not produced as a separate artifact — see "Architecture review scope note" below.
    change_impact_report:
      path: docs/specs/2026-09-srt-caption-export-change-impact-report.md
      sha256: 911c5084a4da60af24fc32bf92fa3a3eff67a656bf47d7c4cc69df3a2ad8aaf1
      coverage_status: COMPLETE
      criticality: Medium
    specialist_reports: []
      # change_impact_report.review_triggers names 4 items (offset-fix correctness, record_stage
      # composition, YouTube API live verification, docs completeness) — none require a *separate*
      # specialist skill invocation; all 4 are folded into this plan's own task-level verification
      # steps and the terminal independent-review task (T9), per this plan's own judgment call
      # below rather than a missing/unresolved upstream artifact.

  tasks:
    - id: T1
      title: Add Cut.subtitle_path column + migration
      dependencies: []
      target_paths:
        - api/models.py
        - migrations/versions/0011_subtitle_caption_export.py
      action: >
        Add `subtitle_path = Column(String(500))` to the Cut model (api/models.py), immediately
        after `black_frame_beat_indices` per the design's §4 precedent. Generate migration 0011
        (next free number — confirmed against migrations/versions/, current head is
        0010_black_frame_visibility.py). Nullable, additive, no backfill.
      required_tests:
        - "alembic upgrade head / downgrade -1 / upgrade head round-trip against real Postgres
           (docker run postgres:16 on a non-default port, per this repo's established
           migration-verification practice)"
      verification_commands:
        - ".venv/bin/alembic revision --autogenerate -m 'subtitle caption export'"
        - ".venv/bin/alembic upgrade head"
        - ".venv/bin/alembic downgrade -1"
        - ".venv/bin/alembic upgrade head"
      estimate_loc: 15
      source_trace: [system_design_spec §4, change_impact_report impacted_data]

    - id: T2
      title: Extend transcribe_audio() to TranscriptResult without changing .words semantics
      dependencies: []
      target_paths:
        - engine/render/captions.py
        - tests/test_audio_text_sync.py
      action: >
        Refactor transcribe_audio() to return a new TranscriptResult dataclass with .words
        (existing shape/semantics, byte-identical output) and .segments (new — one CaptionSegment
        per Whisper segment, text = full segment text). Do NOT add a caller-supplied absolute
        offset parameter with new meaning — beat_offset_s keeps its existing default-0.0,
        beat-relative meaning for BOTH fields, per system_design_spec §2's "Offset handling" note
        (this is the single most safety-critical instruction in this plan — see review_trigger 1
        below).
      required_tests:
        - "Existing whisper-timestamp-path tests in test_audio_text_sync.py updated for the new
           return shape."
        - "NEW regression test: construct a multi-beat scenario and assert .words-derived
           _whisper_timestamps() drawtext output is byte-identical to the pre-change output —
           this is the explicit guard against the offset-doubling bug the design review caught
           and reverted. Mutation-test it: temporarily make transcribe_audio() apply
           beat_offset_s to .words too, confirm this new test fails, then confirm the correct
           implementation passes."
      verification_commands:
        - ".venv/bin/pytest tests/test_audio_text_sync.py -v"
      estimate_loc: 60
      source_trace: [system_design_spec §2 "Offset handling", §3.1, change_impact_report review_triggers.1]

    - id: T3
      title: New engine/render/srt.py SRT writer module
      dependencies: []
      target_paths:
        - engine/render/srt.py
        - tests/test_srt.py
      action: >
        Pure function write_srt(cues: list[CaptionSegment], path: Path) -> Path | None. Standard
        SRT format (sequential numbering, HH:MM:SS,mmm timestamps). Returns None (no file written)
        when cues is empty. No I/O beyond the write itself; no ffmpeg, no network — unit-testable
        without any real media dependency.
      required_tests:
        - "Timestamp formatting correctness (including sub-second/hour-boundary edge cases)"
        - "Sequential numbering"
        - "Empty cues -> returns None, no file created"
        - "Cues spanning multiple beats concatenate with correctly offset absolute times (using
           pre-shifted CaptionSegment inputs — this module does not do the shifting itself, T4
           does, per system_design_spec §3.2)"
      verification_commands:
        - ".venv/bin/pytest tests/test_srt.py -v"
      estimate_loc: 80
      source_trace: [system_design_spec §3.2]

    - id: T4
      title: Wire caption-cue building + SRT write into composite_cut()
      dependencies: [T2, T3]
      target_paths:
        - engine/render/compositor.py
        - tests/test_compositor.py
        - tests/test_golden_reel.py
      action: >
        In composite_cut(), after building beat_transcripts (now TranscriptResult per beat),
        compute the cumulative per-beat absolute offset via an EXPLICIT running sum over
        beat_durations (do not reuse the first loop's `t` variable — it is fully consumed by the
        time this step runs, per system_design_spec §2/§3.2). Shift each beat's .segments cues by
        that offset (mirroring exactly what _whisper_timestamps() already does for .words), fall
        back to the existing proportional vo_script-sentence-split technique when .segments is
        empty (Whisper not installed), call srt.write_srt(), and return the resulting path (or
        None) as a THIRD element of composite_cut()'s return tuple.
        Update ALL 6 real call sites in the same change (verified exhaustive list from
        change_impact_report.impacted_contracts): worker/tasks/render.py:127 (T5, not this task —
        see below), tests/test_compositor.py:51,196,216,235 (4 sites), tests/test_golden_reel.py:68
        (1 site). This task covers the 4 test_compositor.py sites and the 1 test_golden_reel.py
        site since they are compositor-internal test coverage; T5 covers the production call site
        in render.py as part of wiring subtitle_path onto the Cut row.
      required_tests:
        - "Real-ffmpeg composite_cut() tests (4 in test_compositor.py) updated for the 3-tuple
           return AND extended to assert a real .srt file is produced with real
           Whisper-or-fallback timing."
        - "tests/test_golden_reel.py (Phase 7d's real end-to-end no-mocks test) extended with the
           same assertion — this is explicitly the right home for this class of claim per the
           design's own §10 point 4 reasoning."
      verification_commands:
        - "export PATH=\"/opt/homebrew/opt/ffmpeg-full/bin:$PATH\""
        - ".venv/bin/pytest tests/test_compositor.py -v"
        - ".venv/bin/pytest -m golden -v"
      estimate_loc: 120
      source_trace: [system_design_spec §2, §3.2, §10 point 4, change_impact_report impacted_contracts]

    - id: T5
      title: Store subtitle_path on Cut from render_cut
      dependencies: [T1, T4]
      target_paths:
        - worker/tasks/render.py
      action: >
        Update worker/tasks/render.py:127's composite_cut() call to unpack the new 3-tuple and
        assign cut.subtitle_path = str(subtitle_path) if subtitle_path else None, following the
        exact wholesale-replace-on-re-render policy already used for thumbnail_candidates/
        video_path/black_frame_beat_indices.
      required_tests:
        - "Extend an existing render_cut task test (tests/test_render_task.py) to assert
           cut.subtitle_path is populated after a successful render with VO, and reset to None on
           a re-render that produces zero cues (e.g. silent voiceover_mode)."
      verification_commands:
        - ".venv/bin/pytest tests/test_render_task.py -v"
      estimate_loc: 15
      source_trace: [system_design_spec §5, §6 "Render-time (producer)"]

    - id: T6
      title: Subtitle download route + cut-card UI link
      dependencies: [T1]
      target_paths:
        - api/routers/cuts.py
        - ui/templates/fragments/cut_card.html
        - docs/api.md
      action: >
        New GET /api/cuts/{id}/subtitles route mirroring stream_video/stream_thumbnail's exact
        path-traversal guard (Path.resolve().is_relative_to(VIDEO_STORE_DIR), 403 outside, 404 if
        cut.subtitle_path is unset). Content-Type application/x-subrip. Add a "Download captions
        (.srt)" link to cut_card.html, visible whenever cut.subtitle_path is set (not gated to
        in_review — same visibility rule as the existing video download). Add the new route to
        docs/api.md's ## Cuts section alongside GET /api/cuts/{cut_id}/video, closing the
        pre-existing gap where /thumbnail, /thumbnail/{index}, and /hook-variant shipped without
        ever being documented there (change_impact_report review_triggers.4).
      required_tests:
        - "New router test mirroring test_variants_router.py's stream_thumbnail 403/404 style:
           404 when subtitle_path unset, 403 for a path outside VIDEO_STORE_DIR, 200 + correct
           Content-Type for a valid path."
      verification_commands:
        - ".venv/bin/pytest tests/test_cuts_publish_router.py tests/test_variants_router.py -v"
      estimate_loc: 50
      source_trace: [system_design_spec §3.4, change_impact_report review_triggers.4]

    - id: T7
      title: Best-effort YouTube captions upload after publish
      dependencies: [T5]
      target_paths:
        - engine/publish/youtube.py
        - tests/test_youtube_publisher.py
        - tests/test_publish_task.py
      action: >
        After a successful video upload in YouTubePublisher.publish(), if cut.subtitle_path is
        set, POST to YouTube's captions.insert (multipart: JSON snippet + .srt media part), reusing
        get_valid_access_token(). MUST compose correctly with record_stage()'s real (re-raising)
        semantics: place the try/except INSIDE the `with record_stage(db, cut.reel_id,
        "captions_upload", cut_id=cut.id, provider="youtube") as ev:` block, and on exception
        explicitly set ev.ok = False and ev.detail["error"] = repr(exc) before letting the
        function return normally — per system_design_spec §7's exact corrected code sample. Do
        NOT let the exception propagate (would fail publish_cut, contradicting the "never fail
        the job" requirement) and do NOT catch-without-setting-ev.ok (would silently record a
        failed upload as a successful StageEvent). Do not attempt this on the "already has
        platform_post_id, finalize only" branch of publish_cut (structurally impossible anyway
        since that branch never calls publisher.publish()).
      required_tests:
        - "A captions-upload failure does not fail publish_cut."
        - "A captions-upload failure does not affect cut.platform_post_id/published_at."
        - "CRITICAL (review_trigger 2 — easy to omit): a captions-upload failure DOES result in a
           StageEvent with ok=False and a populated detail.error, not the default ok=True.
           Mutation-test this: force the upload call to raise, confirm the resulting StageEvent
           row actually has ok=False before considering this task done."
        - "One-time manual/live verification note (not an automated test): before treating this
           task as fully done, make one real captions.insert call against a real connected
           YouTube account to confirm the API accepts raw SRT bytes as documented
           (system_design_spec §3.3/§9.2 — flagged as unverified by design, still unverified at
           plan time)."
      verification_commands:
        - ".venv/bin/pytest tests/test_youtube_publisher.py tests/test_publish_task.py -v"
      estimate_loc: 70
      source_trace: [system_design_spec §7, change_impact_report review_triggers.2 and unknowns.1]

    - id: T8
      title: Documentation updates
      dependencies: [T1, T4, T5, T6, T7]
      target_paths:
        - CLAUDE.md
        - docs/roadmap.md
        - docs/data-model.md
      action: >
        CLAUDE.md: module-layout entries for captions.py/srt.py/compositor.py/render.py/youtube.py/
        cuts.py changes, Data model entry for Cut.subtitle_path, Key conventions entry for the
        offset-handling rule (T2) and the record_stage composition rule (T7) — both are exactly
        the kind of "non-obvious, already-been-burned-by-this" convention this file's Key
        conventions section exists to preserve for future sessions. docs/roadmap.md: mark 5d done
        with the same level of detail as prior Phase entries. docs/data-model.md: append 0011 to
        the migration-list sentence (line 3) and add a subtitle_path row to the ### cuts table,
        following the black_frame_beat_indices row precedent exactly (change_impact_report
        review_triggers.4 — this file was missing from the design's original rollout list and was
        added during review specifically so it wouldn't be forgotten here).
      required_tests: []
      verification_commands:
        - "grep -n subtitle_path docs/data-model.md CLAUDE.md docs/roadmap.md"
      estimate_loc: 40
      source_trace: [system_design_spec §10 point 8 (revised), change_impact_report review_triggers.4]

    - id: T9
      title: Independent adversarial review of the merged diff
      dependencies: [T1, T2, T3, T4, T5, T6, T7, T8]
      target_paths: []
      action: >
        Dispatch an independent reviewer (isolated context, no access to this plan's own
        reasoning) against the real merged diff, focused specifically on the two must-fix classes
        the design review already found in the doc-level version of this change: (1) re-derive
        that .words semantics are truly unchanged and _whisper_timestamps() output is
        byte-identical pre/post-change by running the T2 regression test AND independently
        constructing one more multi-beat case; (2) re-derive that the T7 record_stage composition
        actually sets ev.ok=False on a forced failure by running that specific test and reading
        the resulting StageEvent row for real, not just reading the diff and agreeing it looks
        right. Also re-verify the full default test suite (.venv/bin/pytest -q), the golden test,
        and ruff (ruff check --select F,E9 .) all stay green, and that the migration round-trips
        against real Postgres one more time on the final merged state.
      required_tests: []
      verification_commands:
        - ".venv/bin/pytest -q"
        - ".venv/bin/pytest -m golden -v"
        - "ruff check --select F,E9 ."
      estimate_loc: 0
      source_trace: [change_impact_report review_triggers.1-4, criticality Medium rationale]

  execution_waves:
    - wave: 1
      tasks: [T1, T2, T3]
    - wave: 2
      tasks: [T4, T6]
    - wave: 3
      tasks: [T5]
    - wave: 4
      tasks: [T7]
    - wave: 5
      tasks: [T8]
    - wave: 6
      tasks: [T9]

  traceability:
    T1: [system_design_spec §4, change_impact_report impacted_data]
    T2: [system_design_spec §2, §3.1, change_impact_report review_triggers.1]
    T3: [system_design_spec §3.2]
    T4: [system_design_spec §2, §3.2, §10.4, change_impact_report impacted_contracts]
    T5: [system_design_spec §5, §6]
    T6: [system_design_spec §3.4, change_impact_report review_triggers.4]
    T7: [system_design_spec §7, change_impact_report review_triggers.2, unknowns.1]
    T8: [system_design_spec §10.8-revised, change_impact_report review_triggers.4]
    T9: [change_impact_report review_triggers.1-4, criticality]
```

## Architecture review scope note

`architecture_review_report` is `null` in the source set above — not an evidence gap, a deliberate
scope call. `system-design`'s own routing table distinguishes turning an *approved* architecture
decision into an implementation design (this skill's job, already done) from deciding *whether the
resulting architecture itself is sound* (`architecture-review`'s job). This change adds one nullable
column, one new pure-function module, and one best-effort external call onto an already-shipped,
already-approved pipeline architecture (Phase 0–7 are all `docs/roadmap.md`-tracked as done) — it does
not introduce, revise, or challenge any architectural decision the existing system has made.
`change_impact_report`'s own `criticality: Medium` (not High) and zero `impacted_services`
architectural boundary changes support this: the risk in this change is implementation correctness
(two specific, already-identified failure modes — T2's offset handling, T7's `record_stage`
composition), not architectural soundness. Both are carried as explicit `review_triggers` into T9
rather than requiring a separate architecture-review pass whose scope (system-wide soundness) doesn't
match this change's actual risk profile.

## Readiness

**READY.** All tasks have grounded target paths (verified against the real repository during change
impact analysis), a valid dependency DAG with no cycles, deterministic execution waves, complete
traceability back to `system_design_spec`/`change_impact_report` sections, and named verification
commands. The two genuine unknowns carried through from upstream (YouTube Captions API live
behavior, §9.2; the narrow finalize-path/mid-publish-crash caption gaps, §9.3) are non-blocking by
the change-impact report's own classification and are threaded into T7/T9 as explicit verification
steps rather than silently dropped.
