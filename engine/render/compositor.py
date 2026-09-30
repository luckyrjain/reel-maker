"""
MoviePy 2.x compositor: assembles a list of beats into a 9:16 MP4.

Pipeline:
  1. MoviePy builds the video+audio track (footage, Ken Burns, VO mix) — no text.
  2. A single FFmpeg drawtext pass burns timed text overlays onto the finished file.
     Each on_screen_text segment is shown for its proportional slice of the beat's
     duration so text tracks the voice rather than being a static block.
"""
import logging
import math
import os
import subprocess
from pathlib import Path

import numpy as np
from moviepy import (
    AudioFileClip,
    CompositeAudioClip,
    CompositeVideoClip,
    ImageClip,
    VideoFileClip,
    concatenate_videoclips,
)
from moviepy.audio.fx import AudioFadeIn, AudioFadeOut
from PIL import Image, ImageDraw, ImageFont

_log = logging.getLogger(__name__)

TARGET_W = 1080
TARGET_H = 1920
FPS = 30
TEXT_Y_CENTER = 0.73  # as fraction of height
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/Helvetica.ttc",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default(size=size)


def _render_text_overlay(lines: list[str]) -> np.ndarray:
    """Return a TARGET_H × TARGET_W × 4 RGBA numpy array with the text burned in."""
    img = Image.new("RGBA", (TARGET_W, TARGET_H), (0, 0, 0, 0))
    if not lines:
        return np.array(img)

    draw = ImageDraw.Draw(img)
    pad = 14
    max_line_w = int(TARGET_W * 0.88)

    # Shrink font until every line fits within max_line_w
    font = _get_font(70)
    for size in range(70, 24, -2):
        font = _get_font(size)
        if all(draw.textbbox((0, 0), l, font=font)[2] <= max_line_w for l in lines):
            break

    line_sizes: list[tuple[int, int]] = []
    for line in lines:
        bb = draw.textbbox((0, 0), line, font=font)
        line_sizes.append((bb[2] - bb[0], bb[3] - bb[1]))

    total_h = sum(h for _, h in line_sizes) + pad * max(0, len(lines) - 1)
    y_center_px = int(TARGET_H * TEXT_Y_CENTER)
    y = y_center_px - total_h // 2
    y = max(int(TARGET_H * 0.12), min(y, int(TARGET_H * 0.88) - total_h))

    for line, (w, h) in zip(lines, line_sizes):
        x = (TARGET_W - w) // 2
        for dx, dy in ((-2, 2), (2, 2), (0, 3), (-2, -2), (2, -2)):
            draw.text((x + dx, y + dy), line, font=font, fill=(0, 0, 0, 210))
        draw.text((x, y), line, font=font, fill=(255, 255, 255, 255))
        y += h + pad

    return np.array(img)


