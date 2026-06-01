from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess


class ModelTraceMixin:
    DEBUG_GENERATION_SOUND_PATH = Path(
        "/usr/share/sounds/freedesktop/stereo/complete.oga"
    )

    def notify_generation_finished_for_debug(self) -> None:
        """Play an optional audible marker when a model generation finishes."""
        paplay = shutil.which("paplay")
        if paplay is not None and self.DEBUG_GENERATION_SOUND_PATH.exists():
            try:
                subprocess.run(
                    [paplay, str(self.DEBUG_GENERATION_SOUND_PATH)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=1.0,
                    check=False,
                )
                return
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            import curses

            curses.beep()
            return
        except Exception:
            pass

        os.write(1, b"\a")

    def canonical_scene(self, scene: dict[str, object] | None):
        """Normalize scene snapshots for equality checks by stripping transient refs."""
        if not isinstance(scene, dict):
            return scene

        def normalize(value):
            """Recursively drop volatile scene identity fields."""
            if isinstance(value, dict):
                normalized = {
                    key: normalize(item)
                    for key, item in value.items()
                    if key not in {"ref", "target_key", "display_name"}
                }
                if "visibility" not in normalized:
                    normalized["visibility"] = "normal"
                if "location_context" not in normalized:
                    normalized["location_context"] = None
                return normalized
            if isinstance(value, list):
                return [normalize(item) for item in value]
            return value

        return normalize(scene)

    def decoded_last_payload(self):
        """Parse the last payload JSON when available for trace snapshots."""
        if not self.last_payload:
            return None
        try:
            return json.loads(self.last_payload)
        except json.JSONDecodeError:
            return self.last_payload

    def snapshot_trace_input(self, request_kind: str) -> None:
        """Capture the exact state presented to the model before it responds."""
        self.last_trace_input = {
            "request_kind": request_kind,
            "current_action_id": self.current_action_id,
            "procedure_status": self.procedure_status,
            "payload": (
                self.last_trace_payload
                if isinstance(self.last_trace_payload, dict)
                else self.decoded_last_payload()
            ),
            "model_payload": self.decoded_last_payload(),
            "current_procedure": (
                dict(self.current_procedure)
                if isinstance(self.current_procedure, dict)
                else None
            ),
            "scene_events": list(self.last_scene_events),
            "first_screen_scene": (
                json.loads(json.dumps(self.last_observed_scene))
                if isinstance(self.last_observed_scene, dict)
                else self.last_observed_scene
            ),
            "scene_before_action": (
                json.loads(json.dumps(self.last_scene))
                if isinstance(self.last_scene, dict)
                else self.last_scene
            ),
        }

    def write_trace_result(
        self,
        *,
        request_kind: str,
        scene_after_action: dict[str, object] | None = None,
    ) -> None:
        """Capture the result of the latest model interaction after execution."""
        self.last_trace_result = {
            "request_kind": request_kind,
            "current_action_id": self.current_action_id,
            "procedure_status": self.procedure_status,
            "raw_model_response": self.last_raw_model_response,
            "parsed_decision": self.last_parsed_decision,
            "selected_action": self.last_selected_action,
            "model_skipped": self.last_model_skipped,
            "executed_low_level_action": self.last_executed_low_level_action,
            "execution_outcome": self.last_execution_outcome,
            "current_procedure": (
                dict(self.current_procedure)
                if isinstance(self.current_procedure, dict)
                else None
            ),
            "scene_events_after_action": list(self.last_scene_events),
            "scene_after_action": scene_after_action,
        }
        self.write_last_decision_trace_log()

    def compact_decision_trace_log(self) -> dict[str, object]:
        """Return a concise non-redundant trace for the latest execution."""
        trace_input = self.last_trace_input if isinstance(self.last_trace_input, dict) else {}
        trace_result = (
            self.last_trace_result if isinstance(self.last_trace_result, dict) else {}
        )
        payload = trace_input.get("model_payload")
        if not isinstance(payload, dict):
            payload = trace_input.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        action_owner = self.trace_action_owner(trace_input, trace_result)
        return {
            "request": {
                "kind": trace_input.get("request_kind"),
                "action_owner": action_owner,
                "model_skipped": trace_result.get("model_skipped", False),
            },
            "before": {
                "current_action_id": trace_input.get("current_action_id"),
                "procedure_status": trace_input.get("procedure_status"),
                "current_procedure": self.compact_procedure(
                    trace_input.get("current_procedure")
                ),
                "scene": self.compact_scene(trace_input.get("scene_before_action")),
                "events": self.compact_events(trace_input.get("scene_events")),
                "recent_actions": self.compact_recent_actions(payload),
                "available_actions": self.compact_actions(
                    self.payload_available_actions(payload)
                ),
            },
            "model": {
                "raw_response": trace_result.get("raw_model_response"),
                "parsed_decision": trace_result.get("parsed_decision"),
            },
            "execution": {
                "selected_action": self.compact_action(
                    trace_result.get("selected_action")
                ),
                "executed_low_level_action": trace_result.get(
                    "executed_low_level_action"
                ),
                "outcome": trace_result.get("execution_outcome"),
            },
            "after": {
                "current_action_id": trace_result.get("current_action_id"),
                "procedure_status": trace_result.get("procedure_status"),
                "current_procedure": self.compact_procedure(
                    trace_result.get("current_procedure")
                ),
                "scene": self.compact_scene(trace_result.get("scene_after_action")),
                "events": self.compact_events(
                    trace_result.get("scene_events_after_action")
                ),
            },
        }

    def trace_action_owner(
        self,
        trace_input: dict[str, object],
        trace_result: dict[str, object],
    ) -> str:
        """Return whether the action was selected by the model or runtime."""
        request_kind = trace_input.get("request_kind")
        if trace_result.get("model_skipped") is True:
            return "runtime"
        if request_kind in {"auto_continue_code", "auto_no_available_actions"}:
            return "runtime"
        return "model"

    def compact_scene(self, scene: object) -> dict[str, object] | None:
        """Summarize a scene without volatile refs or duplicate raw payloads."""
        if not isinstance(scene, dict):
            return None
        compact: dict[str, object] = {}
        for key in ("room_description", "visibility", "location_context", "player"):
            value = scene.get(key)
            if value is not None:
                compact[key] = value
        for key in ("exits", "items", "features", "areas", "entities"):
            entries = scene.get(key)
            if not isinstance(entries, list) or not entries:
                continue
            compact[key] = [self.compact_scene_entry(entry) for entry in entries[:8]]
        return compact

    def compact_scene_entry(self, entry: object) -> object:
        """Keep only stable human-relevant fields for one scene entry."""
        if not isinstance(entry, dict):
            return entry
        compact = {}
        for key in ("description", "pos", "direction", "distance_steps"):
            value = entry.get(key)
            if value is not None:
                compact[key] = value
        return compact

    def payload_available_actions(self, payload: dict[str, object]) -> list[object]:
        """Extract available actions from either full or compact payload shape."""
        actions = payload.get("available_actions")
        return actions if isinstance(actions, list) else []

    def compact_actions(self, actions: list[object]) -> list[dict[str, object]]:
        """Summarize available actions for trace readability."""
        compact = []
        for action in actions[:12]:
            compact_action = self.compact_action(action)
            if compact_action is not None:
                compact.append(compact_action)
        return compact

    def compact_action(self, action: object) -> dict[str, object] | None:
        """Keep the fields needed to understand one selected/available action."""
        if not isinstance(action, dict):
            return None
        result: dict[str, object] = {}
        field_map = (
            ("action_id", "id"),
            ("id", "id"),
            ("label", "label"),
            ("action_type", "type"),
            ("type", "type"),
            ("target_key", "target"),
            ("goal", "goal"),
            ("low_level_goal", "goal"),
            ("next_action", "next_action"),
            ("priority", "priority"),
            ("selection_priority", "priority"),
            ("steps", "steps"),
            ("distance_steps", "steps"),
        )
        for source, target in field_map:
            if target in result:
                continue
            value = action.get(source)
            if value is not None:
                result[target] = value
        return result

    def compact_procedure(self, procedure: object) -> dict[str, object] | None:
        """Summarize active procedure state."""
        if not isinstance(procedure, dict):
            return None
        result = {}
        for key in (
            "action_id",
            "action_type",
            "status",
            "low_level_goal",
            "next_action",
            "path_steps",
        ):
            value = procedure.get(key)
            if value is not None:
                result[key] = value
        return result

    def compact_events(self, events: object) -> list[object]:
        """Summarize model-facing events."""
        if not isinstance(events, list):
            return []
        compact = []
        for event in events[:8]:
            if isinstance(event, dict):
                compact_event = {}
                for key in ("type", "procedure", "status", "text"):
                    value = event.get(key)
                    if value is not None:
                        compact_event[key] = value
                compact.append(compact_event)
            else:
                compact.append(event)
        return compact

    def compact_recent_actions(self, payload: dict[str, object]) -> list[object]:
        """Extract recent low-level actions from either payload shape."""
        recent = payload.get("recent_actions")
        if not isinstance(recent, list):
            recent = payload.get("recent_low_level_actions")
        if not isinstance(recent, list):
            return []
        return recent[-8:]

    def append_response_history(self, response: str) -> None:
        """Store one completed model response in the persistent frontend trace."""
        cleaned = response.strip()
        if not cleaned:
            return
        self.response_history.append(cleaned)

    def payload_pane_text(self) -> str:
        """Build a human-readable summary of the current model payload."""
        payload = self.decoded_last_payload()
        if not isinstance(payload, dict):
            return str(payload or "")
        return "\n".join(self.payload_display_lines(payload))

    def payload_display_lines(self, payload: dict[str, object]) -> list[str]:
        """Format the model payload as a compact bullet list for the TUI."""
        lines: list[str] = []
        scene = payload.get("scene")
        if not isinstance(scene, dict):
            scene_state = payload.get("scene_state")
            scene = scene_state if isinstance(scene_state, dict) else {}

        decision = payload.get("decision")
        decision_request = payload.get("decision_request")
        mode = None
        if isinstance(decision, dict):
            mode = decision.get("mode")
        elif isinstance(decision_request, dict):
            mode = decision_request.get("mode")
        if isinstance(mode, str):
            lines.append(f"- Mode: {mode}")

        identity = scene.get("identity")
        if isinstance(identity, str) and identity:
            lines.append(f"- Player: {identity}")

        room = scene.get("room_description")
        visibility = scene.get("visibility")
        if isinstance(room, str) and room:
            if isinstance(visibility, str) and visibility:
                lines.append(f"- Scene: {room} ({visibility})")
            else:
                lines.append(f"- Scene: {room}")

        context = scene.get("location_context")
        if isinstance(context, dict):
            lines.extend(self.location_context_display_lines(context))

        visible = scene.get("visible")
        if not isinstance(visible, list):
            tabletop = scene.get("tabletop")
            visible = tabletop if isinstance(tabletop, list) else []
        if visible:
            lines.append("- Visible:")
            for entry in visible[:8]:
                text = self.visible_entry_display_text(entry)
                if text:
                    lines.append(f"  - {text}")

        for title, key in (
            ("Events", "events"),
            ("Hazards", "hazards"),
            ("Recent", "recent_actions"),
        ):
            values = payload.get(key)
            if not isinstance(values, list):
                values = payload.get(
                    {
                        "events": "scene_events",
                        "hazards": "ongoing_hazards",
                        "recent_actions": "recent_low_level_actions",
                    }[key]
                )
            if isinstance(values, list) and values:
                lines.append(f"- {title}:")
                for value in values[:6]:
                    text = self.short_display_value(value)
                    if text:
                        lines.append(f"  - {text}")

        procedure = payload.get("current_procedure")
        if isinstance(procedure, dict):
            action_id = procedure.get("action_id")
            status = procedure.get("status")
            goal = procedure.get("goal") or procedure.get("low_level_goal")
            parts = [str(part) for part in (status, action_id, goal) if part]
            if parts:
                lines.append("- Procedure: " + " | ".join(parts))
        else:
            lines.append("- Procedure: none")

        actions = payload.get("available_actions")
        if isinstance(actions, list) and actions:
            lines.append("- Available actions:")
            for action in actions[:10]:
                text = self.action_display_text(action)
                if text:
                    lines.append(f"  - {text}")

        question = payload.get("user_question")
        if isinstance(question, str) and question and mode != "choose_action":
            lines.append(f"- Question: {question}")

        return lines or ["- No payload available."]

    def location_context_display_lines(
        self,
        context: dict[str, object],
    ) -> list[str]:
        """Format location context fields for the payload pane."""
        parts = []
        area_type = context.get("area_type")
        if isinstance(area_type, str):
            parts.append(area_type)
        if context.get("in_room") is True:
            parts.append("in room")
        if context.get("in_corridor") is True:
            parts.append("in corridor")
        if context.get("dark") is True:
            parts.append("dark")
        lines = [f"- Location: {', '.join(parts)}"] if parts else []
        for label, key in (
            ("Corridors", "adjacent_corridors"),
            ("Doors", "adjacent_doors"),
        ):
            values = context.get(key)
            if isinstance(values, list) and values:
                lines.append(f"- {label}: " + ", ".join(str(value) for value in values))
        return lines

    def visible_entry_display_text(self, entry: object) -> str | None:
        """Format one visible scene entry for the payload pane."""
        if not isinstance(entry, dict):
            return self.short_display_value(entry)
        summary = entry.get("summary")
        if isinstance(summary, str):
            return summary
        label = entry.get("label") or entry.get("description") or entry.get("id")
        if not isinstance(label, str) or not label:
            return None
        kind = entry.get("kind")
        steps = entry.get("steps") or entry.get("distance_steps")
        path = entry.get("path") or entry.get("direction_sequence")
        detail = []
        if isinstance(kind, str):
            detail.append(kind)
        if isinstance(steps, int):
            detail.append(f"{steps} steps")
        if isinstance(path, list) and path:
            detail.append("via " + ", ".join(str(step) for step in path[:5]))
        return label if not detail else f"{label} ({'; '.join(detail)})"

    def action_display_text(self, action: object) -> str | None:
        """Format one available action for the payload pane."""
        if not isinstance(action, dict):
            return self.short_display_value(action)
        action_id = action.get("id") or action.get("action_id")
        label = action.get("label") or action_id
        if not isinstance(label, str) or not label:
            return None
        details = []
        priority = action.get("priority") or action.get("selection_priority")
        steps = action.get("steps") or action.get("distance_steps")
        goal = action.get("goal") or action.get("low_level_goal")
        if isinstance(action_id, str):
            details.append(action_id)
        if isinstance(priority, str):
            details.append(priority)
        if isinstance(steps, int):
            details.append(f"{steps} steps")
        if isinstance(goal, str):
            details.append(goal)
        if action.get("disabled"):
            details.append("disabled")
        return label if not details else f"{label} ({'; '.join(details)})"

    def short_display_value(self, value: object) -> str | None:
        """Return a concise display string for one scalar or dict value."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            text = value.get("text")
            if isinstance(text, str):
                return text
            description = value.get("description")
            if isinstance(description, str):
                return description
        if value is None:
            return None
        return str(value)

    def response_entry_display_text(self, response: str) -> str:
        """Format one model response for human-readable UI display."""
        cleaned = response.strip()
        action = None
        raw_decision = cleaned
        if "\n\nAction:" in cleaned:
            raw_decision, action = cleaned.split("\n\nAction:", 1)
            action = action.strip()

        try:
            parsed = json.loads(raw_decision)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("decision"), dict):
            parsed = parsed.get("decision")
        if not isinstance(parsed, dict):
            return cleaned

        lines = []
        decision = parsed.get("decision")
        chosen = parsed.get("chosen_action_id")
        reason = parsed.get("reason")
        if isinstance(decision, str):
            lines.append(f"- Decision: {decision}")
        if isinstance(chosen, str):
            lines.append(f"- Chosen: {chosen}")
        if isinstance(reason, str) and reason:
            lines.append(f"- Reason: {reason}")
        if action:
            lines.append(f"- Executed: {action}")
        return "\n".join(lines) if lines else cleaned

    def response_pane_text(self) -> str:
        """Build response-pane text from history plus any in-flight response."""
        parts = list(self.response_history)
        current = self.last_response.strip()
        if current:
            if not parts or parts[-1] != current:
                parts.append(current)
        return "\n\n".join(self.response_entry_display_text(part) for part in parts)
