"""
MoviePy 2.x compositor: assembles a list of beats into a 9:16 MP4.

Pipeline:
  1. MoviePy builds the video+audio track (footage, Ken Burns, VO mix) — no text.
  2. A single FFmpeg drawtext pass burns timed text overlays onto the finished file.
     Each on_screen_text segment is shown for its proportional slice of the beat's
     duration so text tracks the voice rather than being a static block.
"""
import math
import os
import re
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
from PIL import Image, ImageDraw, ImageFont

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
    """Build a single 9:16 clip from one image or video file."""
    if media_path and media_path.exists():
        if media_path.suffix.lower() in _IMAGE_EXTS:
            img = Image.open(media_path).convert("RGB")
            frame = np.array(_fit_image_9_16(img))
            return _ken_burns(frame, duration_s)
        else:
            raw = VideoFileClip(str(media_path), audio=False)
            if raw.duration < duration_s:
                loops = math.ceil(duration_s / raw.duration) + 1
                raw = concatenate_videoclips(
                    [VideoFileClip(str(media_path), audio=False) for _ in range(loops)]
                )
            return _crop_to_9_16(raw).subclipped(0, duration_s)
    black = np.zeros((TARGET_H, TARGET_W, 3), dtype=np.uint8)
    return ImageClip(black).with_duration(duration_s)


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
    """Escape text for FFmpeg drawtext filter."""
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "’")   # curly apostrophe — safe in filter string
    text = text.replace(":", "\\:")
    text = text.replace("%", "\\%")
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


_FONT_SIZE = 60
_TEXT_Y = int(TARGET_H * TEXT_Y_CENTER) - _FONT_SIZE // 2  # single centred line at 73%

def _build_text_filter(
    beats: list[dict],
    beat_durations: list[float],
    beat_transcripts: list[list | None] | None = None,
) -> str:
    """
    Build an FFmpeg drawtext filter chain.

    Each on_screen_text line is shown sequentially for its proportional share
    of the beat's duration (duration / n lines). When Whisper transcripts are
    available, _whisper_timestamps() maps lines to word-level timestamps instead.
    """
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

        if transcripts:
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
                f"{font_clause}"
                f":fontsize={_FONT_SIZE}:fontcolor=white"
                f":shadowcolor=black@0.85:shadowx=2:shadowy=2"
                f":x=(w-text_w)/2:y={_TEXT_Y}"
            )

        t_start += duration

    return ",".join(parts) if parts else "null"


def _build_beat_clip(
    media_paths: list[Path | None],
    duration_s: float,
) -> "VideoFileClip | CompositeVideoClip | ImageClip":
    """Build the video clip for a beat — no text overlay (added by FFmpeg pass)."""
    duration_s = max(duration_s, 0.5)

    if not media_paths:
        media_paths = [None]

    per = duration_s / len(media_paths)
    sub_clips = [_build_media_sub_clip(p, per) for p in media_paths]
    return concatenate_videoclips(sub_clips) if len(sub_clips) > 1 else sub_clips[0]


def composite_cut(
    beats: list[dict],
    beat_video_paths: list[list[Path | None]],
    beat_vo_paths: list[Path | None],
    output_path: Path,
    thumbnail_path: Path,
) -> float:
    """
    Assemble beats into a single 9:16 MP4.
    Returns the total duration in seconds.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail_path.parent.mkdir(parents=True, exist_ok=True)

    beat_clips = []
    vo_tracks: list[AudioFileClip] = []
    beat_durations: list[float] = []
    t = 0.0

    for beat, media_paths, vo_path in zip(beats, beat_video_paths, beat_vo_paths):
        duration = float(beat.get("duration_s", 5.0))
        beat_durations.append(duration)

        beat_clip = _build_beat_clip(media_paths, duration)
        beat_clips.append(beat_clip)

        if vo_path and vo_path.exists():
            try:
                track = AudioFileClip(str(vo_path))
                if track.duration > duration:
                    track = track.subclipped(0, duration)
                track = track.audio_fadein(0.12).audio_fadeout(0.12).with_start(t)
                vo_tracks.append(track)
            except Exception:
                pass

        t += duration

    final = concatenate_videoclips(beat_clips, method="compose")

    if vo_tracks:
        final = final.with_audio(CompositeAudioClip(vo_tracks))

    # Thumbnail at ~0.5 s into the first beat (no text yet — that's added below)
    thumb_t = min(0.5, final.duration - 0.05)
    frame = final.get_frame(thumb_t)
    Image.fromarray(frame).save(str(thumbnail_path))

    # Write video+audio without text overlays
    notxt_path = output_path.with_suffix(".notxt.mp4")
    try:
        final.write_videofile(
            str(notxt_path),
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            logger=None,
        )
        total_duration = float(final.duration)

        # Attempt Whisper transcription per beat for word-level text timing.
        # Falls back to proportional timing if Whisper is not installed.
        from engine.render.captions import transcribe_audio
        beat_transcripts: list[list | None] = [
            (transcribe_audio(vp) or None) if vp and vp.exists() else None
            for vp in beat_vo_paths
        ]
        text_filter = _build_text_filter(beats, beat_durations, beat_transcripts)
        # Write to a temp path first; atomic replace so a killed process never
        # leaves a half-written servable file.
        tmp_path = output_path.with_suffix(".tmp.mp4")
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", str(notxt_path),
                "-vf", text_filter,
                "-c:a", "copy",
                "-c:v", "libx264",
                str(tmp_path),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            tmp_path.unlink(missing_ok=True)
            raise RuntimeError(
                f"FFmpeg drawtext failed (exit {result.returncode}):\n"
                + result.stderr.decode(errors="replace")
            )
        os.replace(tmp_path, output_path)
    finally:
        notxt_path.unlink(missing_ok=True)

    return total_duration
