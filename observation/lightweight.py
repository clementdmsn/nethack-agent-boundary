from __future__ import annotations

from observation.constants import MONSTER_GLYPHS


class LightweightObservationMixin:
    def screen_signature(self) -> tuple[tuple[str, ...], tuple[int, int] | None]:
        """Return a compact signature for lightweight movement checks."""
        return (tuple(self.last_map_lines), self.last_player_screen_pos)

    def refresh_lightweight_visible_scene_cache(self) -> dict[str, object]:
        """Refresh map topology and obvious monster glyphs without farlook."""
        self.last_lightweight_refresh_was_full = False
        screen = self.render_screen(print_output=False)
        cursor_position = self.cursor_position()
        map_lines, viewport = self.render_map_glyph_rows(screen)
        player_position = self.resolve_player_screen_position(
            map_lines,
            viewport,
            cursor_position,
        )
        if not any("@" in row for row in map_lines):
            self.last_lightweight_refresh_was_full = True
            return self.refresh_scene_cache()
        self.last_map_lines = map_lines
        self.last_viewport = viewport
        self.last_player_screen_pos = player_position

        previous_scene = self.last_scene if isinstance(self.last_scene, dict) else {}
        scene = {
            "room_description": previous_scene.get("room_description", "Visible area."),
            "visibility": previous_scene.get("visibility", "normal"),
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {
                "identity": self.player_identity,
                "pos": [0, 0],
            },
        }
        if viewport is None or player_position is None:
            self.last_scene = scene
            return scene

        player_x, player_y = player_position
        has_room_context = self.player_has_room_boundary_context(
            map_lines,
            viewport,
            player_x,
            player_y,
        )
        scene["location_context"] = self.build_location_context(
            map_lines,
            viewport,
            player_x,
            player_y,
            visibility=str(scene.get("visibility", "normal")),
            has_room_context=has_room_context,
        )
        for local_y, row in enumerate(map_lines):
            if not row:
                continue
            for local_x, glyph in enumerate(row):
                if glyph not in MONSTER_GLYPHS:
                    continue
                screen_x = viewport.left + local_x
                pos = self.compact_position(screen_x - player_x, local_y - player_y)
                if pos == [0, 0]:
                    continue
                ally_description = self.lightweight_known_ally_description(
                    glyph,
                    pos,
                    previous_scene,
                )
                if ally_description is not None:
                    scene["entities"].append(
                        {
                            "description": ally_description,
                            "pos": pos,
                        }
                    )
                    continue
                scene["entities"].append(
                    {
                        "description": "visible monster",
                        "pos": pos,
                    }
                )

        scene = self.merge_visible_map_fallbacks(
            scene,
            map_lines,
            viewport,
            player_x,
            player_y,
        )
        self.last_scene = scene
        return scene

    def lightweight_known_ally_description(
        self,
        glyph: str,
        pos: list[int],
        previous_scene: dict[str, object],
    ) -> str | None:
        """Recover known pet identity for lightweight glyph-only observations."""
        entities = previous_scene.get("entities")
        if not isinstance(entities, list):
            return None
        for entity in entities:
            if not isinstance(entity, dict) or not self.is_ally_entry(entity):
                continue
            description = entity.get("description")
            if not isinstance(description, str):
                continue
            if not self.ally_description_matches_glyph(description, glyph):
                continue
            previous_pos = self.entry_position(entity)
            if previous_pos is None:
                return description
            expected_pos = self.stationary_relative_pos_after_last_move(previous_pos)
            if expected_pos is not None and self.chebyshev_between(pos, expected_pos) <= 1:
                return description
            if self.chebyshev_between(pos, previous_pos) <= 2:
                return description
        return None

    def ally_description_matches_glyph(self, description: str, glyph: str) -> bool:
        """Return whether a known ally description plausibly matches a map glyph."""
        lowered = description.lower()
        if glyph == "d":
            return "dog" in lowered
        if glyph == "f":
            return "kitten" in lowered or "cat" in lowered
        if glyph == "u":
            return "horse" in lowered or "pony" in lowered
        return False

    def chebyshev_between(self, left: list[int], right: list[int]) -> int:
        """Return Chebyshev distance between two compact positions."""
        return max(abs(left[0] - right[0]), abs(left[1] - right[1]))

    def current_screen_position_key(self) -> tuple[int, int] | None:
        """Return the current absolute screen position when the map parser has it."""
        pos = self.last_player_screen_pos
        if (
            isinstance(pos, tuple)
            and len(pos) == 2
            and all(isinstance(value, int) for value in pos)
        ):
            return pos
        return None
