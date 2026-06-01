from __future__ import annotations

import json
from pathlib import Path
import re


LOG_DIR = Path("logs")
LAST_EXECUTION_TRACE_PATH = LOG_DIR / "last_execution_trace.md"
SAVED_TRACE_DIR = Path("docs/demo/traces")


class ObservationLogMixin:
    def ensure_log_parent(self, path: Path) -> None:
        # Creates the runtime log directory when default paths are used.
        path.parent.mkdir(parents=True, exist_ok=True)

    def reset_runtime_logs_for_run(self) -> None:
        # Clears the unified runtime trace once at process startup.
        if not self.trace_logging_enabled:
            return
        path = self.last_execution_trace_path()
        self.ensure_log_parent(path)
        path.write_text("# NetHack Agent Last Execution Trace\n\n", encoding="utf-8")

    def last_execution_trace_path(self) -> Path:
        # Resolves the overwrite-style unified execution trace path.
        import app.runner as runtime

        return runtime.LAST_EXECUTION_TRACE_PATH

    def saved_trace_dir(self) -> Path:
        # Resolves the curated trace directory for manually saved examples.
        import app.runner as runtime

        return runtime.SAVED_TRACE_DIR

    def append_last_execution_trace_entry(
        self,
        title: str,
        *parts: str,
    ) -> None:
        """Append one chronological entry to the latest execution trace."""
        if not self.trace_logging_enabled:
            return
        path = self.last_execution_trace_path()
        self.ensure_log_parent(path)
        body = "\n\n".join(part for part in parts if part)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"## {title}\n\n")
            if body:
                handle.write(body.rstrip())
                handle.write("\n\n")

    def fenced_text(self, value: object, language: str = "text") -> str:
        """Format text or structured data as a Markdown fenced block."""
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, indent=2)
            language = "json"
        return f"```{language}\n{text.rstrip()}\n```"

    def screen_snapshot_text(
        self,
        *,
        reason: str | None = None,
        request_kind: str | None = None,
    ) -> str | None:
        """Build the latest rendered screen section for the chronological trace."""
        if not self.screen:
            return None

        room_description = ""
        element_count = 0
        if self.last_scene is not None:
            room_value = self.last_scene.get("room_description")
            if isinstance(room_value, str):
                room_description = room_value
            for key in ("features", "items", "areas", "entities", "elements", "exits"):
                values = self.last_scene.get(key)
                if isinstance(values, list):
                    element_count += len(values)

        lines = []
        if reason:
            lines.append(f"- Reason: `{reason}`")
        if request_kind:
            lines.append(f"- Request kind: `{request_kind}`")
        if self.player_identity:
            lines.append(f"- Player: {self.player_identity}")
        if room_description:
            lines.append(f"- Parser room: {room_description}")
        lines.append(f"- Parsed elements: {element_count}")
        cleanup = self.last_observation_cleanup
        if isinstance(cleanup, dict) and cleanup:
            lines.append(
                "- Observation cleanup: "
                f"farlook used `{cleanup.get('farlook_used', False)}`, "
                "returned to normal mode "
                f"`{cleanup.get('returned_to_normal_mode', False)}`"
            )
        lines.append("")
        lines.append(self.fenced_text(self.screen))
        return "\n".join(lines)

    def append_screen_snapshot_log(
        self,
        *,
        reason: str | None = None,
        request_kind: str | None = None,
    ) -> None:
        # Writes the latest rendered screen into the chronological trace.
        payload = self.screen_snapshot_text(
            reason=reason,
            request_kind=request_kind,
        )
        if payload is not None:
            self.append_last_execution_trace_entry("Screen", payload)

    def append_model_request_screen_snapshot(self, request_kind: str) -> None:
        # Captures the exact screen state immediately before a model request.
        screen = self.screen_snapshot_text(
            reason="before_model_request",
            request_kind=request_kind,
        )
        parts = ["- Action owner: `model`"]
        if screen is not None:
            parts.append(screen)
        payload = self.decoded_last_payload()
        if payload is not None:
            parts.append("### Parser Payload\n\n" + self.fenced_text(payload, "json"))
        self.append_last_execution_trace_entry("Model Handoff", *parts)

    def print_last_scene(self) -> None:
        # Prints the most recent parsed scene and text log for debugging.
        if self.last_scene is None:
            return

        print("\n--- FINAL LOOK JSON ---\n")
        print(json.dumps(self.last_scene, indent=2))

        if self.last_text_log:
            print("\n--- FINAL TEXT LOG ---\n")
            print("\n\n".join(self.last_text_log))

        self.write_debug_log()

    def write_debug_log(self) -> None:
        # Writes the latest scene/debug dump to the chronological trace.
        if self.last_scene is None:
            return

        parts = ["### Final Scene\n\n" + self.fenced_text(self.last_scene, "json")]
        if self.last_observation_cleanup:
            parts.append(
                "### Observation Cleanup\n\n"
                + self.fenced_text(self.last_observation_cleanup, "json")
            )
        if self.last_text_log:
            parts.append(
                "### Observation Text Log\n\n"
                "Captured while building the scene; this is not necessarily the "
                "current terminal prompt after cleanup.\n\n"
                + self.fenced_text("\n\n".join(self.last_text_log))
            )
        self.append_last_execution_trace_entry("Debug Snapshot", *parts)

    def append_model_response_log(self, request_kind: str, response: str) -> None:
        """Append a completed model response to the chronological trace."""
        self.append_last_execution_trace_entry(
            "Model Response",
            f"- Request kind: `{request_kind}`",
            self.fenced_text(response or ""),
        )

    def write_last_decision_trace_log(self) -> None:
        # Writes the latest model interaction trace to a single overwrite log.
        trace = self.compact_decision_trace_log()

        self.append_last_execution_trace_entry(
            "Execution Result",
            f"- Action owner: `{trace['request']['action_owner']}`",
            self.execution_trace_summary(trace),
        )

    def execution_trace_summary(self, trace: dict[str, object]) -> str:
        """Format one execution result as a short human-readable Markdown block."""
        request = trace.get("request") if isinstance(trace.get("request"), dict) else {}
        before = trace.get("before") if isinstance(trace.get("before"), dict) else {}
        model = trace.get("model") if isinstance(trace.get("model"), dict) else {}
        execution = (
            trace.get("execution") if isinstance(trace.get("execution"), dict) else {}
        )
        after = trace.get("after") if isinstance(trace.get("after"), dict) else {}

        parts = [
            f"- Request kind: `{request.get('kind')}`",
            f"- Model skipped: `{request.get('model_skipped', False)}`",
        ]
        before_proc = self.trace_procedure_label(before.get("current_procedure"))
        after_proc = self.trace_procedure_label(after.get("current_procedure"))
        if before_proc or after_proc:
            parts.append(
                f"- Procedure: `{before_proc or 'none'}` -> `{after_proc or 'none'}`"
            )

        parsed_decision = model.get("parsed_decision")
        if isinstance(parsed_decision, dict):
            decision = parsed_decision.get("decision")
            chosen = parsed_decision.get("chosen_action_id")
            reason = parsed_decision.get("reason")
            if decision or chosen:
                text = f"- Decision: `{decision or 'unknown'}`"
                if chosen:
                    text += f" `{chosen}`"
                if reason:
                    text += f" - {reason}"
                parts.append(text)

        selected_action = execution.get("selected_action")
        selected_label = self.trace_action_label(selected_action)
        if selected_label:
            parts.append(f"- Selected action: `{selected_label}`")
        executed = execution.get("executed_low_level_action")
        if executed:
            parts.append(f"- Executed: `{executed}`")

        outcome = execution.get("outcome")
        if isinstance(outcome, dict):
            status = outcome.get("status")
            outcome_bits = []
            if status:
                outcome_bits.append(f"status `{status}`")
            if "steps" in outcome:
                outcome_bits.append(f"steps `{outcome.get('steps')}`")
            if "attempts" in outcome:
                outcome_bits.append(f"attempts `{outcome.get('attempts')}`")
            if "scene_changed" in outcome:
                outcome_bits.append(f"scene changed `{outcome.get('scene_changed')}`")
            if outcome_bits:
                parts.append("- Outcome: " + ", ".join(outcome_bits))
            chain = self.runtime_chain_summary(trace)
            if chain:
                parts.append(chain)

        before_scene = self.trace_scene_summary(before.get("scene"))
        after_scene = self.trace_scene_summary(after.get("scene"))
        if before_scene:
            parts.append(f"- Before: {before_scene}")
        if after_scene:
            parts.append(f"- After: {after_scene}")

        events = after.get("events")
        if isinstance(events, list) and events:
            parts.append("- Events:")
            for event in events:
                text = self.trace_event_text(event)
                if text:
                    parts.append(f"  - {text}")

        actions = before.get("available_actions")
        if isinstance(actions, list) and actions:
            labels = []
            for action in actions[:6]:
                label = self.trace_action_label(action)
                if label:
                    labels.append(label)
            if labels:
                suffix = "" if len(actions) <= 6 else f" (+{len(actions) - 6} more)"
                parts.append(
                    "- Available before: "
                    + ", ".join(f"`{label}`" for label in labels)
                    + suffix
                )

        return "\n".join(parts)

    def runtime_chain_summary(self, trace: dict[str, object]) -> str | None:
        """Return a demo-readable summary for runtime-managed chains."""
        request = trace.get("request") if isinstance(trace.get("request"), dict) else {}
        execution = (
            trace.get("execution") if isinstance(trace.get("execution"), dict) else {}
        )
        outcome = execution.get("outcome")
        if not isinstance(outcome, dict):
            return None
        if request.get("action_owner") != "runtime":
            return None

        status = outcome.get("status")
        if not isinstance(status, str):
            return None

        steps = outcome.get("steps")
        selected = self.trace_action_label(execution.get("selected_action"))
        executed = execution.get("executed_low_level_action")
        bits = []
        if selected:
            bits.append(f"continued `{selected}`")
        if isinstance(steps, int):
            bits.append(f"advanced `{steps}` step{'s' if steps != 1 else ''}")
        elif isinstance(executed, str):
            bits.append(f"sent `{executed}`")
        bits.append(f"stopped on `{status}`")
        return "- Runtime chain: " + ", ".join(bits)

    def trace_action_label(self, action: object) -> str | None:
        """Return a compact identifier for one action-like dict."""
        if not isinstance(action, dict):
            return None
        action_id = action.get("id") or action.get("action_id")
        label = action.get("label")
        if isinstance(action_id, str):
            return action_id
        if isinstance(label, str):
            return label
        return None

    def trace_procedure_label(self, procedure: object) -> str | None:
        """Return compact procedure id/status text."""
        if not isinstance(procedure, dict):
            return None
        action_id = procedure.get("action_id")
        status = procedure.get("status")
        if isinstance(action_id, str) and isinstance(status, str):
            return f"{action_id} ({status})"
        if isinstance(action_id, str):
            return action_id
        if isinstance(status, str):
            return status
        return None

    def trace_event_text(self, event: object) -> str | None:
        """Return compact text for a scene/procedure event."""
        if isinstance(event, str):
            return event
        if not isinstance(event, dict):
            return None
        text = event.get("text")
        if isinstance(text, str):
            return text
        status = event.get("status")
        if isinstance(status, str):
            return status
        return None

    def trace_scene_summary(self, scene: object) -> str | None:
        """Summarize the scene context in one trace line."""
        if not isinstance(scene, dict):
            return None
        bits = []
        room = scene.get("room_description")
        if isinstance(room, str):
            bits.append(room)
        context = scene.get("location_context")
        if isinstance(context, dict):
            area = context.get("area_type")
            if isinstance(area, str):
                bits.append(f"area `{area}`")
            corridors = context.get("adjacent_corridors")
            if isinstance(corridors, list) and corridors:
                bits.append("corridors " + ", ".join(str(value) for value in corridors))
            doors = context.get("adjacent_doors")
            if isinstance(doors, list) and doors:
                bits.append("doors " + ", ".join(str(value) for value in doors))
        for key in ("entities", "items", "features", "exits"):
            values = scene.get(key)
            if isinstance(values, list) and values:
                bits.append(f"{len(values)} {key}")
        return "; ".join(bits) if bits else None

    def append_runtime_automation_log(
        self,
        *,
        request_kind: str,
        action: dict[str, object] | None,
    ) -> None:
        """Log when runtime code takes ownership of an active procedure."""
        action_id = action.get("action_id") if isinstance(action, dict) else None
        self.append_last_execution_trace_entry(
            "Runtime Automation",
            f"- Request kind: `{request_kind}`",
            "- Action owner: `runtime`",
            f"- Action: `{action_id or self.current_action_id or 'unknown'}`",
        )

    def append_runtime_input_log(
        self,
        *,
        keys: str,
        action: str,
        owner: str,
    ) -> None:
        """Log one gameplay input sent by runtime or model-owned execution."""
        self.append_last_execution_trace_entry(
            "Runtime Input",
            f"- Action owner: `{owner}`",
            f"- Low-level action: `{action}`",
            f"- Keys sent: `{keys}`",
        )

    def append_runtime_input_summary_log(
        self,
        *,
        owner: str,
        procedure: str,
        actions: list[str],
        keys: list[str],
    ) -> None:
        """Log a compact summary of runtime-managed repeated inputs."""
        if not actions and not keys:
            return
        self.append_last_execution_trace_entry(
            "Runtime Input Summary",
            f"- Action owner: `{owner}`",
            f"- Procedure: `{procedure}`",
            f"- Inputs: {self.compact_action_sequence(actions)}",
            f"- Keys sent: {self.compact_key_sequence(keys)}",
        )

    def compact_action_sequence(self, actions: list[str]) -> str:
        """Return a readable compressed sequence like `move(north) x3`."""
        return self.compact_sequence(actions)

    def compact_key_sequence(self, keys: list[str]) -> str:
        """Return a readable compressed key sequence."""
        return self.compact_sequence(keys)

    def compact_sequence(self, values: list[str]) -> str:
        """Compress consecutive duplicate values for trace readability."""
        if not values:
            return "`none`"
        chunks: list[str] = []
        current = values[0]
        count = 1
        for value in values[1:]:
            if value == current:
                count += 1
                continue
            chunks.append(self.compact_sequence_chunk(current, count))
            current = value
            count = 1
        chunks.append(self.compact_sequence_chunk(current, count))
        return ", ".join(chunks)

    def compact_sequence_chunk(self, value: str, count: int) -> str:
        if count <= 1:
            return f"`{value}`"
        return f"`{value}` x{count}"

    def save_last_decision_trace_log(self, name: str) -> Path:
        # Saves the current model interaction trace as a curated demo artifact.
        trace = self.compact_decision_trace_log()
        path = self.saved_trace_log_path(name)
        self.ensure_log_parent(path)
        path.write_text(
            json.dumps(trace, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def saved_trace_log_path(self, name: str) -> Path:
        # Builds a stable curated trace path from a user-provided name.
        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "_", name).strip("._-")
        if not safe_name:
            safe_name = "trace"
        if not safe_name.endswith(".trace.json"):
            safe_name = f"{safe_name}.trace.json"
        return self.saved_trace_dir() / safe_name
