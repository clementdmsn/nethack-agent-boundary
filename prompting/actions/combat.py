from __future__ import annotations

from prompting.actions.factory import build_action_payload


class CombatActionsMixin:
    def action_first_step_delta(
        self,
        action: dict[str, object],
    ) -> tuple[int, int] | None:
        """Return the first movement delta for one action, when it has one."""
        path_steps = action.get("path_steps")
        if isinstance(path_steps, list) and path_steps:
            first_step = path_steps[0]
            if isinstance(first_step, str):
                for delta, direction in self.DIRECTION_NAMES.items():
                    if direction == first_step:
                        return delta

        next_action = action.get("next_action")
        if not isinstance(next_action, str):
            return None
        if not next_action.startswith("move(") or not next_action.endswith(")"):
            return None
        direction = next_action.removeprefix("move(").removesuffix(")")
        for delta, name in self.DIRECTION_NAMES.items():
            if name == direction:
                return delta
        return None

    def nearby_hostile_entries(
        self,
        scene: dict[str, object],
        *,
        max_distance: int = 3,
    ) -> list[dict[str, object]]:
        """Return visible non-allied hostile entries close enough to affect choices."""
        entries = scene.get("entities")
        if not isinstance(entries, list):
            return []

        hostiles = []
        for entry in entries:
            if not isinstance(entry, dict) or self.is_ally_entry(entry):
                continue
            pos = self.entry_position(entry)
            description = entry.get("description")
            if pos is None or not isinstance(description, str):
                continue
            distance = self.chebyshev_distance(pos)
            if distance <= max_distance:
                hostiles.append(
                    {
                        "description": description,
                        "pos": pos,
                        "distance": distance,
                    }
                )
        return hostiles

    def annotate_tactical_action_compatibility(
        self,
        actions: list[dict[str, object]],
        scene: dict[str, object],
    ) -> None:
        """Add geometry notes when loot movement also helps with nearby threats."""
        hostiles = self.nearby_hostile_entries(scene)
        if not hostiles:
            return

        for action in actions:
            if action.get("action_type") != "pick_item":
                continue
            delta = self.action_first_step_delta(action)
            if delta is None:
                continue

            notes = []
            compatible = False
            worsens = False
            for hostile in hostiles:
                pos = hostile.get("pos")
                description = hostile.get("description")
                current_distance = hostile.get("distance")
                if (
                    not isinstance(pos, list)
                    or len(pos) != 2
                    or not isinstance(description, str)
                    or not isinstance(current_distance, int)
                ):
                    continue
                projected_distance = max(
                    abs(pos[0] - delta[0]),
                    abs(pos[1] - delta[1]),
                )
                if projected_distance > current_distance:
                    compatible = True
                    notes.append(
                        f"first step also increases distance from {description}"
                    )
                elif projected_distance == current_distance:
                    notes.append(
                        f"first step keeps distance from {description}"
                    )
                else:
                    worsens = True
                    notes.append(
                        f"first step moves closer to {description}"
                    )

            if notes:
                action["tactical_notes"] = notes
            if compatible and not worsens:
                action["compatible_with_flee"] = True

    def path_moves_adjacent_to_hostile(
        self,
        scene: dict[str, object],
        path: list[list[int]],
    ) -> bool:
        """Report whether a candidate path steps next to a hostile."""
        hostiles = self.hostile_positions(scene)
        if not hostiles:
            return False

        for step in path[1:]:
            if not isinstance(step, list) or len(step) != 2:
                continue
            for hostile in hostiles:
                if max(abs(hostile[0] - step[0]), abs(hostile[1] - step[1])) <= 1:
                    return True
        return False

    def path_ends_farther_from_hostiles(
        self,
        scene: dict[str, object],
        path: list[list[int]],
    ) -> bool:
        """Report whether a path increases distance from visible hostiles."""
        hostiles = self.hostile_positions(scene)
        if not hostiles or len(path) < 2:
            return False
        destination = path[-1]
        if not isinstance(destination, list) or len(destination) != 2:
            return False
        current_distance = min(max(abs(x), abs(y)) for x, y in hostiles)
        destination_distance = min(
            max(abs(hostile[0] - destination[0]), abs(hostile[1] - destination[1]))
            for hostile in hostiles
        )
        return destination_distance > current_distance

    def has_hostile_pressure(self, scene: dict[str, object]) -> bool:
        """Return whether visible hostiles should switch priorities to survival."""
        return any(
            max(abs(pos[0]), abs(pos[1])) <= 3
            for pos in self.hostile_positions(scene)
        )

    def has_nearby_hostile_pressure(self, scene: dict[str, object]) -> bool:
        """Return whether a hostile is close enough to interrupt exploration."""
        return any(
            max(abs(pos[0]), abs(pos[1])) <= 2
            for pos in self.hostile_positions(scene)
        )

    def build_fight_action(
        self,
        scene: dict[str, object],
        entry: dict[str, object],
    ) -> dict[str, object] | None:
        """Create a dynamic pursue-or-attack action for one visible hostile."""
        facts = self.actionable_entry_facts(entry)
        if facts is None:
            return None

        path = self.shortest_visible_path(
            scene,
            facts.pos,
            allow_occupied_destination=True,
        )
        next_action = self.path_to_action(path)
        if next_action is None:
            return None

        return build_action_payload(
            action_id=f"fight:{facts.target_key}",
            action_type="fight",
            label=f"Attack {self.entry_model_name(entry)}",
            target_ref=facts.target_ref,
            target_key=facts.target_key,
            procedure_kind="dynamic",
            low_level_goal=f"engage {facts.description}",
            next_action=next_action,
            path_steps=self.path_step_names(path),
            distance_steps=self.chebyshev_distance(facts.pos),
        )

    def build_flee_action(
        self,
        scene: dict[str, object],
        entry: dict[str, object],
    ) -> dict[str, object] | None:
        """Create a dynamic flee action tied to one nearby hostile."""
        facts = self.actionable_entry_facts(entry)
        if facts is None:
            return None

        next_action = self.choose_flee_action(scene)
        if next_action is None:
            return None

        return build_action_payload(
            action_id=f"flee:{facts.target_key}",
            action_type="flee",
            label=f"Flee {self.entry_model_name(entry)}",
            target_ref=facts.target_ref,
            target_key=facts.target_key,
            procedure_kind="dynamic",
            low_level_goal=f"create distance from {facts.description}",
            next_action=next_action,
            path_steps=[],
            distance_steps=self.chebyshev_distance(facts.pos),
        )
