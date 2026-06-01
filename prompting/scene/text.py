from __future__ import annotations

from dataclasses import dataclass

from observation.constants import PET_NAMES, PET_SPECIES_WORDS


@dataclass(frozen=True)
class SceneEntryFacts:
    """Validated fields needed to turn a scene entry into an action."""

    target_ref: str
    target_key: str
    pos: list[int]
    description: str


class SceneTextMixin:
    def is_tame_pet_description(self, description: str) -> bool:
        """Return whether farlook text describes a known tame pet species."""
        lowered = description.lower()
        if "tame " not in lowered:
            return False
        return any(name.lower() in lowered for name in PET_NAMES) or any(
            species in lowered for species in PET_SPECIES_WORDS
        )

    def find_allies(self, scene: dict[str, object]) -> list[dict[str, object]]:
        """Extract visible allied pets from the compact scene representation."""
        elements = scene.get("entities")
        if not isinstance(elements, list):
            elements = scene.get("elements")
        if not isinstance(elements, list):
            return []

        allies = []
        for element in elements:
            if not isinstance(element, dict):
                continue

            description = element.get("description")
            if not isinstance(description, str):
                continue

            if not self.is_tame_pet_description(description):
                continue

            ally = {
                "description": description,
                "relationship": "ally",
            }
            pos = element.get("pos")
            if isinstance(pos, list) and len(pos) == 2:
                ally["pos"] = pos
            allies.append(ally)

        return allies

    def is_ally_entry(self, entry: dict[str, object]) -> bool:
        """Return whether a scene entry is an ally rather than a hostile."""
        description = entry.get("description")
        if not isinstance(description, str):
            return False
        return self.is_tame_pet_description(description)

    def singular_bucket_name(self, bucket: str) -> str:
        """Normalize plural scene buckets into a ref prefix."""
        if bucket.endswith("ies"):
            return bucket[:-3] + "y"
        if bucket.endswith("s"):
            return bucket[:-1]
        return bucket

    def entry_model_name(self, entry: dict[str, object]) -> str:
        """Return the stable readable name attached during scene normalization."""
        display_name = entry.get("display_name")
        if isinstance(display_name, str) and display_name:
            return display_name
        description = entry.get("description")
        if isinstance(description, str) and description:
            return description
        ref = entry.get("ref")
        if isinstance(ref, str) and ref:
            return ref
        return "target"

    def entry_position(self, entry: dict[str, object]) -> list[int] | None:
        """Return the compact player-relative position when the entry has one."""
        pos = entry.get("pos")
        if (
            isinstance(pos, list)
            and len(pos) == 2
            and all(isinstance(value, int) for value in pos)
        ):
            return [pos[0], pos[1]]
        return None

    def actionable_entry_facts(
        self,
        entry: dict[str, object],
    ) -> SceneEntryFacts | None:
        """Validate common fields needed to build an action from a scene entry."""
        target_ref = entry.get("ref")
        target_key = entry.get("target_key")
        pos = self.entry_position(entry)
        description = entry.get("description")
        if (
            not isinstance(target_ref, str)
            or not isinstance(target_key, str)
            or pos is None
            or not isinstance(description, str)
        ):
            return None
        return SceneEntryFacts(
            target_ref=target_ref,
            target_key=target_key,
            pos=pos,
            description=description,
        )

    def entry_positions(self, entry: dict[str, object]) -> list[list[int]]:
        """Return all positions associated with a scene entry."""
        pos = self.entry_position(entry)
        if pos is not None:
            return [pos]
        positions = entry.get("positions")
        if not isinstance(positions, list):
            return []
        result = []
        for item in positions:
            if (
                isinstance(item, list)
                and len(item) == 2
                and all(isinstance(value, int) for value in item)
            ):
                result.append([item[0], item[1]])
        return result

    def chebyshev_distance(self, pos: list[int] | tuple[int, int]) -> int:
        """Measure turn distance on an 8-direction grid."""
        return max(abs(pos[0]), abs(pos[1]))

    def direction_sequence_for_pos(
        self,
        pos: list[int] | tuple[int, int],
    ) -> list[str]:
        """Format a relative position into one-step movement directions."""
        dx = pos[0]
        dy = pos[1]
        steps: list[str] = []

        while dx or dy:
            step_dx = 0 if dx == 0 else (1 if dx > 0 else -1)
            step_dy = 0 if dy == 0 else (1 if dy > 0 else -1)
            direction = self.DIRECTION_NAMES.get((step_dx, step_dy))
            if direction is None:
                break
            steps.append(direction)
            dx -= step_dx
            dy -= step_dy

        return steps

    def tabletop_entry(self, kind: str, entry: dict[str, object]) -> dict[str, object]:
        """Build a tabletop-style location description for one scene entry."""
        description = entry.get("description")
        pos = self.entry_position(entry)
        if not isinstance(description, str) or pos is None:
            return {
                "ref": entry.get("ref"),
                "kind": kind,
                "description": description,
            }

        steps = self.direction_sequence_for_pos(pos)
        distance = self.chebyshev_distance(pos)
        direction_text = ", ".join(steps) if steps else "here"
        summary = f"{description} is {distance} step"
        if distance != 1:
            summary += "s"
        if steps:
            summary += f" away via {direction_text}"
        else:
            summary += " away here"
        return {
            "ref": entry.get("ref"),
            "kind": kind,
            "description": description,
            "pos": pos,
            "distance_steps": distance,
            "direction_sequence": steps,
            "summary": summary,
        }

    def describe_scene_for_model(self, scene: dict[str, object]) -> str:
        """Build a compact readable summary alongside the structured payload."""
        parts: list[str] = []

        room_description = scene.get("room_description")
        if isinstance(room_description, str) and room_description:
            parts.append(room_description)

        tabletop = []
        for bucket in ("entities", "items", "exits"):
            entries = scene.get(bucket)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                summary = self.tabletop_entry(
                    self.singular_bucket_name(bucket),
                    entry,
                ).get("summary")
                if isinstance(summary, str):
                    tabletop.append(summary)

        if tabletop:
            parts.append("Visible facts: " + "; ".join(tabletop) + ".")

        if not parts:
            return "No scene summary is available."

        return " ".join(parts)

    def recent_actions_for_model(self, limit: int = 5) -> list[str]:
        """Return recently executed low-level actions for loop avoidance."""
        actions = [action for action in self.executed_actions if action.strip()]
        return actions[-limit:]

    def compact_action_view(self, action: dict[str, object]) -> dict[str, object]:
        """Shrink one available action to the fields needed for selection."""
        compact = {
            "id": action.get("action_id"),
            "label": action.get("label"),
            "type": action.get("action_type"),
            "goal": action.get("low_level_goal"),
        }
        recovery_for = action.get("recovery_for_action_id")
        if isinstance(recovery_for, str):
            compact["recovers_blocked_action"] = recovery_for
        selection_priority = action.get("selection_priority")
        if isinstance(selection_priority, str):
            compact["priority"] = selection_priority
        distance = action.get("distance_steps")
        if isinstance(distance, int):
            compact["steps"] = distance
        tactical_notes = action.get("tactical_notes")
        if isinstance(tactical_notes, list):
            notes = [note for note in tactical_notes if isinstance(note, str)]
            if notes:
                compact["tactical_notes"] = notes
        if action.get("runtime_backtracking"):
            compact["runtime_backtracking"] = True
        if action.get("compatible_with_flee"):
            compact["compatible_with_flee"] = True
        if action.get("disabled"):
            compact["disabled"] = True
        return compact

    def compact_current_procedure(
        self,
        current_procedure: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Shrink the active procedure to fields needed for continue/switch."""
        if not isinstance(current_procedure, dict):
            return None
        return {
            "action_id": current_procedure.get("action_id"),
            "label": current_procedure.get("label"),
            "status": current_procedure.get("status"),
            "goal": current_procedure.get("low_level_goal"),
            "next_action": current_procedure.get("next_action"),
        }

    def compact_scene_state(self, scene: dict[str, object]) -> dict[str, object]:
        """Build a minimal scene context for decision turns."""
        state: dict[str, object] = {
            "identity": self.player_identity,
            "room_description": scene.get("room_description"),
            "visibility": scene.get("visibility", "normal"),
            "location_context": scene.get("location_context"),
            "player": scene.get("player"),
        }
        allies = self.find_allies(scene)
        if allies:
            state["pet"] = allies

        context = scene.get("location_context")
        in_room = isinstance(context, dict) and context.get("in_room") is True

        def include_compact_entry(entry: dict[str, object]) -> bool:
            if in_room:
                return True
            pos = self.entry_position(entry)
            if pos is None:
                return False
            if self.is_ally_entry(entry):
                return True
            return self.chebyshev_distance(pos) <= 3

        visible = []
        for bucket in ("entities", "items", "exits", "features", "areas"):
            entries = scene.get(bucket)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if not include_compact_entry(entry):
                    continue
                table = self.tabletop_entry(self.singular_bucket_name(bucket), entry)
                kind = table.get("kind")
                entry_id = entry.get("target_key") or table.get("ref")
                if bucket == "items" and self.entry_is_excluded_pickup(entry):
                    kind = self.excluded_pickup_visible_kind(entry)
                    entry_id = entry.get("ref") or table.get("ref")
                compact = {
                    "id": entry_id,
                    "kind": kind,
                    "label": entry.get("display_name") or table.get("description"),
                    "steps": table.get("distance_steps"),
                    "path": table.get("direction_sequence"),
                }
                if bucket == "items" and self.entry_is_excluded_pickup(entry):
                    compact["not_pickup_target"] = True
                visible.append(compact)
        if visible:
            state["visible"] = visible
        memory = self.compact_procedural_memory()
        if memory:
            state["procedural_memory"] = memory
        return state

    def excluded_pickup_visible_kind(self, entry: dict[str, object]) -> str:
        """Return a non-actionable scene kind for visible but excluded objects."""
        description = entry.get("description")
        normalized = description.lower() if isinstance(description, str) else ""
        if "corpse" in normalized:
            return "remains"
        if "chest" in normalized or "box" in normalized:
            return "container"
        return "object"

    def compact_procedural_memory(self) -> dict[str, object]:
        """Expose only the short-lived facts needed for the next choice."""
        memory: dict[str, object] = {}
        if self.pending_open_door_direction:
            memory["opened_door_pending_step"] = self.pending_open_door_direction
        if self.blocked_corridor_entries:
            memory["dead_end_corridors"] = list(self.blocked_corridor_entries[-4:])
        if self.blocked_ally_positions:
            memory["blocked_by_ally"] = list(self.blocked_ally_positions[-4:])
        return memory

    def is_hidden_model_feature(self, entry: dict[str, object]) -> bool:
        """Return whether a feature should be hidden from model planning."""
        description = entry.get("description")
        if not isinstance(description, str):
            return False
        lowered = description.lower()
        return "staircase" in lowered and "up" in lowered

    def model_visible_scene(
        self,
        scene: dict[str, object],
    ) -> dict[str, object]:
        """Remove facts the model should not plan around from the scene copy."""
        visible = dict(scene)
        features = scene.get("features")
        if isinstance(features, list):
            visible["features"] = [
                entry
                for entry in features
                if not (
                    isinstance(entry, dict)
                    and self.is_hidden_model_feature(entry)
                )
            ]
        return visible
