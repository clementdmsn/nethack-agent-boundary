from __future__ import annotations

from dataclasses import dataclass


CORRIDOR_GLYPH = "#"
DOOR_GLYPH = "+"
ROOM_ENTRY_GLYPHS = frozenset({".", "·"})
CARDINAL_DELTAS = frozenset({(0, -1), (0, 1), (-1, 0), (1, 0)})
COMPONENT_DELTAS = frozenset({(0, -1), (0, 1), (-1, 0), (1, 0)})


@dataclass(frozen=True)
class CorridorDecision:
    """Describe the next deterministic corridor-following step."""

    status: str
    delta: tuple[int, int] | None = None
    reason: str = ""


def reverse_delta(delta: tuple[int, int]) -> tuple[int, int]:
    """Return the opposite movement vector."""
    return (-delta[0], -delta[1])


def corridor_continuations(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
) -> list[tuple[int, int]]:
    """List safe neighboring corridor deltas excluding the tile just left."""
    continuations: list[tuple[int, int]] = []
    blocked = reverse_delta(previous_delta)
    allies = runner.ally_positions(scene)

    for delta in runner.NEIGHBOR_DELTAS:
        if delta == blocked:
            continue
        if delta in allies:
            continue
        if not runner.is_traversable_scene_pos(scene, delta):
            continue
        if runner.glyph_for_relative_pos(delta) != CORRIDOR_GLYPH:
            continue
        continuations.append(delta)

    return canonical_corridor_deltas(
        runner=runner,
        candidates=continuations,
        preferred_delta=previous_delta,
    )


def ally_blocks_forward(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
) -> bool:
    """Return whether an allied pet is occupying the intended corridor route."""
    allies = runner.ally_positions(scene)
    if previous_delta in allies:
        return True
    return any(
        max(abs(pos[0]), abs(pos[1])) <= 1
        for pos in allies
    )


def ally_blocking_corridor_step(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
) -> tuple[int, int] | None:
    """Return the allied-pet tile that should be retried as corridor movement."""
    blocked = reverse_delta(previous_delta)
    allies = {
        pos
        for pos in runner.ally_positions(scene)
        if max(abs(pos[0]), abs(pos[1])) == 1 and pos != blocked
    }
    if previous_delta in allies:
        return previous_delta
    if len(allies) == 1:
        return next(iter(allies))
    return None


def room_floor_continuation(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
) -> tuple[int, int] | None:
    """Pick an adjacent room floor tile when a corridor opens into a room."""
    blocked = reverse_delta(previous_delta)
    candidates: list[tuple[tuple[int, int, int, int], tuple[int, int]]] = []
    for delta in runner.NEIGHBOR_DELTAS:
        if delta == blocked:
            continue
        if not runner.is_traversable_scene_pos(scene, delta):
            continue
        if runner.glyph_for_relative_pos(delta) not in ROOM_ENTRY_GLYPHS:
            continue
        dot = delta[0] * previous_delta[0] + delta[1] * previous_delta[1]
        candidates.append(
            (
                (
                    dot,
                    1 if delta == previous_delta else 0,
                    1 if delta in CARDINAL_DELTAS else 0,
                    -max(abs(delta[0]), abs(delta[1])),
                ),
                delta,
            )
        )

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def visible_corridor_in_forward_cone(
    *,
    runner,
    previous_delta: tuple[int, int],
    depth: int = 6,
) -> bool:
    """Return whether corridor glyphs are still visible beyond local topology."""
    dx, dy = previous_delta
    side_steps = (0,)
    if dx == 0:
        side_steps = (-1, 0, 1)
        positions = [
            (side, dy * distance)
            for distance in range(2, depth + 1)
            for side in side_steps
        ]
    elif dy == 0:
        side_steps = (-1, 0, 1)
        positions = [
            (dx * distance, side)
            for distance in range(2, depth + 1)
            for side in side_steps
        ]
    else:
        positions = [
            (dx * distance, dy * distance)
            for distance in range(2, depth + 1)
        ]
    return any(
        runner.glyph_for_relative_pos(pos) == CORRIDOR_GLYPH
        for pos in positions
    )


