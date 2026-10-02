# Shared person-name extractor — module design (Phase 7w)

Candidate 2 of the full-codebase `improve-codebase-architecture` review ("Worth exploring"),
settled via `/grilling`.

## Problem

The review reported four independently maintained person-name regexes. Reading them showed three
person extractors that had drifted, plus one that is a different concept:

| | regex words | exclusions | returns |
|---|---|---|---|
| `evaluator._person_names` | 3+ letters | case-insensitive exact set + connective stoplist | all |
| `visual_fallback._first_person` | 3+ letters | case-sensitive exact set + prefix regex matched *anywhere* | first |
| `asset_sourcer._NAME_RE` | 2+ letters | none | all |
| `context_enricher._NAMED_ENTITY_RE` | 2+ letters | none (intentionally) | all |

Observed divergences (run side by side): "Angel Di Maria" / "Rodrigo De Paul" / "Giovani Lo
Celso" matched only in the sourcer — the evaluator and `_first_person` dropped them entirely, and
the default niche is Argentine squads; "Because Messi" was returned as a player name by
`_first_person` and excluded by the evaluator; "Real Betis" / "United Kingdom" / "East Germany"
were counted as people by the evaluator and blocked by `_first_person`; and the sourcer sent
"World Cup", "Premier League" and "Real Madrid Bernabeu" to Wikipedia as if they were people.
`_first_person` had zero tests. `asset_sourcer._extract_first_person_name` was dead code.

## Decision

New stdlib-only `engine/names.py`: `person_names(text) -> list[str]` and
`first_person_name(text) -> str | None`.

- **Scope: three extractors, not four.** `context_enricher._NAMED_ENTITY_RE` is a specificity
  signal that must count teams and tournaments; excluding them there would be a bug. Left alone and
  documented in the module docstring.
- **Width: 2+ letters per word** (the sourcer's), so short particles match everywhere. A deliberate
  fix, not a refactor: scoring and player backfill now see names they used to drop.
- **Exclusions: the union** of an exact lowercase phrase set (the evaluator's 20 entries + the one
  new entry from `_first_person`, "west european", + "manchester united" = 22 at the time), the
  evaluator's sentence-opener/connective stoplist, and a club/region prefix blocklist. The review
  round below reworked the opener handling and added a last-word blocklist.
- **Prefix blocklist matches the first word only.** The old `_first_person` regex matched anywhere
  in the name; unioning that into the evaluator would have dropped "Kanye West" from non-football
  niches' name counts (the Phase 6c fairness work). "Manchester United", the one real name that
  relied on a non-first-position match, moved to the exact set. This deviates from the
  "prefix-anywhere" behavior described during grilling and is called out in the PR.
- **Home: `engine/names.py`** (engine level, like `observability.py`), not `script_parser.py`:
  consumers span `engine.generation` and `engine.render`, which have no import edge today, and a
  stdlib-only module cannot create a cycle.
- `_first_person(vo, existing)` stays as a two-line "existing wins" wrapper; the evaluator's four
  call sites use `person_names` directly; `_extract_first_person_name` and
  `_extract_all_person_names` are deleted.

## Behavior changes (intentional)

1. Short-particle names match in scoring and in `_first_person` (affects `score_guide()` for guides
   naming Di Maria / De Paul / Lo Celso and `stub.player`, which gates `_is_shallow_beat`).
2. "Because Messi"-style clause openers are no longer player names.
3. The evaluator stops counting "Real Betis", "United Kingdom", "East Germany".
4. Wikipedia sourcing no longer looks up clubs/tournaments as people; a beat naming only "Real
   Madrid" falls through to Pexels instead of a club photo.

## Test strategy

Characterization tests on the *old* code first (`tests/test_visual_fallback.py`,
`tests/test_asset_sourcer_names.py`, separate commit), then updated for the intended changes.
`tests/test_names.py` (26) holds the two tests moved from `test_evaluator.py` plus particles, 14
non-person phrases, sentence openers and the "Kanye West" case. 8 mutations (width back to 3+,
dropping each exclusion layer, first-word turned into anywhere, the sourcer on the raw regex,
`_first_person` losing "existing wins", the evaluator seeing no names) each fail at least one test.
922 default-run tests passed at PR open (net +43: 26 + 14 + 5 − 2 moved); the review round below
brought it to 1013.

## Not done

The rest of `visual_fallback.py` (the 32-entry keyword table, degenerate-visual blocklist) is still
untested; this change covers only the part it touched.

## Corrections

A 4-persona review on the opened PR (#38). Security: no regression (ReDoS timed to 200k
characters on adversarial inputs, linear, no slower than the old regexes; no new injection surface;
lookups never exceed main's). Real issues found and fixed:

1. **Sentence openers glued onto names (Correctness, the serious one).** Widening to 2-letter words
   let "Is", "In", "To", "So"... match as a name's first word. "Is Vinicius Junior the best player
   alive?" (the app's core interrogative hook format) became "Is Vinicius Junior" instead of
   "Vinicius Junior", which broke `_score_throughline`'s name-callback check (`n.split()[0]` was
   "is"), let "is"/"in" substring-match almost any visual in `_score_alignment`, and fed a
   corrupted name to `stub.player` and the Wikipedia query. The first version's stoplist only held
   3+ letter openers and *dropped* matches; it now **strips** leading openers (2-letter function
   words added, name particles Di/De/Lo/Al/El, real surnames Li/Xi/Do and "Will" deliberately
   excluded) and drops the match only if fewer than two words remain.
2. **"Leeds United"/"Newcastle United"/"Cardiff City" leaked as players in `_first_person`** (the
   old regex blocked "United" anywhere in the name; the first-word-only rewrite lost that). Fixed
   with a last-word blocklist (`united`, `city`) rather than an endless phrase list; "la liga",
   "el clasico" and "atletico madrid" added to the phrase set.
3. **Whitespace bypass of the exclusion set** ("World  Cup", "World\nCup") and `\s+` letting a
   name span a line break ("Messi\nRomero"): the regex now uses horizontal whitespace only and the
   match is whitespace-normalized before exclusion.
4. **Test gaps (Test-Quality, mutation-confirmed):** accented letters untested (ASCII-only regex
   survived), the evaluator-level effect not asserted (reverting to the 3-letter regex passed all
   44 evaluator tests; the clip-availability call site and the throughline `.lower()` survived),
   most opener/prefix words individually unpinned, and the Wikipedia pause never asserted. Closed
   with 83 further tests (109 in `test_names.py`), 3 in `test_evaluator.py`, 3 in
   `test_visual_fallback.py`, 2 in `test_asset_sourcer_names.py`; 13 mutations each fail.
5. **Docs:** two wrong figures in this spec (set size 21 vs 20; five call sites vs four).

Not changed: a first-word club prefix drops the whole greedy match ("Real Madrid Karim Benzema
scored" returns nothing — the old `_first_person` behavior, now shared); the number of Wikipedia
lookups per beat is unbounded (pre-existing); several phrase-set entries are redundant with the
first-word blocklist (kept as documentation).
