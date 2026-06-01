from __future__ import annotations

import time

from constants.runtime import AUTO_ACTION_DELAY, DOOR_OPEN_MAX_ATTEMPTS
from model.action_contract import ActionContract
from navigation.actions import action_to_keys


class DoorProcedureMixin:
    def should_run_door_open_procedure(self, action: dict[str, object]) -> bool:
        """Return whether an adjacent door action needs the internal retry loop."""
        return ActionContract(action).needs_door_open_procedure()

    def door_direction_from_action(self, action: dict[str, object]) -> str | None:
        """Extract the first movement direction associated with a door action."""
        path_steps = action.get("path_steps")
        if isinstance(path_steps, list) and path_steps:
            first = path_steps[0]
            if isinstance(first, str):
                return first
        next_action = action.get("next_action")
        if not isinstance(next_action, str):
            return None
        if next_action.startswith("move(") and next_action.endswith(")"):
            return next_action.removeprefix("move(").removesuffix(")")
        return None

    def door_message_status(self, screen: str) -> str | None:
        """Classify NetHack's door message after an open attempt."""
        lowered = screen.lower()
        if "the door opens" in lowered:
            return "opened"
        if "the door is closed" in lowered:
            return "closed"
        if "the door resists" in lowered:
            return "resists"
        if "this door is locked" in lowered or "the door is locked" in lowered:
            return "locked"
        if "no door" in lowered or "can't open" in lowered or "cannot open" in lowered:
            return "failed"
        return None

    def hostile_scene_signature(
        self,
        scene: dict[str, object] | None,
    ) -> tuple[tuple[str, tuple[int, int] | None], ...]:
        """Build a compact visible-hostile signature for door retry interrupts."""
        if not isinstance(scene, dict):
            return ()
        entities = scene.get("entities")
        if not isinstance(entities, list):
            return ()
        hostiles = []
        for entity in entities:
            if not isinstance(entity, dict) or self.is_ally_entry(entity):
                continue
            description = entity.get("description")
            if not isinstance(description, str):
                description = "hostile"
            pos = self.entry_position(entity)
            hostiles.append(
                (
                    description,
                    (pos[0], pos[1]) if pos is not None else None,
                )
            )
        return tuple(sorted(hostiles))

    def run_door_open_procedure(
        self,
        *,
        action: dict[str, object],
        response: str,
        request_kind: str,
        scene_before_action: dict[str, object] | None,
    ) -> None:
        """Run repeated open-door attempts while the tactical scene stays stable."""
        self.ensure_normal_game_mode_before_action()
        direction = self.door_direction_from_action(action)
        direction_key = action_to_keys(f"move({direction})") if direction else None
        low_level_action = f"open_door({direction})" if direction else "open_door()"
        self.last_executed_low_level_action = low_level_action

        if direction_key is None:
            self.auto_mode = False
            self.last_execution_outcome = {
                "status": "door_open_untranslatable_direction",
                "scene_changed": False,
            }
            self.last_response = (
                f"{response}\n\nAuto stopped: door direction could not be translated."
            )
            self.append_response_history(self.last_response)
            self.write_trace_result(request_kind=request_kind, scene_after_action=None)
            return

        previous_signature = self.hostile_scene_signature(scene_before_action)
        scene_after_action = scene_before_action
        final_status = "door_open_retry_limit"
        attempts = 0

        for attempt in range(1, DOOR_OPEN_MAX_ATTEMPTS + 1):
            attempts = attempt
            keys = "o" + direction_key
            self.ensure_normal_game_mode_before_action()
            self.append_runtime_input_log(
                keys=keys,
                action=low_level_action,
                owner="runtime" if request_kind == "auto_continue_code" else "model",
            )
            self.terminal.send_keys(keys)
            self.record_executed_action(low_level_action)
            screen = self.render_screen(print_output=False)
            message_status = self.door_message_status(screen)
            scene_after_action = self.refresh_scene_cache()
            current_signature = self.hostile_scene_signature(scene_after_action)

            if current_signature != previous_signature:
                final_status = "stopped_hostile_moved"
                break
            previous_signature = current_signature

            if message_status == "opened":
                final_status = "door_opened"
                break
            if message_status in {"locked", "failed"}:
                final_status = f"door_open_{message_status}"
                break

        scene_changed = self.canonical_scene(scene_after_action) != self.canonical_scene(
            scene_before_action
        )
        self.last_execution_outcome = {
            "status": final_status,
            "scene_changed": scene_changed,
            "attempts": attempts,
        }

        if final_status == "door_opened":
            self.pending_open_door_direction = direction
            self.pending_open_door_step = f"move({direction})"
            self.current_action_id = "continue:opened_door"
            self.blocked_action_id = None
            self.current_target_ref = None
            self.current_procedure = {
                "action_id": "continue:opened_door",
                "action_type": "explore_door",
                "label": f"Step through opened {direction} door",
                "target_ref": None,
                "target_key": f"door:{direction}:opened",
                "procedure_kind": "static",
                "low_level_goal": f"move through the opened {direction} doorway",
                "next_action": self.pending_open_door_step,
                "path_steps": [direction],
                "status": "active",
                "interruptible": True,
            }
            self.procedure_status = "active"
        else:
            self.mark_current_procedure_blocked()

        self.last_response = self.visible_decision_response(response, low_level_action)
        self.append_response_history(self.last_response)
        self.next_auto_request_at = time.monotonic() + AUTO_ACTION_DELAY
        self.write_trace_result(
            request_kind=request_kind,
            scene_after_action=scene_after_action,
        )
