from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from terminal.nethack_terminal import NetHackTerminal


class NetHackTerminalRenderTests(unittest.TestCase):
    def test_render_cells_preserves_pyte_color_attributes(self) -> None:
        terminal = NetHackTerminal(rows=1, cols=2)
        terminal.stream.feed("\x1b[31;1m@\x1b[0m.")

        cells = terminal.render_cells()

        self.assertEqual(cells[0][0].char, "@")
        self.assertEqual(cells[0][0].fg, "red")
        self.assertTrue(cells[0][0].bold)
        self.assertEqual(cells[0][1].char, ".")
        self.assertEqual(cells[0][1].fg, "default")

    def test_resize_updates_pyte_dimensions_without_child(self) -> None:
        terminal = NetHackTerminal(rows=1, cols=2)

        terminal.resize(3, 4)

        self.assertEqual(terminal.rows, 3)
        self.assertEqual(terminal.cols, 4)
        self.assertEqual(len(terminal.render_cells()), 3)
        self.assertEqual(len(terminal.render_cells()[0]), 4)

    def test_cursor_position_exposes_pyte_cursor_coordinates(self) -> None:
        terminal = NetHackTerminal(rows=2, cols=3)
        terminal.stream.feed("ab\nc")

        self.assertEqual(terminal.cursor_position(), (3, 1))

    def test_resolve_executable_accepts_path_command(self) -> None:
        terminal = NetHackTerminal(rows=1, cols=1)

        with patch("terminal.nethack_terminal.shutil.which", return_value="/usr/bin/nethack"):
            self.assertEqual(
                terminal.resolve_executable("nethack"),
                Path("/usr/bin/nethack"),
            )

    def test_resolve_executable_accepts_explicit_path(self) -> None:
        terminal = NetHackTerminal(rows=1, cols=1)

        self.assertEqual(
            terminal.resolve_executable("/bin/sh"),
            Path("/bin/sh"),
        )


if __name__ == "__main__":
    unittest.main()
