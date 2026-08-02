"""
Post-processing applied to every generated MasterGuide before it is saved.

Fixes two recurring LLM mistakes that cannot be caught by Pydantic validation:
  1. Section-header prefixes leaked into vo_script  ("MIDFIELD: The engine.")
  2. on_screen_text filled with section labels      (["MIDFIELD"]) instead of
     punchy words from the actual spoken line.
"""
import re

from engine.generation.guide_schema import MasterGuide

# Matches patterns like  "GOALKEEPER: ", "MIDFIELD: ", "Cristian Romero: "
# at the START of a vo_script line — these are structural labels, not speech.
_LABEL_PREFIX = re.compile(r"^(?:[A-Z][A-Za-z\s]{2,}:\s+)+")

# A line that is ONLY an all-caps section label, e.g. "DEFENSE", "ENDING"
_SECTION_LABEL_ONLY = re.compile(r"^[A-Z][A-Z\s]{2,}$")


def _strip_label_prefix(text: str) -> str:
    """Remove leading structural labels from a vo_script string."""
    return _LABEL_PREFIX.sub("", text).strip()


_MAX_LINE_CHARS = 28  # ~fontsize-60 safe width at 1080px

def _derive_on_screen(vo: str, max_lines: int = 3) -> list[str]:
    """
    Pull phrases from the vo_script to use as on_screen_text — one per sentence.
    These are shown sequentially in the compositor, so more segments = better speech sync.
    """
    sentences = [s.strip() for s in re.split(r"[.!?—]+", vo) if s.strip()]
    result = []
    for sent in sentences:
        words = sent.split()
        if not words:
            continue
        line_words: list[str] = []
        for w in words:
            if len(" ".join(line_words + [w])) > _MAX_LINE_CHARS:
                break
            line_words.append(w)
        result.append(" ".join(line_words) if line_words else words[0][:_MAX_LINE_CHARS])
        if len(result) >= max_lines:
            break
    if not result:
        words = vo.split()
        line_words = []
        for w in words:
            if len(" ".join(line_words + [w])) > _MAX_LINE_CHARS:
                break
            line_words.append(w)
        result = [" ".join(line_words) if line_words else vo[:_MAX_LINE_CHARS]]
    return result


def clean_guide(guide: MasterGuide, voiceover_mode: str) -> MasterGuide:
    """
    Mutate guide in-place to fix label leakage and on_screen_text mismatches.
    Returns the same object for chaining.
    """
    is_vo = voiceover_mode not in ("music_only", "silent")

    for cut in guide.cuts:
        for beat in cut.beats:
            if is_vo and beat.vo_script:
                # Strip structural label prefixes, then always re-derive
                # on_screen_text from the cleaned VO so the compositor's
                # proportional timing matches the spoken sentences exactly.
                beat.vo_script = _strip_label_prefix(beat.vo_script)
                beat.on_screen_text = _derive_on_screen(beat.vo_script, max_lines=5)
            elif beat.on_screen_text:
                # Non-VO mode: fix section-label-only lines in place.
                fixed = []
                for line in beat.on_screen_text:
                    if _SECTION_LABEL_ONLY.match(line.strip()):
                        fixed.extend(_derive_on_screen(beat.vo_script))
                    else:
                        fixed.append(line)
                seen: set[str] = set()
                deduped = []
                for line in fixed:
                    if line not in seen:
                        seen.add(line)
                        deduped.append(line)
                beat.on_screen_text = deduped[:5]

    return guide
