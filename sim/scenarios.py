from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class SpawnEvent:
    turn: int
    x: int
    y: int
    glyph: str
    message: str = ""


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    map_text: str
    initial_active_action: dict[str, object] | None = None
    spawn_events: list[SpawnEvent] = field(default_factory=list)


SCENARIOS: dict[str, Scenario] = {
    "corridor_dead_end": Scenario(
        name="corridor_dead_end",
        description="Follow a corridor into a dead end, then backtrack.",
        map_text="""
@#####
""",
    ),
    "corridor_turn": Scenario(
        name="corridor_turn",
        description="Follow a corridor through a bend.",
        map_text="""
@##
  #
  #
""",
    ),
    "corridor_intersection": Scenario(
        name="corridor_intersection",
        description="Stop when a corridor reaches a T-junction.",
        map_text="""
  #
@##
  #
""",
    ),
    "corridor_room_entrance": Scenario(
        name="corridor_room_entrance",
        description="Enter a newly revealed room, then hand control back for room analysis.",
        map_text="""
----- 
|.<$| 
|...# 
  @   
  #   
""",
        initial_active_action={
            "action_id": "explore_corridor:north",
            "action_type": "explore_corridor",
            "label": "Follow corridor north",
            "target_ref": None,
            "target_key": "corridor:north",
            "procedure_kind": "dynamic",
            "low_level_goal": "follow the corridor north",
            "next_action": "follow_corridor(north)",
            "path_steps": ["north"],
            "distance_steps": 1,
            "interruptible": True,
        },
    ),
    "closed_door_adjacent": Scenario(
        name="closed_door_adjacent",
        description="Open an adjacent closed door.",
        map_text="""
-----
|.@+.
-----
""",
    ),
    "adjacent_hostile": Scenario(
        name="adjacent_hostile",
        description="Prefer fleeing when a hostile is adjacent.",
        map_text="""
.....
.o@..
.....
""",
    ),
    "distant_hostile": Scenario(
        name="distant_hostile",
        description="Do not re-engage a visible monster once safe distance exists.",
        map_text="""
o....
.....
....@
""",
    ),
    "pet_in_corridor": Scenario(
        name="pet_in_corridor",
        description="Do not treat a pet as a hostile while navigating.",
        map_text="""
@d##
""",
    ),
    "distant_visible_item": Scenario(
        name="distant_visible_item",
        description="Visible distant room facts should not dominate corridor context.",
        map_text="""
@#
 #
 #
 ---.--
 |.$..|
 -----
""",
    ),
    "hostile_interrupts_pickup": Scenario(
        name="hostile_interrupts_pickup",
        description=(
            "Start a pickup procedure, reveal an adjacent hostile, and switch to flee."
        ),
        map_text="""
@$...
.....
.....
""",
        spawn_events=[
            SpawnEvent(
                turn=1,
                x=2,
                y=0,
                glyph="o",
                message="A goblin steps into view.",
            )
        ],
    ),
}


def scenario_names() -> list[str]:
    return sorted(SCENARIOS)


def get_scenario(name: str) -> Scenario:
    return SCENARIOS[name]
