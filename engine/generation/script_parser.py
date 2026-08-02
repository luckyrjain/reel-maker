"""
Structured-script parser for the guide generation pipeline.

When the user provides a labelled script (GOALKEEPER / DEFENSE / MIDFIELD sections),
we parse it ourselves instead of asking the LLM to do so.  The LLM is then asked only
for visual_direction descriptions — a much simpler task it handles reliably.
"""
import re
from dataclasses import dataclass, field

WORDS_PER_SEC = 2.3   # comfortable English speech rate
MIN_BEAT_S = 3.0      # shorter floor so 2-word beats aren't padded with silence
MAX_BEAT_S = 30.0

# ALL-CAPS label on its own line, optional leading #
_SECTION_RE = re.compile(r"(?m)^(?:#+\s*)?([A-Z][A-Z ]{2,})\s*$")

# "PlayerName: rest of text"  — matches anywhere in a string (not just line-start)
_PLAYER_COLON_RE = re.compile(
    r"(?<![A-Za-z])([A-Z][a-z]+(?: [A-Z][a-z]+)+):\s*"
)


def calc_duration(text: str) -> float:
    words = len(text.split())
    # Add 1s buffer for natural pauses; clamp to MIN/MAX
    return round(max(MIN_BEAT_S, min(MAX_BEAT_S, words / WORDS_PER_SEC + 1.0)), 1)


def _clean_vo(text: str) -> str:
    """Strip leading player-name or section-label prefixes from spoken text."""
    text = re.sub(r"^(?:[A-Z][A-Za-z\s]{2,}:\s+)+", "", text.strip())
    return text.strip()


_MAX_LINE_CHARS = 28  # ~fontsize-60 safe width at 1080px

def derive_on_screen(vo: str, max_items: int = 3) -> list[str]:
    """Derive on_screen_text segments from the vo_script — one per sentence, up to max_items."""
    vo = _clean_vo(vo)
    sentences = [s.strip() for s in re.split(r"[.!?—]+", vo) if s.strip()]
    result: list[str] = []
    for sent in sentences:
        words = sent.split()
        if not words:
            continue
        # Build line word-by-word until it would exceed the character limit
        line_words: list[str] = []
        for w in words:
            candidate = " ".join(line_words + [w])
            if len(candidate) > _MAX_LINE_CHARS:
                break
            line_words.append(w)
        result.append(" ".join(line_words) if line_words else words[0][:_MAX_LINE_CHARS])
        if len(result) >= max_items:
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


def _split_by_player_entries(body: str) -> list[tuple[str, str]]:
    """
    Split a body string on 'PlayerName: ...' patterns wherever they appear.
    Returns [(player_name, text), ...].
    Empty player_name means general commentary.

    Example input:
      "Cristian Romero: Romero defends like every duel is personal.
       Lisandro Martinez: Lisandro is proof... This isn't glamorous."

    Output:
      [("Cristian Romero", "Romero defends like every duel is personal."),
       ("Lisandro Martinez", "Lisandro is proof..."),
       ("", "This isn't glamorous.")]
    """
    entries: list[tuple[str, str]] = []
    pos = 0
    prev_player = ""

    for m in _PLAYER_COLON_RE.finditer(body):
        # Text before this match belongs to the previous player (or is intro)
        chunk = body[pos:m.start()].strip()
        if chunk:
            entries.append((prev_player, chunk))
        prev_player = m.group(1)
        pos = m.end()

    # Remaining text
    remaining = body[pos:].strip()
    if remaining:
        # Check if remaining contains a clear "general commentary" portion
        # (sentences that don't start with a name) — split off from player text
        sentences = re.split(r"(?<=[.!?])\s+", remaining)
        player_lines = []
        general_lines = []
        for sent in sentences:
            # If sentence starts with a previously-seen player name or looks generic, it's general
            if _PLAYER_COLON_RE.match(sent):
                player_lines.append(sent)
            else:
                general_lines.append(sent)

        if player_lines:
            # These are sub-entries within the last player block
            for sub in player_lines:
                m2 = _PLAYER_COLON_RE.match(sub)
                if m2:
                    entries.append((prev_player, ""))
                    prev_player = m2.group(1)
                    entries.append((prev_player, sub[m2.end():].strip()))
                    prev_player = ""
                else:
                    general_lines.insert(0, sub)

        player_text = " ".join(player_lines).strip() if player_lines else remaining if not general_lines else ""
        general_text = " ".join(general_lines).strip()

        if not player_lines and remaining:
            entries.append((prev_player, remaining))
        else:
            if player_text:
                entries.append((prev_player, player_text))
            if general_text:
                entries.append(("", general_text))

    # Remove empty text entries
    return [(p, t) for p, t in entries if t.strip()]


def _split_sections(context: str) -> list[tuple[str, str]]:
    """
    Return [(label, body_text), ...] including a synthetic '' label for any
    intro text that appears before the first section header.
    """
    parts: list[tuple[str, str]] = []
    current_label = ""
    current_lines: list[str] = []

    for line in context.splitlines():
        m = _SECTION_RE.match(line.strip())
        if m:
            if current_lines:
                body = "\n".join(current_lines).strip()
                if body:
                    parts.append((current_label, body))
            current_label = m.group(1).strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        body = "\n".join(current_lines).strip()
        if body:
            parts.append((current_label, body))

    return parts


@dataclass
class BeatStub:
    index: int
    beat_type: str           # hook / body / cta
    section: str
    player: str              # primary player name, empty if none
    vo_script: str
    duration_s: float
    on_screen_text: list[str] = field(default_factory=list)


def _section_to_beats(label: str, body: str, start_index: int) -> list[BeatStub]:
    """Parse one section body into BeatStubs — one per player + one for general lines."""
    beats: list[BeatStub] = []
    idx = start_index

    entries = _split_by_player_entries(body)
    if not entries:
        # Whole body is one general beat
        vo = _clean_vo(body)
        if vo:
            beats.append(BeatStub(
                index=idx, beat_type="body", section=label, player="",
                vo_script=vo, duration_s=calc_duration(vo),
                on_screen_text=derive_on_screen(vo),
            ))
        return beats

    for player, text in entries:
        vo = _clean_vo(text)
        if not vo:
            continue
        beats.append(BeatStub(
            index=idx, beat_type="body", section=label, player=player,
            vo_script=vo, duration_s=calc_duration(vo),
            on_screen_text=derive_on_screen(vo),
        ))
        idx += 1

    return beats


def parse(context: str) -> list[BeatStub] | None:
    """
    Return a list of BeatStubs if context is a structured labelled script,
    otherwise return None.

    Requires at least 3 distinct section headers to be treated as structured.
    """
    sections = _split_sections(context)
    labelled = [s for s in sections if s[0]]  # skip the intro '' label for counting
    if len(labelled) < 3:
        return None

    beats: list[BeatStub] = []

    for s_idx, (label, body) in enumerate(sections):
        section_beats = _section_to_beats(label, body, len(beats))
        if not section_beats:
            continue
        # First beat of the first section (or intro block) → hook
        if not beats and section_beats:
            section_beats[0].beat_type = "hook"
        beats.extend(section_beats)

    if not beats:
        return None

    # Last beat → cta
    beats[-1].beat_type = "cta"

    # Re-number sequentially
    for i, b in enumerate(beats):
        b.index = i

    return beats