def raw_screen_rows(runner) -> list[str]:
    """Return the latest full terminal rows used for corridor safety checks."""
    screen = getattr(runner, "screen", "")
    if not isinstance(screen, str):
        screen = ""
    render_cell_rows = getattr(runner, "render_cell_rows", None)
    if render_cell_rows is not None:
        return render_cell_rows(screen)
    return [line.rstrip() for line in screen.splitlines()]


def raw_player_position_in_screen(runner) -> tuple[int, int] | None:
    """Return the visible player position in full terminal coordinates."""
    rows = raw_screen_rows(runner)
    positions: list[tuple[int, int]] = []
    for y, row in enumerate(rows):
        row_has_status = getattr(runner, "row_has_status_text", lambda _row: False)
        row_is_prompt = getattr(runner, "row_is_prompt_text", lambda _row: False)
        if row_has_status(row) or row_is_prompt(row):
            continue
        for x, glyph in enumerate(row):
            if glyph == "@":
                positions.append((x, y))
    if len(positions) == 1:
        return positions[0]

    player_pos = getattr(runner, "last_player_screen_pos", None)
    if isinstance(player_pos, tuple) and len(player_pos) == 2:
        return player_pos
    return None


def raw_screen_char_at(rows: list[str], x: int, y: int) -> str:
    """Safely read one glyph from full terminal rows."""
    if y < 0 or y >= len(rows):
        return " "
    row = rows[y]
    if x < 0 or x >= len(row):
        return " "
    return row[x]


def raw_screen_neighbor_glyphs(
    *,
    runner,
    previous_delta: tuple[int, int],
) -> dict[tuple[int, int], str]:
    """Read the 8-neighbor topology around @ from the full terminal screen."""
    player = raw_player_position_in_screen(runner)
    if player is None:
        return {}
    rows = raw_screen_rows(runner)
    player_x, player_y = player
    blocked = reverse_delta(previous_delta)
    neighbors: dict[tuple[int, int], str] = {}
    for delta in runner.NEIGHBOR_DELTAS:
        if delta == blocked:
            continue
        neighbors[delta] = raw_screen_char_at(
            rows,
            player_x + delta[0],
            player_y + delta[1],
        )
    return neighbors


def raw_screen_forward_cone_glyphs(
    *,
    runner,
    previous_delta: tuple[int, int],
    depth: int = 6,
) -> list[tuple[tuple[int, int], str]]:
    """Read a small forward cone from @ in full terminal coordinates."""
    player = raw_player_position_in_screen(runner)
    if player is None:
        return []
    rows = raw_screen_rows(runner)
    player_x, player_y = player
    dx, dy = previous_delta
    if dx == 0:
        positions = [
            (side, dy * distance)
            for distance in range(2, depth + 1)
            for side in (-1, 0, 1)
        ]
    elif dy == 0:
        positions = [
            (dx * distance, side)
            for distance in range(2, depth + 1)
            for side in (-1, 0, 1)
        ]
    else:
        positions = [
            (dx * distance, dy * distance)
            for distance in range(2, depth + 1)
        ]
    return [
        (
            pos,
            raw_screen_char_at(rows, player_x + pos[0], player_y + pos[1]),
        )
        for pos in positions
    ]


def visible_player_position_in_map_lines(runner) -> tuple[int, int] | None:
    """Return the visible local @ position in the cropped map rows."""
    positions: list[tuple[int, int]] = []
    for y, row in enumerate(runner.last_map_lines):
        for x, glyph in enumerate(row):
            if glyph == "@":
                positions.append((x, y))
    if len(positions) == 1:
        return positions[0]

    player_pos = runner.last_player_screen_pos
    viewport = runner.last_viewport
    if (
        isinstance(player_pos, tuple)
        and len(player_pos) == 2
        and viewport is not None
    ):
        return (player_pos[0] - viewport.left, player_pos[1])
    return None


def raw_adjacent_corridor_glyphs(
    *,
    runner,
    previous_delta: tuple[int, int],
) -> list[tuple[int, int]]:
    """Read neighboring # glyphs directly around @ on the full terminal screen."""
    neighbors = raw_screen_neighbor_glyphs(
        runner=runner,
        previous_delta=previous_delta,
    )
    return [
        delta
        for delta, glyph in neighbors.items()
        if glyph == CORRIDOR_GLYPH
    ]


