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
# as plain strings so this module stays free of model imports.
JOB_IN_FLIGHT: dict[str, tuple[str, str]] = {
    "enrich": ("reel", "enriching"),
    "generate": ("reel", "generating"),
    "render": ("cut", "rendering"),
    "publish": ("cut", "publishing"),
}

# Every in-flight status per owner kind — what the reaper rolls back, since a
# stalled job of any type may own either kind.
IN_FLIGHT_STATES: dict[str, set[str]] = {}
for _kind, _status in JOB_IN_FLIGHT.values():
    IN_FLIGHT_STATES.setdefault(_kind, set()).add(_status)
del _kind, _status


def transition(obj, new_status: str, transitions_map: dict[str, set[str]]) -> None:
    current = obj.status.value if hasattr(obj.status, "value") else str(obj.status)
    allowed = transitions_map.get(current, set())
    if new_status not in allowed:
        raise ValueError(f"Invalid transition: {current} → {new_status}")
    obj.status = new_status
