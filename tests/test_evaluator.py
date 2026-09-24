"""Tests for engine/generation/evaluator.py"""

from engine.generation.guide_schema import Beat, MasterGuide, PlatformGuide
from engine.generation.evaluator import score_guide, _person_names


# ── fixtures ──────────────────────────────────────────────────────────────────

def _beat(index, type="body", duration_s=8, vo="", visual="player action"):
    return Beat(
        index=index, type=type, duration_s=duration_s,
        visual_direction=visual, on_screen_text=[], vo_script=vo,
    )


def _guide(*beats) -> MasterGuide:
    return MasterGuide(
        title="Test",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45,
            caption="Test caption.",
            hashtags=["football"] * 10,
            beats=list(beats),
        )],
    )


_CTX = (
    "Argentina have a strong squad with Messi, Romero, and Alvarez. "
    "Romero presses relentlessly. Messi has 12 key passes in the tournament. "
    "Alvarez provides intensity. The left-back position is a weakness."
)


# ── hook strength ─────────────────────────────────────────────────────────────

def test_hook_strong_score_is_high():
    guide = _guide(
        _beat(0, "hook", 5, "One position could cost Argentina the World Cup?", "Argentina training"),
        _beat(1, "body", 10, "Romero's press allows Argentina to defend higher.", "Romero tackle"),
        _beat(2, "cta", 5, "Could this position cost them the World Cup? Drop your prediction.", "logo"),
    )
    score, issues = score_guide(_CTX, guide, 45)
    hook_issues = [i for i in issues if i.lower().startswith("weak hook")]
    assert not hook_issues, f"Unexpected hook issue: {hook_issues}"


def test_hook_weak_deducts_points():
    # Body and CTA are identical in both guides; only the hook differs.
    # Uses "Lionel Messi" (2-word name) so entity alignment doesn't zero-credit both guides.
    shared_body = _beat(
        1, "body", 10,
        "Lionel Messi creates 12 key passes per game, more than any other player. "
        "But the left-back is exposed and that weakness is a real danger.",
        "Lionel Messi through-ball key pass Copa America 2024",
    )
    shared_cta = _beat(
        2, "cta", 8,
        "Could this weakness cost Argentina the World Cup? Drop your prediction below.",
        "Argentina World Cup trophy celebration 2022",
    )

    guide_weak = _guide(
        _beat(0, "hook", 5, "Argentina are a good team.", "Argentina game"),
        shared_body, shared_cta,
    )
    guide_strong = _guide(
        _beat(0, "hook", 5, "Could one weakness cost Argentina the World Cup title?", "Argentina training"),
        shared_body, shared_cta,
    )
    score_weak, _ = score_guide(_CTX, guide_weak, 45)
    score_strong, _ = score_guide(_CTX, guide_strong, 45)
    assert score_strong > score_weak


