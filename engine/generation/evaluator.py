import re

from engine.generation.guide_schema import MasterGuide


# ── helpers ──────────────────────────────────────────────────────────────────

def _sentences(text: str) -> list[str]:
    """Split text into sentences; filters short fragments (used for context coverage)."""
    return [s.strip() for s in re.split(r"[.!?\n]+", text) if len(s.strip()) > 15]


def _vo_sentences(text: str) -> list[str]:
    """Split VO into sentences including short ones — used for pacing and opener analysis."""
    return [s.strip() for s in re.split(r"[.!?\n]+", text) if len(s.strip()) > 5]


def _word_set(text: str) -> set[str]:
    return {w.lower() for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 3}


def _keyword_set(text: str) -> set[str]:
    """Meaningful keywords with possessives stripped so 'Romero's' matches 'Romero'."""
    text = re.sub(r"'s\b", "", text.lower())
    return {w for w in re.findall(r"[a-z]+", text) if len(w) > 3}


def _word_count(text: str) -> int:
    return len(text.split())


_NAME_RE = re.compile(r"\b([A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,}(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]{2,})+)\b")

_NON_PERSON_WORDS = {
    "world cup", "copa america", "copa libertadores", "premier league",
    "champions league", "ask france", "ask colombia", "ask anyone",
    "can argentina", "south america", "north america", "united states",
    "real madrid", "manchester city", "inter milan",
    # nationality/demonym forms that the regex also catches
    "south american", "north american", "central american",
    "south american side", "premier division",
}


def _person_names(text: str) -> list[str]:
    candidates = _NAME_RE.findall(text)
    result = []
    for name in candidates:
        if name.lower() in _NON_PERSON_WORDS:
            continue
        first = name.split()[0].lower()
        if first in {"ask", "can", "the", "this", "that", "and", "but", "for",
                     "because", "although", "however", "without", "despite",
                     "unlike", "within", "against", "between", "during"}:
            continue
        result.append(name)
    return result


def _beat_energy(vo: str) -> str:
    """Classify a beat's VO into an energy signature for momentum-shift detection."""
    vo = vo.strip()
    wc = _word_count(vo)
    if "?" in vo:
        return "question"
    if "!" in vo:
        return "exclamation"
    if wc <= 10:
        return "punchy"
    if wc >= 25:
        return "expansive"
    return "standard"


# ── scoring constants ─────────────────────────────────────────────────────────

_HOOK_TRIGGERS = re.compile(
    r"\b(wrong|secret|nobody|problem|weakness|could|might|biggest|"
    r"real question|hidden|actual|truth|stop|decide|fatal|danger|"
    r"risk|threat|surprise|shock|actually|truly|the reason|this is why|"
    r"not what you think|you won't believe|underrated|overrated)\b",
    re.IGNORECASE,
)
_HOOK_STAKES = re.compile(
    r"\b(nation|legacy|history|everything|championship|title|glory|"
    r"tournament|cup|final|trophy|winning|losing|fate|world|dream|season)\b",
    re.IGNORECASE,
)

_OPEN_LOOP = re.compile(
    r"\b(but there'?s|however|the problem|what nobody|nobody talks|"
    r"the real question|the truth is|this is why|here'?s why|and yet|"
    r"so why|but here'?s|the catch|except|unless|the issue|the concern)\b",
    re.IGNORECASE,
)

_ANALYSIS_MARKERS = re.compile(
    r"\b(allows?|enables?|because|which means|explains?|shows?|"
    r"demonstrates?|proves?|suggests?|forces?|creates?|leads? to|"
    r"gives?|means?|results? in)\b",
    re.IGNORECASE,
)

_CONFLICT_MARKERS = re.compile(
    r"\b(but|however|yet|despite|weakness|problem|flaw|risk|concern|"
    r"challenge|threat|exposed|fragile|nevertheless|on the other hand|"
    r"question|doubt|failure|collapse|criticism|vulnerable|danger)\b",
    re.IGNORECASE,
)

# High-quality CTA: asks for opinion, prediction, or debate
_CTA_ACTIONS_HIGH = re.compile(
    r"\b(comment|predict|drop|thoughts?|weigh in|let us know|vote|pick|"
    r"tell me|which do you|who do you|what do you think|your prediction|"
    r"your thoughts|below|down below|in the comments)\b",
    re.IGNORECASE,
)

# Low-quality CTA: passive subscription asks
_CTA_ACTIONS_LOW = re.compile(
    r"\b(subscribe|follow|like|notify|smash|hit|turn on|click|"
    r"watch|check out|join)\b",
    re.IGNORECASE,
)

_CONCLUSION_MARKERS = re.compile(
    r"\b(ultimately|in the end|this means|the verdict|so|therefore|"
    r"which means|the question is|can they|will they|comment|subscribe|"
    r"follow|prediction|verdict|conclusion|bottom line)\b",
    re.IGNORECASE,
)

_INSIGHT_STAT = re.compile(
    r"\b\d+[\+\-]?\s*(goals?|assists?|caps?|trophies|titles?|games?|"
    r"matches?|minutes?|percent|%|appearances?|clean sheets?|saves?|"
    r"tackles?|passes?|yards?|times?|seasons?|tournaments?|metres?|"
    r"kilometers?|seconds?)\b",
    re.IGNORECASE,
)
_INSIGHT_CAUSAL = re.compile(
    r"\b(allows?|enables?|because|which means|since|as a result|"
    r"thanks to|forces?|creates?|means?|explains?|leads? to)\b",
    re.IGNORECASE,
)
_INSIGHT_COMPARATIVE = re.compile(
    r"\b(more than|better than|unlike|higher than|faster than|most|"
    r"fewest|strongest|weakest|best|worst|ahead of|greater than|"
    r"less than|no other|than any|only player|all.?time)\b",
    re.IGNORECASE,
)

