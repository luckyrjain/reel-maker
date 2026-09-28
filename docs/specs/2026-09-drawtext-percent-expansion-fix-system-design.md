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
producing. The roadmap's Low severity rating was never downgraded or upgraded by this fix — it is
noted here only because it apparently hadn't been hit yet in this operator's own usage, despite the
code path itself being unconditionally broken.

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
2. `_escape_drawtext()` no longer touches `%` at all. **Correction (see §7, Correction 1):** the
   first draft of this document claimed leaving the `\%` replacement in place after adding
   `expansion=none` "would print a visible, spurious backslash in the rendered caption." Real-ffmpeg
   frame-byte comparison disproved this — `\%` and `%` render byte-identical under `expansion=none`
   (ffmpeg's generic option-value parser strips the backslash regardless of the expansion setting).
   Removing the `%` handling is still correct, just for the accurate reason given in §2: it was
   never doing anything, not that keeping it would actively break rendering.

No other call site interpolates `on_screen_text` (or any other freeform string) into a `drawtext`
clause — `_escape_drawtext()` has exactly one call site (`_build_text_filter()`), and
`_build_text_filter()` builds exactly one `drawtext=...` clause per on-screen-text line. The
`fontcolor` parameter is a separate, pre-existing, curated-allowlist-validated value
(`CURATED_TEXT_COLORS`) untouched by this fix.

## 4. Why this is the right fix, not a narrower one

An alternative would be to special-case `%{` (the only sequence that actually triggers expansion)
and leave every other `%` unescaped. Rejected: (a) it was empirically confirmed that a bare,
unescaped `%` **not immediately followed by a well-formed `{...}` expansion token** fails
unconditionally — e.g. `50% off` fails, because the "Stray %" error fires on parsing the `%` itself
before drawtext finds a matching `{` to expand. A `%` that IS followed by a well-formed token
(`%{pts}`, `%{eif:6*7:d}`) does NOT fail this way — it successfully expands instead, which is the
separate, second problem this fix also closes (§7 below). So there is no narrower per-character
escape that closes both problems at once: escaping only a lone `%` would still leave a *crafted*
`%{...}` substring free to expand, and escaping `%{` specifically would still crash on any ordinary,
non-adversarial `%` in real caption text. `expansion=none` is the only single change that closes
both; (b)
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
- `tests/test_compositor.py::test_text_filter_percent_expansion_stays_literal_not_expanded` —
  real-ffmpeg frame-hash comparison proving the actual security property `expansion=none` exists
  for: renders `%{pts}` at two different timestamps, on a color source matching this app's real
  `TARGET_W`x`TARGET_H` (see Correction 4 — an earlier version of this test rendered onto an
  arbitrary small frame and passed vacuously), through the real `_build_text_filter()` output, and
  asserts (a) the frame differs from a genuinely textless baseline frame — proving text was actually
  drawn, not just that two black frames happened to match — and (b) the two text-bearing frames are
  byte-identical to each other (drawtext's `%{pts}` expands to the current timestamp when expansion
  is active, so two different timestamps would render visibly different text if expansion weren't
  actually disabled). Added in Correction 2, fixed in Correction 4.

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
`test_text_filter_percent_expansion_stays_literal_not_expanded`, a real-ffmpeg frame-hash
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

**Correction 4 — the frame-hash test added in Correction 2 was itself vacuous, and three prose
claims in this document had wording defects.** A second review round — four personas (Security/
Red-Team, Correctness/Edge-Case, Test-Quality Auditor, Documentation-Consistency), run in parallel
against the merged PR — converged independently (three of the four reviews) on the same finding:
`test_text_filter_percent_expansion_stays_literal_not_expanded` rendered its comparison frames onto
an arbitrary `320x240` color source, but `_build_text_filter()`'s drawtext clause places text at a
fixed `y=_TEXT_Y`, computed from this app's real `TARGET_H=1920` (≈1371px down) — so the text landed
entirely below the visible area of the small test frame, both "frames" were plain, textless black
regardless of the `expansion` setting, and the test **passed even with `:expansion=none` fully
removed**. Correction 2's own claim that this test "proves the actual security property" was
therefore false at the time it was written. Fixed by rendering the comparison frames at this app's
real `TARGET_W`x`TARGET_H` (matching what `_TEXT_Y` actually assumes) and by adding a third
assertion — the text-bearing frame must differ from a genuinely textless baseline frame (rendered
with no `-vf` at all) — so a future off-screen-text regression fails loudly instead of the test
quietly proving nothing. Re-verified by mutation: with the fix applied, deleting `:expansion=none`
from `compositor.py` now makes this test fail with a real assertion mismatch between the two
timestamped frames, confirmed via `git diff`/`git checkout --` before and after. The test's
docstring and its position in the file (it built its own filter string via `_build_text_filter()`
directly and ran a raw ffmpeg command, never calling `composite_cut()`) were also misleading — it
was renamed from `test_composite_cut_percent_expansion_stays_literal_not_expanded` to
`test_text_filter_percent_expansion_stays_literal_not_expanded` to match what it actually exercises.

The same review round also caught three wording defects in this document's earlier text (defects
(a) and (c) were present since the first draft; only (b) was introduced by Correction 1 itself
missing a second occurrence), none of them affecting the fix's correctness, all now fixed in
§2/§3/§4 above: (a) §2 said the roadmap's
severity rating was "downgraded to Low" — it was already Low; nothing was downgraded, only reworded
to remove that implication; (b) §3 item 2 still carried the original, disproven "would print a
stray visible backslash" claim after Correction 1 rewrote §2 to say the opposite — Correction 1
only updated §2 and the code docstring, missing this second occurrence, now fixed with an explicit
pointer back to Correction 1; (c) §4's "(a)" argument claimed a bare `%` "fails today regardless of
what follows it," which read as contradicting the very next section's own finding that `%{eif:...}`
successfully expands rather than failing — reworded to state precisely that only a `%` **not**
followed by a well-formed `{...}` expansion token fails unconditionally, while one that IS followed
by such a token expands instead (the separate problem `expansion=none` also closes), so no single
narrower escape could have fixed both at once.

