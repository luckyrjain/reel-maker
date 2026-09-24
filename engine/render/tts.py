import asyncio
import hashlib
import json
import logging
import re
import subprocess
import unicodedata
from pathlib import Path

_log = logging.getLogger(__name__)

from api.config import settings

# Short words the TTS spells out letter-by-letter because they look like acronyms.
_ACRONYM_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bEmi\b"), "Emmy"),
    (re.compile(r"\bEMI\b"), "Emmy"),
]

# Contractions missing their apostrophe — LLMs frequently omit them, causing
# the TTS engine to spell the word out letter-by-letter.
_CONTRACTION_FIXES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bisnt\b", re.IGNORECASE), "isn't"),
    (re.compile(r"\barent\b", re.IGNORECASE), "aren't"),
    (re.compile(r"\bwasnt\b", re.IGNORECASE), "wasn't"),
    (re.compile(r"\bwerent\b", re.IGNORECASE), "weren't"),
    (re.compile(r"\bdoesnt\b", re.IGNORECASE), "doesn't"),
    (re.compile(r"\bdont\b", re.IGNORECASE), "don't"),
    (re.compile(r"\bdidnt\b", re.IGNORECASE), "didn't"),
    (re.compile(r"\bhasnt\b", re.IGNORECASE), "hasn't"),
    (re.compile(r"\bhavent\b", re.IGNORECASE), "haven't"),
    (re.compile(r"\bhadnt\b", re.IGNORECASE), "hadn't"),
    (re.compile(r"\bwont\b", re.IGNORECASE), "won't"),
    (re.compile(r"\bwouldnt\b", re.IGNORECASE), "wouldn't"),
    (re.compile(r"\bcouldnt\b", re.IGNORECASE), "couldn't"),
    (re.compile(r"\bshouldnt\b", re.IGNORECASE), "shouldn't"),
    (re.compile(r"\bcant\b", re.IGNORECASE), "can't"),
    # "its" removed — cannot distinguish possessive from contraction with a regex;
    # TTS engines handle the possessive form correctly without intervention.
    (re.compile(r"\btheyre\b", re.IGNORECASE), "they're"),
    (re.compile(r"\bweve\b", re.IGNORECASE), "we've"),
    (re.compile(r"\btheyve\b", re.IGNORECASE), "they've"),
    (re.compile(r"\byouve\b", re.IGNORECASE), "you've"),
    (re.compile(r"\bive\b", re.IGNORECASE), "I've"),
    (re.compile(r"\bim\b", re.IGNORECASE), "I'm"),
    (re.compile(r"\bwhos\b", re.IGNORECASE), "who's"),
    (re.compile(r"\bwhats\b", re.IGNORECASE), "what's"),
    (re.compile(r"\bthats\b", re.IGNORECASE), "that's"),
    (re.compile(r"\bhes\b", re.IGNORECASE), "he's"),
    (re.compile(r"\bshes\b", re.IGNORECASE), "she's"),
    (re.compile(r"\blets\b", re.IGNORECASE), "let's"),
]


def _normalize_for_tts(text: str) -> str:
    """Prepare text for TTS:
    1. Restore missing apostrophes in contractions (isnt → isn't) so the TTS
       engine pronounces them as words, not letter sequences.
    2. Expand short words that get spelled out as acronyms (Emi → Emmy).
    3. Strip diacritics so accented names (Martínez, Álvarez) are pronounced
       naturally by an English neural voice.
    """
    for pattern, replacement in _CONTRACTION_FIXES:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _ACRONYM_FIXES:
        text = pattern.sub(replacement, text)
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


class SilentProvider:
    """Fallback: returns a 1 s silence file — no dependencies required."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str) -> Path:  # noqa: ARG002
        path = self.cache_dir / "silence_1s.wav"
        if not path.exists():
            import numpy as np
            import soundfile as sf

            sf.write(str(path), np.zeros(24000, dtype=np.float32), 24000)
        return path


class KokoroProvider:
    """TTS via the kokoro library (pip install kokoro soundfile)."""

    def __init__(
        self,
        cache_dir: Path,
        lang_code: str = "a",
        voice: str = "af_heart",
    ):
        self.cache_dir = cache_dir
        self.lang_code = lang_code
        self.voice = voice
        self._pipeline = None
        cache_dir.mkdir(parents=True, exist_ok=True)

    def _init(self) -> None:
        if self._pipeline is None:
            from kokoro import KPipeline

            self._pipeline = KPipeline(lang_code=self.lang_code)

    def synthesize(self, text: str) -> Path:
        import numpy as np
        import soundfile as sf

        if not text.strip():
            return SilentProvider(self.cache_dir).synthesize("")

        key = hashlib.sha256(text.encode()).hexdigest()[:20]
        out = self.cache_dir / f"{key}.wav"
        if out.exists():
            return out

        self._init()
        chunks = [audio for _, _, audio in self._pipeline(text, voice=self.voice)]
        audio_data = np.concatenate(chunks) if chunks else np.zeros(24000, dtype=np.float32)
        sf.write(str(out), audio_data, 24000)
        return out


def _audio_duration(path: Path) -> float | None:
    """Return audio duration in seconds via ffprobe, or None on failure."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        data = json.loads(result.stdout)
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "audio":
                return float(stream["duration"])
    except Exception:
        pass
    return None


