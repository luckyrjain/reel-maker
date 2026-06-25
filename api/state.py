REEL_TRANSITIONS: dict[str, set[str]] = {
    "draft":      {"generating", "enriching"},
    "enriching":  {"generating", "failed"},
    "generating": {"guide_ready", "failed"},
    "guide_ready": {"failed"},
    "failed":     {"draft"},
}

CUT_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"rendering"},
    "rendering": {"in_review", "failed"},
    "in_review": {"rendering", "approved"},
    "approved": {"publishing", "scheduled"},
    "scheduled": {"publishing"},
    "publishing": {"published", "failed"},
    "failed": {"draft"},
}


def transition(obj, new_status: str, transitions_map: dict[str, set[str]]) -> None:
    current = obj.status.value if hasattr(obj.status, "value") else str(obj.status)
    allowed = transitions_map.get(current, set())
    if new_status not in allowed:
        raise ValueError(f"Invalid transition: {current} → {new_status}")
    obj.status = new_status
