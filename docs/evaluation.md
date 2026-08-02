# Guide Quality Evaluation

`engine/generation/evaluator.py` — `score_guide(context, guide, target_length_s) → (int, list[str])`

Called inside `worker/tasks/generate.py` after each LLM generation attempt.

**Retry behaviour (closed-loop):**
- If combined score < 80, the failure issues from both `score_guide()` and `judge_guide()` are appended as a second user message in the next attempt (via `build_messages(prior_feedback=issues)`). The LLM receives targeted fix instructions, not just the original prompt.
- Up to 3 attempts total. If none clears 80, the **best-of-3** is accepted rather than failing the job.
- Each attempt is recorded in `stage_events` (`stage="generate"`, `stage="judge"`) with `latency_ms`, `score`, and failure reasons in `detail`.

The final score is stored in `job.meta["quality_score"]` and shown in the UI. `job.error` is `None` on success.

---

## Scoring axes

Beat-level axes de-duplicate beats across `guide.cuts` by `(index, vo_script, visual_direction)`.
Both platform guides normally carry identical beats, and counting them twice inflates the capped
axes (insight density, comparatives) and pairs every beat against its own clone in the repetition
axis. Per-cut axes — duration fit, caption, hashtags — deliberately deduct once per platform,
because each platform cut is separately publishable.

Max deductions exceed 100; final score is clamped to 0–100.

| # | Axis | Max deduction | What it measures |
|---|------|--------------|-----------------|
| 1 | Retention Architecture | 20 | Hook quality (10) + open loops (5) + momentum shifts (5) |
| 2 | Narrative Quality | 15 | HOOK→CONTEXT→ANALYSIS→CONFLICT→CONCLUSION arc |
| 3 | Context Coverage | 10 | ≥50% of source context sentences echoed in combined VO |
| 4 | Insight Density | 15 | Stats + causal claims + tactics + comparisons (per-beat distribution) |
| 5 | Script ↔ Visual Alignment | 20 | VO entities/actions match `visual_direction` |
| 6 | Clip Availability | 10 | `visual_direction` describes sourceable footage |
| 7 | Visual Editability | 10 | `visual_direction` is specific enough for automation |
| 8 | Emotional Impact | 13 | Density (10) + distribution across beats (3) |
| 9 | Audio Delivery Quality | 10 | WPS range (hook stricter) + avg sentence length + punchy opener + rhythm |
| 10 | Visual Variety | 5 | Mix of highlight/tactical/celebration/crowd/training |
| 11 | Duration Fit | 5 | Beat durations sum within ±30% of cut target |
| 12 | Caption & Hashtag | 5 | Caption ≥30 chars; ≥10 hashtags |
| 13 | CTA Action | 3 | Quality-weighted: prediction/opinion > passive follow/subscribe |
| 14 | Conversational Tone | 10 | Penalise encyclopaedic phrasing; reward direct address |
| 15 | Hook-CTA Throughline | 5 | CTA references the hook's central tension or player |
| 16 | Per-Beat Specificity | 5 | Each body beat makes at least one falsifiable claim |
| 17 | Repetition | 5 | Body beats use distinct vocabulary across the script |

---

### 1. Retention Architecture — 20 pts

**A. Hook quality (0–10)**

Checks actual VO signals — the beat type is always `hook` so it is not a quality signal.

| Signal | Points |
|--------|--------|
| First sentence ≤8 words (punchy opener) | +2 |
| First sentence ≤12 words | +1 |
| Contains `?` | +3 |
| Contains conflict/trigger keyword (`risk`, `weakness`, `secret`, `nobody`…) | +2 |
| Contains stakes keyword (`world`, `cup`, `title`, `glory`, `legacy`…) | +2 |
| Contains `you` / `your` (direct address) | +1 |

`hook_sub_deduction = max(0, 10 − hook_pts)`

❌ `"Today we're looking at Cristiano Romero and why he's important to Argentina"` — long, passive, no question → 0 pts  
✅ `"Nobody talks about this. Romero might be Argentina's most important player."` — punchy opener + trigger → 4 pts

**B. Open loops (0–5)**

Phrases that create unresolved tension and pull viewers to the next beat: `however`, `but there's`, `the real question`, `what nobody talks about`…

- ≥2 beats: 5 pts  
- 1 beat: 3 pts  
- 0 beats: 0 pts

**C. Momentum shifts (0–5)**

Based on unique energy types across beats (question/exclamation/punchy/expansive/standard) and the presence of both short (≤10 words) and long (≥25 words) beats.

---

### 2. Narrative Quality — 15 pts

