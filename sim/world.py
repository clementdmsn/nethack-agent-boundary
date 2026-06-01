from __future__ import annotations

from dataclasses import dataclass, field

from app.runner import MapViewport
from sim.scenarios import SpawnEvent


DIRECTION_DELTAS = {
    "h": (-1, 0),
    "j": (0, 1),
    "k": (0, -1),
    "l": (1, 0),
    "y": (-1, -1),
    "u": (1, -1),
    "b": (-1, 1),
    "n": (1, 1),
}

DIRECTION_NAMES = {
    "h": "west",
    "j": "south",
    "k": "north",
    "l": "east",
    "y": "northwest",
    "u": "northeast",
    "b": "southwest",
    "n": "southeast",
}

PASSABLE = {".", "#", "<", ">", "$", "[", "d", "f", "u", "o", "j"}
ITEM_DESCRIPTIONS = {
    "$": "3 gold pieces",
    "[": "an elven mithril-coat",
}
PET_DESCRIPTIONS = {
    "d": "tame little dog called Doggo",
    "f": "tame kitten called Kitty",
    "u": "tame saddled pony called Horse",
}
MONSTER_DESCRIPTIONS = {
    "o": "goblin",
    "j": "jackal",
}


@dataclass
class SimWorld:
    """Small deterministic grid world that mimics the agent's needed facts."""

    rows: list[list[str]]
    player: tuple[int, int]
    under_player: str = "."
    turn: int = 0
    messages: list[str] = field(default_factory=list)
    pet_push_attempts: dict[tuple[int, int], int] = field(default_factory=dict)
    spawn_events: list[SpawnEvent] = field(default_factory=list)
    applied_spawn_turns: set[int] = field(default_factory=set)

    @classmethod
    def from_text(
        cls,
        text: str,
        *,
        spawn_events: list[SpawnEvent] | None = None,
    ) -> "SimWorld":
        lines = [line.rstrip("\n") for line in text.strip("\n").splitlines()]
        width = max((len(line) for line in lines), default=0)
        rows: list[list[str]] = []
        player = (0, 0)
        for y, line in enumerate(lines):
            row = list(line.ljust(width))
            for x, glyph in enumerate(row):
                if glyph == "@":
                    player = (x, y)
                    row[x] = "."
            rows.append(row)
        under_player = (
            "#"
            if any(
                0 <= player[1] + dy < len(rows)
                and 0 <= player[0] + dx < len(rows[player[1] + dy])
                and rows[player[1] + dy][player[0] + dx] == "#"
                for dx, dy in DIRECTION_DELTAS.values()
            )
            else "."
        )
        rows[player[1]][player[0]] = under_player
        return cls(
            rows=rows,
            player=player,
            under_player=under_player,
            spawn_events=list(spawn_events or []),
        )

    @property
    def width(self) -> int:
        return max((len(row) for row in self.rows), default=0)

    @property
    def height(self) -> int:
        return len(self.rows)

    def glyph_at(self, x: int, y: int) -> str:
        if y < 0 or y >= self.height or x < 0 or x >= len(self.rows[y]):
            return " "
        if (x, y) == self.player:
            return "@"
        return self.rows[y][x]

    def set_glyph(self, x: int, y: int, glyph: str) -> None:
        if 0 <= y < self.height and 0 <= x < len(self.rows[y]):
            self.rows[y][x] = glyph

    def map_lines(self) -> list[str]:
        lines = []
        for y, row in enumerate(self.rows):
            rendered = []
            for x, glyph in enumerate(row):
                rendered.append("@" if (x, y) == self.player else glyph)
            lines.append("".join(rendered).rstrip())
        return lines

    def render(self) -> str:
        lines = []
        if self.messages:
            lines.append(self.messages[-1])
        lines.extend(self.map_lines())
        return "\n".join(lines)

    def viewport(self) -> MapViewport:
        return MapViewport(
            top=0,
            bottom=max(0, self.height - 1),
            left=0,
            right=max(0, self.width - 1),
            overlay_rows=frozenset(),
        )

    def passable(self, glyph: str) -> bool:
        return glyph in PASSABLE

    def send_keys(self, keys: str) -> None:
        index = 0
        while index < len(keys):
            key = keys[index]
            if key == ",":
                self.pickup()
                index += 1
                continue
            if key == "o" and index + 1 < len(keys):
                self.open_door(keys[index + 1])
                index += 2
                continue
            self.move_key(key)
            index += 1
        self.turn += 1
        self.apply_spawn_events()

    def apply_spawn_events(self) -> None:
        for index, event in enumerate(self.spawn_events):
            if event.turn != self.turn or index in self.applied_spawn_turns:
                continue
            if (event.x, event.y) == self.player:
                continue
            if self.glyph_at(event.x, event.y) == " ":
                continue
            self.set_glyph(event.x, event.y, event.glyph)
            if event.message:
                self.messages.append(event.message)
            self.applied_spawn_turns.add(index)

    def pickup(self) -> None:
        if self.under_player not in ITEM_DESCRIPTIONS:
            self.messages.append("There is nothing here to pick up.")
            return
        x, y = self.player
        description = ITEM_DESCRIPTIONS[self.under_player]
        self.under_player = "."
        self.set_glyph(x, y, ".")
        self.messages.append(f"You pick up {description}.")

    def move_key(self, key: str) -> None:
        delta = DIRECTION_DELTAS.get(key)
        if delta is None:
            return
        x, y = self.player
        nx, ny = x + delta[0], y + delta[1]
        target = self.glyph_at(nx, ny)
        if target == "+":
            self.messages.append("The door is closed.")
            return
        if target in PET_DESCRIPTIONS:
            attempts = self.pet_push_attempts.get((nx, ny), 0) + 1
            self.pet_push_attempts[(nx, ny)] = attempts
            if attempts < 2:
                self.messages.append("The pet blocks your way.")
                return
            old_x, old_y = self.player
            pet_glyph = target
            old_under = self.under_player
            self.set_glyph(nx, ny, old_under)
            self.set_glyph(old_x, old_y, pet_glyph)
            self.player = (nx, ny)
            self.under_player = old_under
            self.pet_push_attempts.clear()
            self.messages.append("You swap places with your pet.")
            return
        if not self.passable(target):
            self.messages.append("You cannot move there.")
            return
        self.player = (nx, ny)
        self.under_player = target

    def open_door(self, key: str) -> None:
        delta = DIRECTION_DELTAS.get(key)
        if delta is None:
            return
        x, y = self.player
        nx, ny = x + delta[0], y + delta[1]
        if self.glyph_at(nx, ny) == "+":
            self.set_glyph(nx, ny, ".")
            self.messages.append("The door opens.")
        else:
            self.messages.append("No door there.")

    def relative_pos(self, x: int, y: int) -> list[int]:
        px, py = self.player
        return [x - px, y - py]

    def location_context(self) -> dict[str, object]:
        x, y = self.player
        adjacent_corridors = []
        adjacent_doors = []
        for key, delta in DIRECTION_DELTAS.items():
            glyph = self.glyph_at(x + delta[0], y + delta[1])
            if glyph == "#":
                adjacent_corridors.append(DIRECTION_NAMES[key])
            elif glyph == "+":
                adjacent_doors.append(DIRECTION_NAMES[key])
        current = self.under_player if self.glyph_at(x, y) == "@" else self.glyph_at(x, y)
        in_corridor = current == "#"
        in_room = current == "." and not in_corridor
        area_type = "corridor" if in_corridor else "room" if in_room else "visible_area"
        return {
            "area_type": area_type,
            "in_corridor": in_corridor,
            "in_room": in_room,
            "dark": False,
            "adjacent_corridors": adjacent_corridors,
            "adjacent_doors": adjacent_doors,
            "in_front_of_door": bool(adjacent_doors),
        }

    def scene(self) -> dict[str, object]:
        features = []
        items = []
        entities = []
        if self.under_player in ITEM_DESCRIPTIONS:
            items.append(
                {
                    "description": ITEM_DESCRIPTIONS[self.under_player],
                    "pos": [0, 0],
                }
            )
        for y, row in enumerate(self.rows):
            for x, glyph in enumerate(row):
                if (x, y) == self.player:
                    continue
                pos = self.relative_pos(x, y)
                if glyph == "+":
                    features.append({"description": "closed door", "pos": pos})
                elif glyph == "<":
                    features.append({"description": "branch staircase up", "pos": pos})
                elif glyph == ">":
                    features.append({"description": "staircase down", "pos": pos})
                elif glyph in ITEM_DESCRIPTIONS:
                    items.append({"description": ITEM_DESCRIPTIONS[glyph], "pos": pos})
                elif glyph in PET_DESCRIPTIONS:
                    entities.append({"description": PET_DESCRIPTIONS[glyph], "pos": pos})
                elif glyph in MONSTER_DESCRIPTIONS:
                    entities.append({"description": MONSTER_DESCRIPTIONS[glyph], "pos": pos})
        return {
            "room_description": "Simulated area.",
            "visibility": "normal",
            "location_context": self.location_context(),
            "player": {"identity": "simulated adventurer", "pos": [0, 0]},
            "exits": [],
            "items": items,
            "entities": entities,
            "features": features,
            "areas": [],
        }


class SimTerminal:
    """Terminal facade used by the existing runtime against SimWorld."""

    def __init__(self, world: SimWorld) -> None:
        self.world = world
        self.sent_keys: list[str] = []

    def render(self) -> str:
        return self.world.render()

    def send_keys(self, keys: str) -> None:
        self.sent_keys.append(keys)
        self.world.send_keys(keys)

    def cursor_position(self) -> tuple[int, int]:
        return self.world.player

    def resize(self, _rows: int, _cols: int) -> None:
        return None
