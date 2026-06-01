from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Callable

from sim.runner import SimRunResult, run_scenario
from sim.scenarios import get_scenario


Check = Callable[[SimRunResult], None]


@dataclass(frozen=True)
class SimCase:
    name: str
    scenario: str
    steps: int
    check: Check


def action_ids(result: SimRunResult, step: int = 0) -> set[str]:
    return {
        action["action_id"]
        for action in result.steps[step].available_actions
        if isinstance(action.get("action_id"), str)
    }


def assert_equal(left: object, right: object, label: str) -> None:
    if left != right:
        raise AssertionError(f"{label}: expected {right!r}, got {left!r}")


def assert_true(value: bool, label: str) -> None:
    if not value:
        raise AssertionError(label)


def check_corridor_dead_end(result: SimRunResult) -> None:
    assert_equal(result.steps[0].outcome["status"], "corridor_follow_end", "follow")
    assert_equal(result.steps[1].selected_action_id, "backtrack:corridor", "backtrack")
    assert_equal(
        result.steps[1].outcome["status"],
        "corridor_backtrack_completed",
        "backtrack status",
    )
    assert_equal(
        result.steps[2].outcome["status"],
        "sim_no_available_actions",
        "dead-end suppression",
    )
    assert_true(not result.steps[2].available_actions, "dead-end route was re-offered")


def check_corridor_turn(result: SimRunResult) -> None:
    assert_equal(result.steps[0].selected_action_id, "explore_corridor:east", "action")
    assert_equal(result.steps[0].outcome["status"], "corridor_follow_end", "status")


def check_corridor_intersection(result: SimRunResult) -> None:
    assert_equal(
        result.steps[0].outcome["status"],
        "corridor_follow_intersection",
        "intersection status",
    )


def check_corridor_room_entrance(result: SimRunResult) -> None:
    assert_equal(
        result.steps[0].selected_action_id,
        "explore_corridor:north",
        "room entry action",
    )
    assert_equal(
        result.steps[0].outcome["status"],
        "corridor_follow_room_entrance",
        "room entry status",
    )
    assert_equal(result.steps[0].outcome["steps"], 1, "room entry steps")
    assert_equal(
        result.steps[1].scene["location_context"]["area_type"],
        "room",
        "room context",
    )
    assert_true(
        bool(result.steps[1].events)
        and "Entered a new room" in result.steps[1].events[0].get("text", ""),
        "room-entry handoff event missing",
    )
    assert_true(
        result.steps[1].selected_action_id
        not in {"explore_corridor:south", "backtrack:corridor"},
        "sim immediately backtracked from the new room",
    )


def check_closed_door(result: SimRunResult) -> None:
    assert_equal(result.steps[0].selected_action_id, "explore_door:door:east", "door")
    assert_equal(result.steps[0].outcome["status"], "door_opened", "door status")
    assert_equal(
        result.steps[1].selected_action_id,
        "continue:opened_door",
        "door continuation",
    )
    assert_equal(result.steps[1].executed_action, "move(east)", "door step")


def check_adjacent_hostile(result: SimRunResult) -> None:
    assert_equal(result.steps[0].selected_action_id, "flee:monster:goblin", "flee")


def check_hostile_resume_exploration(result: SimRunResult) -> None:
    assert_equal(result.steps[0].selected_action_id, "flee:monster:goblin", "flee 1")
    assert_equal(result.steps[1].selected_action_id, "flee:monster:goblin", "flee 2")
    assert_equal(
        result.steps[2].selected_action_id,
        "explore_visible_area",
        "resume exploration",
    )
    assert_true(
        "fight:monster:goblin" not in action_ids(result, 2),
        "distant fight action was offered",
    )


def check_distant_hostile(result: SimRunResult) -> None:
    ids = action_ids(result)
    assert_true(not any(action_id.startswith("flee:") for action_id in ids), "flee")
    assert_true(not any(action_id.startswith("fight:") for action_id in ids), "fight")


