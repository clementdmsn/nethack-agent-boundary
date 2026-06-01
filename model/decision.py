from __future__ import annotations

import json


class ModelDecisionMixin:
    def parse_decision_response(self, response: str) -> dict[str, str | None]:
        """Parse model decision JSON with a fallback for malformed output."""
        decision = None
        chosen_action_id = None
        reason = None
        cleaned_response = response.strip()
        lowered_response = cleaned_response.lower()

        if lowered_response in {"continue", "continue_current_action", "keep"}:
            return {
                "decision": "continue",
                "chosen_action_id": None,
                "reason": None,
            }

        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            parsed = None

        if (
            isinstance(parsed, dict)
            and isinstance(parsed.get("decision"), dict)
        ):
            parsed = parsed.get("decision")

        if isinstance(parsed, dict):
            raw_decision = parsed.get("decision")
            if isinstance(raw_decision, str):
                decision = raw_decision.strip().lower()
            raw_action_id = parsed.get("chosen_action_id")
            if isinstance(raw_action_id, str):
                chosen_action_id = raw_action_id.strip()
            raw_reason = parsed.get("reason")
            if isinstance(raw_reason, str):
                reason = raw_reason.strip()

        if chosen_action_id is None:
            for action_id in self.last_available_actions_by_id:
                if action_id in response:
                    chosen_action_id = action_id
                    break

        if decision in {"continue_current_action", "keep"}:
            decision = "continue"
        elif decision in {"change", "switch_action"}:
            decision = "switch"

        return {
            "decision": decision,
            "chosen_action_id": chosen_action_id,
            "reason": reason,
        }

    def selected_action_from_response(
        self,
        response: str,
    ) -> tuple[dict[str, object] | None, str | None]:
        """Resolve the model decision to one currently available action."""
        parsed = self.parse_decision_response(response)
        self.last_parsed_decision = parsed
        decision = parsed["decision"]
        chosen_action_id = parsed["chosen_action_id"]
        current_action_id = self.current_action_id

        if decision == "continue" and current_action_id:
            chosen_action_id = current_action_id
        elif decision == "continue" and not current_action_id and chosen_action_id:
            decision = "switch"
        elif decision is None and chosen_action_id == current_action_id:
            decision = "continue"
        elif decision is None and chosen_action_id:
            decision = "switch"

        parsed["decision"] = decision
        parsed["chosen_action_id"] = chosen_action_id

        if not isinstance(chosen_action_id, str) or not chosen_action_id:
            return None, "No valid action id was selected."

        action = self.last_available_actions_by_id.get(chosen_action_id)
        if action is None:
            action = self.semantic_action_match(
                chosen_action_id,
                self.last_available_actions,
            )
        if action is None:
            return None, f"Unknown action id: {chosen_action_id}"

        if action.get("disabled"):
            return None, f"Action is disabled: {chosen_action_id}"

        return action, None

    def visible_decision_response(
        self,
        response: str,
        low_level_action: str,
    ) -> str:
        """Show only the executed step while continuing an existing procedure."""
        decision = None
        if isinstance(self.last_parsed_decision, dict):
            raw_decision = self.last_parsed_decision.get("decision")
            if isinstance(raw_decision, str):
                decision = raw_decision

        if decision == "continue":
            return low_level_action

        cleaned = response.strip()
        if cleaned:
            return f"{cleaned}\n\nAction: {low_level_action}"
        return low_level_action

    def has_enabled_available_action(self) -> bool:
        """Return whether the current action catalog contains a selectable action."""
        return any(
            isinstance(action, dict) and not action.get("disabled")
            for action in self.last_available_actions
        )
