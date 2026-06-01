from __future__ import annotations

import argparse
import json
from pathlib import Path

from sim.runner import SimRunResult, run_scenario
from sim.scenarios import get_scenario, scenario_names


def write_trace(result: SimRunResult) -> Path:
    path = Path("logs") / "sim" / f"{result.scenario}.trace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "scenario": result.scenario,
        "final_screen": result.final_screen,
        "steps": [
            {
                "index": step.index,
                "scene": step.scene,
                "available_actions": step.available_actions,
                "selected_action_id": step.selected_action_id,
                "executed_action": step.executed_action,
                "outcome": step.outcome,
                "response": step.response,
            }
            for step in result.steps
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def print_result(result: SimRunResult) -> None:
    print(f"scenario: {result.scenario}")
    print()
    for step in result.steps:
        print(f"step {step.index}")
        location = step.scene.get("location_context")
        if isinstance(location, dict):
            print(f"  location: {location.get('area_type')}")
        print("  actions:")
        for action in step.available_actions:
            print(
                "   - "
                + str(action.get("action_id"))
                + " next="
                + str(action.get("next_action"))
            )
        print(f"  selected: {step.selected_action_id}")
        print(f"  executed: {step.executed_action}")
        print(f"  outcome: {step.outcome}")
        if step.response:
            print(f"  response: {step.response}")
        print()
    print("final screen:")
    print(result.final_screen)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run deterministic NetHack-agent simulator scenarios."
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        choices=scenario_names(),
        help="scenario name",
    )
    parser.add_argument("--steps", type=int, default=20)
    parser.add_argument("--list", action="store_true", help="list scenarios")
    parser.add_argument("--no-trace", action="store_true", help="skip JSON trace")
    args = parser.parse_args()

    if args.list or args.scenario is None:
        for name in scenario_names():
            scenario = get_scenario(name)
            print(f"{name}: {scenario.description}")
        return

    result = run_scenario(get_scenario(args.scenario), steps=args.steps)
    print_result(result)
    if not args.no_trace:
        print()
        print(f"trace: {write_trace(result)}")


if __name__ == "__main__":
    main()
