from __future__ import annotations

import time

from constants.runtime import AUTO_ACTION_DELAY, AUTO_PROMPT
from model.action_contract import ActionContract
from navigation.actions import action_to_keys
from observation.constants import ESCAPE_KEY, FARLOOK_ACTIVE_MARKERS


class ActionExecutionMixin:
    def record_executed_action(self, action: str) -> None:
        """Store recent low-level actions that actually reached the game."""
        cleaned = action.strip()
        if not cleaned:
            return
        self.executed_actions.append(cleaned)
        if len(self.executed_actions) > 20:
            self.executed_actions = self.executed_actions[-20:]

    def screen_is_farlook_mode(self, screen: str) -> bool:
        """Return whether the current terminal screen is still in farlook mode."""
        farlook_is_active = getattr(self, "farlook_is_active", None)
        if farlook_is_active is not None:
            return bool(farlook_is_active(screen))
        return any(marker in screen for marker in FARLOOK_ACTIVE_MARKERS)

    def ensure_normal_game_mode_before_action(self) -> None:
        """Leave farlook/pick mode before sending gameplay commands."""
        if self.terminal is None:
            return
        screen = self.render_screen(print_output=False)
        for _attempt in range(3):
            if not self.screen_is_farlook_mode(screen):
                return
            self.terminal.send_keys(ESCAPE_KEY)
            screen = self.render_screen(print_output=False)

    def translate_action(self, action: str | None = None) -> str | None:
        """Convert a structured low-level action string into game keys."""
        action = self.action if action is None else action

        if not action:
            return None

        return action_to_keys(action)

    def execute_selected_action(
        self,
        *,
        action: dict[str, object],
        response: str,
        request_kind: str,
        model_skipped: bool = False,
    ) -> None:
        """Execute one selected high-level action's next low-level step."""
        self.last_model_skipped = model_skipped
        contract = ActionContract(action)
        low_level_action = contract.next_action
        if low_level_action is None:
            self.auto_mode = False
            self.last_selected_action = action
            self.last_executed_low_level_action = None
            self.last_execution_outcome = {
                "status": "missing_next_step",
                "scene_changed": False,
            }
            self.last_response = (
                f"{response}\n\nAuto stopped: selected action has no next step."
            )
            self.append_response_history(self.last_response)
            self.write_trace_result(
                request_kind=request_kind,
                scene_after_action=None,
            )
            return

        self.activate_procedure(action, low_level_action)

        scene_before_action = None
        if isinstance(self.last_trace_input, dict):
            candidate = self.last_trace_input.get("scene_before_action")
            if isinstance(candidate, dict):
                scene_before_action = candidate

        if self.should_run_door_open_procedure(action):
            self.run_door_open_procedure(
                action=action,
                response=response,
                request_kind=request_kind,
                scene_before_action=scene_before_action,
            )
            return

        if self.should_run_corridor_follow_procedure(action):
            self.run_corridor_follow_procedure(
                action=action,
                response=response,
                request_kind=request_kind,
                scene_before_action=scene_before_action,
            )
            return

        if self.should_run_corridor_backtrack_procedure(action):
            self.run_corridor_backtrack_procedure(
                action=action,
                response=response,
                request_kind=request_kind,
                scene_before_action=scene_before_action,
            )
            return

        self.ensure_normal_game_mode_before_action()
        keys = self.translate_action(low_level_action)

        if keys is None:
            self.auto_mode = False
            self.last_executed_low_level_action = low_level_action
            self.last_execution_outcome = {
                "status": "untranslatable_step",
                "scene_changed": False,
            }
            self.last_response = (
                f"{response}\n\nAuto stopped: selected next step could not be "
                "translated into a game action."
            )
            self.append_response_history(self.last_response)
            self.write_trace_result(
                request_kind=request_kind,
                scene_after_action=None,
            )
            return

        before_screen_text = self.render_screen(print_output=False)
        before_position = self.current_screen_position_key()
        attempted_delta = self.direction_delta_from_action(low_level_action)
        movement_attempted = attempted_delta is not None
        destination_glyph = (
            self.glyph_for_relative_pos(attempted_delta)
            if attempted_delta is not None
            else None
        )
        self.append_runtime_input_log(
            keys=keys,
            action=low_level_action,
            owner="runtime" if model_skipped else "model",
        )
        self.terminal.send_keys(keys)
        self.record_executed_action(low_level_action)
        self.last_executed_low_level_action = low_level_action
        after_screen_text = self.render_screen(print_output=False)
        scene_after_action = self.refresh_scene_cache()
        after_position = self.current_screen_position_key()
        player_moved = (
            before_position is not None
            and after_position is not None
            and before_position != after_position
        )
        movement_blocked = (
            movement_attempted
            and not player_moved
            and before_screen_text == after_screen_text
            and self.action_requires_player_movement(action)
        )
        if not movement_blocked:
            self.record_auto_exploration_position(action)
        scene_changed = self.canonical_scene(scene_after_action) != self.canonical_scene(
            scene_before_action
        )
        if movement_attempted and player_moved:
            scene_changed = True
            self.last_player_underlying_glyph = destination_glyph
            self.project_active_door_target_after_move(attempted_delta)
        if movement_blocked:
            scene_changed = False
            self.last_player_underlying_glyph = None
        self.last_execution_outcome = {
            "status": (
                "movement_blocked"
                if movement_blocked
                else "applied"
                if scene_changed
                else "scene_unchanged"
            ),
            "scene_changed": scene_changed,
        }
        if action.get("action_type") == "push_ally":
            if player_moved:
                self.last_execution_outcome = {
                    "status": "ally_swap_completed",
                    "scene_changed": True,
                }
                self.complete_current_procedure()
            else:
                self.last_execution_outcome = {
                    "status": "ally_still_blocking",
                    "scene_changed": False,
                }
        if movement_blocked and action.get("action_type") != "push_ally":
            self.block_current_procedure(
                status="movement_blocked",
                text=(
                    "Automatic execution stopped because the selected movement "
                    "did not move the player."
                ),
            )
        if (
            model_skipped
            and not scene_changed
            and not movement_blocked
            and self.action_is_static_exploration(action)
            and action.get("action_type") != "push_ally"
        ):
            self.last_execution_outcome = {
                "status": "static_exploration_stuck",
                "scene_changed": False,
            }
            self.block_current_procedure(
                status="static_exploration_stuck",
                text=(
                    "Automatic exploration stopped because the static route "
                    "did not change the scene."
                ),
            )
        if self.action_completed_item_pickup(action, low_level_action):
            self.last_execution_outcome = {
                "status": "item_picked_up",
                "scene_changed": scene_changed,
            }
            self.complete_current_procedure()
            self.procedure_events.append(self.item_pickup_event(action))
        if action.get("approach_door") and scene_changed:
            self.adopt_adjacent_door_action_after_approach(scene_after_action)
        if action.get("completes_procedure_after_step"):
            if not action.get("post_open_door_step") and not movement_blocked:
                self.complete_current_procedure()
        if action.get("post_open_door_step"):
            direction = self.pending_open_door_direction
            self.pending_open_door_step = None
            self.pending_open_door_direction = None
            if not self.start_corridor_after_opened_door(scene_after_action, direction):
                self.complete_current_procedure()
                self.procedure_events.append(
                    self.opened_door_step_event(scene_after_action, direction)
                )
        if self.auto_mode and self.current_action_id and not self.procedure_events:
            self.refresh_cached_payload_from_scene(AUTO_PROMPT, scene_after_action)
        self.last_response = self.visible_decision_response(
            response,
            low_level_action,
        )
        if model_skipped:
            self.last_response = f"auto_continue: {low_level_action}"
        self.append_response_history(self.last_response)
        self.next_auto_request_at = time.monotonic() + AUTO_ACTION_DELAY
        self.write_trace_result(
            request_kind=request_kind,
            scene_after_action=scene_after_action,
        )

    def action_requires_player_movement(self, action: dict[str, object]) -> bool:
        """Return whether an unchanged position means a move action failed."""
        action_type = action.get("action_type")
        return action_type in {
            "explore",
            "explore_door",
            "go_to_door",
            "go_to_exit",
            "go_to_item",
            "go_to_staircase",
            "pick_item",
            "flee",
        }

    def action_completed_item_pickup(
        self,
        action: dict[str, object],
        low_level_action: str,
    ) -> bool:
        """Return whether this step completed a model-selected item pickup."""
        return (
            action.get("action_type") in {"pick_item", "go_to_item"}
            and low_level_action == "pickup()"
        )

    def item_pickup_event(self, action: dict[str, object]) -> dict[str, object]:
        """Build the handoff event after runtime picks up an item."""
        label = action.get("label")
        target = action.get("target_key")
        if isinstance(label, str) and label:
            item_text = label.removeprefix("Pick up ")
        elif isinstance(target, str):
            item_text = target
        else:
            item_text = "the selected item"
        return {
            "type": "procedure",
            "procedure": "item_pickup",
            "status": "item_picked_up",
            "text": f"Picked up {item_text}. Choose the next tactical or exploration action.",
        }

    def adopt_adjacent_door_action_after_approach(
        self,
        scene: dict[str, object],
    ) -> bool:
        """Rebind a door approach route to the adjacent open-door action."""
        actions = self.build_available_actions(scene)
        candidates = [
            candidate
            for candidate in actions
            if candidate.get("action_type") in {"explore_door", "go_to_door", "flee"}
            and candidate.get("requires_open")
            and not candidate.get("approach_door")
            and isinstance(candidate.get("target_key"), str)
            and str(candidate.get("target_key")).startswith("door:")
            and candidate.get("distance_steps") == 1
        ]
        if not candidates:
            return False
        chosen = sorted(
            candidates,
            key=lambda candidate: str(candidate.get("action_id")),
        )[0]
        contract = ActionContract(chosen)
        self.current_action_id = contract.action_id
        self.blocked_action_id = None
        self.current_target_ref = contract.target_ref
        self.current_procedure = contract.active_procedure_snapshot()
        self.current_procedure["interruptible"] = chosen.get("interruptible", True)
        self.procedure_status = "active"
        return True

    def start_corridor_after_opened_door(
        self,
        scene: dict[str, object],
        direction: str | None,
    ) -> bool:
        """Adopt corridor following after stepping through an opened door."""
        if self.has_hostile_pressure(scene):
            return False
        context = scene.get("location_context")
        in_corridor = isinstance(context, dict) and context.get("in_corridor") is True
        adjacent_corridors = (
            context.get("adjacent_corridors") if isinstance(context, dict) else None
        )
        direction_has_corridor = (
            isinstance(direction, str)
            and isinstance(adjacent_corridors, list)
            and direction in adjacent_corridors
        )
        if not in_corridor and not direction_has_corridor:
            return False
        actions = self.build_available_actions(scene)
        preferred_id = f"explore_corridor:{direction}" if direction else None
        chosen = None
        for candidate in actions:
            if candidate.get("action_id") == preferred_id:
                chosen = candidate
                break
        if chosen is None:
            if in_corridor:
                for candidate in actions:
                    if candidate.get("action_type") == "explore_corridor":
                        chosen = candidate
                        break
        if not isinstance(chosen, dict):
            return False
        contract = ActionContract(chosen)
        if contract.next_action is None:
            return False
        self.current_action_id = contract.action_id
        self.blocked_action_id = None
        self.current_target_ref = contract.target_ref
        self.current_procedure = contract.active_procedure_snapshot()
        self.current_procedure["interruptible"] = chosen.get("interruptible", True)
        self.procedure_status = "active"
        return True

    def opened_door_step_event(
        self,
        scene: dict[str, object],
        direction: str | None,
    ) -> dict[str, object]:
        """Build the handoff event after an opened-door continuation stops."""
        context = scene.get("location_context")
        entered_room = isinstance(context, dict) and context.get("in_room") is True
        if entered_room:
            text = (
                f"Opened the {direction} door and stepped through into a room. "
                "Analyze the room and choose the next exploration or tactical action."
            )
        elif self.has_hostile_pressure(scene):
            text = (
                f"Opened the {direction} door and stepped through, but a hostile "
                "is now visible. Choose a tactical action."
            )
        else:
            text = (
                f"Opened the {direction} door and stepped through. Reassess the "
                "new position before choosing the next action."
            )
        return {
            "type": "procedure",
            "procedure": "door_exploration",
            "status": "opened_door_step_completed",
            "text": text,
        }
