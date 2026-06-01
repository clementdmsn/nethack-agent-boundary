from __future__ import annotations


class SceneEventsMixin:
    def consume_procedure_events(self) -> list[dict[str, object]]:
        """Return and clear runtime-generated procedure events for the prompt."""
        events = list(self.procedure_events)
        self.procedure_events = []
        return events

    def event_is_pet_only(self, event: dict[str, object]) -> bool:
        """Return whether a scene event only concerns a tame pet."""
        text = event.get("text")
        if not isinstance(text, str):
            return False
        return self.is_tame_pet_description(text)

    def non_pet_scene_changed(self, events: list[dict[str, object]]) -> bool:
        """Return whether something other than pet motion changed in the scene."""
        for event in events:
            if not isinstance(event, dict):
                continue
            if self.event_is_pet_only(event):
                continue
            return True
        return False

    def movement_event_text(
        self,
        name: str,
        previous_pos: list[int],
        current_pos: list[int],
    ) -> str:
        """Convert a position delta into a short event sentence."""
        delta = [
            current_pos[0] - previous_pos[0],
            current_pos[1] - previous_pos[1],
        ]
        steps = self.direction_sequence_for_pos(delta)
        if not steps:
            return f"{name} stayed in place"
        if len(steps) == 1:
            return f"{name} moved 1 step {steps[0]}"

        return f"{name} moved {len(steps)} steps {' then '.join(steps)}"

    def build_scene_events(
        self,
        previous_scene: dict[str, object] | None,
        scene: dict[str, object],
    ) -> list[dict[str, object]]:
        """Summarize what changed since the previous observed scene."""
        if not isinstance(previous_scene, dict):
            return []

        events: list[dict[str, object]] = []

        for bucket in ("entities", "items", "exits"):
            current_entries = scene.get(bucket, [])
            previous_entries = previous_scene.get(bucket, [])
            if not isinstance(current_entries, list) or not isinstance(
                previous_entries,
                list,
            ):
                continue

            previous_by_ref = {
                entry.get("ref"): entry
                for entry in previous_entries
                if isinstance(entry, dict) and isinstance(entry.get("ref"), str)
            }
            unused_previous = [
                entry for entry in previous_entries if isinstance(entry, dict)
            ]
            current_refs = set()

            for entry in current_entries:
                if not isinstance(entry, dict):
                    continue
                ref = entry.get("ref")
                if not isinstance(ref, str):
                    continue
                name = self.entry_model_name(entry)
                current_refs.add(ref)
                previous = previous_by_ref.get(ref)
                if previous is None:
                    previous = self.semantic_previous_scene_entry(
                        entry,
                        unused_previous,
                    )
                if previous is None:
                    events.append(
                        {
                            "type": "appeared",
                            "ref": ref,
                            "target_key": entry.get("target_key"),
                            "text": f"{name} appeared",
                        }
                    )
                    continue

                if previous in unused_previous:
                    unused_previous.remove(previous)
                previous_pos = self.entry_position(previous)
                current_pos = self.entry_position(entry)
                if previous_pos is None or current_pos is None:
                    continue
                if previous_pos != current_pos:
                    expected_pos = self.stationary_relative_pos_after_last_move(
                        previous_pos,
                    )
                    if expected_pos == current_pos:
                        continue
                    text = self.movement_event_text(name, previous_pos, current_pos)
                    previous_distance = self.chebyshev_distance(previous_pos)
                    current_distance = self.chebyshev_distance(current_pos)
                    if current_distance < previous_distance:
                        text += " toward the player"
                    elif current_distance > previous_distance:
                        text += " away from the player"
                    events.append(
                        {
                            "type": "moved",
                            "ref": ref,
                            "target_key": entry.get("target_key"),
                            "text": text,
                        }
                    )

            for previous_ref, previous in previous_by_ref.items():
                if previous_ref in current_refs:
                    continue
                if previous not in unused_previous:
                    continue
                previous_name = self.entry_model_name(previous)
                events.append(
                    {
                        "type": "disappeared",
                        "ref": previous_ref,
                        "target_key": previous.get("target_key"),
                        "text": f"{previous_name} is no longer visible",
                    }
                )

        for entry in scene.get("entities", []):
            if not isinstance(entry, dict):
                continue
            if self.is_ally_entry(entry):
                continue
            ref = entry.get("ref")
            pos = self.entry_position(entry)
            if not isinstance(ref, str) or pos is None:
                continue
            if self.chebyshev_distance(pos) <= 1:
                name = self.entry_model_name(entry)
                events.append(
                    {
                        "type": "threat",
                        "ref": ref,
                        "target_key": entry.get("target_key"),
                        "text": f"{name} is adjacent and threatening you",
                    }
                )

        return events

    def semantic_previous_scene_entry(
        self,
        entry: dict[str, object],
        previous_entries: list[dict[str, object]],
    ) -> dict[str, object] | None:
        """Find a previous entry by stable target key or description when refs churn."""
        target_key = entry.get("target_key")
        description = entry.get("description")
        best_entry = None
        best_distance = None
        current_pos = self.entry_position(entry)
        for previous in previous_entries:
            if not isinstance(previous, dict):
                continue
            same_target = (
                isinstance(target_key, str)
                and previous.get("target_key") == target_key
            )
            same_description = (
                isinstance(description, str)
                and previous.get("description") == description
            )
            if not same_target and not same_description:
                continue
            previous_pos = self.entry_position(previous)
            if current_pos is None or previous_pos is None:
                distance = 0
            else:
                distance = abs(current_pos[0] - previous_pos[0]) + abs(
                    current_pos[1] - previous_pos[1]
                )
            if best_distance is None or distance < best_distance:
                best_entry = previous
                best_distance = distance
        return best_entry

    def stationary_relative_pos_after_last_move(
        self,
        previous_pos: list[int],
    ) -> list[int] | None:
        """Return where a stationary object should appear after the player's move."""
        last_delta = self.last_executed_move_delta()
        if last_delta is None:
            return None
        return [
            previous_pos[0] - last_delta[0],
            previous_pos[1] - last_delta[1],
        ]

    def ongoing_hazards(self, scene: dict[str, object]) -> list[str]:
        """Produce persistent hazard facts for nearby visible monsters."""
        hazards = []
        entities = scene.get("entities")
        if not isinstance(entities, list):
            return hazards

        for entry in entities:
            if not isinstance(entry, dict):
                continue
            if self.is_ally_entry(entry):
                continue
            ref = entry.get("ref")
            pos = self.entry_position(entry)
            if not isinstance(ref, str) or pos is None:
                continue
            distance = self.chebyshev_distance(pos)
            if distance <= 2:
                name = self.entry_model_name(entry)
                steps = self.direction_sequence_for_pos(pos)
                direction_text = "-".join(steps) if steps else "here"
                hazards.append(
                    f"{name} is a nearby hostile {distance} step"
                    + ("" if distance == 1 else "s")
                    + f" away to the {direction_text}"
                )

        return hazards
