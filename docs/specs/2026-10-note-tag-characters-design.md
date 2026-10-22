# Hidden Unicode tag characters in operator text that reaches an LLM (Phase 8c)

## Problem

Three kinds of operator-supplied text are shown in a browser and then put, verbatim, into LLM prompts:

- **A performance note** (typed or pasted on the insights page). Every active note goes into the
  standard-path generation prompt as feedback: `worker/tasks/generate.py` turns the active rows into
  `active_notes`, they seed `prior_feedback`, and `engine/generation/prompt.py` joins them into a
  second user message. `create_note` only did `.strip()`.
- **A reel's `context`** (the create-reel form). It is the topic of the whole reel: `build_messages`
  puts it in twice, and it reaches `llm_enrich`, the beat enrichment, the hook-variant, judge and
  visuals prompts. A structured script is parsed from it, and its beats go on to enrichment, the
  visuals and caption prompts, the stored guide, TTS and the on-screen text.
- **A reel's `niche`** (the same form), which is in `NICHE:` in `build_messages` and in the
  enrichment, hook, caption and visuals prompts.

The Unicode TAG block (U+E0000-E007F) mirrors ASCII (`U+E0041` is an invisible "A"). Browsers and most
fonts render it as nothing and an LLM reads it, so text that looks like "Messi's World Cup story" can
carry "Ignore prior instructions and ..." that the operator never sees ("ASCII smuggling"), for
example from an article pasted off a hostile page. Pasting article text into the context box is the
most likely route, which makes the context a larger exposure than a note.

