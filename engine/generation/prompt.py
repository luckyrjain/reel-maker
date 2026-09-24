import logging


_log = logging.getLogger(__name__)

_SYSTEM = """\
You are an expert faceless short-form video producer.
Your job: output a COMPLETED production guide as a JSON object.
Rules:
- Output ONLY the JSON object. No markdown fences, no commentary, no schema definitions.
- Every required field must have real content — never placeholder text.
- SCRIPT VERBATIM: If the topic contains explicit voiceover lines, quoted speech, or a structured \
script, copy those lines VERBATIM into the vo_script fields. Do NOT paraphrase, summarise, or \
merge them. Every line of the provided script must appear in some beat's vo_script.
- SECTIONS → BEATS: If the topic has labelled sections (e.g. GOALKEEPER, DEFENSE, HOOK), \
create a SEPARATE beat for each section. Do not merge two sections into one beat.
- NO LABEL PREFIXES IN VO: Do NOT include section headers or player name labels inside vo_script. \
Write ONLY the spoken words. Wrong: "MIDFIELD: The engine." Right: "The engine."
- FILL EACH BEAT: Every beat must have enough vo_script to fill its duration_s at ~3 words/sec. \
A 2.5s beat needs ~7 words. An 8s beat needs ~24 words. A 10s beat needs ~30 words. \
Under-filling creates dead air; over-filling causes audio cut-off.
- HOOK: The first beat must open with a direct question OR direct address to the viewer \
(e.g. "Imagine...", "You've never seen...", "Here's why..."). Max 12 words for the first sentence. \
Include a conflict trigger or clear stakes (legacy, history, final, glory, collapse).
- OPEN LOOPS: At least 2 body beats must use tension phrases that pull the viewer forward: \
"but here's what nobody talks about", "the real question is", "here's why", "but there's a problem", \
"what most people miss", "this is why".
- INSIGHT: Each body beat must EXPLAIN, not just state. Use causal language: "because", \
"which means", "allows", "enables", "forces", "this is why". \
BAD: "Martínez saved two penalties." \
GOOD: "Martínez saved two penalties because he studies shooter body language — \
he knew which way they'd go before they kicked."
- THROUGHLINE: The CTA must reference the central tension or player from the hook. \
If hook asks "Was this the greatest final?" CTA must circle back: \
"So — greatest final ever? Drop your verdict below."
- ON SCREEN TEXT: on_screen_text must be SHORT punchy phrases taken from the beat's vo_script \
(max 5 words each). Never put section labels (GOALKEEPER, DEFENSE, ENDING) as on_screen_text.
- PLAYER IMAGES: When a beat covers a specific player or person, start the visual_direction \
with their FULL NAME (e.g. "Emiliano Martinez saving a penalty kick" not "goalkeeper making save"). \
This is required to source their photo.\
"""

# Minimal concrete example the model can follow directly.
_EXAMPLE = """\
{
  "title": "5 Money Habits That Changed My Life",
  "niche": "personal finance",
  "cuts": [
    {
      "platform": "youtube_shorts",
      "target_length_s": 45.0,
      "beats": [
        {
          "index": 0,
          "type": "hook",
          "duration_s": 2.5,
          "visual_direction": "person staring at empty wallet, close-up hands",
          "on_screen_text": ["BROKE at 25"],
          "vo_script": "I was completely broke at 25.",
          "music_cue": "tense minimal",
          "transition": "cut"
        },
        {
          "index": 1,
          "type": "body",
          "duration_s": 8.0,
          "visual_direction": "person writing in budget notebook, morning light, desk",
          "on_screen_text": ["Habit #1", "Track every dollar"],
          "vo_script": "The first habit that changed everything was tracking every single dollar.",
          "music_cue": null,
          "transition": "cut"
        },
        {
          "index": 2,
          "type": "cta",
          "duration_s": 3.0,
          "visual_direction": "person smiling at phone showing bank balance",
          "on_screen_text": ["Follow for more"],
          "vo_script": "Follow for more money tips that actually work.",
          "music_cue": "uplifting",
          "transition": "fade"
        }
      ],
      "caption": "From broke to saving $10k — these 5 habits changed everything.",
      "hashtags": ["personalfinance", "moneytips", "savingmoney", "financialfreedom",
                   "budgeting", "savingschallenge", "moneyhabits", "financetips",
                   "wealthbuilding", "frugalliving", "moneygoals", "financialliteracy",
                   "savemoney", "moneyadvice", "getrich"]
    },
    {
      "platform": "instagram_reels",
      "target_length_s": 30.0,
      "beats": [
        {
          "index": 0,
          "type": "hook",
          "duration_s": 2.0,
          "visual_direction": "person holding cash fan, close-up",
          "on_screen_text": ["$10k saved"],
          "vo_script": "I saved ten thousand dollars this year.",
          "music_cue": "upbeat",
          "transition": "cut"
        },
        {
          "index": 1,
          "type": "body",
          "duration_s": 6.0,
          "visual_direction": "person at laptop reviewing spreadsheet",
          "on_screen_text": ["Habit #1", "Budget weekly"],
          "vo_script": "I budgeted every single week without fail.",
          "music_cue": null,
          "transition": "cut"
        },
        {
          "index": 2,
          "type": "cta",
          "duration_s": 2.5,
          "visual_direction": "person smiling at camera, bright background",
          "on_screen_text": ["Follow for more tips"],
          "vo_script": "Follow for more money tips like this.",
          "music_cue": "uplifting",
          "transition": "fade"
        }
      ],
      "caption": "Saved $10k in one year with these simple habits. 💰",
      "hashtags": ["personalfinance", "moneytips", "savingmoney", "financialfreedom",
                   "budgeting", "savingschallenge", "moneyhabits", "financetips",
                   "wealthbuilding", "frugalliving", "moneygoals", "financialliteracy",
                   "savemoney", "moneyadvice", "getrich"]
    }
  ]
}\
"""