def raw_adjacent_room_entry_glyphs(
    *,
    runner,
    previous_delta: tuple[int, int],
) -> list[tuple[int, int]]:
    """Read neighboring room-entry glyphs around @ on the full terminal screen."""
    neighbors = raw_screen_neighbor_glyphs(
        runner=runner,
        previous_delta=previous_delta,
    )
    return [
        delta
        for delta, glyph in neighbors.items()
        if glyph in ROOM_ENTRY_GLYPHS
    ]


def adjacent_closed_door_glyphs(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
) -> list[tuple[int, int]]:
    """Return adjacent cardinal closed doors that terminate a corridor."""
    blocked = reverse_delta(previous_delta)
    allies = runner.ally_positions(scene)
    candidates = []
    for delta in CARDINAL_DELTAS:
        if delta == blocked:
            continue
        if delta in allies:
            continue
        if runner.glyph_for_relative_pos(delta) == DOOR_GLYPH:
            candidates.append(delta)
    if candidates:
        return candidates

    neighbors = raw_screen_neighbor_glyphs(
        runner=runner,
        previous_delta=previous_delta,
    )
    return [
        delta
        for delta, glyph in neighbors.items()
        if delta in CARDINAL_DELTAS and glyph == DOOR_GLYPH and delta not in allies
    ]


def preferred_closed_door_delta(
    *,
    candidates: list[tuple[int, int]],
    previous_delta: tuple[int, int],
) -> tuple[int, int] | None:
    """Pick the closed door most consistent with the corridor travel direction."""
    if not candidates:
        return None
    scored = []
    for delta in candidates:
        dot = delta[0] * previous_delta[0] + delta[1] * previous_delta[1]
        scored.append(((dot, 1 if delta == previous_delta else 0), delta))
    return max(scored, key=lambda item: item[0])[1]


def preferred_room_entry_delta(
    *,
    candidates: list[tuple[int, int]],
    previous_delta: tuple[int, int],
) -> tuple[int, int] | None:
    """Pick the room-entry tile most aligned with corridor travel."""
    if not candidates:
        return None
    scored = []
    for delta in candidates:
        dot = delta[0] * previous_delta[0] + delta[1] * previous_delta[1]
        scored.append(
            (
                (
                    dot,
                    1 if delta == previous_delta else 0,
                    1 if delta in CARDINAL_DELTAS else 0,
                    -max(abs(delta[0]), abs(delta[1])),
                ),
                delta,
            )
        )
    return max(scored, key=lambda item: item[0])[1]


def raw_visible_continuation_in_forward_cone(
    *,
    runner,
    previous_delta: tuple[int, int],
) -> bool:
    """Return whether full terminal rows show corridor/room ahead of @."""
    return any(
        glyph == CORRIDOR_GLYPH or glyph in ROOM_ENTRY_GLYPHS
        for _pos, glyph in raw_screen_forward_cone_glyphs(
            runner=runner,
            previous_delta=previous_delta,
        )
    )


def corridor_component_from(
    *,
    runner,
    start: tuple[int, int],
) -> set[tuple[int, int]]:
    """Return the visible 4-connected corridor component from one local tile."""
    if runner.map_char_at(runner.last_map_lines, start[0], start[1]) != CORRIDOR_GLYPH:
        return set()

    visited = {start}
    queue = [start]
    while queue:
        x, y = queue.pop(0)
        for dx, dy in COMPONENT_DELTAS:
            neighbor = (x + dx, y + dy)
            if neighbor in visited:
                continue
            if (
                runner.map_char_at(
                    runner.last_map_lines,
                    neighbor[0],
                    neighbor[1],
                )
                != CORRIDOR_GLYPH
            ):
                continue
            visited.add(neighbor)
            queue.append(neighbor)
    return visited