# A curated subset of edge-tts's ~400 voices, offered on the create-reel form so every
# reel doesn't have to sound identical. Deliberately not the full catalog — a free-text
# voice field would let a typo reach edge_tts.Communicate() deep inside a Celery task at
# render time, failing minutes into a job instead of at submission. (value, display label).
CURATED_EDGE_VOICES: list[tuple[str, str]] = [
    ("en-GB-RyanNeural", "Ryan (British, male) — default"),
    ("en-US-GuyNeural", "Guy (American, male)"),
    ("en-US-JennyNeural", "Jenny (American, female)"),
    ("en-GB-SoniaNeural", "Sonia (British, female)"),
    ("en-AU-WilliamNeural", "William (Australian, male)"),
    ("en-AU-NatashaNeural", "Natasha (Australian, female)"),
    ("en-IE-ConnorNeural", "Connor (Irish, male)"),
    ("en-IN-PrabhatNeural", "Prabhat (Indian, male)"),
]
_CURATED_EDGE_VOICE_NAMES = {v for v, _ in CURATED_EDGE_VOICES}


class EdgeTTSProvider:
    """TTS via Microsoft Edge TTS — free neural voices, no C extensions, Python 3.14 compatible."""

    # British male voice suits football commentary; adjust as needed
    DEFAULT_VOICE = "en-GB-RyanNeural"

    def __init__(self, cache_dir: Path, voice: str = DEFAULT_VOICE):
        self.cache_dir = cache_dir
        self.voice = voice
        cache_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str, rate: str = "+0%") -> Path:
        if not text.strip():
            return SilentProvider(self.cache_dir).synthesize("")

        text = _normalize_for_tts(text)
        key = hashlib.sha256((self.voice + rate + text).encode()).hexdigest()[:20]
        out = self.cache_dir / f"{key}.mp3"
        if out.exists():
            return out

        import edge_tts

        async def _run() -> None:
            await edge_tts.Communicate(text, self.voice, rate=rate).save(str(out))

        asyncio.run(_run())
        return out

    def synth_to_budget(self, text: str, target_s: float, tol: float = 0.15) -> Path:
        """Synthesize VO and nudge speaking rate if duration drifts beyond tolerance.

        tol=0.15 means ±15% of target_s is acceptable without adjustment.
        Rate is clamped to ±25% to stay within natural-sounding range.
        """
        out = self.synthesize(text)
        actual = _audio_duration(out)
        if actual is None:
            return out
        drift = (actual - target_s) / max(target_s, 1.0)
        if abs(drift) <= tol:
            return out
        # drift > 0: actual > target → too slow → speed up → positive rate
        # drift < 0: actual < target → too fast → slow down → negative rate
        pct = max(-25, min(25, int(drift * 100)))
        if pct == 0:
            return out
        return self.synthesize(text, rate=f"{pct:+d}%")


def get_tts_provider(
    cache_dir: Path, voice: str | None = None
) -> "EdgeTTSProvider | KokoroProvider | SilentProvider":
    """`voice` is a per-reel override, only meaningful for EdgeTTSProvider — Kokoro's voice
    IDs (e.g. "af_heart") are a different namespace than edge-tts's (e.g. "en-GB-RyanNeural"),
    so passing an edge voice name through to Kokoro would just fail at synthesis time. Kokoro
    always uses its own default; there is no per-reel voice choice for it in this pipeline.
    An unrecognized value (not in CURATED_EDGE_VOICES) is treated the same as None — see
    api/routers/reels.py::create_reel, which already validates against the curated set before
    it ever reaches here, but this is defense in depth, not the only check."""
    provider = settings.tts_provider.lower()
    if provider == "kokoro":
        try:
            import kokoro  # noqa: F401
            return KokoroProvider(cache_dir)
        except ImportError:
            _log.warning(
                "TTS_PROVIDER=kokoro but kokoro is not installed — falling back to edge-tts. "
                "Run: pip install kokoro soundfile"
            )
    if provider in ("edge", "kokoro"):  # fall through from failed kokoro import
        try:
            import edge_tts  # noqa: F401
            if voice in _CURATED_EDGE_VOICE_NAMES:
                return EdgeTTSProvider(cache_dir, voice=voice)
            return EdgeTTSProvider(cache_dir)
        except ImportError:
            _log.warning(
                "edge-tts not installed — falling back to SilentProvider (no audio). "
                "Run: pip install edge-tts"
            )
    elif provider != "silent":
        _log.warning(
            "Unknown TTS_PROVIDER=%r — falling back to SilentProvider (no audio). "
            "Valid values: edge, kokoro, silent.",
            settings.tts_provider,
        )
    return SilentProvider(cache_dir)