def build_messages(
    context: str,
    niche: str,
    platforms: list[str],
    voiceover_mode: str,
    target_lengths: dict[str, float],
    prior_feedback: list[str] | None = None,
) -> list[dict]:
    platform_lines = "\n".join(
        f"  - {p}: {target_lengths.get(p, 45.0)}s target" for p in platforms
    )

    vo_note = (
        "Set vo_script to empty string '' on every beat — no voiceover."
        if voiceover_mode in ("music_only", "silent")
        else "Write punchy, conversational vo_script for every beat."
    )

    # Minimum beats needed: ~7s per beat is a comfortable average
    min_beats = max(3, int(max(target_lengths.values()) / 7))

    field_rules = "\n".join([
        "Beat.type: first beat must be 'hook' (1.5–3s), last beat must be 'cta'",
        f"Beat count: use AT LEAST {min_beats} beats — enough to fill the full target_length_s",
        "Beat.duration_s: beat durations must sum to ~target_length_s; spread content across all beats",
        "Beat.vo_script: write ~3 words per second of duration_s — a 2.5s beat → ~7 words, 8s beat → ~24 words, 10s beat → ~30 words. Under-filling creates dead air; over-filling causes audio cut-off.",
        "Beat.visual_direction: if the beat is about a specific person, start with their FULL NAME (e.g. 'Lionel Messi dribbling past defenders' — not 'footballer dribbling')",
        "Beat.on_screen_text: 1–3 items, max 5 words each",
        "Beat.transition: 'cut', 'fade', or 'slide'",
        "PlatformGuide.hashtags: exactly 15 tags, no # prefix",
        f"Include one cuts entry for EACH platform: {', '.join(platforms)}",
        f"Voiceover: {vo_note}",
        "IMPORTANT: If the TOPIC is a script with specific lines, use those exact lines as vo_script — do not invent new ones",
    ])

    ctx = context[:2000]
    if len(context) > 2000:
        _log.warning("build_messages: context truncated from %d to 2000 chars", len(context))
        ctx += "\n[context truncated]"

    user_msg = f"""\
Generate a faceless reel production guide for the following topic.
Return ONLY a JSON object with the same structure as the example below.
Do NOT return a schema or definitions — return a populated guide instance.

TOPIC: {ctx}
NICHE: {niche or "general"}
VOICEOVER MODE: {voiceover_mode}
PLATFORMS:
{platform_lines}

Field rules:
{field_rules}

Example output structure (replace ALL content with content about the topic above):
{_EXAMPLE}

Now output the guide for "{ctx}":"""

    msgs = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": user_msg},
    ]
    if prior_feedback:
        msgs.append({
            "role": "user",
            "content": (
                "Your previous attempt scored below the quality threshold. "
                "Fix exactly these issues, keep everything else:\n- "
                + "\n- ".join(prior_feedback)
            ),
        })
    return msgs


def build_visuals_messages(beats: list[dict], niche: str) -> list[dict]:
    """
    Lightweight prompt for structured-script mode.
    We already know the vo_script for every beat — just ask the LLM for
    visual_direction (and optionally music_cue).
    """
    lines = []
    for b in beats:
        player = b.get("player", "")
        player_hint = f"  PLAYER: {player}\n" if player else ""
        lines.append(
            f"BEAT {b['index']} ({b['beat_type']}, {b['duration_s']}s)\n"
            f"{player_hint}"
            f"  VO: {b['vo_script']}"
        )

    beat_list = "\n\n".join(lines)

    user_msg = f"""\
For each beat, write a specific visual_direction that an editor can use to find real footage.

NICHE: {niche or "general"}

Rules:
- Player beats: ALWAYS start with the player's FULL NAME, then describe the specific action from the VO.
- Non-player beats: describe the exact shot type and atmosphere.
- Extract the action or moment from the VO — do NOT write generic descriptions.
- Max 12 words. No vague phrases like "playing football", "action shot", or "highlights".

Examples of BAD vs GOOD:
  VO: "Emiliano Martinez breaks hearts. Ask France."
  BAD:  "Emiliano Martinez playing football, close-up action shot"
  GOOD: "Emiliano Martinez penalty shootout save France World Cup"

  VO: "Romero defends like every ball is a matter of national pride."
  BAD:  "Cristian Romero close-up action shot"
  GOOD: "Cristian Romero aggressive tackle last-ditch defensive clearance"

  VO: "Messi's passing vision unlocks defenses no one else can reach."
  BAD:  "Lionel Messi playing football highlights"
  GOOD: "Lionel Messi through-ball key pass threading defense"

  VO: "Argentina are a footballing empire. Subscribe for more."
  BAD:  "Argentina football footage"
  GOOD: "Argentina squad celebrating trophy lift stadium"

Beats:
{beat_list}

Return a JSON array only — one object per beat:
[{{"index": 0, "visual_direction": "..."}} , ...]
"""
    system = (
        "You are a video director assigning specific footage search queries. "
        "Output ONLY a JSON array. No markdown, no commentary. "
        "For each beat, derive visual_direction ONLY from the specific players, "
        "actions, and events named in that beat's VO. Do not use the global context "
        "or topic to infer additional visual content beyond what the VO explicitly mentions."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
