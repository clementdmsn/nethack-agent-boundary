from __future__ import annotations

import re

from model.action_contract import ActionContract


class ProcedureMixin:
    def normalized_action_type_for_semantics(self, action_type: str) -> str:
        """Return the semantic action type used for stable matching."""
        if action_type == "go_to_item":
            return "pick_item"
        if action_type == "go_to_exit":
            return "explore"
        if action_type == "go_to_door":
            return "explore_door"
        return action_type

    def current_procedure_snapshot(
        self,
        available_actions: list[dict[str, object]],
    ) -> dict[str, object] | None:
        """Resolve the active high-level action against current affordances."""
        if not self.current_action_id:
            self.current_procedure = None
            self.current_target_ref = None
            self.procedure_status = "idle"
            return None

        action = next(
            (
                candidate
                for candidate in available_actions
                if candidate.get("action_id") == self.current_action_id
            ),
            None,
        )
        if action is None:
            action = self.semantic_action_match(
                self.current_action_id,
                available_actions,
            )
        if action is None:
            action = self.exploration_corridor_handoff_action(
                self.current_action_id,
                available_actions,
            )
        if action is None:
            action = self.active_door_action_match(available_actions)
        if action is None:
            blocked_action_id = self.current_action_id
            self.blocked_action_id = blocked_action_id
            self.procedure_status = "blocked"
            self.current_procedure = {
                "action_id": blocked_action_id,
                "status": "blocked",
                "next_action": None,
                "low_level_goal": None,
            }
            self.current_target_ref = None
            self.current_action_id = None
            return self.current_procedure

        contract = ActionContract(action)
        snapshot = contract.active_procedure_snapshot()
        snapshot["interruptible"] = action.get("interruptible", True)
        if contract.action_id is not None:
            self.current_action_id = contract.action_id
            self.blocked_action_id = None
        self.current_procedure = snapshot
        self.current_target_ref = contract.target_ref
        self.procedure_status = "active"
        return snapshot

    def semantic_key_from_action_id(
        self,
        action_id: str | None,
    ) -> tuple[str, str] | None:
        """Parse current and legacy ids into stable action-type/target-key pairs."""
        if not isinstance(action_id, str) or ":" not in action_id:
            return None
        action_type, raw_target = action_id.split(":", 1)
        action_type = self.normalized_action_type_for_semantics(action_type)
        if action_type == "pick" and raw_target.startswith("item:"):
            action_type = "pick_item"
        if raw_target.startswith(
            ("exit:", "monster:", "ally:", "item:", "door:", "staircase:")
        ):
            return (action_type, raw_target)

        exit_match = re.match(r"([a-z]+)_exit(?:_\d+)?$", raw_target)
        if action_type == "explore" and exit_match:
            return ("explore", f"exit:{exit_match.group(1)}")

        for prefix, semantic_prefix in (
            ("visible_door", "door"),
            ("closed_door", "door"),
            ("open_door", "door"),
        ):
            if raw_target.startswith(prefix):
                return (action_type, f"{semantic_prefix}:visible")

        legacy_name = re.sub(r"_\d+$", "", raw_target)
        if action_type in {"fight", "flee"}:
            return (action_type, f"monster:{legacy_name}")
        if action_type == "go_to_item":
            return (action_type, f"item:{legacy_name}")
        return (action_type, raw_target)

    def semantic_key_from_action(
        self,
        action: dict[str, object],
    ) -> tuple[str, str] | None:
        """Return the stable identity for a currently available action."""
        action_type = action.get("action_type")
        target_key = action.get("target_key")
        if isinstance(action_type, str) and isinstance(target_key, str):
            return (self.normalized_action_type_for_semantics(action_type), target_key)
        action_id = action.get("action_id")
        return self.semantic_key_from_action_id(
            action_id if isinstance(action_id, str) else None
        )

    def semantic_action_match(
        self,
        current_action_id: str | None,
        available_actions: list[dict[str, object]],
    ) -> dict[str, object] | None:
        """Find the same high-level intent after volatile target refs changed."""
        current_key = self.semantic_key_from_action_id(current_action_id)
        if current_key is None:
            return None
        for action in available_actions:
            if self.semantic_key_from_action(action) == current_key:
                return action
        current_type, current_target = current_key
        if current_target in {"door:visible", "staircase:visible"}:
            current_prefix = current_target.split(":", 1)[0] + ":"
            for action in available_actions:
                action_key = self.semantic_key_from_action(action)
                if (
                    action_key is not None
                    and action_key[0] == current_type
                    and action_key[1].startswith(current_prefix)
                ):
                    return action
        return None