def preferred_component_delta(
    *,
    runner,
    candidates: list[tuple[int, int]],
    preferred_delta: tuple[int, int] | None,
) -> tuple[int, int]:
    """Pick one representative from equivalent corridor-entry candidates."""
    order = {
        delta: index
        for index, delta in enumerate(getattr(runner, "DIRECTION_NAMES", {}))
    }
    return min(
        candidates,
        key=lambda delta: (
            0 if preferred_delta is not None and delta == preferred_delta else 1,
            0 if delta in CARDINAL_DELTAS else 1,
            order.get(delta, 99),
        ),
    )


def canonical_corridor_deltas(
    *,
    runner,
    candidates: list[tuple[int, int]],
    preferred_delta: tuple[int, int] | None = None,
) -> list[tuple[int, int]]:
    """Collapse candidate steps that enter the same corridor component."""
    if len(candidates) < 2:
        return candidates

    player = visible_player_position_in_map_lines(runner)
    if player is None:
        return candidates

    player_x, player_y = player
    candidate_positions = {
        delta: (player_x + delta[0], player_y + delta[1])
        for delta in candidates
    }
    unassigned = list(candidates)
    representatives: list[tuple[int, int]] = []

    while unassigned:
        seed = unassigned.pop(0)
        component = corridor_component_from(
            runner=runner,
            start=candidate_positions[seed],
        )
        if not component:
            representatives.append(seed)
            continue

        group = [seed]
        remaining = []
        for delta in unassigned:
            if candidate_positions[delta] in component:
                group.append(delta)
            else:
                remaining.append(delta)
        unassigned = remaining
        representatives.append(
            preferred_component_delta(
                runner=runner,
                candidates=group,
                preferred_delta=preferred_delta,
            )
        )

    representative_set = set(representatives)
    return [delta for delta in candidates if delta in representative_set]


def corridor_neighbor_count(
    *,
    runner,
    scene: dict[str, object],
    pos: tuple[int, int],
) -> int:
    """Count corridor neighbors connected to one relative corridor position."""
    allies = runner.ally_positions(scene)
    count = 0

    for delta in runner.NEIGHBOR_DELTAS:
        neighbor = (pos[0] + delta[0], pos[1] + delta[1])
        if neighbor == (0, 0):
            count += 1
            continue
        if neighbor in allies:
            continue
        if not runner.is_traversable_scene_pos(scene, neighbor):
            continue
        if runner.glyph_for_relative_pos(neighbor) != CORRIDOR_GLYPH:
            continue
        count += 1

    return count


