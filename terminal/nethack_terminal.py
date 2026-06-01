from pathlib import Path
import os
import shutil
import time
from dataclasses import dataclass
import uuid

import pexpect
import pyte

from config import (
    NETHACK_BIN,
    NETHACK_OPTIONS,
    TERMINAL_COLS,
    TERMINAL_ROWS,
    TERMINAL_TIMEOUT,
)


@dataclass(frozen=True)
class TerminalCell:
    char: str
    fg: str
    bg: str
    bold: bool
    reverse: bool


class NetHackTerminal:
    def __init__(
        self,
        rows: int = TERMINAL_ROWS,
        cols: int = TERMINAL_COLS,
        timeout: float = TERMINAL_TIMEOUT,
    ) -> None:
        # Initializes the PTY-backed terminal emulator used to run NetHack.
        self.rows = rows
        self.cols = cols
        self.timeout = timeout
        self.child = None
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)

    def start(self) -> None:
        # Launches the NetHack process and primes the terminal buffer.
        binary = self.resolve_executable(NETHACK_BIN)

        if binary is None:
            raise FileNotFoundError(NETHACK_BIN)
        if not os.access(binary, os.X_OK):
            raise PermissionError(
                f"Executable is not executable: {NETHACK_BIN}"
            )

        self.child = pexpect.spawn(
            str(binary),
            args=["-u", f"agent_{uuid.uuid4().hex[:8]}"],
            dimensions=(self.rows, self.cols),
            env={
                **os.environ,
                "TERM": "xterm",
                "NETHACKOPTIONS": NETHACK_OPTIONS,
            },
            encoding="cp437",
        )
        time.sleep(1.0)
        self._read_available()

    def resolve_executable(self, executable: str) -> Path | None:
        # Resolves either an explicit path or a command name available on PATH.
        candidate = Path(executable).expanduser()
        if candidate.parent != Path(".") or candidate.is_absolute():
            return candidate if candidate.exists() else None
        resolved = shutil.which(executable)
        return Path(resolved) if resolved is not None else None

    def stop(self) -> None:
        # Stops the NetHack subprocess if it is still running.
        if self.child and self.child.isalive():
            self.child.close(force=True)

    def send_keys(self, keys: str) -> None:
        # Sends raw key input to NetHack and refreshes the buffered screen state.
        if self.child is None:
            raise RuntimeError("NetHack process is not running.")

        self.child.send(keys)
        time.sleep(self.timeout)
        self._read_available()

    def resize(self, rows: int, cols: int) -> None:
        # Resizes both the emulator state and the live subprocess window.
        rows = max(1, rows)
        cols = max(1, cols)

        if rows == self.rows and cols == self.cols:
            return

        self.rows = rows
        self.cols = cols
        self.screen.resize(lines=rows, columns=cols)

        if self.child is not None and self.child.isalive():
            self.child.setwinsize(rows, cols)
            time.sleep(self.timeout)
            self._read_available()

    def _read_available(self) -> None:
        # Drains currently available PTY output into the pyte screen buffer.
        if self.child is None:
            raise RuntimeError("NetHack process is not running.")

        while True:
            try:
                data = self.child.read_nonblocking(
                    size=8192,
                    timeout=0.05,
                )
            except pexpect.TIMEOUT:
                break
            except pexpect.EOF:
                break

            if not data:
                break
            self.stream.feed(data)

    def render(self) -> str:
        # Renders the terminal display as trimmed plain text lines.
        lines = [line.rstrip() for line in self.screen.display]
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    def render_cells(self) -> list[list[TerminalCell]]:
        # Exposes the current screen as per-cell glyph and color attributes.
        rows = []

        for y in range(self.rows):
            row = []
            for x in range(self.cols):
                char = self.screen.buffer[y][x]
                row.append(
                    TerminalCell(
                        char=char.data,
                        fg=char.fg,
                        bg=char.bg,
                        bold=char.bold,
                        reverse=char.reverse,
                    )
                )
            rows.append(row)

        return rows

    def cursor_position(self) -> tuple[int, int]:
        # Returns the current terminal cursor position from the pyte screen.
        return (self.screen.cursor.x, self.screen.cursor.y)
