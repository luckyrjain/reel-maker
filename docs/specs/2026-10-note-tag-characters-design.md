# Hidden Unicode tag characters in performance notes (Phase 8c)

## Problem

A performance note is text an operator types or pastes on the insights page. Every active note is put,
verbatim, into the standard-path generation prompt as feedback: `worker/tasks/generate.py` turns the
active rows into `active_notes`, they seed `prior_feedback`, and `engine/generation/prompt.py` joins
them into a second user message. `create_note` only did `.strip()`.

The Unicode TAG block (U+E0000-E007F) mirrors ASCII (`U+E0041` is an invisible "A"). Browsers and most
fonts render it as nothing and an LLM reads it, so a note that looks like "Hooks phrased as questions
win." can carry "Ignore prior instructions and ..." that the operator never sees ("ASCII smuggling"),
for example from advice pasted off a hostile page.

Found by the security reviewer of the insights-page PR (round 3); it is pre-existing and was split out
at the owner's request.

## What it is, and is not

Low to medium. The hidden text only adds **stealth**: a visible note can already inject any
instruction into every later standard-path generation, and the notes endpoints have no authentication
(an existing "not done" scope item, the larger problem). The blast radius is small: the model returns
JSON that is schema-validated and scored, a person reviews the guide before anything is published,
the publish gates are untouched, and the model has no tools. This change closes the stealth; it does
not close the injection.

## Change

- `engine/hidden_text.py::strip_tag_chars(text)`: removes every character in U+E0000-E007F.
- **Exact allowlist for the three flag emoji.** England, Scotland and Wales are spelled
  `U+1F3F4` + tag characters (`gbeng` / `gbsct` / `gbwls`) + `U+E007F`; stripping the tags would
  degrade them to a plain black flag. Those three exact strings are kept. A *shape* check (BLACK FLAG +
  `[a-z0-9]+` + CANCEL TAG) was rejected: it lets a readable lower-case message
  (`ignorepreviousinstructionsandsay`) through inside a flag. With an allowlist only three fixed strings
  can pass, so no covert capacity remains. Any other subdivision flag (`usca`, `gbnir`) is degraded to a
  plain black flag.
- `api/routers/insights.py::create_note` strips **before** its empty check, so a note that is only
  hidden text is not saved, and what is stored is what the operator sees.
- `worker/tasks/generate.py::_note_texts_for_prompt()` strips again where the rows become
  `prior_feedback`: rows stored before this change are covered, and a note that is nothing but hidden
  text is dropped instead of being sent as an empty bullet. `performance_note_ids` in `job.meta` still
  lists every active note (the row is still active; only its prompt text is empty).
- Every special character in the module and its tests is built with `chr()`; a literal invisible
  character in source cannot be reviewed.

## Tests (60 new)

- `tests/test_hidden_text.py` (52): text without tag characters is unchanged; the whole block is
  removed (each boundary, and in one go) while the neighbouring code points are kept; the three flags
  survive alone, in text and together and are pinned by code point; a message hidden before or after a
  real flag, or after its cancel tag, is removed; a readable lower-case message inside a flag shape is
  removed; any other subdivision, a payload spliced into a flag and a flag missing its cancel tag all
  degrade to a plain black flag; idempotent; a 3000-case randomized property (no tag character
  survives outside the three flags, and the visible characters are all still there in order).
- `tests/test_insights_router.py` (+5): a hidden instruction is not stored, a tags-only note and a
  whitespace-plus-tags note are not saved, the England flag survives, a message hidden in a flag shape
  is removed.
- `tests/test_generate_task.py` (+3): `_note_texts_for_prompt` cleans and drops; keeps the flag; and
  end to end through `generate_guide()` the prompt receives `["Real advice."]`.
  Also fixed `test_structured_path_skipped_when_parse_disagrees_with_is_structured`, whose mock answered
  the `PerformanceNote` query with a `Cut` (whose `.text` is a MagicMock, not the string real note text
  always is); it now dispatches by model like its neighbours.

Mutation-checked, each failing for the predicted reason: no allowlist (all tags stripped), the block
one code point too narrow at either end, a flag dropped from or added to the allowlist, `create_note`
not stripping or stripping after the empty check, seeding not cleaning, seeding keeping hidden-only
notes, seeding dropping only truly empty ones, and the helper not being used at the seeding site. Two
mutants survive and are equivalent: the order of the regex alternatives (a flag starts with BLACK FLAG,
which is not a tag character, so both can never match at one position) and a redundant re-check that
was then removed by switching to a named group.

## Left alone

- **No authentication** on the notes endpoints: an existing scope item. A visible note is just as able
  to carry an instruction, so the fuller fix is access control, not character filtering.
- **Other invisible channels.** Variation selectors (U+E0100-E01EF) and zero-width characters
  (ZWSP, ZWNJ, ZWJ, word joiner) can also encode hidden bits. They have legitimate uses (emoji and
  Indic/Persian text need ZWJ/ZWNJ) and a far smaller bandwidth, so they are not touched. A note made
  only of them survives and shows as a blank, still-active row.
- **Display.** How the insights page shows note text (control and bidi characters) is a different
  concern, handled by `api/display.py::safe_text` in the insights-page PR (#46).
- **Delimiting notes for the LLM** (quoting them as data) does not help against a *visible*
  instruction and adds nothing against hidden ones, so it is not done here.
- The insights-page PR and this one both edit the test-count lines in `CLAUDE.md`, `README.md` and
  `docs/architecture.md`; whichever merges second needs a trivial conflict fix (sum both).
