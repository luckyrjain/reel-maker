from dataclasses import dataclass
from pathlib import Path


@dataclass
class CaptionSegment:
    text: str
    start_s: float
    end_s: float


def transcribe_audio(audio_path: Path, beat_offset_s: float = 0.0) -> list[CaptionSegment]:
    """
    Use Whisper word-level timestamps to produce caption segments.
    Returns an empty list if openai-whisper is not installed.
    """
    try:
        import whisper
    except ImportError:
        return []

    model = whisper.load_model("base")
    result = model.transcribe(str(audio_path), word_timestamps=True, fp16=False)

    segments: list[CaptionSegment] = []
    for seg in result.get("segments", []):
        for word_info in seg.get("words", []):
            segments.append(
                CaptionSegment(
                    text=word_info["word"].strip(),
                    start_s=beat_offset_s + float(word_info["start"]),
                    end_s=beat_offset_s + float(word_info["end"]),
                )
            )
    return segments
