from __future__ import annotations

from dataclasses import dataclass

from observation.constants import (
    FEATURE_GLYPHS,
    FLOOR_GLYPHS,
    ITEM_GLYPHS,
    LOOK_DESCRIPTION_PATTERN,
    MONSTER_GLYPHS,
    PLAYER_GLYPHS,
    PLAYER_HERE_PATTERN,
    PROMPT_PREFIXES,
    SKIP_GLYPHS,
    STATUS_PATTERN,
    WALL_GLYPHS,
)
from observation.terrain import is_open_door_candidate


@dataclass(frozen=True)
class ObservationTarget:
    screen_x: int
    screen_y: int
    dx: int
    dy: int
    glyph: str
    priority: int


@dataclass(frozen=True)
class MapViewport:
    top: int
    bottom: int
    left: int
    right: int
    overlay_rows: frozenset[int]


class ViewportObservationMixin:
    COMPONENT_GLYPHS = (
        FLOOR_GLYPHS
        | FEATURE_GLYPHS
        | ITEM_GLYPHS
        | MONSTER_GLYPHS
        | PLAYER_GLYPHS
    )

    def is_map_glyph(self, glyph: str) -> bool:
        # Checks whether a character can plausibly belong to the visible map.
        return glyph in (
            FLOOR_GLYPHS
            | WALL_GLYPHS
            | FEATURE_GLYPHS
            | ITEM_GLYPHS
            | MONSTER_GLYPHS
            | PLAYER_GLYPHS
        )

    def render_cell_rows(self, screen: str) -> list[str]:
        # Converts the terminal cell buffer into plain-text screen rows.
        render_cells = getattr(self.terminal, "render_cells", None)
        if render_cells is None:
            return [line.rstrip() for line in screen.splitlines()]

        return [
            "".join(cell.char for cell in cell_row).rstrip()
            for cell_row in render_cells()
        ]

    def row_has_status_text(self, line: str) -> bool:
        # Detects bottom status rows in the rendered screen.
        stripped = line.strip()
        return bool(stripped) and (
            STATUS_PATTERN.search(stripped) is not None or "NetHack-" in stripped
        )

    def row_is_prompt_text(self, line: str) -> bool:
        # Detects human-language prompt lines that should not be treated as map rows.
        stripped = line.strip()
        if not stripped:
            return False
        if stripped.startswith(PROMPT_PREFIXES):
            return True
        return (
            PLAYER_HERE_PATTERN.search(stripped) is not None
            or LOOK_DESCRIPTION_PATTERN.search(stripped) is not None
        )

    def find_status_top(self, rows: list[str]) -> int:
        # Finds the first row where the status area begins.
        for index, row in enumerate(rows):
            if self.row_has_status_text(row):
                return index
        return len(rows)

    def row_map_anchor_positions(self, line: str) -> list[int]:
        # Returns the column positions of structural map glyphs in one row.
        anchors = (
            FLOOR_GLYPHS | WALL_GLYPHS | FEATURE_GLYPHS | ITEM_GLYPHS | PLAYER_GLYPHS
        )
        return [index for index, char in enumerate(line) if char in anchors]

    def row_sparse_monster_anchor_positions(self, line: str) -> list[int]:
        """Return monster-only anchors for sparse attached corridor rows."""
        stripped = line.strip()
        if not stripped:
            return []
        if len(stripped) > 3:
            return []
        if any(char not in MONSTER_GLYPHS for char in stripped):
            return []
        return [index for index, char in enumerate(line) if char in MONSTER_GLYPHS]

    def row_map_or_sparse_monster_anchor_positions(self, line: str) -> list[int]:
        """Return structural anchors, or sparse monster anchors when alone."""
        anchors = self.row_map_anchor_positions(line)
        if anchors:
            return anchors
        return self.row_sparse_monster_anchor_positions(line)

    def row_primary_map_anchor_positions(self, line: str) -> list[int]:
        # Keeps the main contiguous map cluster, ignoring farlook echo fragments.
        anchors = self.row_map_anchor_positions(line)
        if not anchors:
            return []

        groups: list[list[int]] = [[anchors[0]]]
        for position in anchors[1:]:
            if position - groups[-1][-1] <= 4:
                groups[-1].append(position)
            else:
                groups.append([position])

        def group_score(group: list[int]) -> tuple[int, int, int]:
            text = "".join(line[index] for index in group)
            wall_bonus = int(any(char in WALL_GLYPHS for char in text))
            player_bonus = int("@" in text)
            return (len(group), wall_bonus, player_bonus)

        return max(groups, key=group_score)

    def row_map_strength(self, line: str) -> int:
        # Scores how strongly a row looks like map content rather than prose.
        anchors = self.row_primary_map_anchor_positions(line)
        if not anchors:
            return 0
        strength = len(anchors)
        if "@" in line:
            strength += 3
        if any(char in WALL_GLYPHS for char in line):
            strength += 2
        return strength

    def row_is_map_candidate(self, line: str) -> bool:
        # Classifies one row as a candidate member of the map viewport.
        stripped = line.strip()
        if (
            not stripped
            or self.row_has_status_text(line)
            or self.row_is_prompt_text(line)
        ):
            return False
        anchor_positions = self.row_map_anchor_positions(line)
        if len(anchor_positions) < 2 and "@" not in line:
            return False
        return self.row_map_strength(line) >= 3

    def row_extends_map_viewport(self, line: str, left: int, right: int) -> bool:
        # Keeps adjacent one-tile corridors attached to the main visible map.
        stripped = line.strip()
        if (
            not stripped
            or self.row_has_status_text(line)
            or self.row_is_prompt_text(line)
        ):
            return False
        map_anchor_glyphs = (
            FLOOR_GLYPHS | WALL_GLYPHS | FEATURE_GLYPHS | ITEM_GLYPHS | PLAYER_GLYPHS
        )
        anchors = self.row_primary_map_anchor_positions(line)
        if not anchors:
            anchors = self.row_sparse_monster_anchor_positions(line)
        if not anchors:
            return False
        if any(char != " " and char not in map_anchor_glyphs for char in line):
            if anchors != self.row_sparse_monster_anchor_positions(line):
                return False
        if not anchors:
            return False
        return any(left <= position <= right for position in anchors)

    def extend_viewport_candidate_rows(
        self,
        rows: list[str],
        candidate_rows: list[int],
        left: int,
        right: int,
    ) -> list[int]:
        # Includes weak map rows directly attached above or below a strong map block.
        included = set(candidate_rows)
        top = min(candidate_rows)
        bottom = max(candidate_rows)

        index = top - 1
        while index >= 0 and self.row_extends_map_viewport(rows[index], left, right):
            included.add(index)
            index -= 1

        index = bottom + 1
        while (
            index < len(rows)
            and self.row_extends_map_viewport(rows[index], left, right)
        ):
            included.add(index)
            index += 1

        return sorted(included)

    def find_player_map_viewport(self, rows: list[str]) -> MapViewport | None:
        # Prefer the visible map component containing the player when split
        # map islands are visible. Newly revealed rooms can otherwise win the
        # crop and hide the corridor under the player.
        status_top = self.find_status_top(rows)
        search_rows = rows[:status_top]
        player_rows = [
            index
            for index, row in enumerate(search_rows)
            if "@" in row
            and not self.row_has_status_text(row)
            and not self.row_is_prompt_text(row)
        ]
        if not player_rows:
            return None

        player_row = player_rows[0]
        anchors = self.row_primary_map_anchor_positions(search_rows[player_row])
        if not anchors:
            return None

        included = {player_row}
        left = min(anchors)
        right = max(anchors)

        changed = True
        while changed:
            changed = False
            top = min(included)
            bottom = max(included)
            for index in (top - 1, bottom + 1):
                if index < 0 or index >= len(search_rows) or index in included:
                    continue
                if not self.row_extends_map_viewport(search_rows[index], left, right):
                    continue
                included.add(index)
                row_anchors = self.row_map_or_sparse_monster_anchor_positions(
                    search_rows[index]
                )
                left = min(left, min(row_anchors))
                right = max(right, max(row_anchors))
                changed = True

        return MapViewport(
            top=min(included),
            bottom=max(included),
            left=left,
            right=right,
            overlay_rows=frozenset(),
        )

    def viewport_contains_player(
        self,
        rows: list[str],
        viewport: MapViewport,
    ) -> bool:
        # Return whether the crop selected by a viewport includes a visible @.
        for index in range(viewport.top, viewport.bottom + 1):
            if index in viewport.overlay_rows or index < 0 or index >= len(rows):
                continue
            row = rows[index]
            if "@" in row[viewport.left : viewport.right + 1]:
                return True
        return False

    def find_map_viewport(self, rows: list[str]) -> MapViewport | None:
        # Finds the strongest contiguous map block and its overlay rows.
        status_top = self.find_status_top(rows)
        search_rows = rows[:status_top]
        best_block: tuple[int, int, list[int]] | None = None
        block_start: int | None = None
        block_candidates: list[int] = []
        gap_used = False

        for index, row in enumerate(search_rows):
            is_candidate = self.row_is_map_candidate(row)

            if is_candidate:
                if block_start is None:
                    block_start = index
                block_candidates.append(index)
                gap_used = False
                continue

            if block_start is None:
                continue

            if not gap_used and block_candidates and index + 1 < len(search_rows):
                next_row = search_rows[index + 1]
                if self.row_is_map_candidate(next_row):
                    gap_used = True
                    continue

            if block_candidates:
                start = block_start
                end = index - 1
                if best_block is None or len(block_candidates) > len(best_block[2]):
                    best_block = (start, end, block_candidates[:])
            block_start = None
            block_candidates = []
            gap_used = False

        if block_start is not None and block_candidates:
            end = len(search_rows) - 1
            if best_block is None or len(block_candidates) > len(best_block[2]):
                best_block = (block_start, end, block_candidates[:])

        if best_block is None:
            return None

        _top, _bottom, candidate_rows = best_block
        left = min(
            min(self.row_primary_map_anchor_positions(search_rows[index]))
            for index in candidate_rows
        )
        right = max(
            max(self.row_primary_map_anchor_positions(search_rows[index]))
            for index in candidate_rows
        )
        included_rows = self.extend_viewport_candidate_rows(
            search_rows,
            candidate_rows,
            left,
            right,
        )
        top = min(included_rows)
        bottom = max(included_rows)
        overlay_rows = frozenset(
            index for index in range(top, bottom + 1) if index not in included_rows
        )
        viewport = MapViewport(
            top=top,
            bottom=bottom,
            left=left,
            right=right,
            overlay_rows=overlay_rows,
        )
        if self.viewport_contains_player(rows, viewport):
            return viewport
        return self.find_player_map_viewport(rows) or viewport

    def render_map_glyph_rows(
        self, screen: str
    ) -> tuple[list[str], MapViewport | None]:
        # Crops the full screen down to map rows plus viewport metadata.
        rows = self.render_cell_rows(screen)
        viewport = self.find_map_viewport(rows)
        if viewport is None:
            return ["" for _ in rows], None

        filtered_rows = []
        for index, row in enumerate(rows):
            if index < viewport.top or index > viewport.bottom:
                filtered_rows.append("")
                continue
            if index in viewport.overlay_rows:
                filtered_rows.append("")
                continue
            filtered_rows.append(row[viewport.left : viewport.right + 1].rstrip())
        return filtered_rows, viewport

    def resolve_player_screen_position(
        self,
        map_lines: list[str],
        viewport: MapViewport | None,
        cursor_position: tuple[int, int] | None,
    ) -> tuple[int, int] | None:
        """Return the screen position that should anchor player-relative topology."""
        if viewport is None:
            return cursor_position

        player_positions = []
        for y, row in enumerate(map_lines):
            for x, glyph in enumerate(row):
                if glyph in PLAYER_GLYPHS:
                    player_positions.append((viewport.left + x, y))

        if len(player_positions) == 1:
            return player_positions[0]
        return cursor_position

    def map_char_at(
        self,
        map_lines: list[str],
        x: int,
        y: int,
    ) -> str:
        # Safely reads one glyph from the cropped map grid.
        if y < 0 or y >= len(map_lines):
            return " "
        line = map_lines[y]
        if x < 0 or x >= len(line):
            return " "
        return line[x]

    def player_has_room_boundary_context(
        self,
        map_lines: list[str],
        viewport: MapViewport | None,
        player_x: int,
        player_y: int,
    ) -> bool:
        # Checks whether the player is inside/on a visible room, not in a corridor outside it.
        if viewport is None:
            return False

        local_x = player_x - viewport.left
        current_glyph = self.map_char_at(map_lines, local_x, player_y)
        if current_glyph == "#":
            return False

        room_interior_glyphs = (
            FLOOR_GLYPHS
            | (FEATURE_GLYPHS - {"#"})
            | ITEM_GLYPHS
            | MONSTER_GLYPHS
        )

        def row_has_walls_on_both_sides(y: int) -> bool:
            if y < 0 or y >= len(map_lines):
                return False
            row = map_lines[y]
            left = any(
                self.map_char_at(map_lines, x, y) in WALL_GLYPHS
                for x in range(0, max(0, local_x))
            )
            right = any(
                self.map_char_at(map_lines, x, y) in WALL_GLYPHS
                for x in range(local_x + 1, len(row))
            )
            return left and right

        def player_row_has_room_interior() -> bool:
            if player_y < 0 or player_y >= len(map_lines):
                return False
            row = map_lines[player_y]
            return any(
                self.map_char_at(map_lines, x, player_y) in room_interior_glyphs
                for x in range(0, len(row))
                if x != local_x
            )

        if row_has_walls_on_both_sides(player_y):
            return True

        has_wall_above = any(
            row_has_walls_on_both_sides(y)
            for y in range(viewport.top, player_y)
        )
        has_wall_below = any(
            row_has_walls_on_both_sides(y)
            for y in range(player_y + 1, viewport.bottom + 1)
        )
        return has_wall_above and has_wall_below and player_row_has_room_interior()

    def adjacent_map_glyphs(
        self,
        map_lines: list[str],
        viewport: MapViewport | None,
        player_x: int,
        player_y: int,
    ) -> dict[str, list[str]]:
        # Summarizes immediate corridor/door terrain around the player.
        if viewport is None:
            return {"corridors": [], "doors": []}

        local_x = player_x - viewport.left
        corridors = []
        doors = []
        for delta, direction in self.DIRECTION_NAMES.items():
            glyph = self.map_char_at(
                map_lines,
                local_x + delta[0],
                player_y + delta[1],
            )
            if glyph == "#":
                corridors.append(direction)
            elif glyph == "+":
                doors.append(direction)
        return {"corridors": corridors, "doors": doors}

    def build_location_context(
        self,
        map_lines: list[str],
        viewport: MapViewport | None,
        player_x: int,
        player_y: int,
        *,
        visibility: str,
        has_room_context: bool,
    ) -> dict[str, object]:
        # Builds explicit where-am-I context from visible map topology.
        adjacent = self.adjacent_map_glyphs(map_lines, viewport, player_x, player_y)
        if visibility == "dark":
            area_type = "dark"
        elif has_room_context:
            area_type = "room"
        elif adjacent["corridors"]:
            area_type = "corridor"
        else:
            area_type = "visible_area"

        return {
            "area_type": area_type,
            "in_corridor": area_type == "corridor",
            "in_room": area_type == "room",
            "dark": visibility == "dark",
            "adjacent_corridors": adjacent["corridors"],
            "adjacent_doors": adjacent["doors"],
            "in_front_of_door": bool(adjacent["doors"]),
        }

    def visible_component_positions(
        self,
        map_lines: list[str],
        viewport: MapViewport | None,
        player_x: int,
        player_y: int,
    ) -> set[tuple[int, int]]:
        """Return visible map positions connected to the player component."""
        if viewport is None:
            return set()
        start = (player_x - viewport.left, player_y)
        if self.map_char_at(map_lines, start[0], start[1]) not in self.COMPONENT_GLYPHS:
            return set()

        visited = {start}
        queue = [start]
        deltas = (
            (-1, -1),
            (0, -1),
            (1, -1),
            (-1, 0),
            (1, 0),
            (-1, 1),
            (0, 1),
            (1, 1),
        )
        while queue:
            x, y = queue.pop(0)
            for dx, dy in deltas:
                neighbor = (x + dx, y + dy)
                if neighbor in visited:
                    continue
                glyph = self.map_char_at(map_lines, neighbor[0], neighbor[1])
                if glyph not in self.COMPONENT_GLYPHS:
                    continue
                visited.add(neighbor)
                queue.append(neighbor)
        return visited

    def pos_touches_component(
        self,
        pos: tuple[int, int],
        component: set[tuple[int, int]],
    ) -> bool:
        """Return whether a local map position is in or next to the component."""
        if pos in component:
            return True
        x, y = pos
        return any(
            (x + dx, y + dy) in component
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if dx != 0 or dy != 0
        )

    def find_room_exits(
        self,
        map_lines: list[str],
        viewport: MapViewport | None,
        player_x: int,
        player_y: int,
    ) -> list[dict[str, object]]:
        # Detects room exits as openings in the visible room boundary.
        if viewport is None:
            return []
        if not self.player_has_room_boundary_context(
            map_lines,
            viewport,
            player_x,
            player_y,
        ):
            return []

        exits: list[dict[str, object]] = []
        seen_exits: set[tuple[str, int, int]] = set()

        width = viewport.right - viewport.left + 1
        if width <= 2:
            return []
        component = self.visible_component_positions(
            map_lines,
            viewport,
            player_x,
            player_y,
        )

        INTERIOR_GLYPHS = (
            FEATURE_GLYPHS
            | FLOOR_GLYPHS
            | PLAYER_GLYPHS
            | ITEM_GLYPHS
            | MONSTER_GLYPHS
        )
        BLOCKED_EXIT_GLYPHS = MONSTER_GLYPHS | {"+"}

        def is_player_position(local_x: int, screen_y: int) -> bool:
            return viewport.left + local_x == player_x and screen_y == player_y

        def is_interior_tile(local_x: int, screen_y: int) -> bool:
            if component and (local_x, screen_y) not in component:
                return False
            glyph = self.map_char_at(map_lines, local_x, screen_y)
            return glyph in INTERIOR_GLYPHS or is_player_position(local_x, screen_y)

        def add_exit(direction: str, local_x: int, screen_y: int) -> None:
            if component and not self.pos_touches_component(
                (local_x, screen_y),
                component,
            ):
                return
            boundary_char = self.map_char_at(map_lines, local_x, screen_y)
            if boundary_char in BLOCKED_EXIT_GLYPHS:
                return
            screen_x = viewport.left + local_x
            pos = self.compact_position(screen_x - player_x, screen_y - player_y)
            key = (direction, pos[0], pos[1])
            if key in seen_exits:
                return
            seen_exits.add(key)
            exits.append(
                {
                    "description": "exit",
                    "direction": direction,
                    "pos": pos,
                }
            )

        def scan_horizontal_edge(
            screen_y: int,
            direction: str,
            interior_y: int,
        ) -> None:
            gap_start: int | None = None

            for local_x in range(1, width - 1):
                boundary_char = self.map_char_at(map_lines, local_x, screen_y)
                is_opening = (
                    boundary_char not in WALL_GLYPHS
                    and is_interior_tile(local_x, interior_y)
                )

                if not is_opening:
                    if gap_start is not None:
                        gap_end = local_x - 1
                        add_exit(direction, (gap_start + gap_end) // 2, screen_y)
                        gap_start = None
                    continue

                if gap_start is None:
                    gap_start = local_x

            if gap_start is not None:
                gap_end = width - 2
                add_exit(direction, (gap_start + gap_end) // 2, screen_y)

        def scan_vertical_edge(
            local_x: int,
            direction: str,
            interior_x: int,
        ) -> None:
            gap_start: int | None = None

            for screen_y in range(viewport.top + 1, viewport.bottom):
                boundary_char = self.map_char_at(map_lines, local_x, screen_y)
                is_opening = (
                    boundary_char not in WALL_GLYPHS
                    and is_interior_tile(interior_x, screen_y)
                )

                if not is_opening:
                    if gap_start is not None:
                        gap_end = screen_y - 1
                        add_exit(direction, local_x, (gap_start + gap_end) // 2)
                        gap_start = None
                    continue

                if gap_start is None:
                    gap_start = screen_y

            if gap_start is not None:
                gap_end = viewport.bottom - 1
                add_exit(direction, local_x, (gap_start + gap_end) // 2)

        scan_horizontal_edge(viewport.top, "north", viewport.top + 1)
        scan_horizontal_edge(viewport.bottom, "south", viewport.bottom - 1)
        scan_vertical_edge(0, "west", 1)
        scan_vertical_edge(width - 1, "east", width - 2)

        return exits


    def wall_target_priority(
        self,
        map_lines: list[str],
        x: int,
        y: int,
        glyph: str,
    ) -> int | None:
        # Promotes wall glyphs that appear to mark doorways or passages.
        if is_open_door_candidate(map_lines, x, y, glyph):
            return 2

        return None

    def scan_observation_targets(
        self,
        screen: str,
        player_x: int,
        player_y: int,
    ) -> list[ObservationTarget]:
        # Selects visible map tiles worth inspecting with farlook.
        targets = [
            ObservationTarget(
                screen_x=player_x,
                screen_y=player_y,
                dx=0,
                dy=0,
                glyph="@",
                priority=0,
            )
        ]
        seen_positions = {(player_x, player_y)}
        map_lines, viewport = self.render_map_glyph_rows(screen)
        viewport_left = 0 if viewport is None else viewport.left
        component = self.visible_component_positions(
            map_lines,
            viewport,
            player_x,
            player_y,
        )

        for y, line in enumerate(map_lines):
            if not line:
                continue

            for local_x, glyph in enumerate(line):
                screen_x = viewport_left + local_x
                if (screen_x, y) in seen_positions:
                    continue
                local_pos = (local_x, y)
                if component and not self.pos_touches_component(local_pos, component):
                    continue
                if glyph in SKIP_GLYPHS and not is_open_door_candidate(
                    map_lines,
                    local_x,
                    y,
                    glyph,
                ):
                    continue

                priority = None
                if glyph in PLAYER_GLYPHS or glyph in MONSTER_GLYPHS:
                    priority = 1
                elif glyph in ITEM_GLYPHS or glyph in FEATURE_GLYPHS:
                    priority = 2
                elif glyph in WALL_GLYPHS:
                    priority = self.wall_target_priority(
                        map_lines, local_x, y, glyph
                    )

                if priority is None:
                    continue

                seen_positions.add((screen_x, y))
                targets.append(
                    ObservationTarget(
                        screen_x=screen_x,
                        screen_y=y,
                        dx=screen_x - player_x,
                        dy=y - player_y,
                        glyph=glyph,
                        priority=priority,
                    )
                )

        targets.sort(
            key=lambda target: (
                target.priority,
                abs(target.dy) + abs(target.dx),
                target.dy,
                target.dx,
            )
        )
        return targets