# Football-specific tactical vocabulary
_INSIGHT_TACTICAL = re.compile(
    r"\b(press(?:ing)?|high line|defensive block|transition|possession|"
    r"build.?up|false nine|counter.?attack|set piece|dead ball|shape|"
    r"formation|compact|intensity|overload|aerial|recovery|width|depth|"
    r"attacking third|defensive third|defensive line|off.?ball|on.?ball|"
    r"between lines|third man|pivot|regista|pressing trap|gegenpressing)\b",
    re.IGNORECASE,
)

# Universal tactical vocabulary — used for non-football niches
_INSIGHT_TACTICAL_UNIVERSAL = re.compile(
    r"\b(shape|formation|intensity|possession|transition|pressing|defensive|"
    r"attacking|recovery|spacing|rotation|strategy|positioning|momentum)\b",
    re.IGNORECASE,
)

# Football-specific on-screen actions
_VO_ACTIONS = re.compile(
    r"\b(pass(?:ing|es)?|shoot(?:ing|s)?|scor(?:ing|es?)|tackl(?:ing|es?)|"
    r"dribbl(?:ing|es?)|press(?:ing|es)?|sav(?:ing|es?)|head(?:ing|ers?)|"
    r"cross(?:ing|es)?|sprint(?:ing|s)?|defend(?:ing|s)?|attack(?:ing|s)?|"
    r"assist(?:ing|s)?|creat(?:ing|es?)|intercept(?:ing|s)?|clearances?)\b",
    re.IGNORECASE,
)

# Universal on-screen actions — used for non-football niches
_VO_ACTIONS_UNIVERSAL = re.compile(
    r"\b(scor(?:ing|es?)|shoot(?:ing|s)?|defend(?:ing|s)?|attack(?:ing|s)?|"
    r"sprint(?:ing|s)?|creat(?:ing|es?)|assist(?:ing|s)?|block(?:ing|s)?|"
    r"pass(?:ing|es)?|intercept(?:ing|s)?|drive(?:s|ing)?|"
    r"jump(?:ing|s)?|run(?:ning|s)?)\b",
    re.IGNORECASE,
)

# Football-specific event context (tournaments, leagues, matchdays)
_SPECIFIC_CONTEXT = re.compile(
    r"\b(202[0-9]|201[0-9]|World Cup|Champions League|Copa|final|"
    r"semi.?final|quarter.?final|vs\.?|against|tournament|season|"
    r"matchday|group stage)\b",
    re.IGNORECASE,
)

# Universal event context — used for non-football niches
_SPECIFIC_CONTEXT_UNIVERSAL = re.compile(
    r"\b(202[0-9]|201[0-9]|final|semi.?final|quarter.?final|vs\.?|"
    r"against|tournament|season|championship|playoff|league)\b",
    re.IGNORECASE,
)

_GENERIC_VISUAL = re.compile(
    r"\b(show (footage|highlights?|clips?|video|player)|generic|"
    r"football footage|soccer footage|sports footage|player highlights|"
    r"football action|soccer action)\b",
    re.IGNORECASE,
)

# Visuals that describe abstract concepts instead of sourceable footage
_ABSTRACT_VISUAL = re.compile(
    r"\b(embodies?|represents?|spirit|essence|greatness|legacy|"
    r"destiny|journey|narrative|feeling|soul|symbolizes?)\b",
    re.IGNORECASE,
)

# Encyclopaedic phrasing — signals Wikipedia/article tone, not human voiceover
_ARTICLE_TONE = re.compile(
    r"\b(who plays for|born in|is a professional|is known for|"
    r"with his \d+|according to statistics?|"
    r"it is (?:known|said|widely|generally|often)|it has been|"
    r"in this video|we will|we'?re going to|today we'?(?:re|ll)|"
    r"let'?s take a look|let'?s explore|let'?s talk about|"
    r"firstly\s*,|secondly\s*,|thirdly\s*,|"
    r"in conclusion|to summarize|to sum up)\b",
    re.IGNORECASE,
)

# Second-person direct address — marks conversational, viewer-facing tone
_SECOND_PERSON = re.compile(r"\b(you|your|you'?ve|you'?re|you'?d)\b", re.IGNORECASE)

# Direct-hook openers — marks present-tense or imperative opening
_DIRECT_HOOK = re.compile(
    r"\b(imagine|think about|here'?s|look,?|listen,?|"
    r"this is|that'?s why|but wait|now|watch this|picture this)\b",
    re.IGNORECASE,
)

# "world-class" omitted: hyphen prevents word-set matching
_EMOTION_POSITIVE = {
    "glory", "redemption", "greatness", "dominance", "legacy", "triumph",
    "iconic", "legendary", "unstoppable", "clinical", "dream", "destiny",
    "history", "defining", "electric", "courage", "belief", "fire",
    "passion", "pride", "hunger", "relentless",
    "villain", "empire", "warrior", "warriors", "hero", "heroes",
    "fearless", "ruthless", "lethal", "invincible", "formidable",
    "masterclass", "genius", "brilliant", "magic", "special",
    "elite", "greatest", "incredible", "unbelievable",
}
_EMOTION_NEGATIVE = {
    "collapse", "failure", "pressure", "weakness", "criticism", "heartbreak",
    "defeat", "despair", "crisis", "fragile", "exposed", "vulnerable",
    "concern", "flaw", "risk", "danger", "threat", "doubt", "fear", "pain",
    "question", "problem", "worry", "burden", "mistake", "error",
    "struggle", "difficult", "impossible", "chaos", "disaster", "brutal",
}
_EMOTION_WORDS = _EMOTION_POSITIVE | _EMOTION_NEGATIVE