def _fit_image_9_16(img: Image.Image) -> Image.Image:
    scale = max(TARGET_H / img.height, TARGET_W / img.width)
    new_w, new_h = int(img.width * scale), int(img.height * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left, top = (new_w - TARGET_W) // 2, (new_h - TARGET_H) // 2
    return img.crop((left, top, left + TARGET_W, top + TARGET_H))


def _ken_burns(frame: np.ndarray, duration_s: float) -> "CompositeVideoClip":
    """Slow 8% zoom-in using pre-computed keyframes (avoids per-frame PIL overhead)."""
    h, w = frame.shape[:2]
    pil_img = Image.fromarray(frame)
    # ~10 keyframes/s is smooth enough for an 8% zoom; cap at 60 to bound memory
    n = max(2, min(int(duration_s * 10), 60))
    kf_dur = duration_s / n
    clips = []
    for i in range(n):
        t_mid = (i + 0.5) * kf_dur
        zoom = 1.0 + 0.08 * (t_mid / duration_s)
        cw, ch = int(w / zoom), int(h / zoom)
        x1, y1 = (w - cw) // 2, (h - ch) // 2
        resized = np.array(pil_img.crop((x1, y1, x1 + cw, y1 + ch)).resize((w, h), Image.BILINEAR))
        clips.append(ImageClip(resized).with_duration(kf_dur))
    return concatenate_videoclips(clips)


def _crop_to_9_16(clip: VideoFileClip) -> VideoFileClip:
    scale = max(TARGET_H / clip.h, TARGET_W / clip.w)
    new_w = int(clip.w * scale)
    new_h = int(clip.h * scale)
    clip = clip.resized((new_w, new_h))
    return clip.cropped(
        x_center=new_w / 2,
        y_center=new_h / 2,
        width=TARGET_W,
        height=TARGET_H,
    )


def _build_media_sub_clip(media_path: Path | None, duration_s: float):
    """Build a single 9:16 clip from one image or video file.

    Returns (clip, readers): readers is the list of real VideoFileClip instances
    this call opened (empty for an image or black-frame clip). Neither
    concatenate_videoclips() (the default "chain" method only retains clip
    references when a clip has a mask) nor CompositeVideoClip.close() (only
    closes its synthetic bg/audio, never its .clips list) reach these nested
    readers -- the caller must close them explicitly. See
    docs/specs/2026-09-moviepy-reader-leak-system-design.md.

    Closes whatever it already opened before re-raising on failure: a
    VideoFileClip opened here (the probe, or a loop-replica) that never makes
    it into the returned `readers` list (because a LATER open/transform in
    this same call raises) would otherwise be unreachable to any caller --
    nothing outside this function ever learns it existed.
    """
    if not (media_path and media_path.exists()):
        black = np.zeros((TARGET_H, TARGET_W, 3), dtype=np.uint8)
        return ImageClip(black).with_duration(duration_s), []
    if media_path.suffix.lower() in _IMAGE_EXTS:
        img = Image.open(media_path).convert("RGB")
        frame = np.array(_fit_image_9_16(img))
        return _ken_burns(frame, duration_s), []

    readers: list[VideoFileClip] = []
    try:
        raw = VideoFileClip(str(media_path), audio=False)
        readers.append(raw)
        if raw.duration < duration_s:
            loops = math.ceil(duration_s / raw.duration) + 1
            raw.close()
            readers.pop()   # probe clip closed above — the loop copies below replace it
            for _ in range(loops):
                readers.append(VideoFileClip(str(media_path), audio=False))
            raw = concatenate_videoclips(readers)
        return _crop_to_9_16(raw).subclipped(0, duration_s), readers
    except Exception:
        for r in readers:
            try:
                r.close()
            except Exception:
                pass
        raise


_FONT_CANDIDATES = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
]


def _ffmpeg_font() -> str:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return ""