def test_no_hook_beat_type_deducts():
    guide = _guide(
        _beat(0, "body", 5, "Argentina are dangerous.", "Argentina game"),
        _beat(1, "body", 10, "Messi scored 12 goals this tournament.", "Messi goal"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    score, issues = score_guide(_CTX, guide, 45)
    assert any("hook" in i.lower() for i in issues)


# ── narrative flow ────────────────────────────────────────────────────────────

def test_no_cta_deducts():
    # Both guides have the same body content; only difference is presence of a CTA beat.
    # Use a proper-length CTA VO so it doesn't trigger silent-gap audio penalties.
    shared_body = [
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 10, "Romero is strong and reliable.", "Romero tackle"),
        _beat(2, "body", 10, "Messi is the key playmaker.", "Messi dribble"),
    ]
    guide_no_cta = _guide(*shared_body)
    guide_with_cta = _guide(
        *shared_body,
        _beat(3, "cta", 8, "Like and subscribe for more football tactics and analysis.", "Argentina celebration"),
    )
    score_no_cta, _ = score_guide(_CTX, guide_no_cta, 45)
    score_with_cta, _ = score_guide(_CTX, guide_with_cta, 45)
    assert score_with_cta > score_no_cta


def test_narrative_connectors_improve_score():
    flat = _guide(
        _beat(0, "hook", 5, "Could Argentina win?", "Argentina"),
        _beat(1, "body", 8, "Romero defends.", "Romero"),
        _beat(2, "body", 8, "Messi scores.", "Messi"),
        _beat(3, "body", 8, "Alvarez presses.", "Alvarez"),
        _beat(4, "cta", 5, "Subscribe.", "logo"),
    )
    connected = _guide(
        _beat(0, "hook", 5, "Could Argentina win?", "Argentina"),
        _beat(1, "body", 8, "Romero defends, however the left-back is exposed.", "Romero tackle"),
        _beat(2, "body", 8, "Messi scores, but his workload is a concern.", "Messi goal"),
        _beat(3, "body", 8, "Alvarez presses relentlessly, which means space opens up.", "Alvarez press"),
        _beat(4, "cta", 5, "Subscribe.", "logo"),
    )
    score_flat, _ = score_guide(_CTX, flat, 45)
    score_connected, _ = score_guide(_CTX, connected, 45)
    assert score_connected > score_flat


# ── script ↔ visual alignment ─────────────────────────────────────────────────

def test_player_name_absent_from_visual_deducts():
    mismatched = _guide(
        _beat(0, "hook", 5, "Can Messi lead Argentina?", "crowd cheering stadium"),
        _beat(1, "body", 10, "Lionel Messi creates 12 key passes.", "celebration fans"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    aligned = _guide(
        _beat(0, "hook", 5, "Can Messi lead Argentina?", "Messi dribbling through press"),
        _beat(1, "body", 10, "Lionel Messi creates 12 key passes.", "Messi key pass through-ball assists"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    score_mis, _ = score_guide(_CTX, mismatched, 45)
    score_ali, _ = score_guide(_CTX, aligned, 45)
    assert score_ali > score_mis


def test_tactical_vo_with_crowd_visual_deducts():
    tactical_crowd = _guide(
        _beat(0, "hook", 5, "Could one weakness cost Argentina?", "Argentina training"),
        _beat(1, "body", 10, "The high press and defensive transition requires intensity.", "crowd cheering"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    tactical_matched = _guide(
        _beat(0, "hook", 5, "Could one weakness cost Argentina?", "Argentina training"),
        _beat(1, "body", 10, "The high press and defensive transition requires intensity.", "Argentina high press tactical shape compact"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    score_crowd, _ = score_guide(_CTX, tactical_crowd, 45)
    score_matched, _ = score_guide(_CTX, tactical_matched, 45)
    assert score_matched > score_crowd


# ── visual quality ────────────────────────────────────────────────────────────

def test_generic_visuals_deduct():
    generic = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "show football footage"),
        _beat(1, "body", 10, "Messi has 12 key passes.", "show player highlights"),
        _beat(2, "cta", 5, "Subscribe.", "show logo"),
    )
    specific = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina squad intense training session 2024"),
        _beat(1, "body", 10, "Messi has 12 key passes.", "Messi threading through-ball key pass Copa America 2024"),
        _beat(2, "cta", 5, "Subscribe.", "Messi lifting trophy celebration Argentina"),
    )
    score_gen, _ = score_guide(_CTX, generic, 45)
    score_spec, _ = score_guide(_CTX, specific, 45)
    assert score_spec > score_gen


# ── emotional impact ──────────────────────────────────────────────────────────

def test_emotional_words_improve_score():
    # Both guides have the same structure and player names so entity alignment is equal.
    # Flat contains no emotion vocabulary; emotional saturates the axis with positive and negative words.
    flat = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup? This is their situation.", "Argentina training"),
        _beat(1, "body", 10,
              "Lionel Messi allows Argentina to defend higher because his passing creates openings. "
              "Romero controls the left-back position.",
              "Lionel Messi"),
        _beat(2, "cta", 5, "Subscribe for more football content.", "logo"),
    )
    emotional = _guide(
        _beat(0, "hook", 5,
              "Could Argentina win the World Cup glory and destiny? This is their greatest challenge.",
              "Argentina training"),
        _beat(1, "body", 10,
              "Lionel Messi's brilliant relentless vision allows Argentina to defend higher because his legendary "
              "passing creates glory for the nation. Romero's courage controls the fragile left-back position.",
              "Lionel Messi"),
        _beat(2, "cta", 5, "This is triumph or heartbreak. Subscribe for more football content.", "logo"),
    )
    score_flat, flat_issues = score_guide(_CTX, flat, 45)
    score_emo, emo_issues = score_guide(_CTX, emotional, 45)
    assert score_emo > score_flat
    assert any("emotional" in i.lower() for i in flat_issues)


# ── insight density ───────────────────────────────────────────────────────────

def test_stats_and_tactics_improve_insight():
    shallow = _guide(
        _beat(0, "hook", 5, "Could Argentina win?", "Argentina training"),
        _beat(1, "body", 10, "Messi plays well for the team.", "Messi dribble"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    insightful = _guide(
        _beat(0, "hook", 5, "Could Argentina win the title?", "Argentina training"),
        _beat(1, "body", 10,
              "Messi's 12 assists allow Argentina to press higher because "
              "his passing vision unlocks the defensive transition. "
              "No team creates more chances than Argentina this tournament.",
              "Messi through-ball tactical analysis"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    score_s, shallow_issues = score_guide(_CTX, shallow, 45)
    score_i, _ = score_guide(_CTX, insightful, 45)
    assert score_i > score_s
    assert any("insight" in i.lower() for i in shallow_issues)


# ── audio pacing ──────────────────────────────────────────────────────────────

def test_overloaded_beat_deducts():
    # 40 words in 3 seconds = 13 wps (way over 3.0)
    overloaded = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup destiny glory legacy triumph?", "Argentina"),
        _beat(1, "body", 3,
              "Messi Romero Alvarez De Paul Martinez all play with incredible passion "
              "and desire every single match without exception throughout the season.",
              "Argentina"),
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    score, issues = score_guide(_CTX, overloaded, 45)
    assert any("pacing" in i.lower() or "wps" in i.lower() for i in issues)


def test_silent_gap_beat_deducts():
    underloaded = _guide(
        _beat(0, "hook", 5, "Could Argentina win?", "Argentina"),
        _beat(1, "body", 15, "Yes.", "Argentina team"),  # 1 word / 15s = 0.07 wps
        _beat(2, "cta", 5, "Subscribe.", "logo"),
    )
    score, issues = score_guide(_CTX, underloaded, 45)
    assert any("gap" in i.lower() or "pacing" in i.lower() for i in issues)


# ── overall discrimination ────────────────────────────────────────────────────

def test_weak_guide_scores_below_50():
    """A guide with generic visuals, no emotion, no insight, weak hook should fail hard."""
    guide = _guide(
        _beat(0, "body", 9, "Argentina are a team.", "show football footage"),
        _beat(1, "body", 9, "Messi is good.", "show player highlights"),
        _beat(2, "body", 9, "Romero defends.", "show player highlights"),
        _beat(3, "body", 9, "Alvarez attacks.", "show player highlights"),
        _beat(4, "body", 9, "They will play.", "show football footage"),
    )
    score, _ = score_guide(_CTX, guide, 45)
    assert score < 50


def test_e2e_fresh_guide_scores_above_90():
    """
    Fresh end-to-end test using a completely different context (Vinicius Junior /
    Real Madrid) to verify all 17 evaluator axes independently of the Argentina
    fixture used elsewhere.

    Covers every axis explicitly:
      1.  Retention: punchy opener (≤8 words) + question + 'real question' open loop +
          'world' stakes + 'you' address; punchy/expansive/standard/exclamation/question
          energies; 'However' second loop.
      2.  Narrative: hook → context (body w/ player + event) → analysis (allows/which means)
          → conflict (doubt/danger/collapse/However) → conclusion (cta beat).
      3.  Context coverage: all 5 source sentences echoed in combined VO.
      4.  Insight: 2 stats (25 goals, 18 assists), 4 causal, 4 tactical unique terms,
          2 comparative — per-beat distribution.
      5.  Visual alignment: Vinicius Junior + Ancelotti names in visuals; action + context.
      6.  Clip availability: all beats score named-player + action/context.
      7.  Visual editability: all visuals 8+ words, zero generic phrases.
      8.  Emotional impact: glory, relentless (positive) + doubt, pressure, danger, collapse
          (negative) — spread across ≥40% of beats.
      9.  Audio delivery: all beats 1.5–3.0 wps; short opener; non-monotone word counts.
      10. Visual variety: highlight / action / tactical / training / crowd / celebration.
      11. Duration fit: 6+4+10+8+7+5 = 40 s vs 45 s target (89%, within ±30%).
      12. Caption & hashtag: 95-char caption; 15 hashtags.
      13. CTA action: high-quality prediction/opinion prompt.
      14. Conversational tone: 'you've' in script; no encyclopaedic phrases; direct hook opener.
      15. Hook-CTA throughline: 'Vinicius' named in both hook and CTA.
      16. Per-beat specificity: every body beat has stat, causal, action, or conflict signal.
      17. Repetition: each body beat uses distinct vocabulary.
    """
    ctx = (
        "Vinicius Junior has transformed Real Madrid's attack with 25 goals this season. "
        "His pace and dribbling creates chaos for every defense. "
        "Carlo Ancelotti relies on Vinicius as the primary attacking threat. "
        "However critics question his consistency in big Champions League matches. "
        "The pressure of being the world's best player is mounting."
    )
    guide = MasterGuide(
        title="Vinicius Junior — World's Best or Pressure Victim?",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45,
            caption=(
                "Is Vinicius Junior the best player alive — or is the pressure of Real Madrid "
                "exposing his biggest weakness?"
            ),
            hashtags=[
                "Vinicius", "RealMadrid", "ViniciusJunior", "Football", "Soccer",
                "ChampionsLeague", "LaLiga", "FootballAnalysis", "BallonDor",
                "RealMadridFC", "UCL", "FootballTactics", "SportsAnalysis",
                "BrazilFootball", "FutbolMadrid",
            ],
            beats=[
                # Axis 1: punchy opener (8 words), question, 'real question' open loop, 'world' stakes, 'you'
                # Axis 14: 'you've' direct address; no encyclopaedic phrasing
                Beat(index=0, type="hook", duration_s=6,
                     visual_direction="Vinicius Junior dribble skill assist Real Madrid La Liga 2024",
                     on_screen_text=["Is Vinicius the best?"],
                     vo_script=(
                         "You've never seen a player like this. "
                         "Is Vinicius Junior the best in the world? "
                         "This is the real question and football's biggest debate."
                     )),
                # Axis 1: punchy energy (7 words); Axis 16: action verb (run/sprint)
                Beat(index=1, type="body", duration_s=4,
                     visual_direction="Vinicius Junior run sprint attack Real Madrid La Liga 2024",
                     on_screen_text=["He changes everything"],
                     vo_script="Vinicius sprints past defenders and creates chaos."),
                # Axis 4: stats (25 goals, 18 assists), causal (allows/which means),
                #          tactical (pressing/transition/shape); Axis 1: expansive energy (27 words)
                Beat(index=2, type="body", duration_s=10,
                     visual_direction="Vinicius Junior transition shape press Real Madrid Champions League high line",
                     on_screen_text=["25 goals this season", "Every defense crumbles"],
                     vo_script=(
                         "Vinicius Junior's pressing allows Real Madrid to attack on the transition, "
                         "creating 25 goals and 18 assists which means he overwhelms every defensive "
                         "shape across the world."
                     )),
                # Axis 4: causal (because/creates), comparative (more than); Axis 2: analysis stage
                Beat(index=3, type="body", duration_s=8,
                     visual_direction="Ancelotti Vinicius creating chance Real Madrid training session 2024",
                     on_screen_text=["Ancelotti's key weapon"],
                     vo_script=(
                         "Carlo Ancelotti relies on Vinicius because his relentless pressing intensity "
                         "creates more chances than any other player in La Liga."
                     )),
                # Axis 2: conflict; Axis 1: 'However' open loop + exclamation; Axis 8: pressure/danger/collapse/doubt
                Beat(index=4, type="body", duration_s=7,
                     visual_direction="Vinicius Junior Champions League fans stadium crowd atmosphere 2024",
                     on_screen_text=["The weakness exposed"],
                     vo_script=(
                         "However, critics question his Champions League consistency — the pressure of doubt "
                         "is real and the danger of collapse is mounting!"
                     )),
                # Axis 13: high-quality prediction prompt; Axis 15: 'Vinicius' callback to hook
                Beat(index=5, type="cta", duration_s=5,
                     visual_direction="Vinicius Junior trophy celebration title glory Real Madrid Champions League 2024",
                     on_screen_text=["Prove them wrong"],
                     vo_script=(
                         "Can Vinicius silence the critics and claim his glory? "
                         "Drop your prediction below."
                     )),
            ],
        )],
    )
    score, issues = score_guide(ctx, guide, 45)
    assert score >= 90, (
        f"Fresh e2e score {score} < 90. Issues:\n" + "\n".join(f"  - {i}" for i in issues)
    )


def test_e2e_gold_standard_scores_above_90():
    """
    End-to-end test: a hand-crafted gold-standard guide must score ≥90.
    Covers all 17 evaluator axes.
    """
    CTX = (
        "Argentina have a strong squad with Messi, Romero, and Alvarez. "
        "Romero presses relentlessly. Messi has 12 key passes in the tournament. "
        "Alvarez provides intensity. The left-back position is a weakness."
    )
    guide = MasterGuide(
        title="Argentina's World Cup Destiny",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45,
            caption=(
                "Argentina are favourites — but one fragile position could cost them "
                "the World Cup. Can Messi deliver the glory?"
            ),
            hashtags=[
                "Argentina", "WorldCup", "Messi", "Football", "Soccer",
                "LionelMessi", "ArgentinaWorldCup", "FootballAnalysis",
                "WorldCup2026", "Romero", "JulianAlvarez", "CopaAmerica",
                "FootballTactics", "SportsAnalysis", "FutbolArg",
            ],
            beats=[
                # Axis 1: punchy opener (8 words ≤ 8 → +3), question, 'real question' open loop,
                #          'world'/'risk' stakes+trigger, 'you' direct address (+1)
                # Axis 14: 'you' present; no encyclopaedic phrasing
                Beat(index=0, type="hook", duration_s=6,
                     visual_direction="Argentina squad intense training press 2024 World Cup qualifying",
                     on_screen_text=["One position could decide everything"],
                     vo_script=(
                         "You need to see this. "
                         "Could one position cost Argentina the World Cup? "
                         "This is the real question and their biggest risk."
                     )),
                # Axis 1: punchy energy (≤10 words); Axis 16: action verb (tackles) → specific
                # Axis 17: distinct vocab from beats 2-4
                Beat(index=1, type="body", duration_s=4,
                     visual_direction="Cristian Romero aggressive tackle aerial duel World Cup 2024",
                     on_screen_text=["Romero wins every duel"],
                     vo_script="Cristian Romero tackles, presses, and dominates every aerial duel."),
                # Axis 4: stats (15 yards, 90 percent), causal (allows, which means),
                #          tactical (pressing, high line); Axis 1: expansive energy (≥25 words)
                Beat(index=2, type="body", duration_s=10,
                     visual_direction="Cristian Romero high press defensive shape Argentina World Cup tactical analysis",
                     on_screen_text=["15 yards higher", "Tactical edge"],
                     vo_script=(
                         "Cristian Romero's pressing allows Argentina to defend on a high line, "
                         "15 yards higher than 90 percent of rivals, which means turnovers happen "
                         "in dangerous areas."
                     )),
                # Axis 4: stats (12 passes, 3 assists), causal (creates), comparative (more than any)
                # Axis 17: distinct vocab (Messi/passing/vision vs Romero/pressing/shape)
                Beat(index=3, type="body", duration_s=8,
                     visual_direction="Lionel Messi through-ball key pass vision Copa America 2024 dribble",
                     on_screen_text=["12 passes per game"],
                     vo_script=(
                         "Lionel Messi creates more chances than any other — 12 passes and 3 assists "
                         "per game, which means Argentina always find a way through."
                     )),
                # Axis 1: 'However' open loop + exclamation energy
                # Axis 2: conflict stage; Axis 8: negative emotion (fragile, collapse, danger)
                Beat(index=4, type="body", duration_s=8,
                     visual_direction="Argentina defensive shape left flank weakness exposed pressure danger",
                     on_screen_text=["The fragile link"],
                     vo_script=(
                         "However, the left-back position is fragile — "
                         "a collapse waiting to happen and a danger every rival will exploit!"
                     )),
                # Axis 13: high-quality prediction prompt; Axis 15: 'Argentina' + 'World Cup' callback
                # Axis 8: positive emotion (destiny, legacy, glory)
                Beat(index=5, type="cta", duration_s=5,
                     visual_direction="Messi sprinting Argentina World Cup trophy celebration title glory 2022",
                     on_screen_text=["Your prediction?"],
                     vo_script=(
                         "Can Argentina defend their World Cup destiny and claim their legacy? "
                         "Drop your prediction below."
                     )),
            ],
        )],
    )
    score, issues = score_guide(CTX, guide, 45)
    assert score >= 90, f"E2E score {score} < 90. Issues:\n" + "\n".join(f"  - {i}" for i in issues)


def test_strong_guide_scores_above_80():
    guide = _guide(
        _beat(0, "hook", 5,
              "Could one position cost Argentina the World Cup? This is their biggest risk.",
              "Argentina squad training intense press 2024"),
        _beat(1, "body", 8,
              "Cristian Romero's relentless aggression allows Argentina to defend 15 yards "
              "higher than most international teams, which means space opens behind.",
              "Cristian Romero aerial duel aggressive tackle Premier League"),
        _beat(2, "body", 8,
              "But Lionel Messi's passing vision unlocks defenses — 12 key passes in the "
              "last tournament, more than any other player.",
              "Lionel Messi through-ball key pass vision Copa America 2024"),
        _beat(3, "body", 8,
              "However Julian Alvarez's pressing intensity and desire is the engine — "
              "90 minutes of relentless hunger every single match.",
              "Julian Alvarez high press recovery run transition"),
        _beat(4, "body", 8,
              "Despite their glory, the left-back position remains fragile. "
              "A rivalry waiting to be exploited.",
              "Argentina defensive shape exposed left flank weakness"),
        _beat(5, "cta", 5,
              "Can Argentina defend their World Cup destiny? Drop your prediction below.",
              "Argentina World Cup trophy legacy Messi celebration 2022"),
    )
    score, issues = score_guide(_CTX, guide, 45)
    assert score >= 80, f"Score {score} < 80. Issues: {issues}"


# ── context coverage ─────────────────────────────────────────────────────────

def test_context_coverage_off_topic_deducts():
    """A guide that ignores the source context entirely should be penalised."""
    off_topic = _guide(
        _beat(0, "hook", 5, "Today is a great day for sports.", "generic sports"),
        _beat(1, "body", 10, "The players are ready to compete.", "players ready"),
        _beat(2, "cta", 5, "Subscribe for sports content.", "logo"),
    )
    on_topic = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 10, "Romero presses relentlessly, and Messi has 12 key passes.", "Romero Messi highlights"),
        _beat(2, "cta", 5, "Subscribe for more football.", "logo"),
    )
    score_off, issues_off = score_guide(_CTX, off_topic, 45)
    score_on, _ = score_guide(_CTX, on_topic, 45)
    assert score_on > score_off
    assert any("context" in i.lower() for i in issues_off)


def test_context_coverage_no_deduction_when_above_threshold():
    """A guide referencing ≥50% of context sentences gets no context penalty."""
    guide = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 10,
              "Romero presses relentlessly, which means Argentina win the ball high. "
              "Messi has 12 key passes in the tournament, more than any other player.",
              "Romero Messi highlights"),
        _beat(2, "cta", 5, "Subscribe for more football.", "logo"),
    )
    _, issues = score_guide(_CTX, guide, 45)
    assert not any("context coverage" in i.lower() for i in issues)


# ── duration fit ──────────────────────────────────────────────────────────────

def test_duration_mismatch_deducts():
    # beats sum to 3s but target is 45s (ratio ≈ 0.07 — far below 0.70 threshold)
    guide_short = _guide(
        _beat(0, "hook", 1, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 1,
              "Lionel Messi creates 12 key passes per game, which means Argentina threaten more than any rival.",
              "Lionel Messi through-ball Copa America 2024"),
        _beat(2, "cta", 1,
              "Can Argentina win the World Cup? Drop your prediction below.",
              "Argentina World Cup celebration 2022"),
    )
    score_short, issues_short = score_guide(_CTX, guide_short, 45)

    guide_ok = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 30,
              "Lionel Messi creates 12 key passes per game, which means Argentina threaten more than any rival.",
              "Lionel Messi through-ball Copa America 2024"),
        _beat(2, "cta", 10,
              "Can Argentina win the World Cup? Drop your prediction below.",
              "Argentina World Cup celebration 2022"),
    )
    score_ok, issues_ok = score_guide(_CTX, guide_ok, 45)

    assert score_ok > score_short
    assert any("beats sum" in i for i in issues_short)


