# System design: `_escape_drawtext()` escapes only `\ : % '` — the `%` escape is broken

**Status:** Implemented. **Severity:** Low per `docs/roadmap.md`'s Open Issues table, but the
actual failure mode found here is a full render crash, not a cosmetic glitch — see §2.

## 1. Problem

`docs/roadmap.md`'s Open Issues table flags: *"`_escape_drawtext` escapes only `\ : % '` — a
newline or exotic character in `on_screen_text` could break the FFmpeg filter chain; not observed
in practice."*

`engine/render/compositor.py::_escape_drawtext()` (called from `_build_text_filter()` for every
`on_screen_text` line burned into the video via FFmpeg's `drawtext` filter) escapes `%` as `\%`:

```python
text = text.replace("%", "\\%")
```

The stated intent (per the function's pre-fix history and `CLAUDE.md`'s framing of this function
as the thing that sanitizes on-screen text content) is defense against drawtext's own `%{...}`
expansion syntax (`%{pts}`, `%{metadata:...}`, etc.) — a literal `%` from LLM-generated VO/caption
text should never be interpreted as the start of a metadata expansion.

## 2. What direct investigation against real ffmpeg found

Before designing a fix, the actual behavior was verified against the ffmpeg binary this repo's
tests already require on PATH (ffmpeg 9.0.2), not assumed from documentation. A `drawtext` filter
was run with `text_expansion` left at its default (`normal`) for several encodings of a percent
sign:

| Input passed to drawtext's `text=` | Result |
|---|---|
| `50% off` (raw, unescaped) | **FAILS** — `Stray % near ' off'` |
| `50\% off` (`_escape_drawtext()`'s current output) | **FAILS** — identical error |
| `50%% off` | **FAILS** — identical error |

**None of the three encodings produce a literal percent character.** Root cause, precise: ffmpeg's
own generic option-value parser (the layer that unescapes a filter option string before handing the
value to the individual filter) strips a single backslash before drawtext's `%`-expansion parser
ever sees the value — so `\%` arrives at drawtext's expansion parser indistinguishable from a bare
`%`, which is itself the "Stray %" error. (Doubling the backslash, `\\%`, with expansion left at
its default DOES survive to drawtext as a working escape and renders a literal `%` — confirmed
separately — but that is not what `_escape_drawtext()` was producing; it emitted a single backslash
via Python's `"\\%"` replacement, which is one escaping level short for this specific parser
chain.) `_escape_drawtext()`'s `\%` output was consequently never a working escape against the
running ffmpeg's actual parsing behavior, and a bare `%` fails identically regardless.

Consequence: any beat whose `on_screen_text` contains a literal `%` — plausible and likely in this
app's sports/stats niche default ("50% pass completion", "90% win rate") — crashes with
`RuntimeError: FFmpeg text/audio pass failed (exit 234)`, raised from `composite_cut()`'s own
ffmpeg subprocess call in `_build_ffmpeg_args()`'s drawtext pass (not from MoviePy — there is no
separate `write_videofile()` step in this pass), failing the entire render job. This is not the
"not observed in practice" cosmetic edge case the roadmap entry describes; it is a live,
unconditional crash on a realistic input the evaluator/prompt layer has no reason to avoid
producing. Severity is downgraded to Low in the roadmap only because it apparently hadn't been hit
yet in this operator's own usage — the code path itself is unconditionally broken.

## 3. Fix

`ffmpeg -h filter=drawtext` documents an `expansion` option (`none` / `normal` / `strftime`,
default `normal`) that disables drawtext's own `%`-expansion engine at the filter level:

```
expansion  <int>  set the expansion mode (from 0 to 2) (default normal)
  none     0      set no expansion
  normal   1      set normal expansion
  strftime 2      set strftime expansion (deprecated)
