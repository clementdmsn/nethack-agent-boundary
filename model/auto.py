from __future__ import annotations

import time

from constants.runtime import AUTO_PROMPT
from model.action_contract import ActionContract


class AutoModeMixin:
    def event_is_pet_only(self, event: dict[str, object]) -> bool:
        """Return whether a scene event only concerns a tame pet."""
        text = event.get("text")
        if not isinstance(text, str):
            return False
        return self.is_tame_pet_description(text)

    def auto_continue_interrupt_reason(
        self,
        action: dict[str, object] | None,
    ) -> str | None:
        """Return why active auto continuation should ask the model instead."""
        procedure = self.current_procedure
        if (
            isinstance(action, dict)
            and action.get("post_open_door_step")
            and self.pending_open_door_step
        ):
            procedure = ActionContract(action).active_procedure_snapshot()
        if not isinstance(procedure, dict):
            return "no_active_procedure"
        if procedure.get("status") != "active":
            return "procedure_not_active"
        if action is None:
            return "action_unavailable"
        opened_door_continuation = bool(action.get("post_open_door_step"))
        scene = self.last_scene if isinstance(self.last_scene, dict) else {}
        if (
            procedure.get("action_type") not in {"flee", "fight"}
            and self.nearby_hostile_positions(scene)
        ):
            return "hostile_nearby"
        if self.auto_exploration_loop_detected(action):
            return "exploration_loop"
        for event in self.last_scene_events:
            if not isinstance(event, dict):
                continue
            if self.event_is_pet_only(event):
                continue
            event_type = event.get("type")
            if event_type == "threat":
                return "threat"
            if opened_door_continuation:
                continue
            if event_type in {"appeared", "disappeared"}:
                return "scene_event"
            if event_type == "moved":
                return "scene_moved"
            if event_type == "procedure":
                return "procedure_event"
        return None

    def pending_opened_door_action(self) -> dict[str, object] | None:
        """Return the pending opened-door continuation from current actions."""
        if not self.pending_open_door_step:
            return None
        if (
            self.current_action_id
            and self.current_action_id != "continue:opened_door"
            and not (
                isinstance(self.current_procedure, dict)
                and self.current_procedure.get("action_type") == "explore_door"
            )
        ):
            return None
        action = self.last_available_actions_by_id.get("continue:opened_door")
        if action is not None:
            return action
        for candidate in self.last_available_actions:
            if (
                isinstance(candidate, dict)
                and candidate.get("post_open_door_step")
            ):
                return candidate
        return None

    def nearby_hostile_positions(
        self,
        scene: dict[str, object],
        max_distance: int = 2,
    ) -> list[tuple[int, int]]:
        """Return hostiles close enough to require model attention."""
        return [
            pos
            for pos in self.hostile_positions(scene)
            if max(abs(pos[0]), abs(pos[1])) <= max_distance
        ]

    def event_affects_active_target(self, event: dict[str, object]) -> bool:
        """Return whether an event concerns the current procedure target."""
        if not isinstance(self.current_procedure, dict):
            return False
        current_target_key = self.current_procedure.get("target_key")
        event_target_key = event.get("target_key")
        if isinstance(current_target_key, str) and isinstance(event_target_key, str):
            return event_target_key == current_target_key
        current_target_ref = self.current_procedure.get("target_ref")
        event_ref = event.get("ref")
        return (
            isinstance(current_target_ref, str)
            and isinstance(event_ref, str)
            and event_ref == current_target_ref
        )

    def current_action_for_auto_continue(self) -> dict[str, object] | None:
        """Resolve the currently active action from the latest action catalog."""
        action_id = self.current_action_id
        if isinstance(action_id, str):
            action = self.last_available_actions_by_id.get(action_id)
            if action is not None:
                return action
            action = self.semantic_action_match(action_id, self.last_available_actions)
            if action is not None:
                return action
            handoff = self.exploration_corridor_handoff_action(action_id)
            if handoff is not None:
                return handoff
            door_handoff = self.active_door_action_match(self.last_available_actions)
            if door_handoff is not None:
                return door_handoff
        pending_door_action = self.pending_opened_door_action()
        if pending_door_action is not None:
            return pending_door_action
        return None

    def exploration_corridor_handoff_action(
        self,
        action_id: str,
        available_actions: list[dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        """Continue a lost exploration route by adopting an adjacent corridor."""
        if not action_id.startswith(("explore:exit:", "explore:frontier")):
            return None
        actions = available_actions
        if actions is None:
            actions = self.last_available_actions
        for action in actions:
            if action.get("action_type") == "explore_corridor":
                return action
        return None

    def maybe_continue_auto_procedure(self) -> bool:
        """Continue an active auto procedure without a model call when safe."""
        if not self.current_action_id:
            return False

        if not self.last_available_actions:
            self.refresh_payload(AUTO_PROMPT)
        action = self.current_action_for_auto_continue()
        if not self.action_can_auto_continue(action):
            if action is not None:
                self.current_action_id = None
                self.current_target_ref = None
                self.current_procedure = None
                self.procedure_status = "completed"
            return False
        reason = self.auto_continue_interrupt_reason(action)
        if reason is not None:
            if reason == "exploration_loop":
                self.block_current_procedure(
                    status="exploration_loop",
                    text=(
                        "Automatic exploration stopped because it revisited "
                        "the same local position."
                    ),
                )
                self.reset_auto_exploration_loop_state()
            self.last_execution_outcome = {
                "status": "model_required",
                "reason": reason,
                "scene_changed": bool(self.last_scene_events),
                "model_skipped": False,
            }
            return False

        action_id = (
            action.get("action_id")
            if action is not None
            else self.current_action_id
        )
        self.last_raw_model_response = ""
        self.last_parsed_decision = {
            "decision": "continue",
            "chosen_action_id": action_id,
            "reason": "Model skipped because the active procedure remained valid.",
        }
        self.snapshot_trace_input("auto_continue_code")
        self.append_runtime_automation_log(
            request_kind="auto_continue_code",
            action=action,
        )
        self.execute_selected_action(
            action=action,
            response="continue",
            request_kind="auto_continue_code",
            model_skipped=True,
        )
        return True

    def maybe_backtrack_corridor_dead_end(self) -> bool:
        """Backtrack a known corridor dead end without asking the model."""
        if not self.corridor_backtrack_steps:
            return False
        scene = self.last_scene if isinstance(self.last_scene, dict) else {}
        action = self.build_corridor_backtrack_action(scene)
        if action is None:
            return False

        self.refresh_cached_payload_from_scene(AUTO_PROMPT, scene)
        self.last_raw_model_response = ""
        self.last_parsed_decision = {
            "decision": "continue",
            "chosen_action_id": action.get("action_id"),
            "reason": "Runtime backtracked after corridor dead end.",
        }
        self.snapshot_trace_input("auto_continue_code")
        self.append_runtime_automation_log(
            request_kind="auto_continue_code",
            action=action,
        )
        self.execute_selected_action(
            action=action,
            response="continue",
            request_kind="auto_continue_code",
            model_skipped=True,
        )
        return True

    def recover_available_actions_from_lightweight_scene(self) -> bool:
        """Try a glyph-based screen refresh before declaring the catalog empty."""
        scene = self.refresh_lightweight_visible_scene_cache()
        self.refresh_cached_payload_from_scene(AUTO_PROMPT, scene)
        return self.has_enabled_available_action()

    def maybe_start_auto_request(self) -> None:
        """Start the next autonomous action request when auto mode is ready."""
        if not self.auto_mode:
            return
        if self.model_generating or self.raw_keys_mode or self.should_exit:
            return
        if time.monotonic() < self.next_auto_request_at:
            return
        if self.maybe_backtrack_corridor_dead_end():
            return
        if self.maybe_continue_auto_procedure():
            return

        self.refresh_payload(AUTO_PROMPT)
        if not self.has_enabled_available_action():
            if self.recover_available_actions_from_lightweight_scene():
                self.start_model_request(
                    AUTO_PROMPT,
                    request_kind="auto",
                    reuse_payload=True,
                )
                return

            self.auto_mode = False
            self.last_model_skipped = True
            self.last_raw_model_response = ""
            self.last_parsed_decision = None
            self.last_selected_action = None
            self.last_executed_low_level_action = None
            self.last_execution_outcome = {
                "status": "no_available_actions",
                "scene_changed": bool(self.last_scene_events),
                "model_skipped": True,
            }
            self.last_response = "Auto stopped: no enabled actions are available."
            self.append_response_history(self.last_response)
            self.snapshot_trace_input("auto_no_available_actions")
            self.write_trace_result(
                request_kind="auto_no_available_actions",
                scene_after_action=(
                    self.last_scene if isinstance(self.last_scene, dict) else None
                ),
            )
            return

        self.start_model_request(AUTO_PROMPT, request_kind="auto")
