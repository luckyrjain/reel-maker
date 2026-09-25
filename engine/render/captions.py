from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass
class CaptionSegment:
    text: str
    start_s: float
    end_s: float


@dataclass
class TranscriptResult:
    """Both fields are derived from a single Whisper `model.transcribe()` call so a
    render pays for transcription once per beat, not twice (see this module's
    docstring precedent in CLAUDE.md's `captions.py` entry).

    Both fields are **beat-relative timestamps**, exactly like `.words` always has
    been — `beat_offset_s` keeps its existing default-0.0, beat-relative meaning for
    BOTH fields. Do NOT treat either field as reel-absolute: `.words` is consumed by
    `compositor.py::_whisper_timestamps()`, which itself adds the beat's cumulative
    start time to convert beat-relative -> absolute for the burned-in-text overlay. If
    a caller pre-shifts `.words` (or `.segments`) by passing a non-zero `beat_offset_s`
    here, that addition double-applies and silently corrupts timing for every beat past
    the first. See CLAUDE.md's Key conventions entry for this exact rule.
    """

    words: list[CaptionSegment]
    segments: list[CaptionSegment]


@lru_cache(maxsize=1)
def _load_model(name: str):
    """Load (and keep) the Whisper model — transcribe_audio runs once per beat."""
    import whisper
    return whisper.load_model(name)


def transcribe_audio(audio_path: Path, beat_offset_s: float = 0.0) -> TranscriptResult:
    """
    Use Whisper to produce both word-level and segment-level caption cues from a
    single transcription pass.

    Returns `TranscriptResult(words=[], segments=[])` if openai-whisper is not
    installed.

    `beat_offset_s` keeps its pre-existing beat-relative meaning for BOTH `.words`
    and `.segments` — see `TranscriptResult`'s docstring. `composite_cut()` never
    passes a non-zero value here; it computes the reel-absolute offset itself, at
    the point it builds SRT cues, the same way `_whisper_timestamps()` already
    shifts `.words` for the burned-in-text overlay. This parameter is kept only
    because tests may want to construct offset transcripts directly.
    """
    try:
        import whisper  # noqa: F401
    except ImportError:
        return TranscriptResult(words=[], segments=[])

    model = _load_model("base")
    result = model.transcribe(str(audio_path), word_timestamps=True, fp16=False)

    words: list[CaptionSegment] = []
    segments: list[CaptionSegment] = []
    for seg in result.get("segments", []):
        segments.append(
            CaptionSegment(
                text=seg.get("text", "").strip(),
                start_s=beat_offset_s + float(seg["start"]),
                end_s=beat_offset_s + float(seg["end"]),
            )
        )
        for word_info in seg.get("words", []):
            words.append(
                CaptionSegment(
                    text=word_info["word"].strip(),
                    start_s=beat_offset_s + float(word_info["start"]),
                    end_s=beat_offset_s + float(word_info["end"]),
                )
            )
    return TranscriptResult(words=words, segments=segments)