# ── caption & hashtag quality ──────────────────────────────────────────────────

def test_short_caption_deducts():
    from engine.generation.guide_schema import MasterGuide, PlatformGuide
    guide = MasterGuide(
        title="Test",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45,
            caption="Hi.",
            hashtags=["football"] * 10,
            beats=[
                _beat(0, "hook", 5, "Could Argentina win?", "Argentina training"),
                _beat(1, "body", 30, "Messi creates 12 key passes.", "Messi highlights"),
                _beat(2, "cta", 10, "Subscribe for more.", "logo"),
            ],
        )],
    )
    _, issues = score_guide(_CTX, guide, 45)
    assert any("caption" in i.lower() for i in issues)


def test_few_hashtags_deducts():
    from engine.generation.guide_schema import MasterGuide, PlatformGuide
    guide = MasterGuide(
        title="Test",
        niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts",
            target_length_s=45,
            caption="Argentina could win the World Cup with Messi leading the charge.",
            hashtags=["football", "messi", "argentina", "worldcup", "soccer"],
            beats=[
                _beat(0, "hook", 5, "Could Argentina win?", "Argentina training"),
                _beat(1, "body", 30, "Messi creates 12 key passes.", "Messi highlights"),
                _beat(2, "cta", 10, "Subscribe for more.", "logo"),
            ],
        )],
    )
    _, issues = score_guide(_CTX, guide, 45)
    assert any("hashtag" in i.lower() for i in issues)