Found by the security reviewers of the insights-page PR (#46) in two steps: round 3 found it for
notes, and round 1 of this PR found the same channel on the context and niche. Both are pre-existing.

## What it is, and is not

Low to medium. The hidden text only adds **stealth**: a visible instruction in a note or a context can
already steer the model, and these endpoints have no authentication (an existing "not done" scope
item, the larger problem). The blast radius is small: the model returns JSON that is
schema-validated and scored, a person reviews the guide before anything is published, the publish
gates are untouched, and the model has no tools. This change closes the stealth; it does not close
the injection.

## Change

- `engine/hidden_text.py::strip_tag_chars(text)`: removes every character in U+E0000-E007F.
- **Exact allowlist for the three flag emoji.** England, Scotland and Wales are spelled
  `U+1F3F4` + tag characters (`gbeng` / `gbsct` / `gbwls`) + `U+E007F`; stripping the tags would
  degrade them to a plain black flag. Those three exact strings are kept. A *shape* check (BLACK FLAG +
  `[a-z0-9]+` + CANCEL TAG) was rejected: it lets a readable lower-case message
  (`ignorepreviousinstructionsandsay`) through inside a flag. With an allowlist only three fixed strings
  can pass, so no payload text survives; what is left is the choice and count of three visible flags,
  which is not a practical channel. Any other subdivision flag (`usca`, `gbnir`) is degraded to a plain
  black flag.
- `api/routers/reels.py::create_reel` cleans `context` and `niche` before the `Reel` is built. A context
  that is empty once the hidden characters are gone is a 422 (a whitespace-only context is accepted, as
  before). `voiceover_mode` is checked against `voiceover` / `music_only` / `silent` and anything else
  falls back to `voiceover` (same drop-don't-422 policy as `tts_voice`): it was a free-text field that
  was stored and later read back into the guide prompt, which made it a fourth route for hidden text. It is the
  only place a reel is created, so the structured-script stubs, `enriched_context` and every prompt that
  reads them later get clean text; the stored value is exactly what the operator saw. A missing niche
  stays `None`, and a niche that is empty once cleaned becomes `None` too (as an empty submit already is).
- `api/routers/reels.py::estimate_reel` cleans the context too, so the path the form's estimate shows is
  the one the stored context takes (a hidden character inside a header line changes whether a script
  counts as structured).
- `api/routers/insights.py::create_note` strips **before** its empty check, so a note that is only
  hidden text is not saved.
- `worker/tasks/generate.py::_note_texts_for_prompt()` strips again where the rows become
  `prior_feedback`: rows stored before this change are covered, and a note that is nothing but hidden
  text is dropped instead of being sent as an empty bullet. `performance_note_ids` in `job.meta` still
  lists every active note (the row is still active; only its prompt text is empty).
- Every invisible character in the module and its tests is built with `chr()` or a visible `\U`
  escape, never as a literal character, so the source can be reviewed by eye.

## Tests (84 new)

- `tests/test_hidden_text.py` (53): text without tag characters is unchanged; the whole block is
  removed (each boundary, and in one go) while the neighbouring code points are kept; the three flags
  survive alone, in text and together and are pinned by code point; a message hidden before or after a
  real flag, or after its cancel tag, is removed; a readable lower-case message inside a flag shape is
  removed; any other subdivision, a payload spliced into a flag and a flag missing its cancel tag all
  degrade to a plain black flag; idempotent; a 3000-case randomized property (no tag character
  survives outside the three flags, and the visible characters are all still there in order).
- `tests/test_reels_router.py` (+22): `create_reel` stores context and niche without hidden text, keeps
  the England flag, removes a message hidden in a flag shape, still stores `niche=None`, and a
  structured script with hidden text in a section parses to clean stubs; a context that is only hidden
  characters is a 422 and leaves no `Reel` or `Job` row, while an empty or whitespace-only context
  behaves as before; a niche that is only hidden characters is stored as `None`, like an empty niche; each valid
  `voiceover_mode` is stored and any other value falls back to `voiceover`; the cost estimate reports the
  path of the cleaned context. `create_reel` cleaning is tested on `auto`, `standard` and `structured`.
- `tests/test_insights_router.py` (+6): a hidden instruction is not stored, a tags-only note and a
  whitespace-plus-tags note are not saved, the England flag survives, a message hidden in a flag shape
  is removed, and `"  x  " + tags` is stored as `"x"` (the trim runs).
- `tests/test_generate_task.py` (+3): `_note_texts_for_prompt` cleans and drops; keeps the flag; and
  end to end through `generate_guide()` the prompt receives `["Real advice."]`.
  Also fixed `test_structured_path_skipped_when_parse_disagrees_with_is_structured`, whose mock answered
  the `PerformanceNote` query with a `Cut` (whose `.text` is a MagicMock, not the string real note text
  always is); it now dispatches by model like its neighbours.

Mutation-checked, each failing for the predicted reason: no allowlist (all tags stripped), the block
one code point too narrow at either end, a flag dropped from or added to the allowlist, the flag
group always kept or always dropped, `create_note` not stripping, stripping after the empty check or
trimming one side only, `create_reel` not cleaning the context, the niche, or cleaning after the
`Reel` is built, a missing niche becoming `''`, seeding not cleaning, keeping hidden-only notes,
dropping only exactly-empty ones, and the helper not being used at the seeding site. One mutant
survives and is equivalent: the order of the regex's two alternatives (a flag starts with BLACK FLAG,
which is not a tag character, so both can never match at one position).

## Left alone

- **No authentication** on these endpoints: an existing scope item. A visible note or context is just
  as able to carry an instruction, so the fuller fix is access control, not character filtering.
- **Guide edits** (PATCH beat edits, hook-variant swaps, caption edits). No LLM call reads text after
  it has been edited by the operator, so it is not an LLM sink today. They do reach TTS and the
  drawtext overlay, which is a separate concern. If a future feature feeds edited text back to a
  model, it needs the same cleaning.
- **Other invisible channels.** Variation selectors (U+E0100-E01EF), zero-width characters (ZWSP,
  ZWNJ, ZWJ, word joiner) and bidi override characters (U+202A-202E, U+2066-2069) can also hide or
  reorder text. The first three have legitimate uses (emoji and Indic/Persian text need ZWJ/ZWNJ) and
  far smaller bandwidth; the bidi overrides are visible, if oddly ordered. None is touched here. A
  note made only of them survives and shows as a blank, still-active row.
- **Reels created before this change.** A reel's context, niche and voiceover mode are cleaned when the
  reel is created and not again where the worker reads them (notes are, because they are stored
  separately and can predate the fix). A reel created before the deploy whose enrich or generate job is
  still queued, or is resumed by the reaper, would send its old text. No route regenerates an existing
  reel, so this only touches jobs in flight at deploy time; draining the queue before deploying avoids
  it.
- **Notes stored before this change** keep their tag characters in the database and the insights page
  still renders them (invisibly); they are cleaned only when seeded into the prompt. A one-off data
  clean-up is not done.
- **Display.** How the insights page shows text (control and bidi characters) is a different concern,
  handled by `api/display.py::safe_text` in the insights-page PR (#46).
- **NUL in a note.** A note containing U+0000 is stored on SQLite but Postgres rejects it (probably a
  500). Pre-existing and unrelated to tag characters.
- **Delimiting text for the LLM** (quoting it as data) does not help against a *visible*
  instruction and adds nothing against hidden ones, so it is not done here.
- **Phase label.** This is Phase 8c on the assumption that the insights-page PR (#46) lands as 8b; if it
  does not, the label needs renumbering.
- #46 and this PR both edit the test-count lines in `CLAUDE.md`, `README.md` and
  `docs/architecture.md`; whichever merges second needs a trivial conflict fix (sum both).
