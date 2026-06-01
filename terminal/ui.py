from __future__ import annotations

import curses

from config import BASE_URL, MODEL
from constants.runtime import CTRL_C
from terminal.colors import (
    CURSES_COLORS,
    MIN_GAME_COLS,
    MIN_GAME_ROWS,
    MIN_SIDE_PANE_COLS,
)


class TuiMixin:
    def run(self) -> None:
        # Runs the curses UI and always flushes final debug state on exit.
        try:
            curses.wrapper(self.run_tui)
        finally:
            self.write_debug_log()
            self.terminal.stop()

    def run_tui(self, stdscr) -> None:
        # Drives the main curses event loop for the runtime interface.
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        curses.raw()
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
            except curses.error:
                pass
        stdscr.keypad(True)
        stdscr.timeout(100)
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
            curses.mouseinterval(0)
        except curses.error:
            pass

        input_buffer = ""

        while not self.should_exit:
            self.drain_model_events()
            self.maybe_start_auto_request()
            self.render_screen(print_output=False)
            self.draw_tui(stdscr, input_buffer)

            try:
                key = stdscr.get_wch()
            except curses.error:
                continue
            except KeyboardInterrupt:
                if self.raw_keys_mode:
                    self.raw_keys_mode = False
                else:
                    self.should_exit = True
                continue

            if self.raw_keys_mode:
                raw_key = self.curses_key_to_game_key(key)
                if raw_key is not None:
                    self.handle_raw_key(raw_key)
                continue

            if key == "\x1b":
                self.should_exit = True
                continue

            if key == curses.KEY_MOUSE:
                self.handle_mouse_event()
                continue

            if key == curses.KEY_PPAGE:
                self.scroll_payload(-5)
                continue

            if key == curses.KEY_NPAGE:
                self.scroll_payload(5)
                continue

            if key in ("\n", "\r"):
                user_input = input_buffer.strip()
                input_buffer = ""
                if user_input:
                    if user_input.startswith("/"):
                        self.handle_tui_command(user_input)
                    else:
                        self.start_model_request(user_input)
                continue

            if key in ("\b", "\x7f") or key == curses.KEY_BACKSPACE:
                input_buffer = input_buffer[:-1]
                continue

            if isinstance(key, str) and key.isprintable():
                input_buffer += key

    def curses_key_to_game_key(self, key) -> str | None:
        # Maps curses key events into the raw keys understood by NetHack.
        if key == "\x03":
            return CTRL_C
        if isinstance(key, str):
            return key

        special_keys = {
            curses.KEY_UP: "k",
            curses.KEY_DOWN: "j",
            curses.KEY_LEFT: "h",
            curses.KEY_RIGHT: "l",
            curses.KEY_ENTER: "\n",
        }
        return special_keys.get(key)

    def handle_mouse_event(self) -> None:
        # Handles mouse wheel scrolling in the payload and response panes.
        try:
            _id, x, y, _z, button_state = curses.getmouse()
        except curses.error:
            return

        if button_state & getattr(curses, "BUTTON4_PRESSED", 0):
            delta = -3
        elif button_state & getattr(curses, "BUTTON5_PRESSED", 0):
            delta = 3
        else:
            return

        if self.point_in_view(x, y, self.payload_view):
            self.scroll_payload(delta)
        elif self.point_in_view(x, y, self.response_view):
            self.scroll_response(delta)

    def point_in_view(
        self,
        x: int,
        y: int,
        view: tuple[int, int, int, int],
    ) -> bool:
        # Checks whether a screen coordinate falls inside a rectangular pane.
        view_y, view_x, view_height, view_width = view
        return (
            view_y <= y < view_y + view_height
            and view_x <= x < view_x + view_width
        )

    def scroll_payload(self, delta: int) -> None:
        # Updates the payload pane scroll position.
        self.payload_scroll = max(0, self.payload_scroll + delta)

    def scroll_response(self, delta: int) -> None:
        # Updates the response pane scroll position.
        self.response_scroll = max(0, self.response_scroll + delta)

    def draw_tui(self, stdscr, input_buffer: str) -> None:
        # Renders the full split-pane curses interface for the current state.
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        footer_height = 3
        min_height = MIN_GAME_ROWS + footer_height + 1
        min_width = MIN_GAME_COLS + MIN_SIDE_PANE_COLS + 1
        if height < min_height or width < min_width:
            self.safe_addstr(
                stdscr,
                0,
                0,
                (
                    f"Terminal too small. Need at least "
                    f"{min_width}x{min_height} for stable NetHack rendering."
                )[:width],
            )
            stdscr.refresh()
            return

        content_height = height - footer_height
        max_right_width = width - MIN_GAME_COLS - 1
        right_width = max(MIN_SIDE_PANE_COLS, int(width * 0.25))
        right_width = min(right_width, max_right_width)
        left_width = width - right_width - 1
        right_width = width - left_width - 1
        right_x = left_width + 1

        for y in range(content_height):
            self.safe_addstr(stdscr, y, left_width, "│")

        self.safe_addstr(stdscr, 0, 0, "GAME")
        game_height = content_height - 1
        self.resize_game_view(game_height, left_width)
        self.draw_game_cells(stdscr, 1, 0, game_height, left_width)

        response_title = "MODEL RESPONSE"
        payload_title = "MODEL PAYLOAD"
        response_height = max(5, content_height // 3)
        payload_height = content_height - response_height - 1
        self.payload_view = (0, right_x, payload_height, right_width)
        self.response_view = (
            payload_height + 1,
            right_x,
            response_height,
            right_width,
        )

        self.draw_lines(
            stdscr,
            0,
            right_x,
            payload_height,
            right_width,
            [payload_title] + self.payload_pane_text().splitlines(),
            scroll=self.payload_scroll,
        )
        self.safe_addstr(stdscr, payload_height, right_x, "─" * right_width)
        self.draw_lines(
            stdscr,
            payload_height + 1,
            right_x,
            response_height,
            right_width,
            [response_title]
            + self.wrap_text(self.response_pane_text(), right_width),
            scroll=self.response_scroll,
        )

        status_y = height - footer_height
        self.safe_addstr(stdscr, status_y, 0, "─" * width)
        process_status = "alive"
        child = getattr(self.terminal, "child", None)
        if child is not None and not child.isalive():
            process_status = "stopped"
        status = (
            f"Model: {MODEL} | Base URL: {BASE_URL} | "
            f"NetHack: {process_status}"
        )
        self.safe_addstr(stdscr, status_y + 1, 0, status[:width])

        if self.raw_keys_mode:
            prompt = "Raw keys active. Ctrl+C returns to model input."
        else:
            prompt = (
                "Enter submit | /roll reroll | /start run | /step once | Esc quit > "
                f"{input_buffer}"
            )
        self.safe_addstr(stdscr, status_y + 2, 0, prompt[:width])
        stdscr.move(status_y + 2, min(len(prompt), width - 1))
        stdscr.refresh()

    def resize_game_view(self, rows: int, cols: int) -> None:
        # Resizes the game terminal area to match the current UI layout.
        resize = getattr(self.terminal, "resize", None)
        if resize is None:
            return

        resize(rows, cols)
        self.render_screen(print_output=False)

    def draw_lines(
        self,
        stdscr,
        y: int,
        x: int,
        height: int,
        width: int,
        lines: list[str],
        scroll: int = 0,
    ) -> None:
        # Draws wrapped text lines into a bounded rectangular pane.
        if height <= 0 or width <= 0:
            return

        wrapped_lines = []
        for line in lines:
            wrapped_lines.extend(self.wrap_line(line, width))

        if scroll:
            max_scroll = max(0, len(wrapped_lines) - height)
            scroll = min(scroll, max_scroll)
        visible_lines = wrapped_lines[scroll : scroll + height]

        row = y
        for line in visible_lines:
            if row >= y + height:
                break
            self.safe_addstr(stdscr, row, x, line[:width])
            row += 1

    def draw_game_cells(
        self,
        stdscr,
        y: int,
        x: int,
        height: int,
        width: int,
    ) -> None:
        # Draws the live NetHack screen using per-cell glyph attributes when available.
        if height <= 0 or width <= 0:
            return

        render_cells = getattr(self.terminal, "render_cells", None)
        if render_cells is None:
            self.draw_lines(
                stdscr,
                y,
                x,
                height,
                width,
                self.screen.splitlines(),
            )
            return

        for row_index, row in enumerate(render_cells()[:height]):
            for col_index, cell in enumerate(row[:width]):
                attrs = self.cell_attrs(cell)
                self.safe_addstr(
                    stdscr,
                    y + row_index,
                    x + col_index,
                    cell.char,
                    attrs,
                )

    def cell_attrs(self, cell) -> int:
        # Converts a terminal cell's style data into curses attributes.
        attrs = 0
        if getattr(cell, "bold", False):
            attrs |= curses.A_BOLD
        if getattr(cell, "reverse", False):
            attrs |= curses.A_REVERSE

        color_pair = self.color_pair_for(
            getattr(cell, "fg", "default"),
            getattr(cell, "bg", "default"),
        )
        if color_pair:
            attrs |= curses.color_pair(color_pair)

        return attrs

    def color_pair_for(self, fg: str, bg: str) -> int:
        # Allocates or reuses a curses color pair for one foreground/background pair.
        if not curses.has_colors():
            return 0

        key = (fg, bg)
        if key in self.color_pairs:
            return self.color_pairs[key]

        if self.next_color_pair >= curses.COLOR_PAIRS:
            return 0

        fg_color = CURSES_COLORS.get(fg, -1)
        bg_color = CURSES_COLORS.get(bg, -1)
        pair_id = self.next_color_pair

        try:
            curses.init_pair(pair_id, fg_color, bg_color)
        except curses.error:
            return 0

        self.color_pairs[key] = pair_id
        self.next_color_pair += 1
        return pair_id

    def wrap_text(self, text: str, width: int) -> list[str]:
        # Wraps a multi-line string into pane-width chunks.
        if not text:
            return []

        lines = []
        for line in text.splitlines():
            lines.extend(self.wrap_line(line, width))
        return lines

    def wrap_line(self, line: str, width: int) -> list[str]:
        # Splits one line into fixed-width chunks for pane rendering.
        if width <= 0:
            return [""]
        if not line:
            return [""]
        return [line[i : i + width] for i in range(0, len(line), width)]

    def safe_addstr(
        self,
        stdscr,
        y: int,
        x: int,
        text: str,
        attrs: int = 0,
    ) -> None:
        # Writes text to curses while ignoring off-screen drawing errors.
        try:
            stdscr.addstr(y, x, text, attrs)
        except curses.error:
            pass
