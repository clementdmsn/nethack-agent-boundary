from __future__ import annotations

import json
import queue
import threading


class ModelRequestMixin:
    def get_model(self):
        """Create the model client lazily for the runtime."""
        if self.model is None:
            from model.client import ModelClient

            self.model = ModelClient()

        return self.model

    def refresh_payload(self, user_input: str = "") -> str:
        """Rebuild the scene payload for the next model request."""
        scene = self.look()
        self.last_observed_scene = (
            json.loads(json.dumps(scene)) if isinstance(scene, dict) else scene
        )
        self.last_payload = self.build_model_prompt(user_input, scene)
        return self.last_payload

    def refresh_cached_payload_from_scene(
        self,
        user_input: str,
        scene: dict[str, object] | None,
    ) -> None:
        """Update action/procedure context from an already observed scene."""
        if not isinstance(scene, dict):
            return
        self.last_observed_scene = json.loads(json.dumps(scene))
        self.last_payload = self.build_model_prompt(user_input, scene)

    def submit_model_prompt(
        self,
        user_input: str,
        on_response_delta=None,
    ) -> str:
        """Run one synchronous model request and record the streamed response."""
        prompt = self.refresh_payload(user_input)
        self.append_model_request_screen_snapshot("manual")
        self.last_response = ""

        def record_delta(delta: str) -> None:
            """Accumulate streamed model text and forward it to the caller."""
            self.last_response += delta
            if on_response_delta is not None:
                on_response_delta(delta)

        response = self.get_model().ask_stream(
            prompt=prompt,
            on_delta=record_delta,
        )
        self.snapshot_trace_input("manual")
        self.last_response = response
        self.last_raw_model_response = response
        self.last_parsed_decision = None
        self.last_selected_action = None
        self.last_executed_low_level_action = None
        self.last_execution_outcome = None
        self.append_model_response_log("manual", response)
        self.append_response_history(response)
        self.write_trace_result(request_kind="manual", scene_after_action=None)
        self.notify_generation_finished_for_debug()
        return response

    def start_model_request(
        self,
        user_input: str,
        request_kind: str = "manual",
        reuse_payload: bool = False,
    ) -> bool:
        """Start one background model request if no request is already running."""
        if self.model_generating:
            return False

        self.last_model_skipped = False
        if reuse_payload and isinstance(self.last_payload, str) and self.last_payload:
            prompt = self.last_payload
        else:
            prompt = self.refresh_payload(user_input)
        self.snapshot_trace_input(request_kind)
        self.append_model_request_screen_snapshot(request_kind)
        self.model_generating = True
        self.pending_model_request = request_kind
        self.awaiting_first_model_delta = True

        def worker() -> None:
            """Execute the background model request and post queue events."""
            try:
                response = self.get_model().ask_stream(
                    prompt=prompt,
                    on_delta=lambda delta: self.model_events.put(
                        ("delta", delta)
                    ),
                )
            except Exception as exc:
                self.model_events.put(("error", str(exc)))
                return

            self.model_events.put(("done", response))

        self.model_thread = threading.Thread(target=worker, daemon=True)
        self.model_thread.start()
        return True

    def drain_model_events(self) -> None:
        """Apply pending model stream events to runtime state."""
        while True:
            try:
                event, value = self.model_events.get_nowait()
            except queue.Empty:
                break

            if event == "delta":
                if self.awaiting_first_model_delta:
                    self.last_response = ""
                    self.awaiting_first_model_delta = False
                self.last_response += value
            elif event == "done":
                self.awaiting_first_model_delta = False
                self.last_response = value
                self.last_raw_model_response = value
                self.last_model_skipped = False
                self.model_generating = False
                request_kind = self.pending_model_request or "manual"
                self.append_model_response_log(request_kind, value)
                if request_kind not in {"auto", "step"}:
                    self.append_response_history(value)
                self.finish_model_request(value)
                self.notify_generation_finished_for_debug()
            elif event == "error":
                self.awaiting_first_model_delta = False
                self.last_response = f"Model error: {value}"
                self.last_raw_model_response = self.last_response
                self.last_parsed_decision = None
                self.last_selected_action = None
                self.last_model_skipped = False
                self.last_executed_low_level_action = None
                self.last_execution_outcome = {
                    "status": "model_error",
                    "scene_changed": False,
                }
                self.append_response_history(self.last_response)
                self.model_generating = False
                if self.pending_model_request == "auto":
                    self.auto_mode = False
                request_kind = self.pending_model_request or "manual"
                self.pending_model_request = None
                self.write_trace_result(
                    request_kind=request_kind,
                    scene_after_action=None,
                )
                self.notify_generation_finished_for_debug()

    def finish_model_request(self, response: str) -> None:
        """Dispatch a completed model response based on request type."""
        request_kind = self.pending_model_request
        self.pending_model_request = None

        if request_kind == "step":
            self.apply_decision_response(response)
        elif request_kind == "auto" and self.auto_mode:
            self.apply_decision_response(response)
