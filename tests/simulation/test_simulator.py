from __future__ import annotations

import os
import unittest
from pathlib import Path
import tempfile

from sim.runner import run_scenario
from sim.scenarios import get_scenario


class SimulatorScenarioTests(unittest.TestCase):
    def test_simulator_does_not_write_runtime_trace_log(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_cwd = Path.cwd()
            try:
                os.chdir(temp_dir)
                run_scenario(get_scenario("adjacent_hostile"), steps=1)
                self.assertFalse(Path("logs/last_execution_trace.md").exists())
            finally:
                os.chdir(original_cwd)

    def test_corridor_dead_end_backtracks(self) -> None:
        result = run_scenario(get_scenario("corridor_dead_end"), steps=3)

        self.assertEqual(
            result.steps[0].outcome["status"],
            "corridor_follow_end",
        )
        self.assertEqual(
            result.steps[1].selected_action_id,
            "backtrack:corridor",
        )
        self.assertEqual(
            result.steps[1].outcome["status"],
            "corridor_backtrack_completed",
        )
        self.assertEqual(
            result.steps[2].outcome["status"],
            "sim_no_available_actions",
        )
        self.assertFalse(result.steps[2].available_actions)

    def test_corridor_turn_does_not_stop_as_intersection(self) -> None:
        result = run_scenario(get_scenario("corridor_turn"), steps=1)

        self.assertEqual(
            result.steps[0].selected_action_id,
            "explore_corridor:east",
        )
        self.assertEqual(
            result.steps[0].outcome["status"],
            "corridor_follow_end",
        )
        self.assertNotEqual(
            result.steps[0].outcome["status"],
            "corridor_follow_intersection",
        )

    def test_corridor_intersection_stops_for_model_choice(self) -> None:
        result = run_scenario(get_scenario("corridor_intersection"), steps=1)

        self.assertEqual(
            result.steps[0].outcome["status"],
            "corridor_follow_intersection",
        )

    def test_corridor_room_entrance_hands_back_to_room_choice(self) -> None:
        result = run_scenario(get_scenario("corridor_room_entrance"), steps=2)

        self.assertEqual(
            result.steps[0].selected_action_id,
            "explore_corridor:north",
        )
        self.assertEqual(
            result.steps[0].outcome["status"],
            "corridor_follow_room_entrance",
        )
        self.assertEqual(result.steps[0].outcome["steps"], 1)
        self.assertIn(
            "Entered a new room",
            result.steps[1].events[0]["text"],
        )
        self.assertEqual(
            result.steps[1].scene["location_context"]["area_type"],
            "room",
        )
        self.assertNotEqual(
            result.steps[1].selected_action_id,
            "explore_corridor:south",
        )
        self.assertNotEqual(
            result.steps[1].selected_action_id,
            "backtrack:corridor",
        )

    def test_closed_door_opens_then_steps_through(self) -> None:
        result = run_scenario(get_scenario("closed_door_adjacent"), steps=2)

        self.assertEqual(
            result.steps[0].selected_action_id,
            "explore_door:door:east",
        )
        self.assertEqual(
            result.steps[0].outcome["status"],
            "door_opened",
        )
        self.assertEqual(
            result.steps[1].selected_action_id,
            "continue:opened_door",
        )
        self.assertEqual(result.steps[1].executed_action, "move(east)")

    def test_adjacent_hostile_prefers_flee(self) -> None:
        result = run_scenario(get_scenario("adjacent_hostile"), steps=1)

        self.assertEqual(result.steps[0].selected_action_id, "flee:monster:goblin")

    def test_hostile_policy_resumes_exploration_after_safe_distance(self) -> None:
        result = run_scenario(get_scenario("adjacent_hostile"), steps=3)

        self.assertEqual(result.steps[0].selected_action_id, "flee:monster:goblin")
        self.assertEqual(result.steps[1].selected_action_id, "flee:monster:goblin")
        self.assertEqual(result.steps[2].selected_action_id, "explore_visible_area")
        action_ids = {
            action["action_id"]
            for action in result.steps[2].available_actions
            if isinstance(action.get("action_id"), str)
        }
        self.assertNotIn("fight:monster:goblin", action_ids)

    def test_distant_hostile_does_not_offer_combat(self) -> None:
        result = run_scenario(get_scenario("distant_hostile"), steps=1)

        action_ids = {
            action["action_id"]
            for action in result.steps[0].available_actions
            if isinstance(action.get("action_id"), str)
        }
        self.assertFalse(any(action_id.startswith("flee:") for action_id in action_ids))
        self.assertFalse(any(action_id.startswith("fight:") for action_id in action_ids))

    def test_pet_in_corridor_is_not_hostile(self) -> None:
        result = run_scenario(get_scenario("pet_in_corridor"), steps=1)

        action_ids = {
            action["action_id"]
            for action in result.steps[0].available_actions
            if isinstance(action.get("action_id"), str)
        }
        self.assertFalse(any(action_id.startswith("flee:") for action_id in action_ids))
        self.assertFalse(any(action_id.startswith("fight:") for action_id in action_ids))
        self.assertIn("push:blocked_ally", action_ids)
        self.assertEqual(result.steps[0].selected_action_id, "push:blocked_ally")

    def test_pet_blocking_retries_forward_until_swap(self) -> None:
        result = run_scenario(get_scenario("pet_in_corridor"), steps=2)

        self.assertEqual(result.steps[0].selected_action_id, "push:blocked_ally")
        self.assertEqual(result.steps[0].executed_action, "move(east)")
        self.assertEqual(result.steps[0].outcome["status"], "ally_still_blocking")
        self.assertEqual(result.steps[1].selected_action_id, "push:blocked_ally")
        self.assertEqual(result.steps[1].executed_action, "move(east)")
        self.assertEqual(result.steps[1].outcome["status"], "ally_swap_completed")
        self.assertIn("d@##", result.final_screen)

    def test_hostile_interrupts_pickup_and_model_switches_to_flee(self) -> None:
        result = run_scenario(get_scenario("hostile_interrupts_pickup"), steps=2)

        self.assertEqual(result.steps[0].selected_action_id, "pick:item:gold")
        self.assertEqual(result.steps[0].executed_action, "move(east)")
        self.assertEqual(result.steps[1].selected_action_id, "flee:monster:goblin")
        self.assertEqual(result.steps[1].executed_action, "move(southwest)")
        self.assertIn(
            "goblin is adjacent",
            result.steps[1].events[0]["text"],
        )
        action_ids = {
            action["action_id"]
            for action in result.steps[1].available_actions
            if isinstance(action.get("action_id"), str)
        }
        self.assertIn("pick:item:gold", action_ids)


if __name__ == "__main__":
    unittest.main()
