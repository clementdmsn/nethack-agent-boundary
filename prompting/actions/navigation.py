from __future__ import annotations

from prompting.actions.factory import build_action_payload


class NavigationActionsMixin:
    def path_step_names(self, path: list[list[int]] | None) -> list[str]:
        """Convert a full path into movement direction names."""
        if path is None or len(path) < 2:
            return []

        steps = []
        for current, nxt in zip(path, path[1:]):
            delta = (nxt[0] - current[0], nxt[1] - current[1])
            direction = self.DIRECTION_NAMES.get(delta)
            if direction is not None:
                steps.append(direction)
        return steps

    def build_static_action(
        self,
        scene: dict[str, object],
        action_id: str,
        action_type: str,
        label: str,
        target_ref: str,
        target_key: str,
        target_pos: list[int],
        low_level_goal: str,
        require_cardinal_target_entry: bool = False,
    ) -> dict[str, object] | None:
        """Create a static target-following action when a visible path exists."""
        allow_occupied_destination = action_type in {"go_to_item", "pick_item"}
        path = self.shortest_visible_path(
            scene,
            target_pos,
            allow_occupied_destination=allow_occupied_destination,
        )
        if path is None:
            return None
        if require_cardinal_target_entry and not self.path_enters_target_cardinally(path):
            return None
        if self.path_moves_adjacent_to_hostile(scene, path):
            return None

        next_action = None
        if action_type in {"go_to_item", "pick_item"} and target_pos == [0, 0]:
            next_action = "pickup()"
        else:
            next_action = self.path_to_action(path)
        if next_action is None:
            return None

        return build_action_payload(
            action_id=action_id,
            action_type=action_type,
            label=label,
            target_ref=target_ref,
            target_key=target_key,
            procedure_kind="static",
            low_level_goal=low_level_goal,
            next_action=next_action,
            path_steps=self.path_step_names(path),
            distance_steps=max(0, len(path) - 1),
        )

    def path_enters_target_cardinally(self, path: list[list[int]]) -> bool:
        """Return whether the last step into a target is non-diagonal."""
        if len(path) < 2:
            return True
        previous = path[-2]
        target = path[-1]
        if len(previous) != 2 or len(target) != 2:
            return False
        dx = target[0] - previous[0]
        dy = target[1] - previous[1]
        return abs(dx) + abs(dy) == 1

    def build_navigation_action(
        self,
        scene: dict[str, object],
        *,
        action_id: str,
        action_type: str,
        label: str,
        target_ref: str | None,
        target_key: str,
        target_pos: list[int],
        low_level_goal: str,
        direct_direction: str | None = None,
        completes_procedure_after_step: bool = False,
        require_safer_destination: bool = False,
        require_cardinal_target_entry: bool = False,
    ) -> dict[str, object] | None:
        """Create a route action for exploration or fleeing."""
        if direct_direction and target_pos == [0, 0]:
            return build_action_payload(
                action_id=action_id,
                action_type=action_type,
                label=label,
                target_ref=target_ref,
                target_key=target_key,
                procedure_kind="dynamic" if action_type == "flee" else "static",
                low_level_goal=low_level_goal,
                next_action=f"move({direct_direction})",
                path_steps=[direct_direction],
                distance_steps=1,
                completes_procedure_after_step=completes_procedure_after_step,
            )

        if require_safer_destination:
            path = self.threat_aware_visible_path(
                scene,
                target_pos,
                require_cardinal_target_entry=require_cardinal_target_entry,
            )
        else:
            path = self.shortest_visible_path(scene, target_pos)
        if path is None:
            return None
        if require_cardinal_target_entry and not self.path_enters_target_cardinally(path):
            return None
        if require_safer_destination and not self.path_ends_farther_from_hostiles(
            scene,
            path,
        ):
            return None

        next_action = (
            f"move({direct_direction})"
            if direct_direction and target_pos == [0, 0]
            else self.path_to_action(path)
        )
        if next_action is None:
            return None

        return build_action_payload(
            action_id=action_id,
            action_type=action_type,
            label=label,
            target_ref=target_ref,
            target_key=target_key,
            procedure_kind="dynamic" if action_type == "flee" else "static",
            low_level_goal=low_level_goal,
            next_action=next_action,
            path_steps=(
                [direct_direction]
                if direct_direction and target_pos == [0, 0]
                else self.path_step_names(path)
            ),
            distance_steps=1 if target_pos == [0, 0] else max(0, len(path) - 1),
            completes_procedure_after_step=completes_procedure_after_step,
        )