# ── cta action quality ─────────────────────────────────────────────────────────

def test_cta_without_action_phrase_deducts():
    guide = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 30, "Messi creates 12 key passes.", "Messi highlights"),
        _beat(2, "cta", 10, "Argentina are the greatest team.", "logo"),
    )
    guide_with_action = _guide(
        _beat(0, "hook", 5, "Could Argentina win the World Cup?", "Argentina training"),
        _beat(1, "body", 30, "Messi creates 12 key passes.", "Messi highlights"),
        _beat(2, "cta", 10, "Subscribe and drop your prediction below.", "logo"),
    )
    score_no_action, issues_no_action = score_guide(_CTX, guide, 45)
    score_with_action, _ = score_guide(_CTX, guide_with_action, 45)
    assert score_with_action > score_no_action
    assert any("cta" in i.lower() for i in issues_no_action)


# ── keyword_set helper ────────────────────────────────────────────────────────

def test_keyword_set_strips_possessives():
    """_keyword_set strips 's so 'Romero's' and 'Romero' both yield 'romero'."""
    from engine.generation.evaluator import _keyword_set
    assert "romero" in _keyword_set("Romero's press changes the game.")
    assert "romero" in _keyword_set("Romero presses relentlessly.")


def test_keyword_set_matches_possessive_vs_plain():
    """Context 'Romero presses' matches VO 'Romero's press' after possessive stripping."""
    from engine.generation.evaluator import _keyword_set
    ctx_kw = _keyword_set("Romero presses relentlessly")
    vo_kw  = _keyword_set("Romero's press changes everything")
    assert "romero" in ctx_kw & vo_kw


