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
    # "failed" can mean a render failed (needs a full re-render, back to "draft")
    # or a publish attempt failed (missing credentials, network error, safe_to_publish
    # gate — none of which need re-rendering, so "approved" lets the operator retry
    # publish directly). Which target applies is decided by which action the
    # operator retries (trigger_render vs trigger_publish), not tracked on the Cut.
    "failed": {"draft", "approved"},
}


# The owner state each job type is responsible for while it is in flight, as
# (owner_kind, status). Deliberately explicit rather than derived from the maps
# above: "guide_ready" and "in_review" also have a "failed" edge but are not
# in flight, so a job must never roll them back. Keys are JobType values, kept
# as plain strings so this module stays free of model imports. A task's own
# failure path and the reaper both roll back only the state their job type owns.
JOB_IN_FLIGHT: dict[str, tuple[str, str]] = {
    "enrich": ("reel", "enriching"),
    "generate": ("reel", "generating"),
    "render": ("cut", "rendering"),
    "publish": ("cut", "publishing"),
}


def transition(obj, new_status: str, transitions_map: dict[str, set[str]]) -> None:
    current = obj.status.value if hasattr(obj.status, "value") else str(obj.status)
    allowed = transitions_map.get(current, set())
    if new_status not in allowed:
        raise ValueError(f"Invalid transition: {current} → {new_status}")
    obj.status = new_status