**Bonus finding (Security/Red-Team round, not itself a blocker) — this fix incidentally closes a
live hang/DoS vector on `main` today.** `%{e:while(1,0)}` (drawtext's `e:` expression-evaluation
expansion, reachable through the same `%{...}` syntax as `%{eif:...}`) makes ffmpeg hang
indefinitely on the pre-fix code — confirmed with a 15s timeout against real ffmpeg. This app's
`rendering` queue runs at `--concurrency=1` (see CLAUDE.md's Worker queues section), so an
LLM-generated `on_screen_text` string containing this substring would hang the single rendering
worker for up to `render`'s full `max_runtime_s` soft time limit (60 min) — `SoftTimeLimitExceeded`
would interrupt the blocked `subprocess.run()` call at that point (escalating to SIGTERM/SIGKILL
only if that exception were somehow ignored, per CLAUDE.md's Reliability features section) — so a
full hour of the rendering queue stalled behind one bad beat, not merely a failed render. Under
`expansion=none` (the PR's fix), the identical string renders instantly as literal text. No exhaustive fuzz/injection search (400 randomized payloads, 24 hand-built
adversarial ones, targeted lookalike-quote and control-byte cases) found any way to achieve
filter-graph injection (breaking `text='...'` to inject a second filter/option) on either the
pre-fix or fixed code — the `'`→curly-quote and `\`→`\\` escaping were already sufficient for that
specific risk. This hang was previously unknown and unreported; it is recorded here rather than as
its own roadmap item since this PR already closes it as a side effect of the primary fix.

**Confirmed, not changed:** both lenses independently verified `expansion=none` has no other
behavioral side effect relevant to this codebase (nothing in `compositor.py` uses `textfile=`,
`localtime`, or `metadata:` expansion tokens); the existing `'` → curly-quote and `\` → `\\`
escaping remain sufficient to prevent filter-graph injection (an attempted
`x',drawbox=c=red:t=fill,drawtext=text='y` payload, with and without a preceding backslash, both
rendered as inert literal text — 0 unexpected pixels — under the fixed code); `_escape_drawtext()`
has exactly one call site and no other `drawtext` clause exists anywhere else in the codebase to
have missed; and no existing test or documented behavior regressed for text containing neither `%`
nor `\`.
