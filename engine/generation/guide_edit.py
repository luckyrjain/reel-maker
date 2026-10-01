"""
Operator-driven edits to an already-generated guide (the PATCH /cuts/{id} beat-edit
form, and the hook-variant swap) — as opposed to engine/generation/postprocess.py,
which fixes up a freshly-LLM-generated guide before it's first saved.

Pure dict-in/dict-out transforms, no HTTP/DB/FastAPI knowledge, so the normalization
rules here have a direct test surface (see tests/test_guide_edit.py) instead of only
being provable by driving the full PATCH endpoint through a TestClient. Extracted from
api/routers/cuts.py — see docs/specs/2026-09-guide-edit-module-design.md.
"""
from engine.generation.script_parser import derive_on_screen


def _normalize_text(value: str) -> str:
    """CRLF/CR -> LF, then strip. An HTML <textarea> always re-encodes its content's
    line endings on submit, touched or not, so both the submitted value and the
    already-stored value must go through this identically before comparing — comparing
    a normalized submission against a raw stored value would treat every untouched
    multi-line field as a content change on every save (this exact false positive
    shipped once and needed two review rounds to fully close, see CLAUDE.md's Key
    conventions entry on Cut.rendered_guide_fingerprint)."""
    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalize_lines(lines: list) -> list[str]:
    """Same normalization as _normalize_text, applied per-line to an on_screen_text
    list, with blanks dropped and the result capped at 5 — the same cap
    postprocess.py's clean_guide() and _build_text_filter() both already enforce."""
    result = []
    for raw in lines:
        for line in str(raw).replace("\r\n", "\n").replace("\r", "\n").splitlines():
            line = line.strip()
            if line:
                result.append(line)
    return result[:5]


def set_beat_field(beats: list[dict], index: int, field: str, new_value) -> bool:
    """Write `new_value` into beats[index][field] ONLY if it genuinely differs from
    what's stored, after normalizing both sides identically. Returns whether anything
    actually changed, so a caller can OR several calls together into one
    "does this guide need to be reassigned" flag without re-deriving the comparison
    rule itself at every call site.

    field == "vo_script" also re-derives on_screen_text as a side effect, matching
    this codebase's existing behavior: on_screen_text is never independently
    re-derived except as a consequence of a real vo_script edit (an on_screen_text
    edit submitted on its own goes through field == "on_screen_text" instead, which
    does NOT touch vo_script).
    """
    beat = beats[index]

    if field == "duration_s":
        if new_value == beat.get("duration_s"):
            return False
        beat["duration_s"] = new_value
        return True

    if field == "visual_direction":
        new_vd = new_value.strip()
        if new_vd == (beat.get("visual_direction") or "").strip():
            return False
        beat["visual_direction"] = new_vd
        return True

    if field == "vo_script":
        new_vo = _normalize_text(new_value)
        stored_vo = _normalize_text(beat.get("vo_script") or "")
        if new_vo == stored_vo:
            return False
        beat["vo_script"] = new_vo
        beat["on_screen_text"] = derive_on_screen(new_vo, max_items=5)
        return True

    if field == "on_screen_text":
        new_lines = _normalize_lines(new_value)
        stored_lines = _normalize_lines(beat.get("on_screen_text") or [])
        if new_lines == stored_lines:
            return False
        beat["on_screen_text"] = new_lines
        return True

    raise ValueError(f"set_beat_field: unknown field {field!r}")


def replace_beat_vo(guide: dict, beat_index: int, new_vo: str) -> dict:
    """Swap one beat's vo_script (and re-derive its on_screen_text), returning a NEW
    guide dict with the mutation applied. Callers must reassign the result onto
    Cut.guide themselves — SQLAlchemy's JSON column type only detects attribute
    reassignment, not in-place mutation of a mutable value it already holds, so
    `cut.guide["beats"][i]["vo_script"] = x` alone would silently not persist.

    No validation of beat_index/beat type here — that's an HTTP-level business rule
    (e.g. "beat 0 must be a hook beat"), owned by the caller, not this module.
    """
    beats = [dict(b) for b in guide.get("beats", [])]
    beats[beat_index]["vo_script"] = new_vo
    beats[beat_index]["on_screen_text"] = derive_on_screen(new_vo, max_items=5)
    new_guide = dict(guide)
    new_guide["beats"] = beats
    return new_guide
