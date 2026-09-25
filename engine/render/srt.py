"""Pure SRT (SubRip) caption file formatting.

Mirrors compositor.py's existing separation between "build a filter/data structure"
(pure, unit-testable) and "run ffmpeg" (subprocess, integration-tested): this module
does no I/O beyond the final write, no ffmpeg, no network, and its input is already
in the reel's absolute timeline — offset shifting is the caller's job
(`engine/render/compositor.py::composite_cut()`), not this module's. See
docs/specs/2026-09-srt-caption-export-system-design.md §3.2.
"""
from pathlib import Path

from engine.render.captions import CaptionSegment


def _format_timestamp(seconds: float) -> str:
    """Format seconds as SRT's `HH:MM:SS,mmm` timestamp."""
    total_ms = round(max(seconds, 0.0) * 1000)
    hours, rem_ms = divmod(total_ms, 3_600_000)
    minutes, rem_ms = divmod(rem_ms, 60_000)
    secs, millis = divmod(rem_ms, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def write_srt(cues: list[CaptionSegment], path: Path) -> Path | None:
    """Write `cues` to `path` in standard SRT format.

    Sequential numbering starting at 1, `HH:MM:SS,mmm --> HH:MM:SS,mmm` timestamp
    lines, a blank line between cues. Returns `path` on success, or `None` (writing
    nothing) when `cues` is empty — not an error, matches this module's existing
    "no Whisper installed" degrade-gracefully behavior elsewhere in this codebase.
    """
    if not cues:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)

    blocks = []
    for i, cue in enumerate(cues, start=1):
        blocks.append(
            f"{i}\n"
            f"{_format_timestamp(cue.start_s)} --> {_format_timestamp(cue.end_s)}\n"
            f"{cue.text}\n"
        )

    path.write_text("\n".join(blocks), encoding="utf-8")
    return path
