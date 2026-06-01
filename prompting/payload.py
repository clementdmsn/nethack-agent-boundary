from __future__ import annotations

from constants.runtime import AUTO_PROMPT


class PayloadMixin:
    def procedure_is_active(
        self,
        current_procedure: dict[str, object] | None,
    ) -> bool:
        """Return whether the current procedure can be continued this turn."""
        return (
            isinstance(current_procedure, dict)
            and current_procedure.get("status") == "active"
        )

    def decision_request(
        self,
        user_input: str,
        current_procedure: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Describe the expected model output for the current request type."""
        if user_input == AUTO_PROMPT:
            if self.procedure_is_active(current_procedure):
                instruction = (
                    "If the current procedure is still safe and useful, return "
                    "exactly `continue`. If switching, return JSON with "
                    "decision=switch, chosen_action_id, and a reason of 6 words or fewer."
                )
            else:
                instruction = (
                    "Return JSON with decision=switch, chosen_action_id, and "
                    "a reason of 6 words or fewer. There is no active procedure to continue."
                )
            return {
                "mode": "choose_action",
                "instruction": instruction,
                "allowed_decisions": ["continue", "switch"],
                "current_action_id": self.current_action_id,
            }

        return {
            "mode": "answer_question",
            "question": user_input,
        }

    def full_payload(
        self,
        *,
        user_input: str,
        normalized_scene: dict[str, object],
        scene_summary: str,
        previous_scene_summary: str | None,
        scene_events: list[dict[str, object]],
        available_actions: list[dict[str, object]],
        current_procedure: dict[str, object] | None,
    ) -> dict[str, object]:
        """Build the rich debug payload retained in the trace."""
        return {
            "system_context": {
                "game": "NetHack visible-room tactical control",
                "decision_contract": (
                    "Prefer continue_current_action when still safe and useful; "
                    "otherwise switch to one available action id."
                ),
            },
            "high_level_goals": self.high_level_goals(),
            "medium_level_goals": self.medium_level_goals(),
            "scene_state": {
                "identity": self.player_identity,
                "pet": self.find_allies(normalized_scene),
                "room_description": normalized_scene.get("room_description"),
                "visibility": normalized_scene.get("visibility", "normal"),
                "location_context": normalized_scene.get("location_context"),
                "player": normalized_scene.get("player"),
                "tabletop": [
                    self.tabletop_entry(self.singular_bucket_name(bucket), entry)
                    for bucket in ("entities", "items", "exits", "features")
                    for entry in normalized_scene.get(bucket, [])
                    if isinstance(entry, dict)
                ],
                "scene_summary": scene_summary,
                "previous_scene_summary": previous_scene_summary,
                "raw_scene": {
                    "exits": normalized_scene.get("exits", []),
                    "items": normalized_scene.get("items", []),
                    "entities": normalized_scene.get("entities", []),
                    "features": normalized_scene.get("features", []),
                    "areas": normalized_scene.get("areas", []),
                },
            },
            "scene_events": scene_events,
            "non_pet_scene_changed": self.non_pet_scene_changed(scene_events),
            "ongoing_hazards": self.ongoing_hazards(normalized_scene),
            "recent_low_level_actions": self.recent_actions_for_model(),
            "available_actions": available_actions,
            "current_procedure": current_procedure,
            "decision_request": self.decision_request(user_input, current_procedure),
            "user_question": user_input,
        }

    def decision_payload(
        self,
        *,
        normalized_scene: dict[str, object],
        scene_events: list[dict[str, object]],
        available_actions: list[dict[str, object]],
        current_procedure: dict[str, object] | None,
    ) -> dict[str, object]:
        """Build the compact model-facing payload for action selection turns."""
        active_procedure = self.procedure_is_active(current_procedure)
        response_contract = (
            "When continuing the active current_procedure, output exactly: continue"
            if active_procedure
            else (
                "No active procedure exists; switch by returning JSON with "
                "chosen_action_id and a reason of 6 words or fewer."
            )
        )
        return {
            "goals": self.high_level_goals(),
            "priorities": self.medium_level_goals(),
            "scene": self.compact_scene_state(normalized_scene),
            "events": [
                event.get("text")
                for event in scene_events
                if isinstance(event.get("text"), str)
            ],
            "non_pet_scene_changed": self.non_pet_scene_changed(scene_events),
            "hazards": self.ongoing_hazards(normalized_scene),
            "recent_actions": self.recent_actions_for_model(),
            "current_procedure": self.compact_current_procedure(current_procedure),
            "available_actions": [
                self.compact_action_view(action) for action in available_actions
            ],
            "decision": {
                "mode": "choose_action",
                "allowed": ["continue", "switch"],
                "current_action_id": self.current_action_id,
                "response_contract": response_contract,
                "rule": (
                    "Use continue only when current_procedure is active; "
                    "otherwise switch to an id listed in available_actions. "
                    "Visible scene ids are context, not selectable actions. "
                    "Keep reason to 6 words or fewer."
                ),
            },
        }
