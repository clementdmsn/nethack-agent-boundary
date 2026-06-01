from __future__ import annotations

from dataclasses import dataclass


AUTO_CONTINUE_ACTION_TYPES = frozenset(
    {
        "explore_corridor",
        "explore_door",
        "go_to_door",
        "go_to_item",
        "pick_item",
        "go_to_staircase",
        "backtrack_corridor",
        "flee",
        "fight",
        "push_ally",
    }
)
NON_CONTINUABLE_ACTION_IDS = frozenset(
    {
        "explore_visible_area",
        "explore:frontier",
    }
)


@dataclass(frozen=True)
class ActionContract:
    """Typed view over a model-facing action dictionary."""

    action: dict[str, object]

    @property
    def action_id(self) -> str | None:
        """Return the stable action id when present."""
        value = self.action.get("action_id")
        return value if isinstance(value, str) else None

    @property
    def action_type(self) -> str | None:
        """Return the model-facing action type when present."""
        value = self.action.get("action_type")
        return value if isinstance(value, str) else None

    @property
    def target_ref(self) -> str | None:
        """Return the volatile target ref when present."""
        value = self.action.get("target_ref")
        return value if isinstance(value, str) else None

    @property
    def next_action(self) -> str | None:
        """Return the executable low-level action when present."""
        value = self.action.get("next_action")
        return value if isinstance(value, str) else None

    @property
    def target_key(self) -> str | None:
        """Return the semantic target key when present."""
        value = self.action.get("target_key")
        return value if isinstance(value, str) else None

    @property
    def procedure_kind(self) -> str | None:
        """Return whether the action is static or dynamic."""
        value = self.action.get("procedure_kind")
        return value if isinstance(value, str) else None

    @property
    def distance_steps(self) -> int | None:
        """Return the planned distance when present."""
        value = self.action.get("distance_steps")
        return value if isinstance(value, int) else None

    def is_dynamic_exploration(self) -> bool:
        """Return whether this action may retarget during exploration."""
        return self.action_type in {"explore", "explore_corridor"}

    def is_static_exploration(self) -> bool:
        """Return whether this action is a fixed exploration route."""
        return self.action_type == "explore" and self.procedure_kind == "static"

    def can_auto_continue(self) -> bool:
        """Return whether runtime code may continue this action without the model."""
        if self.action.get("auto_continue") is False:
            return False
        if self.action_id in NON_CONTINUABLE_ACTION_IDS:
            return False
        if self.action_type in AUTO_CONTINUE_ACTION_TYPES:
            return True
        return self.is_static_exploration()

    def needs_door_open_procedure(self) -> bool:
        """Return whether this action should use the internal door-open loop."""
        if self.action_type not in {"go_to_door", "explore_door", "flee"}:
            return False
        if self.action.get("approach_door"):
            return False
        if not self.action.get("requires_open"):
            return False
        if not (self.target_key and self.target_key.startswith("door:")):
            return False
        return self.distance_steps is not None and self.distance_steps <= 1

    def needs_corridor_follow_procedure(self) -> bool:
        """Return whether this action should use the corridor-follow loop."""
        return (
            self.action_type == "explore_corridor"
            and self.next_action is not None
            and self.next_action.startswith("follow_corridor(")
        )

    def needs_corridor_backtrack_procedure(self) -> bool:
        """Return whether this action should use the corridor backtrack loop."""
        return (
            self.action_type == "backtrack_corridor"
            and self.next_action is not None
            and self.next_action.startswith("backtrack_corridor(")
        )

    def active_procedure_snapshot(self) -> dict[str, object]:
        """Build the runtime snapshot for an active action procedure."""
        return {
            "action_id": self.action.get("action_id"),
            "action_type": self.action.get("action_type"),
            "label": self.action.get("label"),
            "target_ref": self.action.get("target_ref"),
            "target_key": self.action.get("target_key"),
            "procedure_kind": self.action.get("procedure_kind"),
            "low_level_goal": self.action.get("low_level_goal"),
            "next_action": self.next_action,
            "path_steps": self.action.get("path_steps", []),
            "door_pos": self.action.get("door_pos"),
            "approach_door": self.action.get("approach_door"),
            "requires_open": self.action.get("requires_open"),
            "status": "active",
        }