_VISUAL_CATEGORY_RE = {
    "highlight":   re.compile(r"\b(goal|assist|save|dribble|skill|volley|header|strike|wonder)\b", re.IGNORECASE),
    "tactical":    re.compile(r"\b(tactic|formation|press|shape|block|analysis|defensive|attacking|transition|buildup|high line)\b", re.IGNORECASE),
    "celebration": re.compile(r"\b(celebrat|trophy|lift|champion|title|glory|win)\b", re.IGNORECASE),
    "crowd":       re.compile(r"\b(crowd|fans|stadium|atmosphere|cheer|supporter)\b", re.IGNORECASE),
    "training":    re.compile(r"\b(train|session|practice|drill|warm.?up|preparation)\b", re.IGNORECASE),
    "action":      re.compile(r"\b(run|sprint|jump|aerial|tackle|challenge|intercept|clearance|chase)\b", re.IGNORECASE),
}

MAX_WPS = 4.0
MIN_WPS = 0.8
TARGET_WPS_LOW = 2.0
MAX_AVG_SENTENCE_WORDS = 18
MAX_OPENER_WORDS = 16

# Words excluded from repetition and throughline keyword comparisons
_STOPWORDS = {
    "this", "that", "what", "with", "from", "have", "been", "more", "some",
    "just", "will", "also", "only", "even", "they", "them", "their", "when",
    "then", "than", "your", "here", "there", "where", "every", "each",
    "after", "player", "team", "game", "match", "play", "because", "which",
    "would", "could", "should",
}


def _visual_category(visual: str) -> str:
    for cat, pattern in _VISUAL_CATEGORY_RE.items():
        if pattern.search(visual):
            return cat
    return "other"


# ── main scorer ───────────────────────────────────────────────────────────────

