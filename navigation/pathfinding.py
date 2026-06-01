from __future__ import annotations

import heapq

from navigation.scene_positions import (
    bucket_entries,
    bucket_positions,
    entry_position,
)
from observation.terrain import is_open_door_candidate, is_passable_terrain


class PathfindingMixin:
    DELTA_TO_ACTION = {
        (0, -1): "move(north)",
        (0, 1): "move(south)",
        (-1, 0): "move(west)",
        (1, 0): "move(east)",
        (-1, -1): "move(northwest)",
        (1, -1): "move(northeast)",
        (-1, 1): "move(southwest)",
        (1, 1): "move(southeast)",
    }
    NEIGHBOR_DELTAS = (
        (-1, -1),
        (0, -1),
        (1, -1),
        (-1, 0),
        (1, 0),
        (-1, 1),
        (0, 1),
        (1, 1),
    )

    def relative_pos_to_map_coords(
        self,
        pos: list[int] | tuple[int, int],
    ) -> tuple[int, int] | None:
        # Converts a player-relative scene position into cropped-map coordinates.
        if (
            self.last_viewport is None
            or self.last_player_screen_pos is None
            or not isinstance(pos, (list, tuple))
            or len(pos) != 2
        ):
            return None

        dx, dy = pos
        if not isinstance(dx, int) or not isinstance(dy, int):
            return None

        player_x, player_y = self.last_player_screen_pos
        return (player_x - self.last_viewport.left + dx, player_y + dy)

    def map_coords_to_relative_pos(self, x: int, y: int) -> list[int] | None:
        # Converts cropped-map coordinates back into player-relative positions.
        if self.last_viewport is None or self.last_player_screen_pos is None:
            return None

        player_x, player_y = self.last_player_screen_pos
        return [x - (player_x - self.last_viewport.left), y - player_y]

    def entity_positions(self, scene: dict[str, object]) -> set[tuple[int, int]]:
        # Collects visible occupied scene positions that should block static paths.
        return set(bucket_positions(scene, "entities"))

    def exit_positions(self, scene: dict[str, object]) -> set[tuple[int, int]]:
        # Collects inferred exit tiles from the structured scene.
        return set(bucket_positions(scene, "exits"))

    def is_traversable_scene_pos(
        self,
        scene: dict[str, object],
        pos: tuple[int, int],
        goal_pos: tuple[int, int] | None = None,
    ) -> bool:
        # Applies the minimal visible-grid traversal rules for pathfinding.
        entity_positions = self.entity_positions(scene)
        if pos == goal_pos and pos in entity_positions:
            return True

        map_coords = self.relative_pos_to_map_coords(pos)
        if map_coords is None:
            return False

        x, y = map_coords
        if is_passable_terrain(self.last_map_lines, x, y):
            pass
        elif pos in self.exit_positions(scene):
            pass
        else:
            return False

        if pos in entity_positions and pos != goal_pos:
            return False

        return True

    def scene_glyph_at_relative_pos(
        self,
        pos: list[int] | tuple[int, int],
    ) -> str:
        """Read the visible map glyph for one player-relative position."""
        map_coords = self.relative_pos_to_map_coords(pos)
        if map_coords is None:
            return " "
        return self.map_char_at(self.last_map_lines, map_coords[0], map_coords[1])

    def movement_step_is_legal(
        self,
        scene: dict[str, object],
        current: tuple[int, int],
        neighbor: tuple[int, int],
    ) -> bool:
        """Return whether NetHack accepts the one-step move between two tiles."""
        dx = neighbor[0] - current[0]
        dy = neighbor[1] - current[1]
        if abs(dx) > 1 or abs(dy) > 1 or (dx == 0 and dy == 0):
            return False
        if dx == 0 or dy == 0:
            return True

        target_coords = self.relative_pos_to_map_coords(neighbor)
        if target_coords is None:
            return False
        target_x, target_y = target_coords
        target_glyph = self.map_char_at(self.last_map_lines, target_x, target_y)
        if target_glyph == "+":
            return False
        if is_open_door_candidate(
            self.last_map_lines,
            target_x,
            target_y,
            target_glyph,
        ):
            return False
        return True

    def shortest_visible_path(
        self,
        scene: dict[str, object],
        target_pos: list[int] | tuple[int, int],
        allow_occupied_destination: bool = False,
    ) -> list[list[int]] | None:
        # Finds the shortest visible 8-way path from the player to one target.
        return self.visible_path_between(
            scene,
            (0, 0),
            target_pos,
            allow_occupied_destination=allow_occupied_destination,
        )

    def visible_path_between(
        self,
        scene: dict[str, object],
        start_pos: list[int] | tuple[int, int],
        target_pos: list[int] | tuple[int, int],
        allow_occupied_destination: bool = False,
    ) -> list[list[int]] | None:
        # Finds the shortest visible 8-way path between two relative positions.
        if self.last_viewport is None or self.last_player_screen_pos is None:
            return None
        if not isinstance(start_pos, (list, tuple)) or len(start_pos) != 2:
            return None
        if not isinstance(target_pos, (list, tuple)) or len(target_pos) != 2:
            return None
        if not all(isinstance(value, int) for value in start_pos):
            return None
        if not all(isinstance(value, int) for value in target_pos):
            return None

        goal = (target_pos[0], target_pos[1])
        goal_override = goal if allow_occupied_destination else None
        start = (start_pos[0], start_pos[1])

        if start == goal:
            return [[start[0], start[1]]]
        if start != (0, 0) and not self.is_traversable_scene_pos(
            scene,
            start,
            goal_override,
        ):
            return None
        if not self.is_traversable_scene_pos(scene, goal, goal_override):
            return None

        frontier: list[tuple[int, int, int, int, tuple[int, int]]] = [
            (0, 0, 0, 0, start)
        ]
        came_from: dict[tuple[int, int], tuple[int, int] | None] = {start: None}
        distance: dict[tuple[int, int], int] = {start: 0}
        push_order = 1

        while frontier:
            _priority_cost, _chebyshev, _manhattan, _order, current = (
                heapq.heappop(frontier)
            )
            cost = distance[current]
            if current == goal:
                break

            for dx, dy in self.NEIGHBOR_DELTAS:
                neighbor = (current[0] + dx, current[1] + dy)
                if not self.is_traversable_scene_pos(
                    scene,
                    neighbor,
                    goal_override,
                ):
                    continue
                if not self.movement_step_is_legal(scene, current, neighbor):
                    continue

                next_cost = cost + 1
                if next_cost >= distance.get(neighbor, 10**9):
                    continue

                distance[neighbor] = next_cost
                came_from[neighbor] = current
                remaining_dx = abs(goal[0] - neighbor[0])
                remaining_dy = abs(goal[1] - neighbor[1])
                heapq.heappush(
                    frontier,
                    (
                        next_cost,
                        max(remaining_dx, remaining_dy),
                        remaining_dx + remaining_dy,
                        push_order,
                        neighbor,
                    ),
                )
                push_order += 1

        if goal not in came_from:
            return None

        path: list[list[int]] = []
        current: tuple[int, int] | None = goal
        while current is not None:
            path.append([current[0], current[1]])
            current = came_from[current]
        path.reverse()
        return path

    def threat_aware_visible_path(
        self,
        scene: dict[str, object],
        target_pos: list[int] | tuple[int, int],
        *,
        allow_occupied_destination: bool = False,
        require_cardinal_target_entry: bool = False,
    ) -> list[list[int]] | None:
        # Chooses a target route whose first step maximizes immediate safety.
        hostiles = self.hostile_positions(scene)
        if not hostiles:
            return self.shortest_visible_path(
                scene,
                target_pos,
                allow_occupied_destination=allow_occupied_destination,
            )
        if not isinstance(target_pos, (list, tuple)) or len(target_pos) != 2:
            return None

        best: tuple[tuple[int, int, int, int, int, int, str], list[list[int]]] | None = None

        for _action, first_step in self.walkable_neighbor_actions(scene):
            path_from_step = self.visible_path_between(
                scene,
                first_step,
                target_pos,
                allow_occupied_destination=allow_occupied_destination,
            )
            if path_from_step is None:
                continue
            full_path = [[0, 0]] + path_from_step
            if require_cardinal_target_entry and not self.path_enters_target_cardinally(
                full_path,
            ):
                continue

            score = self.threat_aware_path_score(scene, full_path)
            if best is None or score > best[0]:
                best = (score, full_path)

        if best is None:
            return None
        return best[1]

    def threat_aware_path_score(
        self,
        scene: dict[str, object],
        path: list[list[int]],
    ) -> tuple[int, int, int, int, int, int, str]:
        """Score a route by immediate hostile safety before path length."""
        hostiles = self.hostile_positions(scene)
        if not hostiles or len(path) < 2:
            return (0, 0, 0, 0, 0, -(len(path) - 1), "")

        current_min = min(max(abs(x), abs(y)) for x, y in hostiles)
        first_step = path[1]
        destination = path[-1]
        first_min = min(
            max(abs(hostile[0] - first_step[0]), abs(hostile[1] - first_step[1]))
            for hostile in hostiles
        )
        destination_min = min(
            max(abs(hostile[0] - destination[0]), abs(hostile[1] - destination[1]))
            for hostile in hostiles
        )
        adjacent_steps = sum(
            1
            for step in path[1:]
            if any(
                max(abs(hostile[0] - step[0]), abs(hostile[1] - step[1])) <= 1
                for hostile in hostiles
            )
        )
        first_delta = self.path_first_delta(path)
        return (
            1 if first_min > current_min else 0,
            first_min,
            1 if destination_min > current_min else 0,
            destination_min,
            -adjacent_steps,
            -(len(path) - 1),
            str(self.DELTA_TO_ACTION.get(first_delta, "")),
        )

    def nearest_reachable_target_path(
        self,
        scene: dict[str, object],
        candidate_positions: list[list[int]] | list[tuple[int, int]],
        allow_occupied_destination: bool = False,
        avoid_first_step: tuple[int, int] | None = None,
        avoid_first_steps: set[tuple[int, int]] | None = None,
    ) -> tuple[list[int], list[list[int]]] | None:
        # Chooses the nearest reachable target, preferring shorter real paths.
        best: tuple[int, int, list[int], list[list[int]]] | None = None
        best_avoiding_step: tuple[int, int, list[int], list[list[int]]] | None = None
        avoided_steps = set(avoid_first_steps or set())
        if avoid_first_step is not None:
            avoided_steps.add(avoid_first_step)

        for candidate in candidate_positions:
            path = self.shortest_visible_path(
                scene,
                candidate,
                allow_occupied_destination=allow_occupied_destination,
            )
            if path is None:
                continue

            target = [candidate[0], candidate[1]]
            score = (len(path) - 1, abs(target[0]) + abs(target[1]))
            if best is None or score < (best[0], best[1]):
                best = (score[0], score[1], target, path)
            if self.path_first_delta(path) in avoided_steps:
                continue
            if best_avoiding_step is None or score < (
                best_avoiding_step[0],
                best_avoiding_step[1],
            ):
                best_avoiding_step = (score[0], score[1], target, path)

        selected = best_avoiding_step or best
        if selected is None:
            return None

        return selected[2], selected[3]

    def path_first_delta(
        self,
        path: list[list[int]] | None,
    ) -> tuple[int, int] | None:
        # Returns the first movement delta in a path, when one exists.
        if path is None or len(path) < 2:
            return None
        current = path[0]
        nxt = path[1]
        if len(current) != 2 or len(nxt) != 2:
            return None
        return (nxt[0] - current[0], nxt[1] - current[1])

    def path_to_action(self, path: list[list[int]] | None) -> str | None:
        # Converts the first path step into the existing move(...) action format.
        delta = self.path_first_delta(path)
        if delta is None:
            return None

        return self.DELTA_TO_ACTION.get(delta)

    def choose_auto_navigation_action(
        self,
        scene: dict[str, object],
    ) -> str | None:
        # Derives a visible nearest-exit move without asking the model.
        exit_positions = bucket_positions(scene, "exits")
        if not exit_positions:
            return None

        result = self.nearest_reachable_target_path(scene, exit_positions)
        if result is None:
            return None

        _target, path = result
        return self.path_to_action(path)

    def hostile_positions(
        self,
        scene: dict[str, object],
    ) -> list[tuple[int, int]]:
        # Returns visible hostile monster coordinates for simple danger logic.
        positions = []
        for entity in bucket_entries(scene, "entities"):
            description = entity.get("description")
            if not isinstance(description, str):
                continue
            if "tame " in description.lower():
                continue
            pos = entry_position(entity)
            if pos is not None:
                positions.append(pos)

        return positions

    def walkable_neighbor_actions(
        self,
        scene: dict[str, object],
    ) -> list[tuple[str, tuple[int, int]]]:
        # Enumerates one-step moves that stay within the visible traversable map.
        candidates: list[tuple[str, tuple[int, int]]] = []

        for delta in self.NEIGHBOR_DELTAS:
            action = self.DELTA_TO_ACTION.get(delta)
            if action is None:
                continue
            pos = delta
            if not self.is_traversable_scene_pos(scene, pos):
                continue
            if not self.movement_step_is_legal(scene, (0, 0), pos):
                continue
            candidates.append((action, pos))

        return candidates

    def choose_flee_action(
        self,
        scene: dict[str, object],
    ) -> str | None:
        # Picks a one-step move that increases distance from visible hostiles.
        hostiles = self.hostile_positions(scene)
        if not hostiles:
            return None

        best: tuple[int, int, int, str] | None = None
        current_min = min(
            max(abs(x), abs(y))
            for x, y in hostiles
        )

        for action, pos in self.walkable_neighbor_actions(scene):
            min_distance = min(
                max(abs(hostile[0] - pos[0]), abs(hostile[1] - pos[1]))
                for hostile in hostiles
            )
            total_distance = sum(
                abs(hostile[0] - pos[0]) + abs(hostile[1] - pos[1])
                for hostile in hostiles
            )
            score = (
                min_distance,
                total_distance,
                1 if min_distance > current_min else 0,
                action,
            )
            if best is None or score > best:
                best = score

        if best is None:
            return None

        return best[3]

    def entry_positions_for_bucket(
        self,
        scene: dict[str, object],
        bucket: str,
    ) -> list[tuple[int, int]]:
        # Extracts single and grouped relative positions from a scene bucket.
        return bucket_positions(scene, bucket)

    def traversable_relative_positions(
        self,
        scene: dict[str, object],
    ) -> list[tuple[int, int]]:
        # Lists all visible walkable coordinates known to the current map crop.
        if self.last_viewport is None or self.last_player_screen_pos is None:
            return []

        positions = []
        for y, row in enumerate(self.last_map_lines):
            if not row:
                continue
            for x, _glyph in enumerate(row):
                pos = self.map_coords_to_relative_pos(x, y)
                if pos is None:
                    continue
                candidate = (pos[0], pos[1])
                if self.is_traversable_scene_pos(scene, candidate):
                    positions.append(candidate)
        return positions

    def is_adjacent_to_unknown_space(self, x: int, y: int) -> bool:
        # Unknown adjacent space marks a frontier that may reveal more map.
        map_width = max((len(row) for row in self.last_map_lines), default=0)
        for dx, dy in ((0, -1), (-1, 0), (1, 0), (0, 1)):
            neighbor_x = x + dx
            neighbor_y = y + dy
            if (
                neighbor_x < 0
                or neighbor_y < 0
                or neighbor_y >= len(self.last_map_lines)
                or neighbor_x >= map_width
            ):
                continue
            if self.map_char_at(self.last_map_lines, neighbor_x, neighbor_y) == " ":
                return True
        return False

    def is_safe_exploration_target(
        self,
        target: tuple[int, int],
        hostiles: list[tuple[int, int]],
    ) -> bool:
        # Avoids intentionally exploring into immediate hostile adjacency.
        return all(
            max(abs(hostile[0] - target[0]), abs(hostile[1] - target[1])) > 1
            for hostile in hostiles
        )

    def exploration_frontier_positions(
        self,
        scene: dict[str, object],
    ) -> list[tuple[int, int]]:
        # Finds reachable visible tiles that can reveal currently unknown space.
        hostiles = self.hostile_positions(scene)
        candidates: set[tuple[int, int]] = set()

        for area_pos in self.entry_positions_for_bucket(scene, "areas"):
            for dx, dy in self.NEIGHBOR_DELTAS:
                neighbor = (area_pos[0] + dx, area_pos[1] + dy)
                if not self.is_traversable_scene_pos(scene, neighbor):
                    continue
                if not self.is_safe_exploration_target(neighbor, hostiles):
                    continue
                candidates.add(neighbor)

        for pos in self.traversable_relative_positions(scene):
            map_coords = self.relative_pos_to_map_coords(pos)
            if map_coords is None:
                continue
            if not self.is_adjacent_to_unknown_space(map_coords[0], map_coords[1]):
                continue
            if not self.is_safe_exploration_target(pos, hostiles):
                continue
            candidates.add(pos)

        candidates.discard((0, 0))
        return sorted(
            candidates,
            key=lambda pos: (
                max(abs(pos[0]), abs(pos[1])),
                abs(pos[0]) + abs(pos[1]),
                pos[1],
                pos[0],
            ),
        )

    def nearest_exploration_frontier_path(
        self,
        scene: dict[str, object],
    ) -> tuple[list[int], list[list[int]]] | None:
        # Computes the next dynamic exploration route using visible-grid Dijkstra.
        frontiers = self.exploration_frontier_positions(scene)
        if not frontiers:
            return None
        result = self.nearest_reachable_target_path(
            scene,
            frontiers,
            avoid_first_steps=self.exploration_avoided_first_steps(scene),
        )
        if result is None:
            return None
        _target, path = result
        blocked_steps = self.blocked_corridor_first_steps()
        if self.path_first_delta(path) in blocked_steps:
            return None
        return result
