from __future__ import annotations

from navigation.corridor_topology import canonical_corridor_deltas
from prompting.actions.factory import build_action_payload


class ExplorationActionsMixin:
    def build_feature_action(
        self,
        scene: dict[str, object],
        entry: dict[str, object],
        hostile_pressure: bool = False,
    ) -> dict[str, object] | None:
        """Create a high-level navigation action for visible doors or staircases."""
        facts = self.actionable_entry_facts(entry)
        if facts is None:
            return None

        lowered = facts.description.lower()
        if "door" in lowered:
            direction_steps = self.direction_sequence_for_pos(facts.pos)
            direction = direction_steps[0] if direction_steps else "visible"
            if hostile_pressure:
                action = (
                    self.build_flee_closed_door_action(
                        scene,
                        action_id=f"flee:{facts.target_key}",
                        label=f"Flee through {direction} door",
                        target_ref=facts.target_ref,
                        target_key=facts.target_key,
                        door_pos=facts.pos,
                        low_level_goal=f"escape pressure through {facts.description}",
                    )
                    if "open" not in lowered
                    else self.build_navigation_action(
                        scene,
                        action_id=f"flee:{facts.target_key}",
                        action_type="flee",
                        label=f"Flee through {direction} door",
                        target_ref=facts.target_ref,
                        target_key=facts.target_key,
                        target_pos=facts.pos,
                        low_level_goal=f"escape pressure through {facts.description}",
                        require_safer_destination=True,
                        require_cardinal_target_entry=True,
                    )
                )
                if action is not None:
                    action["requires_open"] = "open" not in lowered
                    return action
                if self.has_nearby_hostile_pressure(scene):
                    return None
            action_type = "explore_door"
            label = f"Explore {direction} door"
            goal = f"explore through {facts.description}"
            if "open" not in lowered:
                action = self.build_closed_door_action(
                    scene,
                    action_id=f"{action_type}:{facts.target_key}",
                    label=label,
                    target_ref=facts.target_ref,
                    target_key=facts.target_key,
                    door_pos=facts.pos,
                    low_level_goal=goal,
                )
                if action is not None:
                    action["requires_open"] = True
                return action
        elif "staircase" in lowered:
            if "up" in lowered:
                return None
            label = f"Go to {self.entry_model_name(entry)}"
            action_type = "go_to_staircase"
            goal = f"reach {facts.description}"
        else:
            return None

        action = self.build_static_action(
            scene,
            action_id=f"{action_type}:{facts.target_key}",
            action_type=action_type,
            label=label,
            target_ref=facts.target_ref,
            target_key=facts.target_key,
            target_pos=facts.pos,
            low_level_goal=goal,
            require_cardinal_target_entry="door" in lowered,
        )
        if action is not None and "door" in lowered:
            action["requires_open"] = "open" not in lowered
        return action

    def build_closed_door_action(
        self,
        scene: dict[str, object],
        *,
        action_id: str,
        label: str,
        target_ref: str,
        target_key: str,
        door_pos: list[int],
        low_level_goal: str,
    ) -> dict[str, object] | None:
        """Create a door action via a cardinal opener tile when needed."""
        door = (door_pos[0], door_pos[1])
        cardinally_adjacent = abs(door[0]) + abs(door[1]) == 1
        if cardinally_adjacent:
            return self.build_static_action(
                scene,
                action_id=action_id,
                action_type="explore_door",
                label=label,
                target_ref=target_ref,
                target_key=target_key,
                target_pos=door_pos,
                low_level_goal=low_level_goal,
                require_cardinal_target_entry=True,
            )

        opener_path = self.nearest_closed_door_opener_path(scene, door)
        if opener_path is None:
            return None
        next_action = self.path_to_action(opener_path)
        if next_action is None:
            return None
        action = build_action_payload(
            action_id=action_id,
            action_type="explore_door",
            label=label,
            target_ref=target_ref,
            target_key=target_key,
            procedure_kind="static",
            low_level_goal=low_level_goal,
            next_action=next_action,
            path_steps=self.path_step_names(opener_path),
            distance_steps=max(0, len(opener_path) - 1),
        )
        action["door_pos"] = door_pos
        action["approach_door"] = True
        return action

    def build_flee_closed_door_action(
        self,
        scene: dict[str, object],
        *,
        action_id: str,
        label: str,
        target_ref: str,
        target_key: str,
        door_pos: list[int],
        low_level_goal: str,
    ) -> dict[str, object] | None:
        """Create a flee route to a cardinal opener tile for a closed door."""
        door = (door_pos[0], door_pos[1])
        if abs(door[0]) + abs(door[1]) == 1:
            return self.build_navigation_action(
                scene,
                action_id=action_id,
                action_type="flee",
                label=label,
                target_ref=target_ref,
                target_key=target_key,
                target_pos=door_pos,
                low_level_goal=low_level_goal,
                require_safer_destination=True,
                require_cardinal_target_entry=True,
            )

        opener_path = self.nearest_closed_door_opener_path(
            scene,
            door,
            threat_aware=True,
        )
        if opener_path is None:
            return None
        if not self.path_ends_farther_from_hostiles(scene, opener_path):
            return None
        next_action = self.path_to_action(opener_path)
        if next_action is None:
            return None
        action = build_action_payload(
            action_id=action_id,
            action_type="flee",
            label=label,
            target_ref=target_ref,
            target_key=target_key,
            procedure_kind="dynamic",
            low_level_goal=low_level_goal,
            next_action=next_action,
            path_steps=self.path_step_names(opener_path),
            distance_steps=max(0, len(opener_path) - 1),
        )
        action["door_pos"] = door_pos
        action["approach_door"] = True
        action["requires_open"] = True
        return action

    def nearest_closed_door_opener_path(
        self,
        scene: dict[str, object],
        door: tuple[int, int],
        *,
        threat_aware: bool = False,
    ) -> list[list[int]] | None:
        """Find a reachable cardinal tile from which a closed door can be opened."""
        candidates = [
            (door[0] - 1, door[1]),
            (door[0] + 1, door[1]),
            (door[0], door[1] - 1),
            (door[0], door[1] + 1),
        ]
        best: tuple[tuple[object, ...], list[list[int]]] | None = None
        for candidate in candidates:
            if candidate == (0, 0):
                return [[0, 0]]
            path = (
                self.threat_aware_visible_path(scene, candidate)
                if threat_aware
                else self.shortest_visible_path(scene, candidate)
            )
            if path is None:
                continue
            score = (
                self.threat_aware_path_score(scene, path)
                if threat_aware
                else (-(len(path) - 1), -abs(candidate[0]) - abs(candidate[1]))
            )
            if best is None or score > best[0]:
                best = (score, path)
        return best[1] if best is not None else None

    def build_corridor_run_actions(
        self,
        scene: dict[str, object],
    ) -> list[dict[str, object]]:
        """Offer managed corridor-following actions when corridor tiles are adjacent."""
        actions = []
        current_glyph = self.glyph_for_relative_pos((0, 0))
        backtrack_delta = self.exploration_backtrack_delta(scene)
        avoided_steps = self.exploration_avoided_first_steps(scene)
        runtime_backtracking_steps = self.corridor_path_backtracking_steps()
        last_delta = self.last_executed_move_delta()
        candidates: list[tuple[int, int]] = []

        for delta, direction in self.DIRECTION_NAMES.items():
            if delta == backtrack_delta:
                continue
            if delta in avoided_steps:
                continue
            if self.is_blocked_corridor_entry(delta):
                continue
            if not self.is_traversable_scene_pos(scene, delta):
                continue
            if delta in self.ally_positions(scene):
                continue
            if self.corridor_step_moves_toward_hostile(scene, delta):
                continue

            target_glyph = self.glyph_for_relative_pos(delta)
            if current_glyph != "#" and target_glyph != "#":
                continue

            candidates.append(delta)

        candidates = canonical_corridor_deltas(
            runner=self,
            candidates=candidates,
            preferred_delta=last_delta,
        )
        for delta in candidates:
            direction = self.DIRECTION_NAMES[delta]
            action = build_action_payload(
                action_id=f"explore_corridor:{direction}",
                action_type="explore_corridor",
                label=f"Follow corridor {direction}",
                target_ref=None,
                target_key=f"corridor:{direction}",
                procedure_kind="dynamic",
                low_level_goal=f"follow the corridor {direction}",
                next_action=f"follow_corridor({direction})",
                path_steps=[direction],
                distance_steps=1,
            )
            if delta == last_delta:
                action["exploration_priority"] = 0
                action["continues_recent_direction"] = True
            if delta in runtime_backtracking_steps:
                action["runtime_backtracking"] = True
                action["exploration_priority"] = max(
                    int(action.get("exploration_priority", 5)),
                    40,
                )
                action["selection_priority"] = "runtime_backtracking"
                action["tactical_notes"] = [
                    "Runtime marks this corridor as backtracking over the path just followed."
                ]
            actions.append(action)

        return actions

    def corridor_step_moves_toward_hostile(
        self,
        scene: dict[str, object],
        delta: tuple[int, int],
    ) -> bool:
        """Return whether one corridor step closes distance to visible pressure."""
        hostiles = self.hostile_positions(scene)
        if not hostiles:
            return False
        current_distance = min(max(abs(x), abs(y)) for x, y in hostiles)
        if current_distance > 3:
            return False
        projected_distance = min(
            max(abs(hostile[0] - delta[0]), abs(hostile[1] - delta[1]))
            for hostile in hostiles
        )
        return projected_distance < current_distance

    def build_pending_door_step_action(
        self,
        scene: dict[str, object],
    ) -> dict[str, object] | None:
        """Continue through a door that the runtime just opened."""
        step = self.pending_open_door_step
        direction = self.pending_open_door_direction
        if not isinstance(step, str) or not isinstance(direction, str):
            return None
        delta = self.direction_delta_from_action(step)
        if delta is None:
            return None
        if delta in self.entity_positions(scene):
            return None
        action = build_action_payload(
            action_id="continue:opened_door",
            action_type="explore_door",
            label=f"Step through opened {direction} door",
            target_ref=None,
            target_key=f"door:{direction}:opened",
            procedure_kind="static",
            low_level_goal=f"move through the opened {direction} doorway",
            next_action=step,
            path_steps=[direction],
            distance_steps=1,
            completes_procedure_after_step=True,
            auto_continue=True,
        )
        action["exploration_priority"] = 0
        action["post_open_door_step"] = True
        return action

    def build_push_blocking_ally_action(
        self,
        scene: dict[str, object],
    ) -> dict[str, object] | None:
        """Keep moving forward when an allied pet blocks a corridor route."""
        allies = self.ally_positions(scene)
        if not allies:
            return None
        adjacent_allies = [
            pos for pos in sorted(allies) if self.chebyshev_distance(pos) <= 1
        ]
        if not adjacent_allies:
            return None
        blocking_allies = [
            pos for pos in adjacent_allies if self.ally_blocks_corridor_path(scene, pos)
        ]
        if not blocking_allies:
            return None
        blocking_ally = blocking_allies[0]
        move_action = self.move_action_from_delta(blocking_ally)
        direction = self.DIRECTION_NAMES.get(blocking_ally)
        if move_action is None or direction is None:
            return None
        self.blocked_ally_positions = [list(pos) for pos in blocking_allies]
        action = build_action_payload(
            action_id="push:blocked_ally",
            action_type="push_ally",
            label=f"Continue through pet {direction}",
            target_ref=None,
            target_key="ally:blocked_path",
            procedure_kind="static",
            low_level_goal=(
                f"keep moving {direction} until the allied pet swaps position"
            ),
            next_action=move_action,
            path_steps=[direction],
            distance_steps=1,
            auto_continue=True,
        )
        action["blocked_ally_positions"] = list(self.blocked_ally_positions)
        return action

    def ally_blocks_corridor_path(
        self,
        scene: dict[str, object],
        pos: tuple[int, int],
    ) -> bool:
        """Return whether an adjacent ally occupies an apparent corridor route."""
        context = scene.get("location_context")
        if isinstance(context, dict) and context.get("in_corridor") is True:
            return True
        current_glyph = self.glyph_for_relative_pos((0, 0))
        if current_glyph == "#":
            return True
        beyond = (pos[0] * 2, pos[1] * 2)
        return self.glyph_for_relative_pos(beyond) == "#"

    def build_corridor_backtrack_action(
        self,
        scene: dict[str, object],
    ) -> dict[str, object] | None:
        """Offer a deterministic reverse route after a corridor dead end."""
        if self.has_hostile_pressure(scene):
            return None
        if not self.corridor_backtrack_steps:
            return None
        action = self.corridor_backtrack_steps[0]
        return build_action_payload(
            action_id="backtrack:corridor",
            action_type="backtrack_corridor",
            label="Backtrack from corridor dead end",
            target_ref=None,
            target_key="corridor:backtrack",
            procedure_kind="static",
            low_level_goal="return from the corridor dead end to the last useful junction or room",
            next_action=f"backtrack_corridor({action})",
            path_steps=list(self.corridor_backtrack_steps),
            distance_steps=len(self.corridor_backtrack_steps),
            auto_continue=True,
        )

    def exploration_backtrack_delta(
        self,
        scene: dict[str, object],
    ) -> tuple[int, int] | None:
        """Return the immediate reverse move to avoid during safe exploration."""
        if self.has_hostile_pressure(scene):
            return None
        last_delta = self.last_executed_move_delta()
        if last_delta is None:
            return None
        return (-last_delta[0], -last_delta[1])

    def exploration_avoided_first_steps(
        self,
        scene: dict[str, object],
    ) -> set[tuple[int, int]]:
        """Return first steps that safe exploration should avoid immediately."""
        avoided = set()
        backtrack_delta = self.exploration_backtrack_delta(scene)
        if backtrack_delta is not None:
            avoided.add(backtrack_delta)
        avoided.update(self.blocked_corridor_first_steps())
        avoided.update(self.corridor_intersection_reverse_steps())
        return avoided

    def corridor_path_backtracking_steps(self) -> set[tuple[int, int]]:
        """Return local steps that move back onto the just-followed corridor path."""
        origin = self.current_screen_position_key()
        if origin is None:
            return set()
        path_positions = {
            (pos[0], pos[1])
            for pos in self.corridor_recent_path_positions
            if isinstance(pos, list)
            and len(pos) == 2
            and all(isinstance(value, int) for value in pos)
        }
        path_positions.discard(origin)
        backtracking = set()
        for delta in self.NEIGHBOR_DELTAS:
            destination = (origin[0] + delta[0], origin[1] + delta[1])
            if destination in path_positions:
                backtracking.add(delta)
        return backtracking

    def corridor_intersection_reverse_steps(self) -> set[tuple[int, int]]:
        """Return reverse steps suppressed at the current intersection."""
        avoided: set[tuple[int, int]] = set()
        origin = self.current_screen_position_key()
        if origin is None:
            return avoided
        for entry in self.corridor_intersection_avoid_steps:
            if not isinstance(entry, dict):
                continue
            raw_origin = entry.get("origin")
            raw_delta = entry.get("delta")
            if raw_origin != [origin[0], origin[1]]:
                continue
            if (
                isinstance(raw_delta, list)
                and len(raw_delta) == 2
                and all(isinstance(value, int) for value in raw_delta)
            ):
                avoided.add((raw_delta[0], raw_delta[1]))
        return avoided

    def blocked_corridor_first_steps(self) -> set[tuple[int, int]]:
        """Return local first steps known to lead into recent dead ends."""
        blocked: set[tuple[int, int]] = set()
        origin = self.current_screen_position_key()
        if origin is None:
            return blocked
        for entry in self.blocked_corridor_entries:
            if not isinstance(entry, dict):
                continue
            raw_origin = entry.get("origin")
            raw_delta = entry.get("delta")
            if raw_origin != [origin[0], origin[1]]:
                continue
            if (
                isinstance(raw_delta, list)
                and len(raw_delta) == 2
                and all(isinstance(value, int) for value in raw_delta)
            ):
                blocked.add((raw_delta[0], raw_delta[1]))
        return blocked

    def glyph_for_relative_pos(
        self,
        pos: list[int] | tuple[int, int],
    ) -> str:
        """Read the visible map glyph for one player-relative position."""
        map_coords = self.relative_pos_to_map_coords(pos)
        if map_coords is None:
            return " "
        return self.map_char_at(self.last_map_lines, map_coords[0], map_coords[1])

    def fallback_explore_action(
        self,
        scene: dict[str, object],
    ) -> dict[str, object] | None:
        """Create generic nearby exploration when no semantic target exists."""
        neighbors = self.walkable_neighbor_actions(scene)
        blocked_steps = self.exploration_avoided_first_steps(scene)
        if blocked_steps:
            neighbors = [
                (action, pos)
                for action, pos in neighbors
                if pos not in blocked_steps
            ]
        if not neighbors:
            return None

        best_action, _pos = max(
            neighbors,
            key=lambda item: self.fallback_explore_score(scene, item[0], item[1]),
        )
        return build_action_payload(
            action_id="explore_visible_area",
            action_type="explore",
            label="Explore visible area",
            target_ref=None,
            target_key="area:visible",
            procedure_kind="dynamic",
            low_level_goal="explore nearby visible tiles",
            next_action=best_action,
            path_steps=[],
            distance_steps=1,
            completes_procedure_after_step=True,
            auto_continue=False,
        )

    def fallback_explore_score(
        self,
        scene: dict[str, object],
        action: str,
        pos: tuple[int, int],
    ) -> tuple[int, int, str]:
        """Score a neighboring step by exploration value and loop avoidance."""
        score = 0
        map_coords = self.relative_pos_to_map_coords(pos)
        if map_coords is not None:
            x, y = map_coords
            glyph = self.map_char_at(self.last_map_lines, x, y)
            if glyph == "#":
                score += 35
            elif glyph == "+":
                score += 25
            elif glyph in {".", "·"}:
                score += 10
            elif glyph in {"<", ">"}:
                score -= 30

            if self.is_adjacent_to_unknown_space(x, y):
                score += 60

        last_delta = self.last_executed_move_delta()
        if last_delta is not None:
            dot = pos[0] * last_delta[0] + pos[1] * last_delta[1]
            if pos == last_delta:
                score += 55
            elif dot > 0:
                score += 30
            elif dot < 0:
                score -= 45

        recent_deltas = self.recent_executed_move_deltas(limit=6)
        for index, delta in enumerate(recent_deltas):
            reverse = (-delta[0], -delta[1])
            if pos == reverse:
                score -= max(20, 85 - index * 10)
            elif pos == delta and index > 0:
                score += max(5, 25 - index * 5)

        if pos in self.ally_positions(scene):
            score -= 80

        return (score, -max(abs(pos[0]), abs(pos[1])), action)

    def current_screen_position_key(self) -> tuple[int, int] | None:
        """Return the current player screen coordinate used for short memory."""
        pos = self.last_player_screen_pos
        if not isinstance(pos, tuple) or len(pos) != 2:
            return None
        x, y = pos
        if not isinstance(x, int) or not isinstance(y, int):
            return None
        return (x, y)

    def is_blocked_corridor_entry(self, delta: tuple[int, int]) -> bool:
        """Return whether this local corridor direction just led to a dead end."""
        origin = self.current_screen_position_key()
        if origin is None:
            return False
        for entry in self.blocked_corridor_entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("origin") != [origin[0], origin[1]]:
                continue
            if entry.get("delta") == [delta[0], delta[1]]:
                return True
        return False

    def recent_executed_move_deltas(self, limit: int = 5) -> list[tuple[int, int]]:
        """Recover recent movement deltas, newest first."""
        action_to_delta = {
            action: delta
            for delta, action in getattr(self, "DELTA_TO_ACTION", {}).items()
        }
        deltas = []
        for action in reversed(self.executed_actions):
            delta = action_to_delta.get(action)
            if delta is None:
                continue
            deltas.append(delta)
            if len(deltas) >= limit:
                break
        return deltas

    def last_executed_move_delta(self) -> tuple[int, int] | None:
        """Recover the last actual movement direction for fallback exploration."""
        deltas = self.recent_executed_move_deltas(limit=1)
        return deltas[0] if deltas else None

    def ally_positions(self, scene: dict[str, object]) -> set[tuple[int, int]]:
        """Return relative positions occupied by visible allies."""
        positions: set[tuple[int, int]] = set()
        entities = scene.get("entities")
        if not isinstance(entities, list):
            return positions
        for entry in entities:
            if not isinstance(entry, dict) or not self.is_ally_entry(entry):
                continue
            pos = self.entry_position(entry)
            if pos is not None:
                positions.add((pos[0], pos[1]))
        return positions

    def build_explore_frontier_action(
        self,
        scene: dict[str, object],
        blocked_action_id: str | None = None,
    ) -> dict[str, object] | None:
        """Create a dynamic exploration action toward the nearest safe frontier."""
        result = self.nearest_exploration_frontier_path(scene)
        if result is None:
            return None

        target_pos, path = result
        next_action = self.path_to_action(path)
        if next_action is None:
            return None

        path_steps = self.path_step_names(path)
        action = build_action_payload(
            action_id="explore:frontier",
            action_type="explore",
            label="Explore unknown area",
            target_ref=None,
            target_key="frontier:nearest",
            target_pos=target_pos,
            procedure_kind="dynamic",
            low_level_goal="move toward the nearest safe frontier that can reveal new map tiles",
            next_action=next_action,
            path_steps=path_steps,
            distance_steps=max(0, len(path) - 1),
        )
        if self.path_first_delta(path) == self.last_executed_move_delta():
            action["exploration_priority"] = 0
            action["continues_recent_direction"] = True
        if self.frontier_recovers_blocked_action(blocked_action_id, path_steps):
            direction = blocked_action_id.rsplit(":", 1)[-1]
            action.update(
                {
                    "label": f"Recover blocked {direction} route",
                    "low_level_goal": (
                        f"recover the blocked {direction} route by revealing "
                        "the nearest safe frontier"
                    ),
                    "recovery_for_action_id": blocked_action_id,
                }
            )
        return action

    def frontier_recovers_blocked_action(
        self,
        blocked_action_id: str | None,
        path_steps: list[str],
    ) -> bool:
        """Return whether a frontier path continues a recently blocked exit."""
        if (
            not isinstance(blocked_action_id, str)
            or not blocked_action_id.startswith(("go_to_exit:exit:", "explore:exit:"))
            or not path_steps
        ):
            return False
        blocked_direction = blocked_action_id.rsplit(":", 1)[-1]
        first_step = path_steps[0]
        if first_step == blocked_direction:
            return True
        if blocked_direction == "east":
            return first_step in {"east", "northeast", "southeast"}
        if blocked_direction == "west":
            return first_step in {"west", "northwest", "southwest"}
        if blocked_direction == "north":
            return first_step in {"north", "northeast", "northwest"}
        if blocked_direction == "south":
            return first_step in {"south", "southeast", "southwest"}
        return first_step == blocked_direction
