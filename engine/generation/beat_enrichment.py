"""Beat-level insight enrichment and conflict-beat synthesis.

Pure content generation — no Celery, no db, no Job. Both prompts carry an
explicit topic fence ("Do not introduce matches, tournaments, scorelines, or
players not mentioned...") to stop the LLM drifting into unrelated events.
"""
import json
import re as _re

from engine.generation.script_parser import BeatStub, calc_duration, derive_on_screen


_TACTICAL_MARKERS = _re.compile(
    r"\b(allows?|enables?|because|which means|forces?|creates?|"
    r"press(?:ing)?|transition|high line|formation|shape|space|channel|"
    r"recover|position|structure|movement|role|system)\b",
    _re.IGNORECASE,
)


def _is_shallow_beat(stub: BeatStub) -> bool:
    return (
        stub.beat_type == "body"
        and bool(stub.player)
        and (
            len(stub.vo_script.split()) < 25
            or not _TACTICAL_MARKERS.search(stub.vo_script)
        )
    )


_ENRICH_BATCH = 3


def _enrich_batch(batch: list[BeatStub], context: str, llm) -> dict[int, str]:
    beat_lines = "\n".join(
        f'  {{"index": {s.index}, "player": "{s.player}", "vo": "{s.vo_script[:100]}"}}'
        for s in batch
    )
    messages = [
        {"role": "system", "content":
            "You are a football tactical analyst. "
            "Return ONLY valid JSON — a list, one object per beat, "
            "each with 'index' (int) and 'tactical_sentence' (string)."},
        {"role": "user", "content": (
            f"CONTEXT:\n{context[:800]}\n\n"
            "For each beat write ONE sentence: what the player ENABLES tactically, "
            "not how they feel. Use specific mechanisms.\n\n"
            "BAD: 'Romero defends with passion.'\n"
            "GOOD: 'Romero's line-stepping lets Argentina defend 15 yards higher, "
            "creating turnovers in dangerous zones.'\n\n"
            "IMPORTANT: Only reference players, events, and facts already present in "
            "each beat. Do not introduce matches, tournaments, scorelines, or players "
            "not mentioned in the beat text.\n\n"
            f"Beats:\n[\n{beat_lines}\n]\n\n"
            'Return: [{"index": 0, "tactical_sentence": "..."}, ...]'
        )},
    ]
    try:
        raw = llm.complete(messages, json_mode=True)
        data = json.loads(raw)
        if isinstance(data, dict):
            items = [data] if "index" in data else next(
                (v for v in data.values() if isinstance(v, list)), []
            )
        else:
            items = data
        return {item["index"]: item.get("tactical_sentence", "").strip() for item in items
                if isinstance(item, dict)}
    except Exception:
        return {}


def _enrich_with_insight(stubs: list[BeatStub], context: str, llm) -> None:
    shallow = [s for s in stubs if _is_shallow_beat(s)]
    if not shallow:
        return
    stub_map = {s.index: s for s in shallow}
    for i in range(0, len(shallow), _ENRICH_BATCH):
        batch = shallow[i:i + _ENRICH_BATCH]
        sentences = _enrich_batch(batch, context, llm)
        for idx, sentence in sentences.items():
            stub = stub_map.get(idx)
            if stub and sentence and len(sentence.split()) >= 5:
                stub.vo_script = stub.vo_script.rstrip(" .") + ". " + sentence
                words = len(stub.vo_script.split())
                stub.duration_s = round(max(3.0, min(20.0, words / 2.3 + 1.0)), 1)


_CONFLICT_RE = _re.compile(
    r"\b(but|however|weakness|problem|concern|risk|challenge|"
    r"fragile|exposed|vulnerable|despite|worry|danger|question)\b",
    _re.IGNORECASE,
)


def _has_conflict_beat(stubs: list[BeatStub]) -> bool:
    return any(_CONFLICT_RE.search(s.vo_script) for s in stubs if s.beat_type == "body")


def _make_conflict_stub(context: str, llm, index: int) -> tuple[BeatStub, str] | None:
    messages = [
        {"role": "system", "content":
            "You are writing voiceover for a sports video. "
            "Return ONLY valid JSON with keys 'vo_script' and 'visual_direction'."},
        {"role": "user", "content": (
            f"CONTEXT:\n{context[:1200]}\n\n"
            "Write 2-3 sentences of voiceover identifying ONE genuine weakness, risk, or "
            "challenge for this squad. Be specific and factual. Conversational, not academic.\n\n"
            "IMPORTANT: Only reference players, events, and challenges present in the CONTEXT "
            "above. Do not introduce matches, tournaments, scorelines, or players not mentioned "
            "in the context.\n\n"
            "Also write a visual_direction (max 12 words) that an editor can use to source footage. "
            "Start with a player's full name if one is relevant.\n\n"
            '{"vo_script": "...", "visual_direction": "..."}'
        )},
    ]
    try:
        raw = llm.complete(messages, json_mode=True)
        data = json.loads(raw)
        vo = data.get("vo_script", "").strip()
        visual = data.get("visual_direction", "").strip()
        if not vo:
            return None
        stub = BeatStub(
            index=index, beat_type="body", section="CONFLICT",
            player="", vo_script=vo, duration_s=calc_duration(vo),
            on_screen_text=derive_on_screen(vo),
        )
        return stub, visual
    except Exception:
        return None
