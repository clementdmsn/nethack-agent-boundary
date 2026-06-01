from __future__ import annotations

from collections import OrderedDict

from observation.constants import (
    END_FLAG,
    ESCAPE_KEY,
    FARLOOK_ACTIVE_MARKERS,
    FARLOOK_KEY,
    FARLOOK_NOISE_PREFIXES,
    GENERIC_TERRAIN_DESCRIPTIONS,
    ITEM_GLYPHS,
    LOOK_DESCRIPTION_PATTERN,
    MONSTER_GLYPHS,
    MORE_FLAG,
    STATUS_PATTERN,
)
from observation.scene_builder import (
    fallback_feature_description,
    feature_positions,
    matching_here_description_entries,
)


class ObservationSessionMixin:
    PICKUP_DESCRIPTION_WORDS = (
        "amulet",
        "armor",
        "bag",
        "book",
        "boots",
        "chest",
        "cloak",
        "coin",
        "coins",
        "food",
        "gem",
        "gold",
        "helmet",
        "potion",
        "ring",
        "scroll",
        "shield",
        "spellbook",
        "wand",
        "weapon",
    )

    def scene_visibility(self, room_description: str) -> str:
        # Classifies coarse visibility for cases like dark rooms.
        if "can't guess the size of this area" in room_description.lower():
            return "dark"
        return "normal"

    def description_is_pickup_item(self, description: str) -> bool:
        """Return whether farlook text names a pickup-able object."""
        normalized = description.lower()
        return any(word in normalized for word in self.PICKUP_DESCRIPTION_WORDS)

    def classify_scene_bucket(self, target: ObservationTarget, description: str) -> str:
        # Classifies one observed element into a stability-oriented scene bucket.
        normalized = description.lower()
        if "unexplored area" in normalized:
            return "areas"
        if "statue" in normalized or self.description_is_pickup_item(description):
            return "items"
        if target.glyph in MONSTER_GLYPHS or "tame " in normalized:
            return "entities"
        if target.glyph in ITEM_GLYPHS:
            return "items"
        return "features"

    def consume_scene_pages(self) -> None:
        # Advances through pending --More-- pages before starting observation.
        while True:
            screen = self.render_screen(print_output=False)
            if not self.screen_has_more(screen):
                break

            self.terminal.send_keys("\n")

    def begin_farlook(self) -> str:
        # Enters farlook mode and clears any initial help or paging text.
        self.terminal.send_keys(FARLOOK_KEY)
        return self.settle_farlook_screen()

    def farlook_is_active(self, screen: str) -> bool:
        """Return whether it is safe to send farlook cursor movement keys."""
        return any(marker in screen for marker in FARLOOK_ACTIVE_MARKERS) or (
            self.extract_current_look_description(screen) is not None
        )

    def end_farlook(self) -> None:
        # Exits farlook mode and refreshes the visible game screen.
        screen = self.render_screen(print_output=False)
        for _attempt in range(3):
            if not self.farlook_is_active(screen):
                return
            self.terminal.send_keys(ESCAPE_KEY)
            screen = self.render_screen(print_output=False)

    def cursor_position(self) -> tuple[int, int] | None:
        # Reads the current farlook cursor position from the terminal backend.
        get_cursor_position = getattr(self.terminal, "cursor_position", None)
        if get_cursor_position is None:
            return None
        return get_cursor_position()

    def settle_farlook_screen(self) -> str:
        # Advances transient farlook screens until a stable description is visible.
        screen = self.render_screen(print_output=False)

        while self.screen_has_more(screen) or self.screen_has_tutorial_end(
            screen
        ):
            self.terminal.send_keys("\n")
            screen = self.render_screen(print_output=False)

        return screen

    def look_description_lines(self, screen: str) -> list[str]:
        # Extracts the active farlook description lines from the rendered screen.
        lines = []
        for line in screen.splitlines():
            cleaned = self.clean_log_line(line.strip())
            if not cleaned:
                continue
            if STATUS_PATTERN.search(cleaned) or "NetHack-" in cleaned:
                continue
            if cleaned.startswith(FARLOOK_NOISE_PREFIXES):
                continue
            if LOOK_DESCRIPTION_PATTERN.search(cleaned):
                lines.append(cleaned)
        return lines

    def extract_current_look_description(
        self, screen: str
    ) -> tuple[str, str] | None:
        # Parses the current farlook description and its relative location label.
        lines = self.look_description_lines(screen)
        if not lines:
            return None

        match = LOOK_DESCRIPTION_PATTERN.search(lines[-1])
        if match is None:
            return None

        return (
            match.group("description").strip().rstrip("."),
            match.group("location").strip(),
        )

    def cursor_path(self, dx: int, dy: int) -> str:
        # Converts a cursor delta into a compact NetHack movement key sequence.
        diagonals = []
        vertical = ""
        horizontal = ""

        diagonal_steps = min(abs(dx), abs(dy))
        if diagonal_steps:
            if dy < 0 and dx < 0:
                diagonals.append("y" * diagonal_steps)
            elif dy < 0 and dx > 0:
                diagonals.append("u" * diagonal_steps)
            elif dy > 0 and dx < 0:
                diagonals.append("b" * diagonal_steps)
            else:
                diagonals.append("n" * diagonal_steps)

        remaining_dy = abs(dy) - diagonal_steps
        remaining_dx = abs(dx) - diagonal_steps

        if remaining_dy:
            vertical = ("k" if dy < 0 else "j") * remaining_dy
        if remaining_dx:
            horizontal = ("h" if dx < 0 else "l") * remaining_dx

        return "".join(diagonals) + vertical + horizontal

    def move_farlook_cursor(
        self,
        current_x: int,
        current_y: int,
        target_x: int,
        target_y: int,
    ) -> str:
        # Moves the farlook cursor to a target tile and settles the resulting screen.
        screen = self.render_screen(print_output=False)
        if not self.farlook_is_active(screen):
            return screen

        keys = self.cursor_path(target_x - current_x, target_y - current_y)
        if keys:
            self.terminal.send_keys(keys)
        return self.settle_farlook_screen()

    def should_keep_element_description(self, description: str) -> bool:
        # Filters out generic terrain descriptions that add little scene value.
        normalized = description.strip().rstrip(".").lower()
        return normalized not in GENERIC_TERRAIN_DESCRIPTIONS

    def build_scene_from_observations(
        self,
        room_description: str,
        observations: list[tuple[ObservationTarget, str]],
        exits: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        # Compacts farlook observations into the current scene JSON schema.
        grouped_positions: OrderedDict[
            tuple[str, str], list[list[int]]
        ] = OrderedDict()
        exit_entries = exits or []

        for target, description in observations:
            if target.dx == 0 and target.dy == 0:
                continue
            if not self.should_keep_element_description(description):
                continue

            bucket = self.classify_scene_bucket(target, description)
            positions = grouped_positions.setdefault((bucket, description), [])
            position = self.compact_position(target.dx, target.dy)
            if position in positions:
                continue
            positions.append(position)

        scene = {
            "room_description": room_description,
            "visibility": self.scene_visibility(room_description),
            "observations": [
                {
                    "description": description,
                    "glyph": target.glyph,
                    "pos": self.compact_position(target.dx, target.dy),
                }
                for target, description in observations
                if target.dx != 0 or target.dy != 0
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": exit_entries,
            "player": {
                "identity": self.player_identity,
                "pos": [0, 0],
            },
        }

        for (bucket, description), positions in grouped_positions.items():
            if len(positions) == 1:
                scene[bucket].append(
                    {
                        "description": description,
                        "pos": positions[0],
                    }
                )
                continue

            compact_description = (
                "unexplored areas"
                if description == "unexplored area"
                else description
            )
            scene[bucket].append(
                {
                    "description": compact_description,
                    "positions": positions,
                }
            )

        return scene

    def merge_visible_map_fallbacks(
        self,
        scene: dict[str, object],
        map_lines: list[str],
        viewport: MapViewport | None,
        player_x: int,
        player_y: int,
    ) -> dict[str, object]:
        # Adds minimal local structure from visible glyphs when farlook semantics are sparse.
        if viewport is None:
            return scene

        features = scene.get("features")
        if not isinstance(features, list):
            return scene
        seen_feature_positions = feature_positions(scene)
        component = self.visible_component_positions(
            map_lines,
            viewport,
            player_x,
            player_y,
        )

        for local_y, row in enumerate(map_lines):
            if not row:
                continue
            for local_x, glyph in enumerate(row):
                if component and not self.pos_touches_component(
                    (local_x, local_y),
                    component,
                ):
                    continue
                screen_x = viewport.left + local_x
                screen_y = local_y
                pos = self.compact_position(screen_x - player_x, screen_y - player_y)
                pos_key = (pos[0], pos[1])
                if pos_key in seen_feature_positions:
                    continue
                description = fallback_feature_description(
                    map_lines,
                    local_x,
                    local_y,
                    glyph,
                )
                if description is None:
                    continue
                features.append({"description": description, "pos": pos})
                seen_feature_positions.add(pos_key)

        return scene

    def description_here_contradicted(self, scene: dict[str, object]) -> bool:
        # Detects stale location-specific messages contradicted by observed positions.
        room_description = scene.get("room_description")
        if not isinstance(room_description, str):
            return False

        normalized = room_description.lower()
        if " here" not in normalized or not normalized.startswith("there "):
            return False

        matching_entries = matching_here_description_entries(scene, normalized)
        if not matching_entries:
            return False

        return all(entry.get("pos") != [0, 0] for entry in matching_entries)

    def reconcile_room_description(self, scene: dict[str, object]) -> dict[str, object]:
        # Avoids carrying stale "here" messages when glyph observations disagree.
        if self.description_here_contradicted(scene):
            scene = dict(scene)
            scene["room_description"] = "Visible area."
            return scene
        context = scene.get("location_context")
        room_description = scene.get("room_description")
        if (
            isinstance(context, dict)
            and context.get("in_room") is not True
            and isinstance(room_description, str)
            and " here" in room_description.lower()
        ):
            scene = dict(scene)
            scene["room_description"] = "Visible area."
        return scene

    def inspect_farlook_targets(
        self,
        initial_screen: str,
        player_x: int,
        player_y: int,
    ) -> list[tuple[ObservationTarget, str]]:
        # Visits each chosen farlook target and records its resolved description.
        if not self.farlook_is_active(initial_screen):
            return []

        current_x = player_x
        current_y = player_y
        targets = self.scan_observation_targets(
            initial_screen,
            current_x,
            current_y,
        )
        observations = []

        for target in targets:
            screen = self.move_farlook_cursor(
                current_x,
                current_y,
                target.screen_x,
                target.screen_y,
            )
            current_x = target.screen_x
            current_y = target.screen_y
            observation = self.extract_current_look_description(screen)
            if observation is None:
                continue
            description, _location = observation
            observations.append((target, description))

        return observations

    def resolve_room_description(self) -> str:
        # Chooses the best available room description for the current scene.
        room_description = self.parse_room_description(
            [self.clean_message_line(line) for line in self.last_text_log]
        )
        if room_description:
            return room_description

        if self.last_scene is not None:
            previous = self.last_scene.get("room_description")
            if isinstance(previous, str):
                return previous

        return ""

    def look(self) -> dict[str, object]:
        # Runs the full observation pipeline and returns one compact scene snapshot.
        self.update_player_identity_from_lines(
            [self.clean_message_line(line) for line in self.last_text_log]
        )
        self.reset_observation_log()
        self.consume_scene_pages()
        self.update_player_identity_from_lines(
            [self.clean_message_line(line) for line in self.last_text_log]
        )
        room_description = self.resolve_room_description()
        previous_capture_prompted_text = self.capture_prompted_text
        self.capture_prompted_text = False
        try:
            screen = self.begin_farlook()
            cursor_position = self.cursor_position()
            map_lines, viewport = self.render_map_glyph_rows(screen)
            player_position = self.resolve_player_screen_position(
                map_lines,
                viewport,
                cursor_position,
            )
            self.last_map_lines = map_lines
            self.last_viewport = viewport
            self.last_player_screen_pos = player_position
            visibility = self.scene_visibility(room_description)
            has_room_context = self.player_has_room_boundary_context(
                map_lines,
                viewport,
                player_position[0] if player_position is not None else 0,
                player_position[1] if player_position is not None else 0,
            )
            if (
                not has_room_context
                and isinstance(room_description, str)
                and (
                    room_description.startswith("You are in ")
                    or " here" in room_description.lower()
                )
            ):
                room_description = "Visible area."
            exits = []
            if visibility != "dark" and has_room_context:
                exits = self.find_room_exits(
                    map_lines,
                    viewport,
                    player_position[0] if player_position is not None else 0,
                    player_position[1] if player_position is not None else 0,
                )
            if player_position is None:
                scene = self.build_legacy_scene_from_text_log()
                self.last_scene = scene
                return scene

            observations = self.inspect_farlook_targets(
                screen,
                player_position[0],
                player_position[1],
            )
            scene = self.build_scene_from_observations(
                room_description,
                observations,
                exits=exits,
            )
            scene["location_context"] = self.build_location_context(
                map_lines,
                viewport,
                player_position[0],
                player_position[1],
                visibility=scene.get("visibility", "normal"),
                has_room_context=has_room_context,
            )
            scene = self.merge_visible_map_fallbacks(
                scene,
                map_lines,
                viewport,
                player_position[0],
                player_position[1],
            )
            scene = self.reconcile_room_description(scene)

            self.last_scene = scene

            return scene
        finally:
            self.end_farlook()
            self.ensure_normal_game_mode_before_action()
            cleanup_screen = self.render_screen(print_output=False)
            farlook_active = self.farlook_is_active(cleanup_screen)
            self.last_observation_cleanup = {
                "farlook_used": True,
                "returned_to_normal_mode": not farlook_active,
                "terminal_mode_after_cleanup": (
                    "farlook_or_targeting" if farlook_active else "normal"
                ),
            }
            self.capture_prompted_text = previous_capture_prompted_text

    def refresh_scene_cache(self) -> dict[str, object]:
        # Re-observes the current game state and updates the cached scene.
        scene = self.look()
        self.last_scene = scene
        return scene
