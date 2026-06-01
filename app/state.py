from __future__ import annotations

import queue
from dataclasses import dataclass, field, fields
from typing import Any


@dataclass
class RuntimeState:
    # Holds the mutable runtime/session state shared across mixins.
    terminal: Any = None
    model: Any = None
    action: str | None = None
    screen: str = ""
    auto_mode: bool = False
    last_scene: dict[str, object] | None = None
    last_observed_scene: dict[str, object] | None = None
    player_identity: str | None = None
    last_map_lines: list[str] = field(default_factory=list)
    last_viewport: Any = None
    last_player_screen_pos: tuple[int, int] | None = None
    current_action_id: str | None = None
    blocked_action_id: str | None = None
    current_procedure: dict[str, object] | None = None
    current_target_ref: str | None = None
    procedure_step_index: int = 0
    procedure_status: str = "idle"
    procedure_events: list[dict[str, object]] = field(default_factory=list)
    last_scene_events: list[dict[str, object]] = field(default_factory=list)
    last_available_actions: list[dict[str, object]] = field(default_factory=list)
    last_available_actions_by_id: dict[str, dict[str, object]] = field(
        default_factory=dict
    )
    last_decision_context: dict[str, object] | None = None
    last_trace_payload: dict[str, object] | None = None
    last_trace_input: dict[str, object] | None = None
    last_trace_result: dict[str, object] | None = None
    trace_logging_enabled: bool = True
    executed_actions: list[str] = field(default_factory=list)
    scene_ref_counters: dict[str, int] = field(default_factory=dict)
    last_text_log: list[str] = field(default_factory=list)
    seen_text_log_lines: set[str] = field(default_factory=set)
    capture_prompted_text: bool = True
    last_observation_cleanup: dict[str, object] = field(default_factory=dict)
    last_payload: str = ""
    last_response: str = ""
    last_scene_summary: str | None = None
    last_raw_model_response: str = ""
    last_parsed_decision: dict[str, object] | None = None
    last_selected_action: dict[str, object] | None = None
    last_model_skipped: bool = False
    last_executed_low_level_action: str | None = None
    last_player_underlying_glyph: str | None = None
    last_execution_outcome: dict[str, object] | None = None
    auto_exploration_action_id: str | None = None
    auto_exploration_positions: list[tuple[int, int]] = field(default_factory=list)
    corridor_backtrack_steps: list[str] = field(default_factory=list)
    blocked_corridor_entries: list[dict[str, object]] = field(default_factory=list)
    corridor_intersection_avoid_steps: list[dict[str, object]] = field(default_factory=list)
    corridor_recent_path_positions: list[list[int]] = field(default_factory=list)
    corridor_pending_discoveries: list[dict[str, object]] = field(default_factory=list)
    pending_open_door_step: str | None = None
    pending_open_door_direction: str | None = None
    blocked_ally_positions: list[list[int]] = field(default_factory=list)
    last_lightweight_refresh_was_full: bool = False
    response_history: list[str] = field(default_factory=list)
    raw_keys_mode: bool = False
    should_exit: bool = False
    payload_scroll: int = 0
    response_scroll: int = 0
    payload_view: tuple[int, int, int, int] = (0, 0, 0, 0)
    response_view: tuple[int, int, int, int] = (0, 0, 0, 0)
    color_pairs: dict[tuple[str, str], int] = field(default_factory=dict)
    next_color_pair: int = 1
    model_events: queue.Queue[tuple[str, str]] = field(
        default_factory=queue.Queue
    )
    model_thread: Any = None
    model_generating: bool = False
    pending_model_request: str | None = None
    awaiting_first_model_delta: bool = False
    next_auto_request_at: float = 0.0


RUNTIME_STATE_FIELDS = {field.name for field in fields(RuntimeState)}
