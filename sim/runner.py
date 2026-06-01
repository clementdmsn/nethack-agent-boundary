from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.runner import Runner, RuntimeState
from constants.runtime import AUTO_PROMPT
from sim.scenarios import Scenario
from sim.world import SimTerminal, SimWorld


@dataclass
class SimStep:
    index: int
    scene: dict[str, object]
    events: list[dict[str, object]]
    available_actions: list[dict[str, object]]
    selected_action_id: str | None
    executed_action: str | None
    outcome: dict[str, object] | None
    response: str


@dataclass
class SimRunResult:
    scenario: str
    steps: list[SimStep] = field(default_factory=list)
    final_screen: str = ""


def make_sim_runner(world: SimWorld) -> Runner:
    """Create a Runner wired to SimWorld without launching NetHack."""
    runner = Runner.__new__(Runner)
    runner.state = RuntimeState(terminal=SimTerminal(world))
    runner.trace_logging_enabled = False
    runner.player_identity = "simulated adventurer"
    runner.notify_generation_finished_for_debug = lambda: None

    def render_screen(print_output: bool = True) -> str:
        screen = runner.terminal.render()
        runner.screen = screen
        if print_output:
            print(screen)
        return screen

    def look() -> dict[str, object]:
        runner.screen = world.render()
        scene = world.scene()
        runner.last_map_lines = world.map_lines()
        runner.last_viewport = world.viewport()
        runner.last_player_screen_pos = world.player
        runner.last_scene = scene
        return scene

    runner.render_screen = render_screen
    runner.look = look
    runner.refresh_scene_cache = look
    runner.refresh_lightweight_visible_scene_cache = look
    render_screen(print_output=False)
    look()
    return runner


def choose_scripted_response(runner: Runner) -> str | None:
    """Choose a deterministic action from the current compact model payload."""
    if not runner.last_payload:
        return None
    payload = json.loads(runner.last_payload)
    actions = payload.get("available_actions")
    if not isinstance(actions, list) or not actions:
        return None

    current = payload.get("current_procedure")
    if isinstance(current, dict) and current.get("status") == "active":
        hazards = payload.get("hazards")
        events = payload.get("events")
        unsafe = bool(hazards) or any(
            isinstance(event, str) and "threatening" in event
            for event in events
            if isinstance(events, list)
        )
        if not unsafe:
            return "continue"

    def action_score(action: dict[str, object]) -> tuple[int, int, str]:
        action_type = action.get("type")
        action_id = action.get("id")
        priority = action.get("priority")
        steps = action.get("steps")
        if not isinstance(steps, int):
            steps = 99
        rank = 50
        if action_type == "flee":
            rank = 0
        elif action_type == "backtrack_corridor":
            rank = 5
        elif action_type == "pick_item":
            rank = 8
        elif priority == "nearest_exploration":
            rank = 10
        elif action_type == "explore_corridor":
            rank = 15
        elif action_type in {"explore", "explore_door"}:
            rank = 20
        elif action_type == "fight":
            rank = 40
        return (rank, steps, str(action_id))

    chosen = min(
        (action for action in actions if isinstance(action, dict)),
        key=action_score,
    )
    action_id = chosen.get("id")
    if not isinstance(action_id, str):
        return None
    return json.dumps(
        {
            "decision": "switch",
            "chosen_action_id": action_id,
            "reason": "scripted simulator policy",
        }
    )


def run_scenario(scenario: Scenario, *, steps: int = 20) -> SimRunResult:
    """Run one deterministic scenario through the real action/procedure code."""
    world = SimWorld.from_text(
        scenario.map_text,
        spawn_events=scenario.spawn_events,
    )
    runner = make_sim_runner(world)
    runner.auto_mode = True
    if scenario.initial_active_action is not None:
        original_build_available_actions = runner.build_available_actions

        def build_available_actions_with_initial(
            scene: dict[str, object],
        ) -> list[dict[str, object]]:
            actions = original_build_available_actions(scene)
            action_id = scenario.initial_active_action.get("action_id")
            if runner.current_action_id == action_id and not any(
                action.get("action_id") == action_id for action in actions
            ):
                actions.append(scenario.initial_active_action)
            return actions

        runner.build_available_actions = build_available_actions_with_initial
        next_action = scenario.initial_active_action.get("next_action")
        if isinstance(next_action, str):
            runner.activate_procedure(scenario.initial_active_action, next_action)
    result = SimRunResult(scenario=scenario.name)

    for index in range(steps):
        scene = runner.refresh_scene_cache()
        runner.last_payload = runner.build_model_prompt(AUTO_PROMPT, scene)
        events = list(runner.last_scene_events)
        available_actions = list(runner.last_available_actions)
        if runner.maybe_continue_auto_procedure():
            result.steps.append(
                SimStep(
                    index=index,
                    scene=scene,
                    events=events,
                    available_actions=available_actions,
                    selected_action_id=(
                        runner.last_selected_action or {}
                    ).get("action_id"),
                    executed_action=runner.last_executed_low_level_action,
                    outcome=runner.last_execution_outcome,
                    response=runner.last_response,
                )
            )
            continue

        response = choose_scripted_response(runner)
        if response is None:
            runner.last_execution_outcome = {
                "status": "sim_no_available_actions",
                "scene_changed": False,
            }
            result.steps.append(
                SimStep(
                    index=index,
                    scene=scene,
                    events=events,
                    available_actions=available_actions,
                    selected_action_id=None,
                    executed_action=None,
                    outcome=runner.last_execution_outcome,
                    response="",
                )
            )
            break

        runner.apply_decision_response(response)
        result.steps.append(
            SimStep(
                index=index,
                scene=scene,
                events=events,
                available_actions=available_actions,
                selected_action_id=(runner.last_selected_action or {}).get("action_id"),
                executed_action=runner.last_executed_low_level_action,
                outcome=runner.last_execution_outcome,
                response=runner.last_response,
            )
        )

    result.final_screen = world.render()
    return result