def test_keyword_set_excludes_short_words():
    from engine.generation.evaluator import _keyword_set
    kw = _keyword_set("It's a big win for the team today.")
    assert "it" not in kw
    assert "win" not in kw   # 3 chars, excluded
    assert "team" in kw      # 4 chars, included
    assert "today" in kw


# ── score overflow diagnostic ─────────────────────────────────────────────────

def test_score_overflow_appends_issue():
    """A guide that accumulates more than 100 pts of deductions reports a diagnostic issue."""
    # Worst possible guide: no hook beat, no names, generic 1-word visuals, no emotion,
    # no insight, no CTA action, wrong duration — designed to exceed 100 deduction pts.
    from engine.generation.guide_schema import MasterGuide, PlatformGuide
    worst = MasterGuide(
        title="Bad", niche="football",
        cuts=[PlatformGuide(
            platform="youtube_shorts", target_length_s=45,
            caption="Ok.", hashtags=["x"] * 5,
            beats=[
                Beat(index=0, type="body", duration_s=1, visual_direction="stuff",
                     on_screen_text=["ok"], vo_script="Yes."),
                Beat(index=1, type="body", duration_s=1, visual_direction="things",
                     on_screen_text=["ok"], vo_script="No."),
                Beat(index=2, type="cta",  duration_s=1, visual_direction="logo",
                     on_screen_text=["ok"], vo_script="Done."),
            ],
        )],
    )
    score, issues = score_guide(_CTX, worst, 45)
    assert score == 0
    assert any("overflow" in i.lower() for i in issues)


