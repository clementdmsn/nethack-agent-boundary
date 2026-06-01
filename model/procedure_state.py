from __future__ import annotations

from model.action_contract import ActionContract


class ProcedureStateMixin:
    def action_contract(
        self,
        action: dict[str, object] | None,
    ) -> ActionContract | None:
        """Return a typed contract for a valid action dictionary."""
        if not isinstance(action, dict):
            return None
        return ActionContract(action)

    def action_is_dynamic_exploration(self, action: dict[str, object] | None) -> bool:
        """Return whether an action is code-managed exploration that can retarget."""
        contract = self.action_contract(action)
        return contract.is_dynamic_exploration() if contract is not None else False

    def action_is_static_exploration(self, action: dict[str, object] | None) -> bool:
        """Return whether an action is a static exploration route."""
        contract = self.action_contract(action)
        return contract.is_static_exploration() if contract is not None else False

    def action_can_auto_continue(self, action: dict[str, object] | None) -> bool:
        """Return whether code may continue this action without model reasoning."""
        contract = self.action_contract(action)
        return contract.can_auto_continue() if contract is not None else False

    def active_door_target_pos(self) -> tuple[int, int] | None:
        """Return the projected active door position in current-relative coords."""
        procedure = self.current_procedure
        if not isinstance(procedure, dict):
            return None
        if procedure.get("action_type") not in {"explore_door", "go_to_door", "flee"}:
            return None
        pos = procedure.get("door_pos")
        if not (
            isinstance(pos, list)
            and len(pos) == 2
            and all(isinstance(value, int) for value in pos)
        ):
            return None
        return (pos[0], pos[1])

    def project_active_door_target_after_move(
        self,
        delta: tuple[int, int] | None,
    ) -> None:
        """Keep the aimed door stable as relative coordinates change."""
        target = self.active_door_target_pos()
        if target is None or delta is None:
            return
        if not isinstance(self.current_procedure, dict):
            return
        self.current_procedure["door_pos"] = [
            target[0] - delta[0],
            target[1] - delta[1],
        ]

    def active_door_target_matches_action(self, action: dict[str, object]) -> bool:
        """Return whether a renamed door action points to the active door."""
        target = self.active_door_target_pos()
        if target is None:
            return False
        if action.get("action_type") not in {"explore_door", "go_to_door", "flee"}:
            return False
        pos = action.get("door_pos")
        return (
            isinstance(pos, list)
            and len(pos) == 2
            and pos[0] == target[0]
            and pos[1] == target[1]
        )

    def active_door_action_match(
        self,
        available_actions: list[dict[str, object]],
    ) -> dict[str, object] | None:
        """Find the active door after its relative door:* name changed."""
        for action in available_actions:
            if self.active_door_target_matches_action(action):
                return action
        return None

    def reset_auto_exploration_loop_state(self) -> None:
        """Clear the position history used to detect local exploration loops."""
        self.auto_exploration_action_id = None
        self.auto_exploration_positions = []

    def record_auto_exploration_position(
        self,
        action: dict[str, object],
    ) -> None:
        """Track visited positions for one dynamic exploration procedure."""
        action_id = action.get("action_id")
        if (
            not isinstance(action_id, str)
            or not self.action_is_dynamic_exploration(action)
        ):
            self.reset_auto_exploration_loop_state()
            return

        current_pos = self.current_screen_position_key()
        if current_pos is None:
            self.reset_auto_exploration_loop_state()
            return

        if self.auto_exploration_action_id != action_id:
            self.auto_exploration_action_id = action_id
            self.auto_exploration_positions = []

        self.auto_exploration_positions.append(current_pos)
        if len(self.auto_exploration_positions) > 8:
            self.auto_exploration_positions = self.auto_exploration_positions[-8:]

    def auto_exploration_loop_detected(
        self,
        action: dict[str, object] | None,
    ) -> bool:
        """Return whether dynamic exploration has revisited a recent position."""
        if not self.action_is_dynamic_exploration(action):
            return False
        action_id = action.get("action_id") if isinstance(action, dict) else None
        if action_id != self.auto_exploration_action_id:
            return False
        current_pos = self.current_screen_position_key()
        if current_pos is None:
            return False
        return self.auto_exploration_positions.count(current_pos) >= 2

    def block_current_procedure(
        self,
        *,
        status: str,
        text: str,
    ) -> None:
        """Mark the active procedure blocked and emit a procedure event."""
        self.blocked_action_id = self.current_action_id
        self.current_action_id = None
        self.current_target_ref = None
        self.current_procedure = {
            "action_id": self.blocked_action_id,
            "status": "blocked",
            "next_action": None,
            "low_level_goal": None,
        }
        self.procedure_status = "blocked"
        self.procedure_events.append(
            {
                "type": "procedure",
                "procedure": "auto_exploration",
                "status": status,
                "text": text,
            }
        )

    def complete_current_procedure(self) -> None:
        """Clear active procedure state after a procedure completes."""
        self.current_action_id = None
        self.blocked_action_id = None
        self.current_target_ref = None
        self.current_procedure = None
        self.procedure_status = "completed"

    def mark_current_procedure_blocked(self) -> None:
        """Preserve the active action id as blocked and stop auto execution."""
        self.auto_mode = False
        self.blocked_action_id = self.current_action_id
        self.procedure_status = "blocked"
        if isinstance(self.current_procedure, dict):
            self.current_procedure = {
                **self.current_procedure,
                "status": "blocked",
            }

    def activate_procedure(
        self,
        action: dict[str, object],
        low_level_action: str,
    ) -> None:
        """Store active procedure metadata before executing an action step."""
        contract = ActionContract(action)
        self.last_selected_action = dict(action)
        self.current_action_id = contract.action_id
        self.blocked_action_id = None
        self.current_target_ref = contract.target_ref
        self.current_procedure = contract.active_procedure_snapshot()
        self.procedure_status = "active"
        self.action = low_level_action
