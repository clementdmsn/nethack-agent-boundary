from __future__ import annotations

from observation.terrain import terrain_kind_at


FALLBACK_FEATURE_DESCRIPTIONS = {
    "+": "visible door",
    "<": "visible staircase up",
    ">": "visible staircase down",
}


def feature_positions(scene: dict[str, object]) -> set[tuple[int, int]]:
    """Return feature positions already present in a scene."""
    features = scene.get("features")
    if not isinstance(features, list):
        return set()

    return {
        tuple(entry["pos"])
        for entry in features
        if isinstance(entry, dict)
        and isinstance(entry.get("pos"), list)
        and len(entry["pos"]) == 2
    }


def fallback_feature_description(
    map_lines: list[str],
    local_x: int,
    local_y: int,
    glyph: str,
) -> str | None:
    """Classify glyph-only map facts that should become scene features."""
    terrain_kind = terrain_kind_at(map_lines, local_x, local_y)
    if terrain_kind == "open_door_candidate":
        return "visible open door"
    return FALLBACK_FEATURE_DESCRIPTIONS.get(glyph)


def scene_entries_for_contradiction(
    scene: dict[str, object],
) -> list[dict[str, object]]:
    """Collect entries that can contradict a location-specific room message."""
    scene_entries: list[dict[str, object]] = []
    for bucket in ("features", "items", "entities"):
        entries = scene.get(bucket)
        if isinstance(entries, list):
            scene_entries.extend(entry for entry in entries if isinstance(entry, dict))
    return scene_entries


def matching_here_description_entries(
    scene: dict[str, object],
    normalized_room_description: str,
) -> list[dict[str, object]]:
    """Return observed entries matching a stale 'here' room description."""
    scene_entries = scene_entries_for_contradiction(scene)
    has_stair_text = (
        "staircase" in normalized_room_description
        or "stairs" in normalized_room_description
    )
    if has_stair_text:
        return [
            entry
            for entry in scene_entries
            if "stair" in str(entry.get("description", "")).lower()
        ]
    if "door" in normalized_room_description:
        return [
            entry
            for entry in scene_entries
            if "door" in str(entry.get("description", "")).lower()
        ]
    return []