Checks 5 required story stages: HOOK · CONTEXT · ANALYSIS · CONFLICT · CONCLUSION.  
Each missing stage: −3 pts.

| Stage | Detection |
|-------|-----------|
| HOOK | `hook` beat exists |
| CONTEXT | a body beat names a person or references a specific event/tournament |
| ANALYSIS | a body beat contains causal language (`allows`, `because`, `which means`…) |
| CONFLICT | a body beat contains conflict language (`but`, `however`, `weakness`, `exposed`…) |
| CONCLUSION | CTA beat exists, or last 2 beats contain conclusion markers |

❌ Body beats with no player names, no causal language, no conflict → misses CONTEXT + ANALYSIS + CONFLICT  
✅ `"Romero's press allows Argentina to defend higher, however the left-back remains exposed"` → CONTEXT + ANALYSIS + CONFLICT

---

### 3. Context Coverage — 10 pts

Does the combined VO echo the source material?

For each sentence in `context` (up to 30, filtered to len > 15 chars):
- Extract meaningful keywords (words >3 chars, possessives stripped)
- A sentence is **matched** when keyword overlap with all VO ≥ 30%

```python
ratio = matched_sentences / total_context_sentences
deduction = clamp(int((0.50 - ratio) * 40), 0, 10)   # applied when ratio < 0.50
```

❌ Guide that talks about generic sport with no player/event references → −10  
✅ Guide that names the players and events from the source text → 0

---

### 4. Insight Density — 15 pts

Script should explain *why*, not just *what*. Points are awarded per beat that carries the signal, not by total occurrence count — a single dense beat cannot inflate the score.

| Signal | Points | Cap |
|--------|--------|-----|
| Beats containing a stat (`\d+ goals/assists/passes…`) | 2 pts each | max 5 |
| Body beats containing causal language (`allows`, `because`, `which means`…) | 1 pt each | max 4 |
| Unique tactical terms across all VO (`press`, `high line`, `transition`, `shape`…) | 1 pt each | max 4 |
| Beats containing a comparison (`more than`, `better than`, `no other`…) | 1 pt each | max 2 |

```
deduction = clamp(15 - insight_pts, 0, 15)
```

❌ `"Romero is a good defender."` — 0 pts → −15  
✅ `"Romero's press forces turnovers in the final third, letting Argentina defend 15 yards higher than 90% of rivals."` — stat + causal + tactical + comparison → 5+ pts

Niche routing: football/soccer/futbol uses football-specific tactical vocabulary; all other niches use universal sport patterns.

---

### 5. Script ↔ Visual Alignment — 20 pts

For each beat, does `visual_direction` support what's being said?

Three sub-scores (8 + 8 + 4 pts):
- **Entity (8):** If VO names a player, that name appears in `visual_direction` (partial credit per beat)
- **Action (8):** If VO has action verbs (`tackles`, `passes`, `scores`…), so does the visual
- **Context (4):** If VO mentions a tournament/year, the visual references the same

```
deduction = round(20 − (entity_score + action_score + context_score))
```

❌ VO: `"Messi's vision unlocks defenses"` / Visual: `"Argentina team celebration"` → miss  
✅ VO: `"Messi's vision unlocks defenses"` / Visual: `"Messi through-ball key pass Copa 2024"` → hit

---

### 6. Clip Availability — 10 pts

Can an editor or stock site realistically find footage for each `visual_direction`?

| Visual | Points |
|--------|--------|
| Named person + action or match context | 3 pts |
| Named person OR (action + ≥5 words) | 2 pts |
| ≥4 words, no abstract language | 1 pt |
| Abstract language (`"embodies greatness"`, `"spirit of the game"`) | 0 pts |

```
deduction = clamp(round((0.55 − clip_ratio) * 22), 0, 10)   # when ratio < 0.55
```

---

### 7. Visual Editability — 10 pts

Is each `visual_direction` specific enough that automation knows what to fetch?

- ≥8 words, no generic phrases (`"show footage"`, `"player highlights"`): 2 pts/beat
- ≥5 words, no generic phrases: 1 pt/beat
- Generic or <4 words: 0 pts

```
deduction = clamp(round((0.5 − edit_ratio) * 20), 0, 10)
```

---

### 8. Emotional Impact — 13 pts

**Density (0–10):** Does the script contain emotional vocabulary across both polarities?

| Condition | Deduction |
|-----------|-----------|
| < 2 emotion words total | up to −10 |
| < 4 emotion words total | −4 |
| ≥ 4 words but only positive OR only negative | −2 |
| ≥ 4 words, both polarities | 0 |

