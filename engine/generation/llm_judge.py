"""
LLM-based quality judge for generated reel guides.

Evaluates five semantic dimensions that rule-based heuristics cannot reliably measure:
  - Factual accuracy   (facts grounded in source context)
  - Expertise depth    (tactical/analytical depth beyond surface claims)
  - Natural speech     (conversational vs Wikipedia tone)
  - Hallucination risk (claims traceable to source vs invented)
  - Shareability       (bold opinions, surprising comparisons, predictions)

Called from worker/tasks/generate.py after score_guide() passes the minimum rule bar.
Falls back to a neutral score (50) on any LLM/parse error so generation never blocks.
"""

import json

from pydantic import BaseModel, Field

from engine.generation.guide_schema import MasterGuide
from engine.generation.llm import LLMProvider


class _JudgeResult(BaseModel):
    factual_accuracy: int = Field(ge=0, le=20)
    factual_accuracy_issue: str | None = None
    expertise_depth: int = Field(ge=0, le=20)
    expertise_depth_issue: str | None = None
    natural_speech: int = Field(ge=0, le=20)
    natural_speech_issue: str | None = None
    hallucination_risk: int = Field(ge=0, le=20)
    hallucination_risk_issue: str | None = None
    shareability: int = Field(ge=0, le=20)
    shareability_issue: str | None = None


_SYSTEM = """\
You are a quality judge for short-form sports video scripts.
Score the GENERATED SCRIPT against the SOURCE CONTEXT on 5 dimensions (each 0-20).
Return ONLY valid JSON — no markdown, no commentary.\
"""

_USER_TEMPLATE = """\
SOURCE CONTEXT:
{context}

GENERATED SCRIPT:
{beats}

Score each dimension 0-20.
Provide a one-line issue description only if the score is below 15 (set to null if score >= 15).

Scoring guide:

factual_accuracy (0-20)
  Does every claim match the source? No invented stats, wrong positions, fabricated records.
  0 = multiple invented facts
  10 = mostly accurate, one or two unsupported claims
  20 = fully grounded — every fact traceable to the source

expertise_depth (0-20)
  Would a knowledgeable analyst find this insightful?
  0 = generic ("Messi is great")
  10 = some depth, occasional tactical observation
  20 = explains WHY, non-obvious tactical/positional insight throughout

natural_speech (0-20)
  Does narration sound like a confident presenter or stiff Wikipedia text?
  0 = formal ("demonstrates exceptional qualities in his role")
  10 = mixed — some punchy lines, some academic phrasing
  20 = fully conversational and punchy ("Messi makes football look unfair")

hallucination_risk (0-20)
  How grounded is the script in the provided source?
  0 = fabricates most content — quotes, stats, records not in source
  10 = some claims added beyond source (may be common knowledge)
  20 = everything traceable to source — no invented content

shareability (0-20)
  Would someone forward this to a friend?
  0 = dry facts with no opinion or surprise
  10 = one engaging hook or bold claim
  20 = multiple share-worthy moments: surprising comparison, unpopular opinion, strong prediction

Return exactly this JSON:
{{
  "factual_accuracy": <0-20>,
  "factual_accuracy_issue": <"one-line issue" or null>,
  "expertise_depth": <0-20>,
  "expertise_depth_issue": <"one-line issue" or null>,
  "natural_speech": <0-20>,
  "natural_speech_issue": <"one-line issue" or null>,
  "hallucination_risk": <0-20>,
  "hallucination_risk_issue": <"one-line issue" or null>,
  "shareability": <0-20>,
  "shareability_issue": <"one-line issue" or null>
}}\
"""


def _format_beats(guide: MasterGuide) -> str:
    lines = []
    for cut in guide.cuts:
        for beat in cut.beats:
            vo = beat.vo_script[:200] if beat.vo_script else "(silent)"
            lines.append(
                f"[{beat.index}] {beat.type} ({beat.duration_s:.0f}s)\n"
                f"  VO: {vo}\n"
                f"  Visual: {beat.visual_direction[:120]}"
            )
    return "\n\n".join(lines)


def build_judge_messages(context: str, guide: MasterGuide) -> list[dict]:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER_TEMPLATE.format(
            context=context[:1500],
            beats=_format_beats(guide),
        )},
    ]


def judge_guide(
    context: str,
    guide: MasterGuide,
    provider: LLMProvider,
) -> tuple[int, list[str]]:
    """
    Run the LLM quality judge. Returns (score 0–100, list of issue strings).

    On any LLM or parse failure, returns (50, [warning message]) so generation
    continues without blocking — the rule-based score still gates quality.
    """
    messages = build_judge_messages(context, guide)
    try:
        raw = provider.complete(messages, json_mode=True)
        data = json.loads(raw)
        result = _JudgeResult.model_validate(data)
    except Exception as exc:
        return 50, [f"LLM judge unavailable (neutral score applied): {exc}"]

    score = (
        result.factual_accuracy
        + result.expertise_depth
        + result.natural_speech
        + result.hallucination_risk
        + result.shareability
    )

    issues = [
        label
        for label in [
            result.factual_accuracy_issue  and f"Factual accuracy: {result.factual_accuracy_issue}",
            result.expertise_depth_issue   and f"Expertise depth: {result.expertise_depth_issue}",
            result.natural_speech_issue    and f"Natural speech: {result.natural_speech_issue}",
            result.hallucination_risk_issue and f"Hallucination risk: {result.hallucination_risk_issue}",
            result.shareability_issue      and f"Shareability: {result.shareability_issue}",
        ]
        if label
    ]

    return max(0, min(100, score)), issues