def _escape_drawtext(text: str) -> str:
    """Escape text for FFmpeg drawtext filter.

    No `%` handling here on purpose — drawtext's `%`-expansion engine is
    disabled wholesale via `:expansion=none` on the filter itself (see
    _build_text_filter()), not per-character escaping. The old `\\%`
    escape never actually reached drawtext as an escape: ffmpeg's own
    generic option-value parser strips a single backslash before
    drawtext's `%`-expansion parser sees the value, so `\\%` arrived
    there indistinguishable from a bare `%` — itself a "Stray %" parse
    error with expansion left at its default. Confirmed against real
    ffmpeg that a bare `%`, the old `\\%` escape, and `%%` all fail
    identically. With expansion=none, `\\%` and `%` render byte-identical
    (verified by real-ffmpeg frame comparison) — either would work, but a
    bare `%` is simpler and matches every other unescaped character this
    function leaves alone.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "’")   # curly apostrophe — safe in filter string
    text = text.replace(":", "\\:")
    return text


def _whisper_timestamps(
    lines: list[str],
    segments: list,  # list[CaptionSegment] — avoided import for optional dep
    beat_start: float,
    beat_dur: float,
) -> list[tuple[str, float, float]]:
    """Map on_screen_text lines to Whisper word-level timestamps.

    Divides the Whisper word stream proportionally across the N lines,
    producing absolute (start, end) times anchored to beat_start.

    Requires len(segments) >= len(lines): with fewer words than lines the
    slice arithmetic wraps to segments[-1] and lines overlap on screen.
    The caller checks this and falls back to proportional timing.
    """
    n = len(lines)
    m = len(segments)
    result = []
    for i, line in enumerate(lines):
        first = segments[i * m // n]
        last = segments[min((i + 1) * m // n, m) - 1]
        abs_start = max(beat_start, beat_start + first.start_s)
        abs_end = min(beat_start + beat_dur, beat_start + last.end_s)
        abs_end = max(abs_start + 0.4, abs_end)  # floor so flash is readable
        result.append((line, abs_start, abs_end))
    return result


def _build_beat_transcripts(beat_vo_paths: list[Path | None]) -> list:
    """One `transcribe_audio()` call per beat with VO audio — `None` for beats with
    no/missing audio. Always called at `transcribe_audio()`'s default
    `beat_offset_s=0.0` for every beat, never a per-beat cumulative offset: this list
    (via `.words`) feeds `_build_text_filter()` -> `_whisper_timestamps()`, which
    itself adds each beat's cumulative start time to convert beat-relative ->
    absolute. Passing a non-zero offset here would double-apply that shift and
    silently corrupt burned-in-text timing for every beat past the first — see
    `engine/render/captions.py::TranscriptResult`'s docstring and CLAUDE.md's Key
    conventions entry for this exact rule. `composite_cut()` also reads `.segments`
    off this same list to build SRT cues, shifting those itself at that call site
    (never inside `transcribe_audio()`) for the identical reason.
    """
    from engine.render.captions import transcribe_audio
    return [
        transcribe_audio(vp) if vp and vp.exists() else None
        for vp in beat_vo_paths
    ]


_FONT_SIZE = 60
_TEXT_Y = int(TARGET_H * TEXT_Y_CENTER) - _FONT_SIZE // 2  # single centred line at 73%
DEFAULT_TEXT_COLOR = "white"

# Curated named colors ffmpeg's drawtext filter recognizes, offered on the create-reel
# form — deliberately not a free-text/hex field. `text_color` is interpolated directly
# into the filter string as `fontcolor={text_color}` with no escaping (unlike the text
# content itself, which _escape_drawtext() sanitizes); an unvalidated value here would
# be a filter-graph injection point, not just a rendering-quality one. (value, display label).
CURATED_TEXT_COLORS: list[tuple[str, str]] = [
    (DEFAULT_TEXT_COLOR, "White — default"),
    ("yellow", "Yellow"),
    ("cyan", "Cyan"),
    ("red", "Red"),
    ("orange", "Orange"),
    ("black", "Black"),
]
_CURATED_TEXT_COLOR_NAMES = {c for c, _ in CURATED_TEXT_COLORS}


def _build_text_filter(
    beats: list[dict],
    beat_durations: list[float],
    beat_transcripts: list[list | None] | None = None,
    text_color: str = DEFAULT_TEXT_COLOR,
) -> str:
    """
    Build an FFmpeg drawtext filter chain.

    Each on_screen_text line is shown sequentially for its proportional share
    of the beat's duration (duration / n lines). When Whisper transcripts are
    available, _whisper_timestamps() maps lines to word-level timestamps instead.
    """
    if text_color not in _CURATED_TEXT_COLOR_NAMES:
        text_color = DEFAULT_TEXT_COLOR   # defense in depth — see CURATED_TEXT_COLORS' docstring
    font = _ffmpeg_font()
    font_clause = f":fontfile={font}" if font else ""
    parts: list[str] = []
    t_start = 0.0

    for bi, (beat, duration) in enumerate(zip(beats, beat_durations)):
        lines = (beat.get("on_screen_text") or [])[:5]
        if not lines:
            t_start += duration
            continue

        n = len(lines)
        transcripts = (beat_transcripts[bi]
                       if beat_transcripts and bi < len(beat_transcripts)
                       else None)

        if transcripts and len(transcripts) >= n:
            timed = _whisper_timestamps(lines, transcripts, t_start, duration)
        else:
            # Word-count proportional: longer sentences stay on screen longer.
            # Use ALL VO sentences for the denominator so each line's duration
            # reflects its true fraction of the audio, even when on_screen_text
            # has fewer lines than sentences.
            import re as _re
            vo = beat.get("vo_script", "")
            sentences = [s.strip() for s in _re.split(r"[.!?—]+", vo) if s.strip()]
            all_wcs = [len(s.split()) for s in sentences]
            W_total = sum(all_wcs) if all_wcs else n
            # Per-line word counts: real sentence for the first len(sentences) lines,
            # weight=1 for any extra lines beyond the sentence count.
            line_wcs = [all_wcs[i] if i < len(all_wcs) else 1 for i in range(n)]
            timed = []
            t = t_start
            beat_end = t_start + duration
            for line, wc in zip(lines, line_wcs):
                seg = max(0.3, duration * wc / W_total)
                seg_end = min(t + seg, beat_end)
                if t < beat_end:
                    timed.append((line, t, seg_end))
                t += seg
                if t >= beat_end:
                    break
            # Stretch last segment to fill any remaining beat time
            if timed:
                timed[-1] = (timed[-1][0], timed[-1][1], beat_end)

        for line, seg_start, seg_end in timed:
            escaped = _escape_drawtext(line)
            parts.append(
                f"drawtext=enable='between(t,{seg_start:.3f},{seg_end:.3f})'"
                f":text='{escaped}'"
                f":expansion=none"
                f"{font_clause}"
                f":fontsize={_FONT_SIZE}:fontcolor={text_color}"
                f":shadowcolor=black@0.85:shadowx=2:shadowy=2"
                f":x=(w-text_w)/2:y={_TEXT_Y}"
            )

        t_start += duration

    return ",".join(parts) if parts else "null"


def _center_crop_half(clip):
    """Crop an already-9:16 (TARGET_W x TARGET_H) clip to its centered half-width
    strip, for the 2-up collage layout. A pure lazy MoviePy transform — like
    .resized()/.subclipped() elsewhere in this file, it shares the underlying
    reader via shallow copy and opens no new VideoFileClip. Both _ken_burns()'s
    zoom keyframes and this crop are centered on the same full frame, so nesting
    them stays centered at every timestamp — no independent drift."""
    return clip.cropped(
        x_center=clip.w / 2, y_center=clip.h / 2,
        width=TARGET_W // 2, height=TARGET_H,
    )


def _build_collage_clip(media_paths: list[Path | None], duration_s: float):
    """2-up side-by-side layout for a beat with exactly two resolved media items,
    e.g. a beat naming two people, each with their own Wikipedia photo (the
    realistic multi-item case — see engine/render/asset_sourcer.py::
    resolve_beat_assets()'s Wikipedia branch). Both items play for the FULL beat
    duration side by side, instead of _build_beat_clip()'s default sequential
    cycling (duration split N ways). Only used for exactly 2 items — see
    docs/specs/2026-09-multi-image-collage-system-design.md for why 3+ falls
    back to sequential rather than a grid.

    Returns (clip, readers) — same shape as _build_media_sub_clip()/
    _build_beat_clip(): the returned clip is a CompositeVideoClip, and
    CompositeVideoClip.close() does not cascade to nested clips, but nothing
    relies on that — the real VideoFileClip readers opened by the two
    _build_media_sub_clip() calls below are exactly what the caller's existing
    readers-list threading already closes, unchanged. bg_color=(0, 0, 0) on the
    composite avoids MoviePy's transparent/alpha-mask compositing path (real
    per-frame work) — the two halves already tile the full frame edge to edge,
    so no background pixel is ever visible anyway.

    Everything after the first _build_media_sub_clip() call is wrapped in one
    try/except that closes every reader accumulated so far before re-raising —
    not just readers opened by the SECOND _build_media_sub_clip() call. A
    security review caught the first draft only guarded that second call:
    _center_crop_half() or the CompositeVideoClip(...) construction raising
    after BOTH sub-clips already succeeded would otherwise leak both real
    VideoFileClip readers with no caller ever able to reach them — the exact
    leak class this codebase mutation-tested and fixed four times already for
    the sequential path (Phase 7i, CLAUDE.md's Key conventions).
    """
    left_clip, left_readers = _build_media_sub_clip(media_paths[0], duration_s)
    readers = list(left_readers)
    try:
        right_clip, right_readers = _build_media_sub_clip(media_paths[1], duration_s)
        readers.extend(right_readers)
        left = _center_crop_half(left_clip).with_position((0, 0))
        right = _center_crop_half(right_clip).with_position((TARGET_W // 2, 0))
        collage = CompositeVideoClip([left, right], size=(TARGET_W, TARGET_H), bg_color=(0, 0, 0))
        return collage, readers
    except Exception:
        for r in readers:
            try:
                r.close()
            except Exception:
                pass
        raise


def _build_beat_clip(
    media_paths: list[Path | None],
    duration_s: float,
):
    """Build the video clip for a beat — no text overlay (added by FFmpeg pass).

    Returns (clip, readers): readers is every real VideoFileClip opened across
    this beat's media items, flattened, for the caller to close — see
    _build_media_sub_clip()'s docstring.

    Closes whatever readers earlier media items in this beat already opened if
    a LATER item's _build_media_sub_clip() call raises: that later call's own
    try/except already closes anything it opened internally before re-raising
    (see its docstring), but readers from an earlier, already-SUCCEEDED item in
    this same beat only exist in this function's own `readers` list — nothing
    outside this function has seen them yet, so this function must close them
    itself before propagating.
    """
    duration_s = max(duration_s, 0.5)

    if not media_paths:
        media_paths = [None]

    # Exactly 2 items -> side-by-side collage, both playing the full beat
    # duration. This branch must stay AFTER the duration floor and the
    # empty-list guard above — placed earlier, a short real beat would reach
    # _build_collage_clip() with an unfloored (possibly near-zero) duration,
    # diverging from every other path's guarantee. See
    # docs/specs/2026-09-multi-image-collage-system-design.md Correction 2.
    if len(media_paths) == 2:
        return _build_collage_clip(media_paths, duration_s)

    per = duration_s / len(media_paths)
    sub_clips = []
    readers: list[VideoFileClip] = []
    try:
        for p in media_paths:
            clip, item_readers = _build_media_sub_clip(p, per)
            sub_clips.append(clip)
            readers.extend(item_readers)
        beat_clip = concatenate_videoclips(sub_clips) if len(sub_clips) > 1 else sub_clips[0]
        return beat_clip, readers
    except Exception:
        for r in readers:
            try:
                r.close()
            except Exception:
                pass
        raise


_MUSIC_VOLUME = 0.18       # music level under narration, once ducked further by sidechaincompress
_MUSIC_ONLY_VOLUME = 0.7   # music level when there's no VO to duck against


def _build_ffmpeg_args(
    notxt_path: Path,
    tmp_path: Path,
    text_filter: str,
    music_path: Path | None,
    has_vo_audio: bool,
    total_duration: float = 0.0,
) -> list[str]:
    """Build the ffmpeg drawtext (+ optional music mixing) command.

    Kept as a pure function (no subprocess execution) so the filter graph is
    unit-testable without needing a real ffmpeg binary.
    """
    if music_path is None:
        return [
            "ffmpeg", "-y", "-i", str(notxt_path),
            "-vf", text_filter,
            "-c:a", "copy",
            "-c:v", "libx264",
            str(tmp_path),
        ]

    # The music input is looped indefinitely (-stream_loop -1 below) — atrim
    # gives ffmpeg an explicit, unambiguous stop point for it. Relying on
    # -shortest alone is not enough: without a bound in the filtergraph itself,
    # an infinite input into a filter chain with no other duration reference
    # (the has_vo_audio=False case has no [0:a] at all) can make ffmpeg's
    # internal filter buffering misbehave — reproduced in testing as a bogus
    # "No space left on device" filtering error with plenty of real disk free.
    trim = f"atrim=duration={total_duration:.3f},"

    if has_vo_audio:
        # Duck music under the VO via sidechaincompress — smooth, proportional
        # volume reduction driven by the VO's envelope. (CLAUDE.md's roadmap
        # notes mention "agate" as the planned approach; sidechaincompress is
        # used instead — agate is a hard on/off noise gate, not the smooth
        # ducking a narrated video actually wants. normalize=0 on amix keeps
        # the VO at its original level; without it amix halves both inputs'
        # volume to prevent clipping, which would quietly undercut the VO.)
        filter_complex = (
            f"[0:v]{text_filter}[vout];"
            f"[1:a]{trim}volume={_MUSIC_VOLUME}[music];"
            f"[music][0:a]sidechaincompress=threshold=0.05:ratio=8:attack=5:release=300[ducked];"
            f"[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]"
        )
    else:
        # No VO to duck against (music_only/silent voiceover_mode) — just mix
        # the looped/trimmed music track in at a normal listening level.
        filter_complex = f"[0:v]{text_filter}[vout];[1:a]{trim}volume={_MUSIC_ONLY_VOLUME}[aout]"

    return [
        "ffmpeg", "-y",
        "-i", str(notxt_path),
        "-stream_loop", "-1", "-i", str(music_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-c:a", "aac",
        "-shortest",  # music is looped indefinitely (-stream_loop -1) — must be capped
        str(tmp_path),
    ]


def _proportional_caption_cues(vo_script: str, duration: float) -> list:
    """Fallback per-beat SRT cues when a beat's Whisper `.segments` came back empty
    (Whisper not installed, or the beat has no VO audio at all). Reuses the exact
    same proportional word-count-based timing technique `_build_text_filter()`
    already uses for its own no-Whisper fallback (`re.split(r"[.!?—]+", vo)`) — not a
    second implementation — applied to every VO sentence rather than only the
    (5-line-capped) `on_screen_text` summary: a real caption track should cover the
    full VO, not a truncated highlight reel of it (see
    docs/specs/2026-09-srt-caption-export-system-design.md §3.2). Returns
    beat-relative `CaptionSegment` cues — the caller shifts to absolute time.
    """
    import re as _re

    from engine.render.captions import CaptionSegment

    sentences = [s.strip() for s in _re.split(r"[.!?—]+", vo_script or "") if s.strip()]
    if not sentences:
        return []

    word_counts = [len(s.split()) for s in sentences]
    total_words = sum(word_counts) or len(sentences)

    cues: list[CaptionSegment] = []
    t = 0.0
    for sentence, wc in zip(sentences, word_counts):
        seg = max(0.3, duration * wc / total_words)
        seg_end = min(t + seg, duration)
        if t < duration:
            cues.append(CaptionSegment(text=sentence, start_s=t, end_s=seg_end))
        t += seg
        if t >= duration:
            break
    if cues:
        cues[-1].end_s = duration
    return cues


# Extra candidate timestamps as fractions of total duration, sampled alongside the
# original ~0.5s-in frame (kept first/unchanged so a caller that only reads
# thumbnail_candidates[0] sees exactly the old single-frame behavior).
_THUMBNAIL_CANDIDATE_FRACTIONS = (0.25, 0.6, 0.85)


def _write_thumbnail_candidates(final, thumbnail_path: Path) -> list[Path]:
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    timestamps = [min(0.5, final.duration - 0.05)] + [
        min(max(frac * final.duration, 0.1), final.duration - 0.05)
        for frac in _THUMBNAIL_CANDIDATE_FRACTIONS
    ]
    paths = []
    for i, t in enumerate(timestamps):
        path = thumbnail_path if i == 0 else thumbnail_path.with_name(
            f"{thumbnail_path.stem}_{i}{thumbnail_path.suffix}"
        )
        frame = final.get_frame(max(t, 0.0))
        Image.fromarray(frame).save(str(path))
        paths.append(path)
    return paths


def composite_cut(
    beats: list[dict],
    beat_video_paths: list[list[Path | None]],
    beat_vo_paths: list[Path | None],
    output_path: Path,
    thumbnail_path: Path,
    music_path: Path | None = None,
    text_color: str = DEFAULT_TEXT_COLOR,
) -> tuple[float, list[Path], Path | None]:
    """
    Assemble beats into a single 9:16 MP4.
    Returns (total duration in seconds, thumbnail candidate paths — [0] is
    thumbnail_path itself, subtitle .srt path or None if there was nothing to
    caption — e.g. every beat's vo_script is empty, as in silent voiceover_mode).
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)

    beat_clips = []
    vo_tracks: list[AudioFileClip] = []
    video_readers: list[VideoFileClip] = []
    beat_durations: list[float] = []
    t = 0.0
    final = None
    notxt_path = output_path.with_suffix(".notxt.mp4")
    # Everything from here on must go through the finally block below so
    # video_readers/vo_tracks/final are closed on every exit path, not just the
    # happy one. Two review-caught corrections folded in: the per-beat loop
    # itself (which populates video_readers) is now INSIDE this try, not just
    # the code after it — a beat raising partway through (e.g. a corrupt video
    # file) used to leak every earlier beat's already-opened readers, since
    # nothing tracking them had been reached by the try/finally yet. Likewise
    # _write_thumbnail_candidates() moved inside this try — it used to sit
    # before the try even started, so a failure there skipped cleanup entirely.
    try:
        for beat, media_paths, vo_path in zip(beats, beat_video_paths, beat_vo_paths):
            duration = float(beat.get("duration_s", 5.0))
            beat_durations.append(duration)

            beat_clip, readers = _build_beat_clip(media_paths, duration)
            beat_clips.append(beat_clip)
            video_readers.extend(readers)

            if vo_path and vo_path.exists():
                audio_reader = None
                try:
                    audio_reader = AudioFileClip(str(vo_path))
                    track = audio_reader
                    if track.duration > duration:
                        track = track.subclipped(0, duration)
                    track = track.with_effects(
                        [AudioFadeIn(0.12), AudioFadeOut(0.12)]
                    ).with_start(t)
                    vo_tracks.append(track)
                except Exception:
                    _log.exception("Failed to load VO track %s for beat at t=%.2fs — beat will be silent", vo_path, t)
                    # track never made it into vo_tracks (append above is skipped on any
                    # raise), so the finally block's vo_tracks close loop can never reach
                    # it. Close the pre-transform audio_reader specifically, not whatever
                    # `track` currently is -- .subclipped()/.with_effects()/.with_start()
                    # return NEW wrapper objects via shallow copy that share the same
                    # underlying .reader, so closing the original releases it regardless
                    # of which (if any) transform succeeded before the raise.
                    if audio_reader is not None:
                        try:
                            audio_reader.close()
                        except Exception:
                            pass

            t += duration

        final = concatenate_videoclips(beat_clips, method="compose")

        if vo_tracks:
            final = final.with_audio(CompositeAudioClip(vo_tracks))

        # Candidate thumbnails at a few points across the reel (no text yet —
        # that's added below). thumbnail_candidates[0] is always thumbnail_path
        # itself, so a caller that ignores the rest of the list gets exactly the
        # old single-frame behavior at exactly the old timestamp.
        thumbnail_candidates = _write_thumbnail_candidates(final, thumbnail_path)

        final.write_videofile(
            str(notxt_path),
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            logger=None,
        )
        total_duration = float(final.duration)

        # Attempt Whisper transcription per beat — one call serves both the
        # burned-in-text timing below (.words) and the SRT cues built after it
        # (.segments). Falls back to proportional timing if Whisper is not installed.
        transcripts = _build_beat_transcripts(beat_vo_paths)
        beat_transcripts: list[list | None] = [
            (tr.words or None) if tr else None for tr in transcripts
        ]
        text_filter = _build_text_filter(beats, beat_durations, beat_transcripts, text_color)
        # Write to a temp path first; atomic replace so a killed process never
        # leaves a half-written servable file.
        tmp_path = output_path.with_suffix(".tmp.mp4")
        ffmpeg_args = _build_ffmpeg_args(
            notxt_path, tmp_path, text_filter,
            music_path=music_path, has_vo_audio=bool(vo_tracks),
            total_duration=total_duration,
        )
        result = subprocess.run(ffmpeg_args, capture_output=True)
        if result.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"FFmpeg text/audio pass failed (exit {result.returncode}):\n"
                + result.stderr.decode(errors="replace")
            )
        os.replace(tmp_path, output_path)

        # Build SRT cues from the same Whisper pass: each beat's .segments (or, when
        # Whisper produced none for that beat — not installed, or the beat has no VO
        # audio — the same proportional vo_script-sentence-split fallback
        # _build_text_filter() already uses above) shifted from beat-relative to the
        # reel's absolute timeline via an EXPLICIT running sum over beat_durations.
        # This deliberately does NOT reuse the `t` loop variable from the first loop
        # above — that loop has already run to completion by this point and `t` holds
        # only the reel's final total duration, not a per-beat cumulative offset.
        # This mirrors exactly what _whisper_timestamps() already does for .words,
        # just performed here instead of inside transcribe_audio() — see
        # engine/render/captions.py::TranscriptResult's docstring and CLAUDE.md's Key
        # conventions entry for this offset-handling rule.
        from engine.render import srt as srt_writer
        from engine.render.captions import CaptionSegment

        srt_cues: list[CaptionSegment] = []
        cue_offset = 0.0
        for bi, (beat, duration) in enumerate(zip(beats, beat_durations)):
            tr = transcripts[bi] if bi < len(transcripts) else None
            beat_segments = tr.segments if tr else []
            if not beat_segments:
                beat_segments = _proportional_caption_cues(beat.get("vo_script", ""), duration)
            for cue in beat_segments:
                srt_cues.append(
                    CaptionSegment(
                        text=cue.text,
                        start_s=cue_offset + cue.start_s,
                        end_s=cue_offset + cue.end_s,
                    )
                )
            cue_offset += duration

        subtitle_path = srt_writer.write_srt(srt_cues, output_path.with_suffix(".srt"))
    finally:
        notxt_path.unlink(missing_ok=True)
        # Each AudioFileClip/VideoFileClip holds an open ffmpeg reader; without
        # this a long reel leaks one process per beat until the worker recycles.
        # final.close() alone does not reach these: concatenate_videoclips()'s
        # default "chain" method only retains sub-clip references when a clip
        # has a mask, and CompositeVideoClip.close() never iterates self.clips
        # (only its own synthetic bg/audio) — see
        # docs/specs/2026-09-moviepy-reader-leak-system-design.md.
        for track in vo_tracks:
            try:
                track.close()
            except Exception:
                pass
        for reader in video_readers:
            try:
                reader.close()
            except Exception:
                pass
        try:
            final.close()
        except Exception:
            pass

    return total_duration, thumbnail_candidates, subtitle_path
