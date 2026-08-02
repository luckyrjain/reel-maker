"""Fallback visual_direction synthesis for beats the visuals LLM did not cover.

Pure content generation — no Celery, no db, no Job. Imported by
worker/tasks/generate.py when the LLM returns a degenerate or missing
visual_direction for a beat.
"""
import re as _re

from engine.generation.script_parser import BeatStub


_SECTION_FALLBACK_VISUALS: dict[str, str] = {
    "": "national team players celebrating trophy lift",
    "GOALKEEPER": "goalkeeper penalty save dramatic dive crowd reaction",
    "DEFENSE": "defender last-ditch tackle aerial duel clearance",
    "MIDFIELD": "midfielder pressing recovery run through-ball vision",
    "ATTACK": "striker one-on-one goal celebration sprint",
    "SECRET": "team tactical huddle training ground session",
    "ENDING": "team trophy lift celebration fans stadium",
}

_VO_TO_SHOT: list[tuple[str, str]] = [
    ("penalt",      "penalty save dramatic dive"),
    ("save",        "reflex save goalkeeper fingertip"),
    ("tackle",      "crunching tackle last-ditch clearance"),
    ("press",       "high press recovery run intense"),
    ("intercept",   "interception reading play anticipation"),
    ("dribble",     "dribbling skill beat defender"),
    ("assist",      "key pass through-ball assist"),
    ("pass",        "vision through-ball creative passing"),
    ("goal",        "goal celebration strike finish"),
    ("finish",      "clinical finish one-on-one goal"),
    ("shoot",       "long-range strike shot on goal"),
    ("header",      "aerial header dominant set piece"),
    ("cross",       "cross delivery wide position"),
    ("sprint",      "explosive sprint pace recovery run"),
    ("defend",      "defensive positioning block clearance"),
    ("width",       "overlapping run wide position attack"),
    ("overlap",     "overlapping run cross delivery"),
    ("engine",      "box-to-box run defensive work rate"),
    ("architect",   "vision creative passing midfield"),
    ("glue",        "link play pressing combination midfield"),
    ("balance",     "defensive cover positioning wide"),
    ("iq",          "positional awareness anticipation reading game"),
    ("intelligent", "positional awareness anticipation reading game"),
    ("striker",     "striker movement clinical finish"),
    ("forward",     "forward run in behind goal"),
    ("winger",      "winger dribbling wide attack"),
    ("greatest",    "iconic career best moments highlight reel"),
    ("scar",        "decisive pressure match moment"),
    ("terrif",      "unstoppable attacking run danger"),
    ("depend",      "team relying on player decisive moment"),
    ("elevat",      "player raising teammates performance"),
    ("deadli",      "clinical striker finishing goal"),
    ("captain",     "captain armband leading team"),
    ("trophy",      "trophy lift celebration winners medal"),
]


def _fallback_visual(stub: BeatStub) -> str:
    if stub.player:
        vo_lower = stub.vo_script.lower() if stub.vo_script else ""
        for keyword, shot in _VO_TO_SHOT:
            if keyword in vo_lower:
                return f"{stub.player} {shot}"
        return f"{stub.player} match action highlight"
    return _SECTION_FALLBACK_VISUALS.get(
        stub.section.upper(),
        f"football match {stub.section.lower()} intense action",
    )


_VO_NAME_RE = _re.compile(
    r'\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})+)\b'
)
_NON_PERSON = {"South American", "Premier League", "Copa America", "World Cup",
               "Champions League", "North American", "West European"}
_NON_PERSON_PREFIXES = _re.compile(
    r'\b(South|North|West|East|Premier|Copa|Champions|United|Real|Inter)\b'
)


def _first_person(vo: str, existing: str) -> str:
    """Return the first plausible person name in `vo`, or `existing` if already known."""
    if existing:
        return existing
    for m in _VO_NAME_RE.finditer(vo):
        name = m.group(1)
        if name not in _NON_PERSON and not _NON_PERSON_PREFIXES.search(name):
            return name
    return ""


_DEGENERATE_SUFFIXES = (" footage", " highlights", " action shot", " close-up action shot")
_DEGENERATE_PHRASES = ("playing football", "playing soccer", "show footage", "show highlights")


def _is_degenerate_visual(v: str) -> bool:
    vl = v.strip().lower()
    if not vl or len(vl) < 10:
        return True
    if any(vl.endswith(s) for s in _DEGENERATE_SUFFIXES):
        return True
    if any(p in vl for p in _DEGENERATE_PHRASES):
        return True
    return False
