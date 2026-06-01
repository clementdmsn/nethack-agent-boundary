from __future__ import annotations

from constants.runtime import AUTO_PROMPT, CTRL_C


class RuntimeCommandsMixin:
    def handle_tui_command(
        self,
        user_input: str,
        on_response_delta=None,
    ) -> None:
        # Routes one slash command or free-form prompt from the TUI.
        if user_input == "/roll":
            self.start_new_game_for_roll()
            return

        if user_input == "/start":
            self.accept_start_and_run()
            return

        if user_input == "/step":
            self.start_model_request(AUTO_PROMPT, request_kind="step")
            return

        if user_input.startswith("/save-trace"):
            name = user_input.removeprefix("/save-trace").strip()
            path = self.save_last_decision_trace_log(name)
            self.last_response = f"Saved trace: {path}"
            self.append_response_history(self.last_response)
            return

        if user_input == "/quit":
            self.should_exit = True
            return

        if user_input.startswith("/"):
            self.last_response = f"Unknown command: {user_input}"
            self.append_response_history(self.last_response)
            return

        self.submit_model_prompt(
            user_input,
            on_response_delta=on_response_delta,
        )

    def handle_raw_key(self, key: str) -> None:
        # Handles raw key mode input and forwards it to the game.
        if key == CTRL_C:
            self.raw_keys_mode = False
            return

        self.terminal.send_keys(key)
        self.render_screen(print_output=False)