Positive set: `glory`, `redemption`, `greatness`, `legacy`, `triumph`, `iconic`, `legendary`, `courage`, `passion`, `pride`, `brilliant`, `fearless`, `ruthless`, `masterclass`, `genius`, `incredible`…  
Negative set: `collapse`, `failure`, `pressure`, `weakness`, `heartbreak`, `defeat`, `exposed`, `fragile`, `crisis`, `threat`, `doubt`, `chaos`, `disaster`, `brutal`…

**Distribution (0–3):** If the guide has ≥4 emotion words but fewer than 40% of beats contain at least one, −3 pts. Emotion should be woven throughout, not dumped in the hook and CTA.

---

### 9. Audio Delivery Quality — 10 pts

Six signals checked; each pacing violation deducts points depending on severity (capped at −10 total):

| Condition | Deduction | Reason |
|-----------|-----------|--------|
| Hook beat WPS > 3.0 | −4 pts each | A rushed hook is the worst first impression |
| Body/CTA beat WPS > 4.0 | −2 pts each | VO will be cut off by clip boundary |
| Beat WPS < 0.8 and duration ≥ 4 s | −2 pts | Silent gap feels unfinished |
| All beats within ±25% of mean word count | −2 pts | Monotone rhythm; no pacing variety |
| Avg sentence length > 18 words | −2 pts | Hard for TTS to deliver naturally |
| > 35% of sentences exceed 22 words | −2 pts | Too many long sentences |
| Hook's first sentence > 16 words | −1 pt (Axis 1A) | Opener not punchy |

Hook violations are counted separately and cost 4 pts each; regular pacing violations cost 2 pts each. Total deduction = `min(10, hook_violations × 4 + regular_violations × 2)`.

Target: ~3 words/sec for hook beats, ~3.5 words/sec for body beats. Hook opener ≤12 words for full points. Prompt instructs LLM with explicit per-beat word budgets (e.g. 8s beat → ~24 words; hook beat → aim for ≤3.0 wps).

---

### 10. Visual Variety — 5 pts

Categories: `highlight`, `tactical`, `celebration`, `crowd`, `training`, `action`, `other`

| Unique categories | Deduction |
|-------------------|-----------|
| ≥3 | 0 |
| 2 | −2 |
| 1 | −5 |

---

### 11. Duration Fit — 5 pts (per cut)

```
ratio = sum(beat.duration_s) / cut.target_length_s
deduction = clamp(round(abs(1.0 − ratio) * 10), 0, 5)   # when ratio < 0.70 or > 1.30
```

---

### 12. Caption & Hashtag Quality — 5 pts (per cut)

- Caption shorter than 30 chars: −2 pts
- Fewer than 10 hashtags (LLM prompt asks for 15): −2 pts

---

### 13. CTA Action Quality — 3 pts (quality-weighted)

Quality gradient — not all CTAs are equal:

| CTA type | Deduction |
|----------|-----------|
| High-quality engagement (`comment`, `predict`, `drop`, `who do you think`, `your prediction`…) | 0 |
| Passive ask (`subscribe`, `follow`, `like`, `smash`, `hit`…) | −1 |
| No action phrase at all | −3 |

❌ `"Subscribe for more content"` — passive → −1  
✅ `"Who wins this? Drop your prediction below."` — opinion prompt → 0

---

### 14. Conversational Tone — 10 pts

The single biggest differentiator between a robotic LLM reel and a human one.

**Encyclopaedic phrases penalised (−2 pts each, max −6):**

`who plays for`, `born in`, `is a professional`, `is known for`, `according to statistics`, `it is known`, `in this video`, `we will`, `we're going to`, `let's take a look`, `let's explore`, `firstly,`, `secondly,`, `in conclusion`, `to summarize`…

**No second-person (`you`/`your`) anywhere in the script: −3 pts**

Top reels speak directly to the viewer. At minimum one "you've never seen this" or "think about what that means" should appear.

**Hook lacks direct address and no question: −1 pt**

The opener should start with `Imagine`, `Here's`, `Look`, `This is`, or a direct question.

❌ `"Cristiano Romero is an Argentine professional footballer who plays for…"` — encyclopaedic, third-person → −6  
✅ `"You've never seen a defender do this. Romero doesn't just tackle — he changes Argentina's entire shape."` — direct address, punchy → 0

---

### 15. Hook-CTA Throughline — 5 pts

The CTA should call back to the central tension introduced in the hook.

Two checks (either passes):
1. At least one player named in the hook appears in the CTA VO
2. ≥2 meaningful keywords from the hook also appear in the CTA

If neither passes: −5 pts

