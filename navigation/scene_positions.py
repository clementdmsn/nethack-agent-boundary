from __future__ import annotations


def compact_position_tuple(value: object) -> tuple[int, int] | None:
    """Return a normalized two-int position tuple when value is a scene pos."""
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, int) for item in value)
    ):
        return (value[0], value[1])
    return None


def bucket_entries(scene: dict[str, object], bucket: str) -> list[dict[str, object]]:
    """Return dictionary entries from a scene bucket."""
    entries = scene.get(bucket)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def entry_position(entry: dict[str, object]) -> tuple[int, int] | None:
    """Return a normalized tuple for one entry's primary position."""
    return compact_position_tuple(entry.get("pos"))


def entry_positions(entry: dict[str, object]) -> list[tuple[int, int]]:
    """Return primary and grouped positions for one scene entry."""
    pos = entry_position(entry)
    if pos is not None:
        return [pos]

    grouped = entry.get("positions")
    if not isinstance(grouped, list):
        return []

    positions = []
    for item in grouped:
        item_pos = compact_position_tuple(item)
        if item_pos is not None:
            positions.append(item_pos)
    return positions


def bucket_positions(scene: dict[str, object], bucket: str) -> list[tuple[int, int]]:
    """Return every normalized position from a scene bucket."""
    positions: list[tuple[int, int]] = []
    for entry in bucket_entries(scene, bucket):
        positions.extend(entry_positions(entry))
    return positions
