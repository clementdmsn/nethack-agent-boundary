from __future__ import annotations

import re

from prompting.actions.factory import build_action_payload


class ActionCatalogMixin:
    def build_available_actions(
        self,
        scene: dict[str, object],
    ) -> list[dict[str, object]]:
        """Build and rank every action the model can choose this turn."""
        actions: list[dict[str, object]] = []
        blocked_action_id = (
            self.blocked_action_id or self.current_action_id
            if self.procedure_status == "blocked"
            and isinstance(self.blocked_action_id or self.current_action_id, str)
            else None
        )
        hostile_pressure = self.has_hostile_pressure(scene)
        nearby_hostile_pressure = self.has_nearby_hostile_pressure(scene)
        pending_door_action = self.build_pending_door_step_action(scene)
        if pending_door_action is not None:
            self.append_unique_action(actions, pending_door_action)
        backtrack_action = self.build_corridor_backtrack_action(scene)
        if backtrack_action is not None:
            self.append_unique_action(actions, backtrack_action)

        trap_action = self.build_trap_escape_action(scene)
        if trap_action is not None:
            for action in self.entity_actions(scene):
                self.append_unique_action(actions, action)
            self.append_unique_action(actions, trap_action)
            actions.sort(key=self.action_sort_key(blocked_action_id))
            return actions

        if self.scene_is_corridor_context(scene) and not hostile_pressure:
            for action in self.build_corridor_run_actions(scene):
                self.append_unique_action(actions, action)
            self.mark_nearest_exploration_action(actions, nearby_hostile_pressure)
            actions.sort(key=self.action_sort_key(blocked_action_id))
            if actions:
                return actions

        for action in self.exit_actions(scene, hostile_pressure):
            self.append_unique_action(actions, action)
        for action in self.item_actions(scene):
            self.append_unique_action(actions, action)
        for action in self.feature_actions(scene, hostile_pressure):
            self.append_unique_action(actions, action)
        if not nearby_hostile_pressure:
            for action in self.build_corridor_run_actions(scene):
                self.append_unique_action(actions, action)
        for action in self.entity_actions(scene):
            self.append_unique_action(actions, action)

        self.append_unique_action(
            actions,
            self.build_explore_frontier_action(
                scene,
                blocked_action_id=blocked_action_id,
            ),
        )
        self.append_fallback_explore_action(scene, actions, nearby_hostile_pressure)
        self.append_unique_action(actions, self.build_push_blocking_ally_action(scene))
        self.mark_nearest_exploration_action(actions, nearby_hostile_pressure)
        self.annotate_tactical_action_compatibility(actions, scene)
        actions.sort(key=self.action_sort_key(blocked_action_id))
        return actions

    def scene_is_corridor_context(self, scene: dict[str, object]) -> bool:
        """Return whether the player is currently in a corridor context."""
        context = scene.get("location_context")
        return isinstance(context, dict) and context.get("in_corridor") is True

    def append_unique_action(
        self,
        actions: list[dict[str, object]],
        action: dict[str, object] | None,
    ) -> None:
        """Add one action, replacing duplicate ids only when the route is shorter."""
        if action is None:
            return
        action_id = action.get("action_id")
        if not isinstance(action_id, str):
            return
        for index, existing in enumerate(actions):
            if existing.get("action_id") != action_id:
                continue
            existing_distance = existing.get("distance_steps")
            action_distance = action.get("distance_steps")
            if not isinstance(existing_distance, int):
                existing_distance = 99
            if not isinstance(action_distance, int):
                action_distance = 99
            if action_distance < existing_distance:
                actions[index] = action
            return
        actions.append(action)

    def exit_actions(
        self,
        scene: dict[str, object],
        hostile_pressure: bool,
    ) -> list[dict[str, object]]:
        """Turn visible exits into exploration actions or flee routes."""
        actions = []
        exits = scene.get("exits")
        if not isinstance(exits, list):
            return actions

        for entry in exits:
            if not isinstance(entry, dict):
                continue
            target_ref = entry.get("ref")
            target_key = entry.get("target_key")
            target_pos = self.entry_position(entry)
            direction = entry.get("direction")
            if (
                not isinstance(target_ref, str)
                or not isinstance(target_key, str)
                or target_pos is None
                or not isinstance(direction, str)
            ):
                continue
            if hostile_pressure:
                action = self.build_navigation_action(
                    scene,
                    action_id=f"flee:{target_key}",
                    action_type="flee",
                    label=f"Flee through {direction} exit",
                    target_ref=target_ref,
                    target_key=target_key,
                    target_pos=target_pos,
                    low_level_goal=f"escape pressure through the {direction} exit",
                    direct_direction=direction,
                    completes_procedure_after_step=target_pos == [0, 0],
                    require_safer_destination=True,
                )
            else:
                action = self.build_navigation_action(
                    scene,
                    action_id=f"explore:{target_key}",
                    action_type="explore",
                    label=f"Explore through {direction} opening",
                    target_ref=target_ref,
                    target_key=target_key,
                    target_pos=target_pos,
                    low_level_goal=f"explore through the {direction} opening",
                    direct_direction=direction,
                    completes_procedure_after_step=target_pos == [0, 0],
                )
            if action is not None:
                actions.append(action)
        return actions

    def item_actions(self, scene: dict[str, object]) -> list[dict[str, object]]:
        """Turn reachable visible items into pickup actions."""
        actions = []
        items = scene.get("items")
        if not isinstance(items, list):
            return actions

        for entry in items:
            if not isinstance(entry, dict):
                continue
            if self.entry_is_excluded_pickup(entry):
                continue
            facts = self.actionable_entry_facts(entry)
            if facts is None:
                continue
            action = self.build_static_action(
                scene,
                action_id=f"pick:{facts.target_key}",
                action_type="pick_item",
                label=f"Pick up {self.entry_model_name(entry)}",
                target_ref=facts.target_ref,
                target_key=facts.target_key,
                target_pos=facts.pos,
                low_level_goal=f"pick up {facts.description}",
            )
            if action is not None:
                action["auto_continue"] = True
                if action.get("next_action") == "pickup()":
                    action["completes_procedure_after_step"] = True
                actions.append(action)
        return actions

    def entry_is_excluded_pickup(self, entry: dict[str, object]) -> bool:
        """Return whether an item should be visible but not picked up automatically."""
        description = entry.get("description")
        if not isinstance(description, str):
            return False
        normalized = description.lower()
        return (
            "corpse" in normalized
            or re.search(r"\bbox(?:es)?\b", normalized) is not None
            or re.search(r"\bchests?\b", normalized) is not None
        )

    def scene_indicates_bear_trap(self, scene: dict[str, object]) -> bool:
        """Return whether the scene says the player is immobilized in a bear trap."""
        room_description = scene.get("room_description")
        return (
            isinstance(room_description, str)
            and "caught in a bear trap" in room_description.lower()
        )

    def build_trap_escape_action(
        self,
        scene: dict[str, object],
    ) -> dict[str, object] | None:
        """Build a specific action for struggling out of an active bear trap."""
        if not self.scene_indicates_bear_trap(scene):
            return None
        next_action = self.trap_escape_move_action(scene)
        if next_action is None:
            return None
        return build_action_payload(
            action_id="escape:bear_trap",
            action_type="escape_trap",
            label="Escape bear trap",
            target_ref=None,
            target_key="trap:bear",
            procedure_kind="dynamic",
            low_level_goal="struggle free from the bear trap",
            next_action=next_action,
            path_steps=[],
            distance_steps=1,
            completes_procedure_after_step=True,
            auto_continue=False,
        )

    def trap_escape_move_action(self, scene: dict[str, object]) -> str | None:
        """Pick a legal-looking movement attempt to escape a bear trap."""
        last_delta = self.last_executed_move_delta()
        preferred: list[tuple[int, int]] = []
        if last_delta is not None:
            preferred.append(last_delta)
            preferred.append((-last_delta[0], -last_delta[1]))
        preferred.extend(
            delta for delta in self.DIRECTION_NAMES if delta not in preferred
        )

        allies = self.ally_positions(scene)
        for delta in preferred:
            if delta in allies:
                continue
            if self.is_traversable_scene_pos(scene, delta):
                return self.DELTA_TO_ACTION.get(delta)
        return None

    def feature_actions(
        self,
        scene: dict[str, object],
        hostile_pressure: bool,
    ) -> list[dict[str, object]]:
        """Turn visible doors and staircases into feature actions."""
        actions = []
        features = scene.get("features")
        if not isinstance(features, list):
            return actions

        for entry in features:
            if not isinstance(entry, dict):
                continue
            action = self.build_feature_action(
                scene,
                entry,
                hostile_pressure=hostile_pressure,
            )
            if action is not None:
                actions.append(action)
        return actions

    def entity_actions(self, scene: dict[str, object]) -> list[dict[str, object]]:
        """Turn visible non-allied entities into fight and nearby flee actions."""
        actions = []
        entities = scene.get("entities")
        if not isinstance(entities, list):
            return actions

        for entry in entities:
            if not isinstance(entry, dict):
                continue
            description = entry.get("description")
            if not isinstance(description, str):
                continue
            if self.is_ally_entry(entry):
                continue
            distance = self.chebyshev_distance(self.entry_position(entry) or [9, 9])
            if distance <= 1:
                fight_action = self.build_fight_action(scene, entry)
                if fight_action is not None:
                    actions.append(fight_action)
            if distance <= 2:
                flee_action = self.build_flee_action(scene, entry)
                if flee_action is not None:
                    actions.append(flee_action)
        return actions

    def append_fallback_explore_action(
        self,
        scene: dict[str, object],
        actions: list[dict[str, object]],
        hostile_pressure: bool,
    ) -> None:
        """Add generic exploration when no better safe exploration action exists."""
        has_explore_action = any(
            action.get("action_type")
            in {
                "explore",
                "explore_corridor",
                "explore_door",
                "go_to_door",
                "go_to_item",
                "pick_item",
                "go_to_staircase",
                "backtrack_corridor",
            }
            for action in actions
        )
        has_tactical_action = any(
            action.get("action_type") in {"flee", "fight"}
            for action in actions
        )
        if (
            (not hostile_pressure or not has_tactical_action)
            and (not has_explore_action or scene.get("visibility") == "dark")
        ):
            explore_action = self.fallback_explore_action(scene)
            if explore_action is not None:
                existing_ids = {
                    action.get("action_id")
                    for action in actions
                    if isinstance(action.get("action_id"), str)
                }
                if explore_action["action_id"] not in existing_ids:
                    actions.append(explore_action)

    def mark_nearest_exploration_action(
        self,
        actions: list[dict[str, object]],
        hostile_pressure: bool,
    ) -> None:
        """Mark the nearest non-tactical exploration action as the default lead."""
        if hostile_pressure:
            return
        exploration_types = {
            "explore_corridor",
            "explore",
            "explore_door",
            "go_to_door",
            "go_to_staircase",
            "backtrack_corridor",
        }
        candidates = [
            action
            for action in actions
            if action.get("action_type") in exploration_types
            and not action.get("disabled")
        ]
        if not candidates:
            return

        def candidate_key(action: dict[str, object]) -> tuple[int, int, str]:
            distance = action.get("distance_steps")
            if not isinstance(distance, int):
                distance = 99
            exploration_priority = action.get("exploration_priority")
            if not isinstance(exploration_priority, int):
                exploration_priority = 5
            return (distance, exploration_priority, str(action.get("action_id")))

        preferred = min(candidates, key=candidate_key)
        preferred["preferred_exploration"] = True
        preferred["selection_priority"] = "nearest_exploration"

    def action_sort_key(self, blocked_action_id: str | None):
        """Return the stable priority function used for model-facing action order."""
        def sort_key(action: dict[str, object]) -> tuple[int, int, int, str]:
            """Rank one action by tactical priority, distance, and deterministic id."""
            action_type = action.get("action_type")
            distance = action.get("distance_steps")
            if not isinstance(distance, int):
                distance = 99
            exploration_priority = action.get("exploration_priority")
            if not isinstance(exploration_priority, int):
                exploration_priority = 5
            if action_type == "flee":
                priority = 0
            elif action_type == "fight":
                priority = 1
            elif action_type == "escape_trap":
                priority = 2
            elif action_type in {"pick_item", "go_to_item"}:
                priority = 3
            elif action.get("preferred_exploration"):
                priority = 4
            elif action.get("post_open_door_step"):
                priority = 4
            elif action_type == "explore_corridor":
                priority = 5
            elif action_type == "explore":
                priority = 6
            elif action.get("recovery_for_action_id") == blocked_action_id:
                priority = 7
            elif action_type in {"explore_door", "go_to_door", "go_to_staircase"}:
                priority = 8
            elif action_type == "wait":
                priority = 10
            else:
                priority = 9
            return (
                priority,
                exploration_priority,
                distance,
                str(action.get("action_id")),
            )

        return sort_key
