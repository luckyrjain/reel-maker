# Shared proportional-timing core — module design (Phase 7y)

Candidate 3 of the full-codebase `improve-codebase-architecture` review ("Strong"), settled via
`/grilling`.

## Problem

`engine/render/compositor.py` placed items proportionally across a beat's duration in two
places, each with its own inline copy of the same loop:

- `_build_text_filter()`'s no-Whisper fallback — one weight per displayed `on_screen_text` line
  (<= 5), absolute time from the beat's start;
- `_proportional_caption_cues()` — one weight per VO sentence, beat-relative (the caller adds the
  running `cue_offset`, the Phase 5d offset rule).

The shared policy (0.3 s minimum span, clamp, stop at the beat end, stretch the last span to the
end) and the `re.split(r"[.!?—]+", vo)` sentence split existed twice. The SRT copy had zero direct
tests and the drawtext copy was only reachable through a regex over an ffmpeg filter string.

## Decision

Two private helpers in `compositor.py`, next to their callers:

- `_split_vo_sentences(vo) -> list[str]` — the one sentence splitter (`_SENTENCE_SPLIT_RE`).
- `_proportional_spans(weights, total_weight, duration, start=0.0) -> list[(index, start, end)]`
  — the one placement loop. `total_weight` is a separate argument because the drawtext caller's
  denominator is the word count of ALL VO sentences while it places only its displayed lines.
  `start` lets drawtext stay in absolute time with byte-identical filter strings, while SRT passes
  0.0 and keeps its caller-side offset shift.

Each caller keeps what is genuinely its own: drawtext builds its weights (real sentence counts,
weight 1 for extra lines) and its empty-VO equal split; SRT keeps "no sentences -> no cues". The
Whisper word path (`_whisper_timestamps`) maps lines onto a word stream — a different algorithm —
and is untouched, as are the Phase 5d offset rules (`_build_beat_transcripts` at offset 0.0, the
explicit running sum in `composite_cut`).

Not done, deliberately: a shared module/location outside `compositor.py` (`captions.py` is the
optional-Whisper module, `srt.py` is documented as a pure formatter with no timing math).

## Behavior

A pure refactor. The characterization tests were committed first, on the old code
(`tests/test_proportional_timing.py`), and pass unmodified against the extracted code, as do all
pre-existing drawtext timing tests.

Known quirk, preserved and pinned by a labeled characterization test in each copy: when
`duration / n < 0.3 s` the floor makes the cursor outrun the beat and later items are not placed
(20 one-word sentences in 3 s give 11 cues). Drawtext is bounded by its 5-line cap; for SRT it is a
caption track silently missing text. How to degrade (compress, merge, drop) is a product decision,
left as a follow-up.

One intentional hardening, found by differential fuzzing in review (400k comparisons otherwise
byte-identical to `main`): a beat whose `vo_script` key is present but `None` used to raise
`TypeError` in the drawtext path (`re.split(None)`); it now takes the empty-VO equal-split path.
The SRT copy already tolerated `None`.

## Dead logic found by mutation testing

The original loops clamped each span to the beat end and `break`-ed once the cursor passed it.
Mutation testing showed both were equivalent mutants: the last placed span is always overwritten
with the beat end, and nothing is placed after the cursor passes it. Both were removed; behavior is
identical.

## Tests

`tests/test_proportional_timing.py` (28): 19 caller-level tests (16 committed first on the old code, 3 added in review: the 5-line cap, a line-less beat still advancing the cursor, and `vo_script=None`) and 9 direct tests of the helpers. `test_compositor.py` gains a real-ffmpeg guard for the SRT offset shift on the fallback path (previously unguarded). Mutations that each fail at least one test: no
last-span stretch, floor changed, `start` ignored, denominator replaced by `sum(weights)`,
drawtext denominator narrowed, em dash dropped from the split, the `t < end` placement guard
removed.
