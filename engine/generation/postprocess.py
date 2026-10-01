"""
Post-processing applied to every generated MasterGuide before it is saved.

Fixes two recurring LLM mistakes that cannot be caught by Pydantic validation:
  1. Section-header prefixes leaked into vo_script  ("MIDFIELD: The engine.")
  2. on_screen_text filled with section labels      (["MIDFIELD"]) instead of
     punchy words from the actual spoken line.
"""
import re

from engine.generation.guide_schema import MasterGuide
from engine.generation.script_parser import derive_on_screen

# Matches patterns like  "GOALKEEPER: ", "MIDFIELD: ", "Cristian Romero: "
# at the START of a vo_script line — these are structural labels, not speech.
_LABEL_PREFIX = re.compile(r"^(?:[A-Z][A-Za-z\s]{2,}:\s+)+")

# A line that is ONLY an all-caps section label, e.g. "DEFENSE", "ENDING"
_SECTION_LABEL_ONLY = re.compile(r"^[A-Z][A-Z\s]{2,}$")


def _strip_label_prefix(text: str) -> str:
    """Remove leading structural labels from a vo_script string."""
    return _LABEL_PREFIX.sub("", text).strip()


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
                beat.on_screen_text = derive_on_screen(beat.vo_script, max_items=5)
            elif beat.on_screen_text:
                # Non-VO mode: fix section-label-only lines in place.
                fixed = []
                for line in beat.on_screen_text:
                    if _SECTION_LABEL_ONLY.match(line.strip()):
                        fixed.extend(derive_on_screen(beat.vo_script))
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