❌ Hook: `"Is Romero the best defender in the world?"` → CTA: `"Subscribe for more football content"` — no callback → −5  
✅ Hook: `"Is Romero the best defender in the world?"` → CTA: `"So IS Romero the best? Drop your take below."` → 0

---

### 16. Per-Beat Specificity — 5 pts

Each body beat should make at least one falsifiable claim — a stat, a causal link, an action verb, or a comparison. Beats with only vague assertions ("He is incredible", "One of the best ever") are flagged.

A beat is **vague** if it contains none of:
- A number (`\d+`)
- Causal language (`allows`, `because`, `which means`…)
- A sport action verb (`tackles`, `scores`, `passes`…)
- A comparison (`better than`, `more than`, `no other`…)

If ≥40% of body beats are vague:
```
deduction = clamp(round(vague_ratio * 8), 0, 5)
```

❌ `"He is one of the most incredible players of this generation."` — vague → flagged  
✅ `"His press forces the opposition to turn over the ball in the final third, which means Argentina can attack from a higher starting position."` — causal + action → not flagged

---

### 17. Repetition — 5 pts

Body beats should each introduce new language and ideas. Beats that recycle the same keywords signal filler or circular logic.

Computed: for every pair of body beats, keyword overlap ratio = `shared keywords / min(keywords_a, keywords_b)`. Common stopwords and sport-generic words are excluded.

If >40% of pairs share >50% keywords:
```
deduction = clamp(round(overlap_ratio * 10), 0, 5)
```

❌ Beats 2, 3, 4 all saying "Romero is aggressive, Romero wins headers, Romero is physical" — same keyword cluster → flagged  
✅ Beat 2: pressing + defensive line; Beat 3: left-back vulnerability + counterattack risk; Beat 4: 2022 WC stats + comparison — distinct ideas → 0

---

## Combined score

```python
combined = int(rule_score * 0.4 + llm_score * 0.6)
```

The `judge_guide()` LLM judge (5 semantic dimensions × 20 pts) is called when `rule_score ≥ 55`.

| Combined score | Outcome |
|----------------|---------|
| ≥ threshold | Guide accepted, saved to DB |
| < threshold | Retry with prior issues as feedback (up to 3 attempts) |
| < threshold after 3 attempts | Best-of-3 accepted; score written to `job.meta["quality_score"]` |

Threshold is **80** when `USE_NVIDIA_FOR_GENERATION=true`, **65** for local Ollama (`QUALITY_THRESHOLD_LOCAL`). Feedback passed to retries strips `"Score breakdown — ..."` lines — the LLM doesn't understand internal axis notation.

---

## What this evaluator does NOT check

These are covered by `judge_guide()` (LLM semantic judge):

- **Factual accuracy** — are the stats real? Does Romero actually play for Argentina?
- **Hallucination risk** — does the script invent events not in the source?
- **Expertise depth** — are the tactical insights non-obvious, or just generic claims?
- **Natural speech quality** — does it *sound* like a human commentator, beyond what keyword signals can detect?
- **Shareability** — bold opinions, surprising comparisons, predictions that provoke reaction

The rule scorer catches structural and mechanical failures. The LLM judge catches semantic quality failures. Both are required.

---

## Tuning guide

| You want to… | Change |
|---|---|
| Accept lower-quality guides (local models) | `QUALITY_THRESHOLD_LOCAL` in `generate.py` (currently 65) |
| Accept lower-quality guides (NVIDIA) | `QUALITY_THRESHOLD` in `generate.py` (currently 80) |
| Require stronger hooks | Raise hook signal thresholds in Axis 1A |
| Require more context coverage | Lower the 30% keyword overlap threshold in Axis 3 |
| Relax visual alignment checks | Increase `_GENERIC_VISUAL` / `_ABSTRACT_VISUAL` patterns |
| Require more tactical language | Raise tactical insight baseline above 2 in Axis 4 |
| Allow faster speech (body beats) | Raise `MAX_WPS` from 4.0 → 4.5 |
| Allow faster hook delivery | Raise `MAX_WPS_HOOK` from 3.0 → 3.5 |
| Allow longer sentences | Raise `MAX_AVG_SENTENCE_WORDS` from 18 → 22 |
| Loosen opener requirement | Raise `MAX_OPENER_WORDS` from 16 → 20 |
| Add more emotion words | Extend `_EMOTION_POSITIVE` / `_EMOTION_NEGATIVE` sets |
| Add encyclopaedic patterns to catch | Extend `_ARTICLE_TONE` regex |
| Penalise more repetition | Lower the 0.5 overlap threshold in Axis 17 |

All constants are at the top of `evaluator.py`.
