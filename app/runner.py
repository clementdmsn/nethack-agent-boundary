from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.commands import RuntimeCommandsMixin
from app.state import RUNTIME_STATE_FIELDS, RuntimeState
from config import BASE_URL, MODEL, NETHACK_BIN
from constants.runtime import AUTO_PROMPT, SKIP_INTRO_KEYS
from model.session import ModelSessionMixin
from navigation.pathfinding import PathfindingMixin
from observation.constants import END_FLAG, MORE_FLAG, MORE_PATTERN
from telemetry.observation_log import (
    LAST_EXECUTION_TRACE_PATH,
    LOG_DIR,
    SAVED_TRACE_DIR,
    ObservationLogMixin,
)
from observation.legacy import LegacyObservationMixin
from observation.session import ObservationSessionMixin
from observation.textlog import ScreenTextLogMixin
from observation.viewport import (
    MapViewport,
    ObservationTarget,
    ViewportObservationMixin,
)
from prompting.context import PromptContextMixin
from terminal.nethack_terminal import NetHackTerminal
from terminal.ui import TuiMixin

if TYPE_CHECKING:
    from model.client import ModelClient


class Runner(
    RuntimeCommandsMixin,
    ScreenTextLogMixin,
    LegacyObservationMixin,
    ViewportObservationMixin,
    ObservationSessionMixin,
    PathfindingMixin,
    PromptContextMixin,
    ModelSessionMixin,
    ObservationLogMixin,
    TuiMixin,
):
    def __getattr__(self, name: str):
        # Proxies known runtime state fields through the embedded state object.
        state = self.__dict__.get("state")
        if state is not None and name in RUNTIME_STATE_FIELDS:
            return getattr(state, name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value) -> None:
        # Writes known runtime state fields into the embedded state object.
        state = self.__dict__.get("state")
        if state is not None and name in RUNTIME_STATE_FIELDS:
            setattr(state, name, value)
            return
        object.__setattr__(self, name, value)

    def __init__(self) -> None:
        # Builds the coordinator and starts the NetHack terminal session.
        self.state = RuntimeState(terminal=NetHackTerminal())
        self.reset_runtime_logs_for_run()
        self.start_new_game_for_roll(reset_logs=False)

    def start_new_game_for_roll(self, *, reset_logs: bool = True) -> None:
        """Start a fresh game and stop before any parser/model handoff."""
        old_model = self.model
        old_terminal = self.terminal
        if old_terminal is not None:
            stop = getattr(old_terminal, "stop", None)
            if stop is not None:
                stop()
        self.state = RuntimeState(terminal=NetHackTerminal(), model=old_model)
        if reset_logs:
            self.reset_runtime_logs_for_run()
        self.terminal.start()
        self.skip_intro()
        self.render_screen(print_output=False)
        self.last_payload = ""
        self.last_response = "Rolled start. Use /roll to reroll or /start to run."

    def skip_intro(self) -> None:
        """Advance the NetHack intro so the first dungeon screen is visible."""
        for key in SKIP_INTRO_KEYS:
            self.terminal.send_keys(key)
            self.render_screen(print_output=False)
        self.settle_startup_screen()

    def settle_startup_screen(self, max_pages: int = 8) -> None:
        """Dismiss startup paging without running observation parsing."""
        for _attempt in range(max_pages):
            screen = self.render_screen(print_output=False)
            if not self.screen_needs_startup_advance(screen):
                return
            self.terminal.send_keys("\n")
        self.render_screen(print_output=False)

    def accept_start_and_run(self) -> None:
        """Accept the visible start and let auto mode begin observing."""
        self.auto_mode = True
        self.next_auto_request_at = 0.0
        self.last_response = "Start accepted. Auto mode enabled."
        self.append_response_history(self.last_response)

    def render_screen(self, print_output: bool = True) -> str:
        # Refreshes cached screen text and optionally prints it to stdout.
        self.screen = self.terminal.render()
        if self.capture_prompted_text:
            self.record_prompted_text(self.screen)
        self.update_player_identity_from_screen(self.screen)

        if not print_output:
            return self.screen

        print("\n--- SCREEN ---\n")
        print(self.screen)
        return self.screen

    def print_header(self) -> None:
        # Prints a small startup summary and the available commands.
        print("\n================================================")
        print("NetHack Agent Runtime")
        print("================================================")
        print(f"Executable : {NETHACK_BIN}")
        print(f"Model      : {MODEL}")
        print(f"Base URL   : {BASE_URL}")
        print("================================================")

        print("\nCommands:")
        print("/roll           reroll a fresh skipped-intro start")
        print("/start          accept this start and run the agent")
        print("/step           run one model step")
        print("/save-trace <name> save latest trace as a curated demo artifact")
        print("/quit           exit")

    def send_keys_slow(self, keys: str, delay: float = 0.15) -> None:
        # Sends a key sequence one character at a time for manual debugging.
        for key in keys:
            self.terminal.send_keys(key)
            time.sleep(delay)
            self.render_screen()

    def clean_message_line(self, line: str) -> str:
        # Removes paging markers from a message line.
        return MORE_PATTERN.sub("", line).strip()

    def screen_has_more(self, screen: str) -> bool:
        # Detects whether the current screen is waiting on a --More-- prompt.
        return MORE_FLAG in screen

    def screen_has_tutorial_end(self, screen: str) -> bool:
        # Detects the tutorial/end marker shown during farlook help text.
        return END_FLAG in screen

    def screen_has_startup_greeting_without_map(self, screen: str) -> bool:
        # Some NetHack builds show a welcome page without a visible --More--.
        lowered = screen.lower()
        return (
            "hello " in lowered
            and "welcome to nethack" in lowered
            and "@" not in screen
        )

    def screen_needs_startup_advance(self, screen: str) -> bool:
        # Keeps /roll from stopping on non-map startup pages.
        return (
            self.screen_has_more(screen)
            or self.screen_has_tutorial_end(screen)
            or self.screen_has_startup_greeting_without_map(screen)
        )


__all__ = [
    "AUTO_PROMPT",
    "LAST_EXECUTION_TRACE_PATH",
    "LOG_DIR",
    "MapViewport",
    "ObservationTarget",
    "Runner",
    "RuntimeState",
    "SAVED_TRACE_DIR",
]
