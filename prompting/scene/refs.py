from __future__ import annotations

import re


class SceneRefsMixin:
    def slugify(self, text: str) -> str:
        """Convert arbitrary descriptions into stable ref/name fragments."""
        slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return slug or "target"

    def display_name_for_entry(self, bucket: str, entry: dict[str, object]) -> str:
        """Produce model-facing names without volatile synthetic suffixes."""
        description = entry.get("description")
        if not isinstance(description, str) or not description:
            description = self.singular_bucket_name(bucket)

        if bucket == "exits":
            direction = entry.get("direction")
            if isinstance(direction, str) and direction:
                return f"{direction} exit"
            return "exit"

        lowered = description.lower()
        if bucket == "features":
            if "door" in lowered:
                return "door"
            if "staircase" in lowered:
                if "up" in lowered:
                    return "up staircase"
                if "down" in lowered:
                    return "down staircase"
                return "staircase"

        if bucket == "items":
            return self.item_target_name(description)

        return description

    def item_target_name(self, description: str) -> str:
        """Return stable item identity for volatile item descriptions."""
        lowered = description.lower()
        item_classes = (
            ("gold", ("gold", "zorkmid")),
            ("spellbook", ("spellbook",)),
            ("scroll", ("scroll",)),
            ("potion", ("potion",)),
            ("wand", ("wand",)),
            ("amulet", ("amulet",)),
            (
                "armor",
                (
                    "armor",
                    "armour",
                    "helmet",
                    "helm",
                    "cloak",
                    "boots",
                    "shield",
                    "mail",
                    "coat",
                    "gloves",
                    "gauntlets",
                    "shoes",
                ),
            ),
            ("ring", ("ring",)),
            ("food", ("food", "ration", "tripe")),
            ("bag", ("bag",)),
            (
                "weapon",
                (
                    "weapon",
                    "sword",
                    "dagger",
                    "mace",
                    "bow",
                    "arrow",
                    "axe",
                    "spear",
                    "staff",
                    "club",
                ),
            ),
            ("gem", ("gem", "stone")),
            ("container", ("chest", "box")),
            ("corpse", ("corpse",)),
        )
        for item_class, markers in item_classes:
            if any(
                self.item_description_has_marker(lowered, marker)
                for marker in markers
            ):
                return item_class
        return description

    def item_description_has_marker(self, lowered: str, marker: str) -> bool:
        """Return whether an item class marker appears as a word or phrase."""
        pattern = r"(?<![a-z0-9])" + re.escape(marker) + r"(?![a-z0-9])"
        return re.search(pattern, lowered) is not None

    def target_key_for_entry(self, bucket: str, entry: dict[str, object]) -> str:
        """Build semantic action identity that survives ref changes."""
        description = entry.get("description")
        if not isinstance(description, str) or not description:
            description = self.singular_bucket_name(bucket)

        if bucket == "exits":
            direction = entry.get("direction")
            if isinstance(direction, str) and direction:
                return f"exit:{self.slugify(direction)}"
            return "exit:visible"

        if bucket == "entities":
            prefix = "ally" if self.is_ally_entry(entry) else "monster"
            return f"{prefix}:{self.slugify(description)}"

        if bucket == "items":
            return f"item:{self.slugify(self.item_target_name(description))}"

        if bucket == "features":
            lowered = description.lower()
            if "door" in lowered:
                pos = self.entry_position(entry)
                steps = self.direction_sequence_for_pos(pos) if pos is not None else []
                direction = steps[0] if steps else "here"
                return f"door:{direction}"
            if "staircase" in lowered:
                if "up" in lowered:
                    return "staircase:up"
                if "down" in lowered:
                    return "staircase:down"
                return "staircase:visible"
            return f"feature:{self.slugify(description)}"

        return f"{self.singular_bucket_name(bucket)}:{self.slugify(description)}"

    def next_scene_ref(self, bucket: str, entry: dict[str, object]) -> str:
        """Create a stable synthetic ref for an observed scene element."""
        if bucket == "exits":
            direction = entry.get("direction")
            if isinstance(direction, str) and direction:
                base = f"{direction}_exit"
            else:
                base = "exit"
        else:
            description = entry.get("description")
            if not isinstance(description, str) or not description:
                description = self.singular_bucket_name(bucket)
            base = self.slugify(description)

        next_index = self.scene_ref_counters.get(base, 0) + 1
        self.scene_ref_counters[base] = next_index
        return f"{base}_{next_index}"

    def scene_match_score(
        self,
        previous: dict[str, object],
        current: dict[str, object],
    ) -> tuple[int, int, int, int]:
        """Score how well two scene entries match across consecutive turns."""
        previous_description = previous.get("description")
        current_description = current.get("description")
        same_description = int(previous_description == current_description)
        if (
            not same_description
            and isinstance(previous_description, str)
            and isinstance(current_description, str)
            and self.item_target_name(previous_description)
            == self.item_target_name(current_description)
        ):
            same_description = 1
        previous_direction = previous.get("direction")
        current_direction = current.get("direction")
        same_direction = int(previous_direction == current_direction)
        previous_pos = self.entry_position(previous)
        current_pos = self.entry_position(current)
        if previous_pos is None or current_pos is None:
            return (same_description, same_direction, 0, 0)
        position_gap = abs(previous_pos[0] - current_pos[0]) + abs(
            previous_pos[1] - current_pos[1]
        )
        return (
            same_description,
            same_direction,
            -position_gap,
            -self.chebyshev_distance(current_pos),
        )

    def attach_refs_to_bucket(
        self,
        bucket: str,
        entries: list[dict[str, object]],
        previous_entries: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        """Carry refs forward across turns by greedily matching nearby entries."""
        normalized = []
        unused_previous = [
            entry for entry in previous_entries if isinstance(entry, dict)
        ]

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            copy = dict(entry)
            best_index = None
            best_score = None

            for index, previous in enumerate(unused_previous):
                score = self.scene_match_score(previous, copy)
                if best_score is None or score > best_score:
                    best_score = score
                    best_index = index

            if (
                best_index is not None
                and best_score is not None
                and best_score[0] > 0
                and best_score[2] >= -2
            ):
                previous = unused_previous.pop(best_index)
                ref = previous.get("ref")
                if isinstance(ref, str) and ref:
                    copy["ref"] = ref
                else:
                    copy["ref"] = self.next_scene_ref(bucket, copy)
            else:
                copy["ref"] = self.next_scene_ref(bucket, copy)

            copy["target_key"] = self.target_key_for_entry(bucket, copy)
            copy["display_name"] = self.display_name_for_entry(bucket, copy)
            if bucket == "items":
                copy["item_class"] = self.item_target_name(
                    str(copy.get("description", ""))
                )
            normalized.append(copy)

        return normalized

    def copy_scene_with_refs(self, scene: dict[str, object]) -> dict[str, object]:
        """Clone the observed scene and attach stable refs to actionable entries."""
        previous_scene = self.last_scene if isinstance(self.last_scene, dict) else {}
        normalized: dict[str, object] = {
            "room_description": scene.get("room_description"),
            "visibility": scene.get("visibility", "normal"),
            "location_context": scene.get("location_context"),
            "player": scene.get("player"),
        }

        for bucket in self.ACTION_BUCKETS:
            entries = scene.get(bucket, [])
            if not isinstance(entries, list):
                entries = []
            previous_entries = previous_scene.get(bucket, [])
            if not isinstance(previous_entries, list):
                previous_entries = []
            normalized[bucket] = self.attach_refs_to_bucket(
                bucket,
                entries,
                previous_entries,
            )

        elements = scene.get("elements")
        if isinstance(elements, list):
            normalized["elements"] = [
                dict(entry) for entry in elements if isinstance(entry, dict)
            ]

        return normalized