# ── helper unit tests ─────────────────────────────────────────────────────────

def test_person_names_extracts_correctly():
    names = _person_names("Lionel Messi and Cristian Romero play for Argentina in the World Cup.")
    assert "Lionel Messi" in names
    assert "Cristian Romero" in names


def test_person_names_excludes_non_persons():
    names = _person_names("World Cup and Premier League are tournaments. Real Madrid won.")
    assert not any(n.lower() in {"world cup", "premier league", "real madrid"} for n in names)


# ── multi-platform de-duplication ─────────────────────────────────────────────

def test_identical_platform_cuts_score_the_same_as_one():
    """Both platform guides normally hold the same beats — scoring must not count them twice.

    Duplicated beats inflate the capped per-beat axes (insight, comparatives) and
    pair every beat against its own clone in the repetition axis. Per-cut axes
    (caption, hashtags, duration fit) are deliberately scored once per platform,
    so this guide is built to pass all of them.
    """
    beats = [
        _beat(0, "hook", 5, "Could one position cost Argentina the World Cup?", "Argentina training"),
        _beat(1, "body", 10, "Romero's press allows Argentina to defend 15 yards higher.", "Romero tackle"),
        _beat(2, "body", 10, "But the left-back is exposed, which means teams attack that channel.", "Tagliafico defending"),
        _beat(3, "cta", 5, "So could that position cost them everything? Drop your prediction below.", "Argentina squad"),
    ]

    def _cut(platform):
        return PlatformGuide(
            platform=platform,
            target_length_s=30,   # beats sum to exactly 30 → duration axis clean
            caption="Argentina's one weak spot could decide the whole tournament.",
            hashtags=["football"] * 12,
            beats=list(beats),
        )

    one_cut = MasterGuide(title="Test", niche="football", cuts=[_cut("youtube_shorts")])
    two_cuts = MasterGuide(
        title="Test", niche="football",
        cuts=[_cut("youtube_shorts"), _cut("instagram_reels")],
    )

    assert score_guide(_CTX, two_cuts, 30)[0] == score_guide(_CTX, one_cut, 30)[0]
