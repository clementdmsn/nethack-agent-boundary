from __future__ import annotations

from observation.constants import (
    FEATURE_GLYPHS,
    FLOOR_GLYPHS,
    ITEM_GLYPHS,
    MONSTER_GLYPHS,
    PLAYER_GLYPHS,
    WALL_GLYPHS,
)


HORIZONTAL_WALL_GLYPHS = {"-", "─"}
VERTICAL_WALL_GLYPHS = {"|", "│"}
PASSAGE_GLYPHS = FLOOR_GLYPHS | PLAYER_GLYPHS | ITEM_GLYPHS | MONSTER_GLYPHS | {"#"}


def map_char_at(map_lines: list[str], x: int, y: int) -> str:
    """Read one map glyph safely from a cropped map row set."""
    if y < 0 or y >= len(map_lines):
        return " "
    line = map_lines[y]
    if x < 0 or x >= len(line):
        return " "
    return line[x]


def is_open_door_candidate(
    map_lines: list[str],
    x: int,
    y: int,
    glyph: str | None = None,
) -> bool:
    """Return whether a wall-looking glyph has open-door topology."""
    glyph = map_char_at(map_lines, x, y) if glyph is None else glyph
    left = map_char_at(map_lines, x - 1, y)
    right = map_char_at(map_lines, x + 1, y)
    up = map_char_at(map_lines, x, y - 1)
    down = map_char_at(map_lines, x, y + 1)

    if glyph in HORIZONTAL_WALL_GLYPHS:
        return left in PASSAGE_GLYPHS and right in PASSAGE_GLYPHS and (
            up in WALL_GLYPHS or down in WALL_GLYPHS
        )

    if glyph in VERTICAL_WALL_GLYPHS:
        return up in PASSAGE_GLYPHS and down in PASSAGE_GLYPHS and (
            left in WALL_GLYPHS or right in WALL_GLYPHS
        )

    return False


def terrain_kind_at(map_lines: list[str], x: int, y: int) -> str:
    """Classify one visible map tile into semantic terrain."""
    glyph = map_char_at(map_lines, x, y)
    if is_open_door_candidate(map_lines, x, y, glyph):
        return "open_door_candidate"
    if glyph in FLOOR_GLYPHS:
        return "floor"
    if glyph in PLAYER_GLYPHS:
        return "player"
    if glyph in FEATURE_GLYPHS:
        return "feature"
    if glyph in WALL_GLYPHS:
        return "wall"
    if glyph in ITEM_GLYPHS:
        return "item"
    if glyph in MONSTER_GLYPHS:
        return "entity"
    if glyph == " ":
        return "void"
    return "unknown"


def is_passable_terrain(map_lines: list[str], x: int, y: int) -> bool:
    """Return whether visible terrain can be used by pathfinding."""
    return terrain_kind_at(map_lines, x, y) in {
        "floor",
        "item",
        "player",
        "feature",
        "open_door_candidate",
    }
