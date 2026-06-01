from __future__ import annotations

from observation.constants import (
    DARK_ROOM_PATTERN,
    ENTITY_DESCRIPTION_FRAGMENT_PATTERN,
    ITEM_DESCRIPTION_FRAGMENT_PATTERN,
    PLAYER_HERE_PATTERN,
    RELATIVE_ELEMENT_PATTERN,
    RELATIVE_PART_PATTERN,
    ROOM_DESCRIPTION_PATTERN,
    STARTUP_IDENTITY_PATTERN,
    STATUS_IDENTITY_PATTERN,
    TRANSIENT_MESSAGE_PREFIXES,
    TRANSIENT_MESSAGE_PATTERN,
)


class LegacyObservationMixin:
    STATUS_TITLE_ROLE_MAP = {
        "aspirant": "priest",
        "candidate": "monk",
        "digger": "archeologist",
        "evoker": "wizard",
        "footpad": "rogue",
        "gallant": "knight",
        "hatamoto": "samurai",
        "plunderer": "barbarian",
        "plunderess": "barbarian",
        "rambler": "tourist",
        "rhizotomist": "healer",
        "stripling": "valkyrie",
        "tenderfoot": "ranger",
        "troglodyte": "caveman",
    }

    def parse_startup_identity(self, line: str) -> str | None:
        # Converts the new-game greeting into the normalized player identity string.
        startup_match = STARTUP_IDENTITY_PATTERN.search(line)
        if startup_match is None:
            return None

        name = startup_match.group("name").strip().lower()
        role_text = startup_match.group("role_text").strip().lower()

        ignored_words = {"lawful", "neutral", "chaotic", "male", "female"}
        role_parts = [
            part
            for part in role_text.split()
            if part and part not in ignored_words
        ]
        if not role_parts:
            return None

        role = role_parts[-1]
        race = " ".join(role_parts[:-1]).strip()
        if race:
            return f"{race} {role} called {name}"
        return f"{role} called {name}"

    def parse_status_identity(self, line: str) -> str | None:
        # Converts a status/title row into a normalized player identity string.
        status_match = STATUS_IDENTITY_PATTERN.search(line.strip())
        if status_match is None:
            return None

        raw_name = status_match.group("name").strip()
        name = raw_name.lower()
        title = status_match.group("title").strip().lower()
        role = self.STATUS_TITLE_ROLE_MAP.get(title, title)
        if not role:
            return None
        return f"{role} called {name}"

    def update_player_identity_from_lines(self, lines: list[str]) -> None:
        # Updates cached player identity from legacy startup/message lines when present.
        player = self.parse_player(lines)
        description = player.get("description")
        if isinstance(description, str) and description:
            self.player_identity = description

    def update_player_identity_from_screen(self, screen: str) -> None:
        # Updates cached player identity from visible status/title rows when present.
        for line in screen.splitlines():
            description = self.parse_status_identity(line)
            if description:
                self.player_identity = description
                return

    def parse_relative_location(self, location: str) -> dict[str, object]:
        # Parses legacy textual directions like "2north,7east" into offsets.
        location = location.strip()

        if location == "here":
            return {
                "text": location,
                "dx": 0,
                "dy": 0,
                "parts": [],
            }

        dx = 0
        dy = 0
        parts = []

        for match in RELATIVE_PART_PATTERN.finditer(location):
            direction = match.group("direction")
            count = int(match.group("count") or "1")

            if direction == "north":
                dy -= count
            elif direction == "south":
                dy += count
            elif direction == "east":
                dx += count
            elif direction == "west":
                dx -= count

            parts.append(
                {
                    "direction": direction,
                    "distance": count,
                }
            )

        return {
            "text": location,
            "dx": dx,
            "dy": dy,
            "parts": parts,
        }

    def format_relative_location(self, dx: int, dy: int) -> dict[str, object]:
        # Formats offsets back into the legacy relative-location structure.
        if dx == 0 and dy == 0:
            return {
                "text": "here",
                "dx": 0,
                "dy": 0,
                "parts": [],
            }

        parts = []
        text_parts = []

        if dy:
            direction = "north" if dy < 0 else "south"
            distance = abs(dy)
            parts.append({"direction": direction, "distance": distance})
            text_parts.append(
                f"{distance}{direction}" if distance != 1 else direction
            )

        if dx:
            direction = "west" if dx < 0 else "east"
            distance = abs(dx)
            parts.append({"direction": direction, "distance": distance})
            text_parts.append(
                f"{distance}{direction}" if distance != 1 else direction
            )

        return {
            "text": ",".join(text_parts),
            "dx": dx,
            "dy": dy,
            "parts": parts,
        }

    def compact_position(self, dx: int, dy: int) -> list[int]:
        # Converts offsets into the compact coordinate form used in the scene.
        return [dx, dy]

    def parse_scene_description(self, lines: list[str]) -> dict[str, object]:
        # Rebuilds a scene from the older text-log-based observation format.
        lines = [self.clean_message_line(line) for line in lines]
        self.update_player_identity_from_lines(lines)

        return {
            "room_description": self.parse_room_description(lines),
            "elements": self.parse_elements(lines),
        }

    def build_legacy_scene_from_text_log(self) -> dict[str, object]:
        # Builds a fallback scene using only the recorded text log.
        return self.parse_scene_description(self.last_text_log)

    def parse_player(self, lines: list[str]) -> dict[str, object]:
        # Extracts the player identity line from the legacy text log.
        for line in lines:
            player_match = PLAYER_HERE_PATTERN.search(line)
            if player_match:
                return {
                    "description": player_match.group("description").strip(),
                }

        for line in lines:
            startup_identity = self.parse_startup_identity(line)
            if startup_identity:
                return {"description": startup_identity}

        return {}

    def parse_elements(self, lines: list[str]) -> list[dict[str, object]]:
        # Extracts legacy described elements and deduplicates them by text/location.
        elements = []
        seen_elements = set()

        for line in lines:
            for match in RELATIVE_ELEMENT_PATTERN.finditer(line):
                description = self.clean_message_line(
                    match.group("text")
                ).rstrip(".")
                location = self.parse_relative_location(match.group("location"))
                key = ("relative", location["text"], description)
                if key in seen_elements:
                    continue
                seen_elements.add(key)
                elements.append(
                    {
                        "description": description,
                        "relative": location,
                    }
                )

        return elements

    def parse_room_description(self, lines: list[str]) -> str:
        # Extracts or synthesizes the room description from text-log lines.
        text = " ".join(lines)
        room_match = ROOM_DESCRIPTION_PATTERN.search(text)
        if room_match:
            return room_match.group(0)
        dark_match = DARK_ROOM_PATTERN.search(text)
        if dark_match:
            return dark_match.group(0)

        room_lines = []
        for line in lines:
            if self.is_transient_message_line(line):
                continue
            if self.is_entity_description_fragment(line):
                continue
            if self.is_item_description_fragment(line):
                continue
            if PLAYER_HERE_PATTERN.search(line):
                continue
            if STARTUP_IDENTITY_PATTERN.search(line):
                continue
            cleaned = RELATIVE_ELEMENT_PATTERN.sub("", line).strip()
            if self.is_entity_description_fragment(cleaned):
                continue
            if self.is_item_description_fragment(cleaned):
                continue
            room_lines.append(cleaned)

        return " ".join(" ".join(room_lines).split()).strip()

    def is_transient_message_line(self, line: str) -> bool:
        """Return whether a prompted line describes an event, not the room."""
        cleaned = line.strip()
        return cleaned.startswith(TRANSIENT_MESSAGE_PREFIXES) or bool(
            TRANSIENT_MESSAGE_PATTERN.search(cleaned)
        )

    def is_entity_description_fragment(self, line: str) -> bool:
        """Return whether a line is farlook entity text, not a room."""
        cleaned = line.strip().rstrip(".")
        if not cleaned:
            return False
        return bool(ENTITY_DESCRIPTION_FRAGMENT_PATTERN.search(cleaned))

    def is_item_description_fragment(self, line: str) -> bool:
        """Return whether a line is farlook item text, not a room."""
        cleaned = line.strip().rstrip(".")
        if not cleaned:
            return False
        return bool(ITEM_DESCRIPTION_FRAGMENT_PATTERN.search(cleaned))