def preferred_corridor_continuation(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
    continuations: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """Pick a clear corridor bend/continuation from multiple local candidates."""
    if not continuations:
        return None

    if previous_delta not in continuations and previous_delta not in CARDINAL_DELTAS:
        forward = [
            delta
            for delta in continuations
            if delta[0] * previous_delta[0] + delta[1] * previous_delta[1] > 0
        ]
        if len(forward) == 1:
            return forward[0]

    if previous_delta in continuations and len(continuations) > 1:
        side_candidates = [delta for delta in continuations if delta != previous_delta]
        if all(delta not in CARDINAL_DELTAS for delta in side_candidates):
            return previous_delta
        return None

    # Prefer a candidate that does not immediately fan out. At real
    # intersections, more than one candidate tends to have onward corridor
    # neighbors; at bends, one candidate is usually the only narrow continuation.
    narrow = [
        delta
        for delta in continuations
        if corridor_neighbor_count(runner=runner, scene=scene, pos=delta) <= 2
    ]
    if len(narrow) == 1:
        return narrow[0]

    return None


def backward_diagonal_artifacts(
    previous_delta: tuple[int, int],
) -> set[tuple[int, int]]:
    """Return diagonal contacts that point behind cardinal corridor travel."""
    dx, dy = previous_delta
    if dx == 1 and dy == 0:
        return {(-1, -1), (-1, 1)}
    if dx == -1 and dy == 0:
        return {(1, -1), (1, 1)}
    if dx == 0 and dy == -1:
        return {(-1, 1), (1, 1)}
    if dx == 0 and dy == 1:
        return {(-1, -1), (1, -1)}
    return set()


def filter_backward_diagonal_artifacts(
    *,
    candidates: list[tuple[int, int]],
    previous_delta: tuple[int, int],
) -> list[tuple[int, int]]:
    """Suppress backward diagonal corridor contacts when another route remains."""
    artifacts = backward_diagonal_artifacts(previous_delta)
    if not artifacts:
        return candidates
    filtered = [delta for delta in candidates if delta not in artifacts]
    return filtered if filtered else candidates


def corridor_follow_decision(
    *,
    runner,
    scene: dict[str, object],
    previous_delta: tuple[int, int],
) -> CorridorDecision:
    """Choose the next corridor step or classify why following must stop."""
    if runner.hostile_positions(scene):
        return CorridorDecision(
            status="corridor_follow_monster_seen",
            reason="hostile appeared",
        )
    context = scene.get("location_context")
    if isinstance(context, dict) and context.get("in_room") is True:
        return CorridorDecision(
            status="corridor_follow_room_entrance",
            reason="room entrance reached",
        )

    continuations = corridor_continuations(
        runner=runner,
        scene=scene,
        previous_delta=previous_delta,
    )
    continuations = filter_backward_diagonal_artifacts(
        candidates=continuations,
        previous_delta=previous_delta,
    )
    if not continuations:
        ally_delta = ally_blocking_corridor_step(
            runner=runner,
            scene=scene,
            previous_delta=previous_delta,
        )
        if ally_delta is not None:
            return CorridorDecision(
                status="continue",
                delta=ally_delta,
                reason="allied pet blocks corridor route",
            )
        room_delta = room_floor_continuation(
            runner=runner,
            scene=scene,
            previous_delta=previous_delta,
        )
        if room_delta is not None:
            return CorridorDecision(
                status="continue",
                delta=room_delta,
                reason="room floor entrance",
            )
        raw_room_entries = raw_adjacent_room_entry_glyphs(
            runner=runner,
            previous_delta=previous_delta,
        )
        raw_room_delta = preferred_room_entry_delta(
            candidates=raw_room_entries,
            previous_delta=previous_delta,
        )
        if raw_room_delta is not None:
            return CorridorDecision(
                status="continue",
                delta=raw_room_delta,
                reason="raw adjacent room entrance",
            )
        raw_continuations = raw_adjacent_corridor_glyphs(
            runner=runner,
            previous_delta=previous_delta,
        )
        raw_continuations = canonical_corridor_deltas(
            runner=runner,
            candidates=raw_continuations,
            preferred_delta=previous_delta,
        )
        if len(raw_continuations) == 1:
            return CorridorDecision(
                status="continue",
                delta=raw_continuations[0],
                reason="raw adjacent corridor glyph",
            )
        if len(raw_continuations) > 1:
            return CorridorDecision(
                status="corridor_follow_intersection",
                reason="multiple raw adjacent corridor glyphs",
            )
        if raw_visible_continuation_in_forward_cone(
            runner=runner,
            previous_delta=previous_delta,
        ):
            return CorridorDecision(
                status="corridor_follow_lost_topology",
                reason="raw screen shows continuation beyond crop",
            )
        if visible_corridor_in_forward_cone(
            runner=runner,
            previous_delta=previous_delta,
        ):
            return CorridorDecision(
                status="corridor_follow_lost_topology",
                reason="corridor visible beyond missing local continuation",
            )
        door_delta = preferred_closed_door_delta(
            candidates=adjacent_closed_door_glyphs(
                runner=runner,
                scene=scene,
                previous_delta=previous_delta,
            ),
            previous_delta=previous_delta,
        )
        if door_delta is not None:
            return CorridorDecision(
                status="corridor_follow_closed_door",
                delta=door_delta,
                reason="closed door at corridor end",
            )
        return CorridorDecision(
            status="corridor_follow_end",
            reason="no corridor continuation",
        )
    if len(continuations) > 1:
        preferred = preferred_corridor_continuation(
            runner=runner,
            scene=scene,
            previous_delta=previous_delta,
            continuations=continuations,
        )
        if preferred is not None:
            return CorridorDecision(
                status="continue",
                delta=preferred,
                reason="clear corridor continuation through bend",
            )
        return CorridorDecision(
            status="corridor_follow_intersection",
            reason="multiple corridor continuations",
        )

    return CorridorDecision(
        status="continue",
        delta=continuations[0],
        reason="single safe corridor continuation",
    )
