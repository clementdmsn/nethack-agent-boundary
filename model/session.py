from __future__ import annotations

from model.auto import AutoModeMixin
from model.decision import ModelDecisionMixin
from model.execution import ActionExecutionMixin
from model.procedure_state import ProcedureStateMixin
from model.request import ModelRequestMixin
from model.trace import ModelTraceMixin
from observation.lightweight import LightweightObservationMixin
from procedures.corridor import CorridorProcedureMixin
from procedures.door import DoorProcedureMixin


class ModelSessionMixin(
    ModelRequestMixin,
    ModelDecisionMixin,
    ActionExecutionMixin,
    AutoModeMixin,
    ProcedureStateMixin,
    DoorProcedureMixin,
    CorridorProcedureMixin,
    LightweightObservationMixin,
    ModelTraceMixin,
):
    def apply_decision_response(self, response: str) -> None:
        """Execute the next low-level action from the selected high-level procedure."""
        request_kind = "step"
        if self.auto_mode:
            request_kind = "auto"
        action, error = self.selected_action_from_response(response)
        if action is None:
            self.ensure_normal_game_mode_before_action()
            self.auto_mode = False
            self.last_selected_action = None
            self.last_executed_low_level_action = None
            self.last_execution_outcome = {
                "status": "invalid_decision",
                "scene_changed": False,
            }
            self.last_response = (
                f"{response}\n\nAuto stopped: {error or 'invalid decision response.'}"
            )
            self.append_response_history(self.last_response)
            self.write_trace_result(
                request_kind=request_kind,
                scene_after_action=None,
            )
            return

        self.execute_selected_action(
            action=action,
            response=response,
            request_kind=request_kind,
            model_skipped=False,
        )
