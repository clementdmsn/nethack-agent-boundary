from __future__ import annotations


def build_action_payload(
    *,
    action_id: str,
    action_type: str,
    label: str,
    target_ref: str | None,
    target_key: str,
    procedure_kind: str,
    low_level_goal: str,
    next_action: str,
    path_steps: list[str],
    distance_steps: int,
    target_pos: list[int] | None = None,
    interruptible: bool = True,
    completes_procedure_after_step: bool = False,
    auto_continue: bool | None = None,
) -> dict[str, object]:
    """Build the shared model-facing high-level action dictionary."""
    action: dict[str, object] = {
        "action_id": action_id,
        "action_type": action_type,
        "label": label,
        "target_ref": target_ref,
        "target_key": target_key,
        "procedure_kind": procedure_kind,
        "low_level_goal": low_level_goal,
        "next_action": next_action,
        "path_steps": path_steps,
        "distance_steps": distance_steps,
        "interruptible": interruptible,
    }
    if target_pos is not None:
        action["target_pos"] = target_pos
    if completes_procedure_after_step:
        action["completes_procedure_after_step"] = True
    if auto_continue is not None:
        action["auto_continue"] = auto_continue
    return action