def check_pet_not_hostile(result: SimRunResult) -> None:
    ids = action_ids(result)
    assert_true(not any(action_id.startswith("flee:") for action_id in ids), "flee")
    assert_true(not any(action_id.startswith("fight:") for action_id in ids), "fight")
    assert_true("push:blocked_ally" in ids, "push ally action missing")
    assert_equal(result.steps[0].selected_action_id, "push:blocked_ally", "push")


def check_pet_swap(result: SimRunResult) -> None:
    assert_equal(result.steps[0].selected_action_id, "push:blocked_ally", "push 1")
    assert_equal(result.steps[0].executed_action, "move(east)", "move 1")
    assert_equal(
        result.steps[0].outcome["status"],
        "ally_still_blocking",
        "first push",
    )
    assert_equal(result.steps[1].selected_action_id, "push:blocked_ally", "push 2")
    assert_equal(result.steps[1].executed_action, "move(east)", "move 2")
    assert_equal(
        result.steps[1].outcome["status"],
        "ally_swap_completed",
        "second push",
    )
    assert_true("d@##" in result.final_screen, "pet/player swap not visible")


def check_hostile_interrupts_pickup(result: SimRunResult) -> None:
    assert_equal(result.steps[0].selected_action_id, "pick:item:gold", "pickup intent")
    assert_equal(result.steps[0].executed_action, "move(east)", "approach item")
    assert_equal(
        result.steps[1].selected_action_id,
        "flee:monster:goblin",
        "hostile interrupt",
    )
    assert_equal(result.steps[1].executed_action, "move(southwest)", "flee step")
    assert_true(
        "pick:item:gold" in action_ids(result, 1),
        "interrupted pickup was not still visible",
    )
    assert_true(
        bool(result.steps[1].events)
        and "goblin is adjacent" in result.steps[1].events[0].get("text", ""),
        "adjacent hostile event missing",
    )


CASES = (
    SimCase("corridor dead end backtracks", "corridor_dead_end", 3, check_corridor_dead_end),
    SimCase("corridor turn continues", "corridor_turn", 1, check_corridor_turn),
    SimCase(
        "corridor intersection stops",
        "corridor_intersection",
        1,
        check_corridor_intersection,
    ),
    SimCase(
        "corridor room entrance hands back",
        "corridor_room_entrance",
        2,
        check_corridor_room_entrance,
    ),
    SimCase("closed door opens then enters", "closed_door_adjacent", 2, check_closed_door),
    SimCase("adjacent hostile flees", "adjacent_hostile", 1, check_adjacent_hostile),
    SimCase(
        "hostile safe distance resumes exploration",
        "adjacent_hostile",
        3,
        check_hostile_resume_exploration,
    ),
    SimCase("distant hostile is not combat", "distant_hostile", 1, check_distant_hostile),
    SimCase("pet is not hostile", "pet_in_corridor", 1, check_pet_not_hostile),
    SimCase("pet block retries until swap", "pet_in_corridor", 2, check_pet_swap),
    SimCase(
        "hostile interrupts pickup",
        "hostile_interrupts_pickup",
        2,
        check_hostile_interrupts_pickup,
    ),
)


def print_case_summary(case: SimCase, result: SimRunResult) -> None:
    selected = [step.selected_action_id or "none" for step in result.steps]
    statuses = [
        str(step.outcome.get("status")) if isinstance(step.outcome, dict) else "none"
        for step in result.steps
    ]
    print(f"  actions: {' -> '.join(selected)}")
    print(f"  status : {' -> '.join(statuses)}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run all deterministic simulator behavior checks."
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="only print failing cases and final summary",
    )
    args = parser.parse_args()

    failures = 0
    for case in CASES:
        result = run_scenario(get_scenario(case.scenario), steps=case.steps)
        try:
            case.check(result)
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {case.name}")
            print(f"  scenario: {case.scenario}")
            print(f"  reason  : {exc}")
            print_case_summary(case, result)
            print("  final screen:")
            for line in result.final_screen.splitlines():
                print(f"    {line}")
            continue

        if not args.quiet:
            print(f"PASS {case.name}")
            print_case_summary(case, result)

    total = len(CASES)
    passed = total - failures
    print(f"\n{passed}/{total} simulated checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