```

Verified against real ffmpeg: with `:expansion=none` added to the `drawtext` filter clause, a
literal, **unescaped** `%` renders successfully, and an attempted expansion payload
(`%{pts}`) also renders successfully as literal text with no substitution — the expansion engine
that `%{...}` would otherwise trigger is off entirely, not merely escaped-and-hoped.

Two changes, both in `engine/render/compositor.py`:

1. `_build_text_filter()`'s per-line `drawtext=...` clause gains `:expansion=none`.
2. `_escape_drawtext()` no longer touches `%` at all. Leaving the `\%` replacement in place after
   adding `expansion=none` would be actively wrong in the other direction: with expansion off, a
   bare `%` is already literal, so `\%` would print a visible, spurious backslash in the rendered
   caption.

No other call site interpolates `on_screen_text` (or any other freeform string) into a `drawtext`
clause — `_escape_drawtext()` has exactly one call site (`_build_text_filter()`), and
`_build_text_filter()` builds exactly one `drawtext=...` clause per on-screen-text line. The
`fontcolor` parameter is a separate, pre-existing, curated-allowlist-validated value
(`CURATED_TEXT_COLORS`) untouched by this fix.

## 4. Why this is the right fix, not a narrower one

An alternative would be to special-case `%{` (the only sequence that actually triggers expansion)
and leave a bare `%` unescaped. Rejected: (a) it was empirically confirmed that even a **bare,
unescaped** `%` fails today regardless of what follows it — the "Stray %" error fires on parsing
the `%` itself, before drawtext ever looks ahead for a matching `{...}`, so there is no narrower
per-character escape that fixes the crash while leaving expansion selectively available; (b)
`expansion=none` is strictly safer than any escaping scheme for the original threat this function
was defending against (an LLM-generated on-screen-text line containing a coincidental or
adversarial `%{...}`-shaped substring being expanded into ffmpeg metadata) — the expansion engine
is off, not merely escaped, so there is no possible input that reaches it. Verified this closes a
real, if low-severity, gap that existed even before this fix: `%{eif:6*7:d}` (drawtext's expression-
evaluation expansion) rendered as `42` against the pre-fix code's `\%`-escaping attempt for any
occurrence of `%{...}` that didn't already trip the "Stray %" crash — an LLM-generated caption
string containing a coincidental `%{...}`-shaped substring could have triggered arbitrary
expression evaluation inside drawtext. `expansion=none` renders it as the literal text
`%{eif:6*7:d}` instead. This was never a filter-graph-injection risk (drawtext's expansion engine
cannot break out of the `text='...'` value into a second filter), only an unintended-expansion one,
but it is now closed outright.

## 5. Other characters in the same function

The roadmap entry's broader framing ("a newline or exotic character... could break the chain") was
also checked empirically against the same real-ffmpeg harness: comma, semicolon, brackets (`[`
`]`), and an embedded literal newline were all tested unescaped inside `_build_text_filter()`'s
single-quoted `text='...'` value and **all rendered successfully** — ffmpeg's filtergraph parser
treats content inside a matched, unescaped pair of single quotes as literal for its own
comma/semicolon/bracket separators; only backslash and the quote character itself need escaping at
that layer (both already handled: backslash-doubling and apostrophe-to-curly-quote substitution),
and `%` needed the `expansion=none` filter-level fix above because it is drawtext's own
*second-layer* parser, not the filtergraph layer, that was rejecting it. No further escaping gap
was found; this closes the roadmap item in full rather than partially.

## 6. Testing

- `tests/test_compositor.py::test_text_filter_disables_drawtext_expansion` — pure string-building,
  asserts `:expansion=none` is present in the built filter chain.
- `tests/test_compositor.py::test_escape_drawtext_leaves_percent_untouched` — pure function test,
  asserts `_escape_drawtext("50% off") == "50% off"`.
- `tests/test_compositor.py::test_composite_cut_renders_on_screen_text_containing_a_percent_sign`
  — real-ffmpeg integration test (this file's existing convention for `composite_cut()`
  regressions): a beat with `on_screen_text=["Win rate: 50% today"]`, no video/VO media, asserts
  the render succeeds and the output file exists. Mutation-tested: reverted to the pre-fix
  `compositor.py`, confirmed this test fails with the exact `RuntimeError`/"Stray %" error this
  design predicts, then restored the fix. This test alone cannot distinguish "the escape was
  removed" from "the escape was merely harmless" — see Correction 1.
- `tests/test_compositor.py::test_composite_cut_percent_expansion_stays_literal_not_expanded` —
  real-ffmpeg frame-hash comparison proving the actual security property `expansion=none` exists
  for: renders `%{pts}` at two different timestamps through the real `_build_text_filter()` output
  and asserts the two frames are byte-identical (drawtext's `%{pts}` expands to the current
  timestamp when expansion is active, so two different timestamps would render visibly different
  text if expansion weren't actually disabled). Added in Correction 2 below.

Full suite: 737 tests (was 733; +4), 1 deselected (golden), `ruff check --select F,E9 .` clean.

## 7. Corrections (from adversarial dual-lens review of the built code)

Two independent review passes (Lens A — Safety/State; Lens B — Contracts/Operations) ran against
the implemented diff before this shipped. Both were run in a real, non-mocked ffmpeg environment
and were asked to verify claims empirically rather than trust this document.

**Correction 1 — the original root-cause explanation and the "stray backslash" claim were both
wrong.** The first draft of this document (and of `_escape_drawtext()`'s docstring) claimed `\%`
"is not a valid escape for that engine" in a way that implied any backslash-escaped `%` fails, and
separately claimed that leaving the `\%` replacement in place after adding `expansion=none` "would
print a stray visible backslash in the rendered text." Both lens reviews independently disproved
this by rendering real frames and comparing bytes: with `expansion=none` active, `\%` and a bare
`%` render **byte-identical** — ffmpeg's generic option-value parser strips the single backslash
before drawtext ever sees it, in both the expansion-active and expansion-disabled cases equally.
The `\%` escape wasn't harmful, it was simply pointless — one escaping level short of a working
escape when expansion was active (a *correctly* doubled `\\%` DOES survive as a working escape
under normal expansion, confirmed separately), and inert once expansion is off. §2 and
`_escape_drawtext()`'s docstring were rewritten to state this precisely instead of the disproven
claim. This did not change the fix itself (removing the now-pointless `%` handling is still
correct, just for a more precise reason) or its correctness — only the stated rationale was wrong,
caught by both lenses independently rendering and byte-comparing frames rather than trusting the
first draft's prose.

**Correction 2 — no test proved `expansion=none` actually suppressed expansion, only that it didn't
error.** `test_composite_cut_renders_on_screen_text_containing_a_percent_sign` (the sole
integration test in the first draft) asserts only that the render succeeds — Lens B pointed out
that this cannot distinguish "expansion is genuinely off" from "this particular string happened not
to trip a parse error," and that keeping the old, pointless `\%` escape would *also* pass it (since
`\%` and `%` are byte-identical under `expansion=none` per Correction 1). Fixed by adding
`test_composite_cut_percent_expansion_stays_literal_not_expanded`, a real-ffmpeg frame-hash
comparison at two different timestamps through an on-screen-text string of `%{pts}` — this is the
property §4's injection-closure argument actually depends on, and it was previously asserted in
prose only.

**Correction 3 — a related, pre-existing bug incidentally fixed as a side effect, previously
undocumented and untested.** Lens A found that under the pre-fix code, a literal backslash in
`on_screen_text` (produced by `_escape_drawtext()`'s own `\\` → `\\\\` doubling step for a source
string that itself contained `\`) silently disappeared from the rendered frame — `"a\\b"` rendered
identically to `"ab"` — because ffmpeg's generic option-value parser was consuming the backslash's
escaping role for a character drawtext's own parser then did nothing further with (expansion at
its default without a valid trailing expansion token treats the escaped character as invalid but
doesn't error the way `%` does, and drops it instead). Under `expansion=none`, a literal backslash
now renders correctly. No test was added for this specific case — it is out of scope for the
percent-sign bug this design exists to close and was not itself reported in the roadmap — but it is
recorded here since an operator debugging a future "disappearing backslash" report predating this
fix would otherwise have no trail to it.

**Confirmed, not changed:** both lenses independently verified `expansion=none` has no other
behavioral side effect relevant to this codebase (nothing in `compositor.py` uses `textfile=`,
`localtime`, or `metadata:` expansion tokens); the existing `'` → curly-quote and `\` → `\\`
escaping remain sufficient to prevent filter-graph injection (an attempted
`x',drawbox=c=red:t=fill,drawtext=text='y` payload, with and without a preceding backslash, both
rendered as inert literal text — 0 unexpected pixels — under the fixed code); `_escape_drawtext()`
has exactly one call site and no other `drawtext` clause exists anywhere else in the codebase to
have missed; and no existing test or documented behavior regressed for text containing neither `%`
nor `\`.