def score_guide(
    context: str,
    guide: MasterGuide,
    target_length_s: float,
    axis_multipliers: dict[str, float] | None = None,
) -> tuple[int, list[str]]:
    """
    Score a generated MasterGuide against the standard for a top-tier automated reel.
    Returns (score 0–100, list of human-readable issues).

    axis_multipliers: optional per-axis deduction scaling (Settings.evaluator_axis_weight_multipliers).
    None/empty is a no-op — every axis behaves exactly as documented below. See the
    correction block at the end of this function for the exact formula and worked examples.

    Axes (max deductions exceed 100; final score clamped to 0–100)
    ────
    1  Retention Architecture  (20 pts)  hook quality(10) + open loops(5) + momentum shifts(5)
    2  Narrative Quality       (15 pts)  HOOK→CONTEXT→ANALYSIS→CONFLICT→CONCLUSION arc
    3  Context Coverage        (10 pts)  ≥50% of source context sentences echoed in VO
    4  Insight Density         (15 pts)  stats + causal + tactics + comparisons (per-beat distribution)
    5  Script → Visual Align   (20 pts)  entity(8) + action(8) + context(4) match
    6  Clip Availability       (10 pts)  visual_direction describes sourceable footage
    7  Visual Editability      (10 pts)  visual_direction is specific enough for automation
    8  Emotional Impact        (13 pts)  density(10) + distribution across beats(3)
    9  Audio Delivery           (5 pts)  WPS range + avg sentence length + punchy opener + rhythm
    10 Visual Variety           (5 pts)  mix of highlight/tactical/celebration/crowd/training
    11 Duration Fit             (5 pts)  beat durations sum within ±30% of cut target
    12 Caption & Hashtag        (5 pts)  caption ≥30 chars; ≥10 hashtags
    13 CTA Action               (3 pts)  quality-weighted: prediction/opinion > passive follow
    14 Conversational Tone     (10 pts)  penalise encyclopaedic phrasing; reward direct address
    15 Hook-CTA Throughline     (5 pts)  CTA references the hook's central tension or player
    16 Per-Beat Specificity     (5 pts)  each body beat makes at least one falsifiable claim
    17 Repetition               (5 pts)  body beats use distinct vocabulary across the script

    Tactical/action/context vocabulary selected by guide.niche:
      football/soccer/futbol → full football-specific patterns
      all other niches       → universal sport patterns
    """
    issues: list[str] = []
    score = 100
    deductions: dict[str, int] = {}

    # Both platform guides usually carry identical beats (the structured path
    # builds them from one stub list). Counting each beat twice inflates the
    # capped per-beat axes and pairs every beat against its own clone in the
    # repetition axis, so dedupe by content. Per-cut axes below still iterate
    # guide.cuts directly.
    _seen: set[tuple[int, str, str]] = set()
    all_beats = []
    for _cut in guide.cuts:
        for _b in _cut.beats:
            _key = (_b.index, _b.vo_script, _b.visual_direction)
            if _key in _seen:
                continue
            _seen.add(_key)
            all_beats.append(_b)
    all_vo = " ".join(b.vo_script for b in all_beats)
    all_vo_words = _word_set(all_vo)
    total_beats = len(all_beats)
    hook_beats = [b for b in all_beats if b.type == "hook"]
    body_beats = [b for b in all_beats if b.type == "body"]
    cta_beats = [b for b in all_beats if b.type == "cta"]
    first_vo = hook_beats[0].vo_script if hook_beats else (all_beats[0].vo_script if all_beats else "")

    # Select vocabulary based on niche
    _football = guide.niche.lower() in {"football", "soccer", "futbol"}
    _tactical_re = _INSIGHT_TACTICAL         if _football else _INSIGHT_TACTICAL_UNIVERSAL
    _actions_re  = _VO_ACTIONS               if _football else _VO_ACTIONS_UNIVERSAL
    _context_re  = _SPECIFIC_CONTEXT         if _football else _SPECIFIC_CONTEXT_UNIVERSAL

    # ── 1. Retention Architecture (20 pts) ───────────────────────────────────
    # Sub-axis A: Hook quality (0–10)
    # Checks actual VO signals — beat type is always "hook" so it's not a quality signal.
    hook_pts = 0
    first_sents = _vo_sentences(first_vo)
    opener_wc = len(first_sents[0].split()) if first_sents else 99
    if opener_wc <= 12:
        hook_pts += 3   # punchy opener (short first sentence = high impact)
    elif opener_wc <= 16:
        hook_pts += 1
    if "?" in first_vo:
        hook_pts += 3
    if _HOOK_TRIGGERS.search(first_vo):
        hook_pts += 2
    if _HOOK_STAKES.search(first_vo):
        hook_pts += 2
    if _SECOND_PERSON.search(first_vo):
        hook_pts += 1   # direct address ("you've never seen...")
    hook_pts = min(10, hook_pts)
    hook_sub_deduction = max(0, 10 - hook_pts)

    # Sub-axis B: Open Loops (0–5) — phrases that pull viewers to the next beat
    loop_beats = sum(1 for b in all_beats if _OPEN_LOOP.search(b.vo_script))
    if loop_beats >= 2:
        open_loop_pts = 5
    elif loop_beats == 1:
        open_loop_pts = 3
    else:
        open_loop_pts = 0
    open_loop_deduction = 5 - open_loop_pts

    # Sub-axis C: Momentum Shifts (0–5) — energy variety across beats
    energies = [_beat_energy(b.vo_script) for b in all_beats if b.vo_script.strip()]
    vo_lengths = [_word_count(b.vo_script) for b in all_beats if b.vo_script.strip()]
    unique_energies = len(set(energies))
    has_short = any(l <= 10 for l in vo_lengths)
    has_long = any(l >= 25 for l in vo_lengths)
    momentum_pts = min(5, unique_energies + int(has_short) + int(has_long) - 1)
    momentum_pts = max(0, momentum_pts)
    momentum_deduction = max(0, 5 - momentum_pts)

    retention_deduction = hook_sub_deduction + open_loop_deduction + momentum_deduction
    if retention_deduction > 0:
        score -= retention_deduction
        deductions["retention"] = retention_deduction
        if hook_sub_deduction > 0:
            issues.append(
                f'Weak hook: "{first_vo[:80]}…" — needs a short punchy opener (≤8 words), '
                "a question, conflict trigger, direct address ('you'), or clear stakes"
            )
        if open_loop_deduction > 0:
            issues.append(
                "No open loops — add phrases like 'but there's one problem' or 'what nobody talks about' "
                "to pull viewers to the next beat"
            )
        if momentum_deduction > 0:
            issues.append(
                "Flat energy — every beat feels the same; mix short punchy lines with longer analysis"
            )

    # ── 2. Narrative Quality (15 pts) ────────────────────────────────────────
    # 5 required story stages: HOOK · CONTEXT · ANALYSIS · CONFLICT · CONCLUSION
    has_hook_stage = bool(hook_beats)
    # Context: a body beat that names someone specific or references a real event/tournament
    has_context_stage = any(
        bool(_person_names(b.vo_script)) or bool(_context_re.search(b.vo_script))
        for b in body_beats
    )
    has_analysis_stage = any(_ANALYSIS_MARKERS.search(b.vo_script) for b in body_beats)
    has_conflict_stage = any(_CONFLICT_MARKERS.search(b.vo_script) for b in body_beats)
    has_conclusion_stage = bool(cta_beats) or any(
        _CONCLUSION_MARKERS.search(b.vo_script) for b in all_beats[-2:]
    )

    stages = [has_hook_stage, has_context_stage, has_analysis_stage,
              has_conflict_stage, has_conclusion_stage]
    missing_stages = stages.count(False)
    narrative_deduction = min(15, missing_stages * 3)
    if narrative_deduction > 0:
        score -= narrative_deduction
        deductions["narrative"] = narrative_deduction
        labels = ["HOOK", "CONTEXT", "ANALYSIS", "CONFLICT", "CONCLUSION"]
        absent = [labels[i] for i, present in enumerate(stages) if not present]
        issues.append(
            f"Incomplete story arc — missing stage(s): {', '.join(absent)} "
            "(needs HOOK→CONTEXT→ANALYSIS→CONFLICT→CONCLUSION)"
        )

    # ── 3. Context Coverage (10 pts) ─────────────────────────────────────────
    ctx_sentences = _sentences(context)[:30]
    if ctx_sentences:
        all_vo_kw = _keyword_set(all_vo)
        ctx_matched = 0
        for ctx_sent in ctx_sentences:
            ctx_kw = _keyword_set(ctx_sent)
            if not ctx_kw:
                ctx_matched += 1
                continue
            if len(ctx_kw & all_vo_kw) / len(ctx_kw) >= 0.30:
                ctx_matched += 1
        ctx_ratio = ctx_matched / len(ctx_sentences)
        if ctx_ratio < 0.50:
            ctx_deduction = min(10, int((0.50 - ctx_ratio) * 40))
            score -= ctx_deduction
            deductions["context"] = ctx_deduction
            issues.append(
                f"Low context coverage ({ctx_matched}/{len(ctx_sentences)} source sentences "
                f"echoed in VO, {ctx_ratio:.0%}) — script ignores too much of the source material"
            )

    # ── 4. Insight Density (15 pts) — per-beat distribution ─────────────────
    # Count beats that carry each signal rather than total occurrences, so a single
    # information-dense beat cannot inflate the score.
    insight_pts = 0
    insight_pts += min(5, sum(1 for b in all_beats   if _INSIGHT_STAT.search(b.vo_script)) * 2)
    insight_pts += min(4, sum(1 for b in body_beats  if _INSIGHT_CAUSAL.search(b.vo_script)))
    tactical_hits = len(set(m.lower() for m in _tactical_re.findall(all_vo)))
    # Narrative reels (no tactical vocab) get 2/4 baseline — they aren't shallow, just a different style
    insight_pts += min(4, tactical_hits) if tactical_hits > 0 else 2
    insight_pts += min(2, sum(1 for b in all_beats   if _INSIGHT_COMPARATIVE.search(b.vo_script)))

    insight_deduction = max(0, 15 - insight_pts)
    if insight_deduction > 0:
        score -= insight_deduction
        deductions["insight"] = insight_deduction
        if insight_deduction > 5:
            issues.append(
                "Low insight density — script states facts rather than explaining them "
                "(e.g. 'Romero defends well' → 'Romero's press lets Argentina defend 15 yards higher "
                "because it forces turnovers in dangerous areas')"
            )
        else:
            issues.append(
                f"Thin insight ({insight_pts}/15 pts) — add one more stat, causal claim, or tactical observation"
            )

    # ── 5. Script → Visual Alignment (20 pts) ────────────────────────────────
    entity_matches: float = 0
    entity_total = 0
    action_matches = action_total = 0
    context_matches = context_total = 0

    for beat in all_beats:
        vis = beat.visual_direction
        vis_lower = vis.lower()

        names = _person_names(beat.vo_script)
        if names:
            entity_total += 1
            n_matched = sum(
                1 for name in names
                if any(p in vis_lower for p in name.lower().split())
            )
            entity_matches += n_matched / len(names)

        if _actions_re.search(beat.vo_script):
            action_total += 1
            if _actions_re.search(vis):
                action_matches += 1

        if _context_re.search(beat.vo_script):
            context_total += 1
            if _context_re.search(vis):
                context_matches += 1

    # Each sub-signal only contributes to the 20-point max when it actually applies to
    # this content (entity_total/action_total/context_total > 0). A niche whose VO never
    # names a person or uses action/event vocabulary (personal finance, tech reviews,
    # cooking — anything without named individuals or competition-style verbs) would
    # otherwise have every sub-signal's *_total stay 0, collapsing this whole axis to a
    # flat 20-point deduction regardless of how well the visuals actually match the
    # script — confirmed empirically: a realistic, well-aligned personal-finance guide
    # scored align_deduction=20 (the maximum) purely because _person_names()/_actions_re/
    # _context_re found nothing to check, not because anything was actually misaligned.
    # Redistributing the 20 points across only the applicable sub-signals — and treating
    # zero applicable sub-signals as full credit rather than zero — mirrors the same
    # "not applicable ≠ maximally bad" principle Insight Density already applies to
    # non-tactical reels (the 2/4 tactical baseline above).
    applicable_max = 0.0
    align_score = 0.0
    if entity_total:
        applicable_max += 8
        align_score += (entity_matches / entity_total) * 8
    if action_total:
        applicable_max += 8
        align_score += (action_matches / action_total) * 8
    if context_total:
        applicable_max += 4
        align_score += (context_matches / context_total) * 4

    if applicable_max == 0:
        align_deduction = 0
    else:
        align_deduction = max(0, round(20 - (align_score / applicable_max) * 20))
    if align_deduction > 0:
        score -= align_deduction
        deductions["alignment"] = align_deduction
        parts: list[str] = []
        if entity_total and entity_matches / entity_total < 0.7:
            pct = int(entity_matches / entity_total * 100)
            parts.append(f"entity mismatch ({pct}% of named players covered in visuals)")
        if action_total and action_matches / action_total < 0.6:
            parts.append(f"action mismatch ({action_matches}/{action_total} action beats aligned)")
        if context_total and context_matches / context_total < 0.5:
            parts.append(f"context mismatch ({context_matches}/{context_total} event references aligned)")
        if parts:
            issues.append("Script → visual misalignment: " + "; ".join(parts))

    # ── 6. Clip Availability (10 pts) ────────────────────────────────────────
    clip_pts = 0
    for beat in all_beats:
        vis = beat.visual_direction.strip()
        has_name = bool(_person_names(vis))
        has_action = bool(_actions_re.search(vis)) or bool(_tactical_re.search(vis))
        has_ctx = bool(_context_re.search(vis))
        is_abstract = bool(_ABSTRACT_VISUAL.search(vis))

        if is_abstract:
            clip_pts += 0     # "Messi embodies destiny" — impossible to source
        elif has_name and (has_action or has_ctx):
            clip_pts += 3     # "Messi through-ball vs Croatia 2022" — highly sourceable
        elif has_name or (has_action and len(vis.split()) >= 5):
            clip_pts += 2     # named person or detailed action
        elif len(vis.split()) >= 4:
            clip_pts += 1     # some specificity

    max_clip = total_beats * 3
    clip_ratio = clip_pts / max(max_clip, 1)
    if clip_ratio < 0.55:
        clip_deduction = min(10, round((0.55 - clip_ratio) * 22))
        score -= clip_deduction
        deductions["clip"] = clip_deduction
        hard_count = sum(1 for b in all_beats if _ABSTRACT_VISUAL.search(b.visual_direction))
        issues.append(
            f"Low clip availability ({clip_pts}/{max_clip} pts) — "
            f"{hard_count} beat(s) use abstract descriptions that can't be sourced as footage; "
            "use: player name + specific action + match context"
        )

    # ── 7. Visual Editability (10 pts) ───────────────────────────────────────
    editable_pts = 0
    vague_beats: list[str] = []
    for beat in all_beats:
        vis = beat.visual_direction.strip()
        wc = len(vis.split())
        is_generic = bool(_GENERIC_VISUAL.search(vis))

        if is_generic or wc < 4:
            vague_beats.append(f'beat {beat.index}: "{vis[:50]}"')
        elif wc >= 8 and not is_generic:
            editable_pts += 2   # detailed and specific
        elif wc >= 5:
            editable_pts += 1   # adequate

    max_editable = total_beats * 2
    edit_ratio = editable_pts / max(max_editable, 1)
    if edit_ratio < 0.5 or len(vague_beats) / max(total_beats, 1) > 0.3:
        edit_deduction = min(10, max(
            round((0.5 - edit_ratio) * 20),
            round(len(vague_beats) / max(total_beats, 1) * 12)
        ))
        score -= edit_deduction
        deductions["editability"] = edit_deduction
        issues.append(
            f"{len(vague_beats)} beat(s) have vague visual_direction — "
            "an automated editor can't reliably fetch these; "
            "be specific: 'Messi beats Gvardiol then assists Alvarez, 2022 WC semifinal'"
        )

    # ── 8. Emotional Impact (13 pts) — density + distribution ────────────────
    pos_hits = len(all_vo_words & _EMOTION_POSITIVE)
    neg_hits = len(all_vo_words & _EMOTION_NEGATIVE)
    total_hits = pos_hits + neg_hits

    if total_hits < 2:
        emo_deduction = min(10, (3 - total_hits) * 4)
        score -= emo_deduction
        deductions["emotion"] = emo_deduction
        issues.append(
            f"Emotionally flat ({total_hits} emotion words) — "
            "top reels are emotion engines; add glory, redemption, collapse, pressure, destiny"
        )
    elif total_hits < 4:
        score -= 4
        deductions["emotion"] = 4
        issues.append(
            f"Low emotional density ({total_hits} emotion words, pos={pos_hits}/neg={neg_hits}) — "
            "script needs more emotional contrast"
        )
    elif pos_hits == 0 or neg_hits == 0:
        score -= 2
        deductions["emotion"] = 2
        issues.append(
            "Single emotional polarity — mix positive (glory, legacy) with negative (weakness, pressure) "
            "for more compelling contrast"
        )

    # Distribution: emotion should spread across ≥40% of beats, not cluster in one
    if total_hits >= 4:
        beats_with_emotion = sum(1 for b in all_beats if _word_set(b.vo_script) & _EMOTION_WORDS)
        if total_beats >= 3 and beats_with_emotion < max(2, int(total_beats * 0.4)):
            score -= 3
            deductions["emotion"] = deductions.get("emotion", 0) + 3
            issues.append(
                f"Emotion clustered — only {beats_with_emotion}/{total_beats} beats have emotional language; "
                "spread glory/pressure/destiny across the reel, not just hook and CTA"
            )

    # ── 9. Audio Delivery Quality (5 pts) ────────────────────────────────────
    # Hook beats use a tighter WPS cap — a rushed hook is the worst first impression.
    MAX_WPS_HOOK = 3.0

    pacing_problems: list[str] = []
    hook_pacing_problems: list[str] = []
    all_beat_wcs = [_word_count(b.vo_script) for b in all_beats if b.vo_script.strip()]

    for cut in guide.cuts:
        for beat in cut.beats:
            vo = beat.vo_script.strip()
            if not vo:
                continue
            wps = _word_count(vo) / max(beat.duration_s, 0.1)
            wps_cap = MAX_WPS_HOOK if beat.type == "hook" else MAX_WPS
            if wps > wps_cap:
                msg = (
                    f"hook beat {beat.index} ({wps:.1f} wps > {wps_cap} — hook will feel rushed; "
                    f"shorten VO or increase duration_s)"
                    if beat.type == "hook"
                    else f"beat {beat.index} ({wps:.1f} wps — will be cut off)"
                )
                (hook_pacing_problems if beat.type == "hook" else pacing_problems).append(msg)
            elif wps < MIN_WPS and beat.duration_s >= 4.0:
                pacing_problems.append(
                    f"beat {beat.index} ({_word_count(vo)}w/{beat.duration_s:.0f}s = {wps:.1f} wps — silent gap)"
                )

    # Beat-level rhythm: penalise if every beat has near-identical word count
    if len(all_beat_wcs) >= 3:
        mean_wc = sum(all_beat_wcs) / len(all_beat_wcs)
        monotone = all(abs(w - mean_wc) / max(mean_wc, 1) < 0.25 for w in all_beat_wcs)
        if monotone:
            pacing_problems.append("all beats same length — no rhythm variation")

    # Sentence-level: long sentences sound unnatural in TTS delivery
    all_sents = []
    for b in all_beats:
        all_sents.extend(_vo_sentences(b.vo_script))
    if all_sents:
        avg_sent_words = sum(len(s.split()) for s in all_sents) / len(all_sents)
        long_sents = [s for s in all_sents if len(s.split()) > 22]
        if avg_sent_words > MAX_AVG_SENTENCE_WORDS:
            pacing_problems.append(
                f"avg sentence {avg_sent_words:.0f} words (target ≤{MAX_AVG_SENTENCE_WORDS}) — "
                "break long sentences for natural TTS delivery"
            )
        elif len(long_sents) > len(all_sents) * 0.35:
            pacing_problems.append(
                f"{len(long_sents)} sentence(s) >22 words — hard to deliver naturally; split them"
            )

    # Punchy opener: hook's first sentence should be short
    if first_sents and len(first_sents[0].split()) > MAX_OPENER_WORDS:
        pacing_problems.append(
            f"hook opener is {len(first_sents[0].split())} words — punchy hooks need ≤{MAX_OPENER_WORDS}"
        )

    # Hook pacing problems are worth double — a rushed hook is the worst outcome.
    all_pacing = hook_pacing_problems + pacing_problems
    if all_pacing:
        audio_deduction = min(10, len(hook_pacing_problems) * 4 + len(pacing_problems) * 2)
        score -= audio_deduction
        deductions["audio"] = audio_deduction
        issues.append(
            f"Audio delivery problems ({len(all_pacing)}): "
            + "; ".join(all_pacing[:3])
        )

    # ── 10. Visual Variety (5 pts) ────────────────────────────────────────────
    # _VISUAL_CATEGORY_RE (unlike the niche-gated regexes above) is not conditioned on
    # `_football` at all — its six categories are entirely football/sports vocabulary
    # (goal, tactic, celebrat, crowd, train, sprint...). _visual_category() falls back to
    # "other" when nothing matches, which every beat's visual_direction does for a niche
    # like personal finance or cooking — collapsing unique_cats to 1 and triggering this
    # deduction on every single non-football reel, regardless of how visually varied the
    # footage actually is. Only penalize when the shared category is a REAL one the
    # scheme recognized (a genuine repetition finding); "all beats are 'other'" means the
    # categorization scheme doesn't apply to this content at all, not that it's repetitive.
    categories = [_visual_category(b.visual_direction) for b in all_beats]
    unique_cats = len(set(categories))
    if unique_cats == 1 and categories[0] == "other":
        pass   # scheme found nothing football-specific anywhere — inapplicable, not a finding
    elif unique_cats < 2:
        score -= 5
        deductions["variety"] = 5
        issues.append(
            f"No visual variety — all beats map to '{categories[0]}'; "
            "mix in highlight, tactical, celebration, training, and crowd shots"
        )
    elif unique_cats < 3:
        score -= 2
        deductions["variety"] = 2
        issues.append(
            f"Limited visual variety ({unique_cats} types) — "
            "aim for 4+ visual categories across the reel"
        )

    # ── 11. Duration Fit (up to 5 pts per cut) ───────────────────────────────
    for cut in guide.cuts:
        actual_s = sum(b.duration_s for b in cut.beats)
        ratio = actual_s / max(cut.target_length_s, 1)
        if ratio < 0.70 or ratio > 1.30:
            dur_deduction = min(5, round(abs(1.0 - ratio) * 10))
            score -= dur_deduction
            deductions["duration"] = deductions.get("duration", 0) + dur_deduction
            issues.append(
                f"{cut.platform}: beats sum to {actual_s:.0f}s vs target {cut.target_length_s:.0f}s "
                f"({ratio:.0%}) — beat durations must sum within ±30% of target"
            )

    # ── 12. Caption & Hashtag Quality (up to 5 pts) ──────────────────────────
    for cut in guide.cuts:
        if len(cut.caption.strip()) < 30:
            score -= 2
            deductions["caption_hashtag"] = deductions.get("caption_hashtag", 0) + 2
            issues.append(
                f"{cut.platform}: caption too short ({len(cut.caption.strip())} chars) — "
                "write 1–2 punchy sentences"
            )
        if len(cut.hashtags) < 10:
            score -= 2
            deductions["caption_hashtag"] = deductions.get("caption_hashtag", 0) + 2
            issues.append(
                f"{cut.platform}: only {len(cut.hashtags)} hashtag(s) — "
                "aim for 15 (5 broad, 5 niche, 5 trending)"
            )

    # ── 13. CTA Action Quality (3 pts, quality-weighted) ─────────────────────
    if cta_beats:
        cta_vo_combined = " ".join(b.vo_script for b in cta_beats)
        if _CTA_ACTIONS_HIGH.search(cta_vo_combined):
            pass   # high-quality engagement action — full marks
        elif _CTA_ACTIONS_LOW.search(cta_vo_combined):
            score -= 1
            deductions["cta"] = 1
            issues.append(
                "Weak CTA — 'subscribe/follow' is passive; ask for a prediction or opinion: "
                "'Who wins this? Drop it below.'"
            )
        else:
            score -= 3
            deductions["cta"] = 3
            issues.append(
                "CTA has no action phrase — add 'comment your prediction', "
                "'who wins this? drop it below', 'what do you think?', etc."
            )

    # ── 14. Conversational Tone (10 pts) ─────────────────────────────────────
    # Penalise encyclopaedic phrasing; reward direct address and present-tense drama.
    article_matches = _ARTICLE_TONE.findall(all_vo)
    has_second_person = bool(_SECOND_PERSON.search(all_vo))
    has_direct_hook_opener = bool(_DIRECT_HOOK.search(first_vo))

    tone_deduction = 0
    if article_matches:
        tone_deduction += min(6, len(article_matches) * 2)
        issues.append(
            f"{len(article_matches)} encyclopaedic phrase(s) detected "
            f"({', '.join(repr(m) for m in article_matches[:2])}) — "
            "rewrite as direct speech, e.g. 'He doesn't just defend — he transforms Argentina's shape'"
        )
    if not has_second_person:
        tone_deduction += 3
        issues.append(
            "No 'you/your' in script — top reels speak directly to the viewer; "
            "add at least one 'you've never seen this' or 'think about what that means'"
        )
    if not has_direct_hook_opener and "?" not in first_vo:
        tone_deduction += 1
        issues.append(
            "Hook lacks direct address — open with 'Imagine...', 'Here's why...', or a direct question"
        )

    tone_deduction = min(10, tone_deduction)
    if tone_deduction > 0:
        score -= tone_deduction
        deductions["tone"] = tone_deduction

    # ── 15. Hook-CTA Throughline (5 pts) ─────────────────────────────────────
    # The CTA should call back to the central tension or player introduced in the hook.
    if cta_beats and hook_beats:
        cta_vo_all = " ".join(b.vo_script for b in cta_beats)
        hook_names = _person_names(first_vo)
        name_callback = any(
            n.split()[0].lower() in cta_vo_all.lower() for n in hook_names
        ) if hook_names else False
        hook_kw = _keyword_set(first_vo) - _STOPWORDS
        cta_kw  = _keyword_set(cta_vo_all) - _STOPWORDS
        kw_callback = len(hook_kw & cta_kw) >= 2

        if not name_callback and not kw_callback:
            score -= 5
            deductions["throughline"] = 5
            issues.append(
                "Throughline missing — CTA doesn't reference the hook's central tension; "
                "e.g. hook: 'Is Romero the best?' → CTA: 'So IS Romero the best? Drop your take below'"
            )

    # ── 16. Per-Beat Specificity (5 pts) ─────────────────────────────────────
    # Each body beat should make at least one falsifiable claim.
    vague_body: list[str] = []
    for beat in body_beats:
        vo = beat.vo_script
        has_stat       = bool(re.search(r'\b\d+\b', vo))
        has_causal     = bool(_INSIGHT_CAUSAL.search(vo))
        has_action     = bool(_actions_re.search(vo))
        has_comparison = bool(_INSIGHT_COMPARATIVE.search(vo))
        has_conflict   = bool(_CONFLICT_MARKERS.search(vo))   # conflict beats are inherently specific
        if not any([has_stat, has_causal, has_action, has_comparison, has_conflict]):
            vague_body.append(f"beat {beat.index}")

    if vague_body:
        vague_ratio = len(vague_body) / max(len(body_beats), 1)
        if vague_ratio >= 0.4:
            spec_deduction = min(5, round(vague_ratio * 8))
            score -= spec_deduction
            deductions["specificity"] = spec_deduction
            issues.append(
                f"{len(vague_body)} body beat(s) make only vague assertions — "
                "each beat needs a stat, causal claim, action verb, or comparison "
                f"({', '.join(vague_body[:3])})"
            )

    # ── 17. Repetition (5 pts) ────────────────────────────────────────────────
    # Body beats should introduce distinct ideas — >50% keyword overlap signals filler.
    if len(body_beats) >= 3:
        beat_kw_sets = [_keyword_set(b.vo_script) - _STOPWORDS for b in body_beats]
        total_pairs = 0
        high_overlap_pairs = 0
        for i in range(len(beat_kw_sets)):
            for j in range(i + 1, len(beat_kw_sets)):
                a, b_set = beat_kw_sets[i], beat_kw_sets[j]
                if not a or not b_set:
                    continue
                total_pairs += 1
                if len(a & b_set) / min(len(a), len(b_set)) > 0.5:
                    high_overlap_pairs += 1

        if total_pairs > 0 and high_overlap_pairs / total_pairs > 0.4:
            rep_deduction = min(5, round(high_overlap_pairs / total_pairs * 10))
            score -= rep_deduction
            deductions["repetition"] = rep_deduction
            issues.append(
                f"Repetitive vocabulary — {high_overlap_pairs}/{total_pairs} body beat pairs "
                "share >50% keywords; each beat should introduce distinct language and ideas"
            )

    # Per-axis breakdown appended as a diagnostic for the retry loop.
    if deductions:
        breakdown = "  ".join(f"{k}:−{v}" for k, v in deductions.items())
        issues.append(f"Score breakdown — {breakdown}")

    if score < 0:
        issues.append(
            f"Score overflow ({abs(score)} pts below zero) — guide fails on too many axes "
            "simultaneously; fix the highest-deduction issues above before retrying"
        )

    # Per-axis multiplier correction — see docs/evaluation.md and
    # docs/specs/2026-09-phase5-quality-engagement-feedback.md §3.6 for the derivation.
    # No-op (score unchanged) when axis_multipliers is None/empty, or for any axis name
    # not present in `deductions` (an operator typo is never a KeyError). This runs
    # AFTER the score<0 diagnostic above on purpose — that message is built from the
    # pre-correction score/deduction total and can legitimately diverge from the final
    # multiplier-corrected score when a multiplier is active; that's a cosmetic
    # inconsistency in an edge-case diagnostic, not a scoring bug.
    if axis_multipliers:
        score += sum(
            deductions[k] * (1.0 - axis_multipliers.get(k, 1.0))
            for k in deductions
        )

    return max(0, min(100, score)), issues
