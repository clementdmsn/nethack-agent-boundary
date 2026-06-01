from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.runner as runtime
from navigation.actions import action_to_keys
from terminal.colors import MIN_GAME_COLS, MIN_GAME_ROWS, MIN_SIDE_PANE_COLS
from app.runner import AUTO_PROMPT, Runner


TEST_ARTIFACT_DIR = tempfile.TemporaryDirectory()
TEST_ARTIFACT_PATH = Path(TEST_ARTIFACT_DIR.name)
runtime.LAST_EXECUTION_TRACE_PATH = TEST_ARTIFACT_PATH / "last_execution_trace.md"
runtime.SAVED_TRACE_DIR = TEST_ARTIFACT_PATH / "saved_traces"


class FakeTerminal:
    def __init__(
        self,
        screens: list[str],
        cursor_positions: list[tuple[int, int]] | None = None,
        cell_screens: list[list[list[object]]] | None = None,
    ) -> None:
        self.screens = screens
        self.index = 0
        self.sent_keys: list[str] = []
        self.resize_calls: list[tuple[int, int]] = []
        self.cursor_positions = cursor_positions or [(0, 0)] * len(screens)
        self.cell_screens = cell_screens

    def render(self) -> str:
        return self.screens[self.index]

    def send_keys(self, keys: str) -> None:
        self.sent_keys.append(keys)
        self.index = min(self.index + max(1, len(keys)), len(self.screens) - 1)

    def resize(self, rows: int, cols: int) -> None:
        self.resize_calls.append((rows, cols))

    def cursor_position(self) -> tuple[int, int]:
        return self.cursor_positions[self.index]

    def render_cells(self) -> list[list[object]]:
        if self.cell_screens is None:
            screen = self.screens[self.index].splitlines()
            width = max((len(line) for line in screen), default=0)
            rows = []
            for line in screen:
                padded = line.ljust(width)
                rows.append(
                    [
                        SimpleNamespace(
                            char=char,
                            fg="default",
                            bg="default",
                            bold=False,
                            reverse=False,
                        )
                        for char in padded
                    ]
                )
            return rows
        return self.cell_screens[self.index]


def make_runner() -> Runner:
    runner = Runner.__new__(Runner)
    runner.state = runtime.RuntimeState()
    return runner


class FakeModel:
    def __init__(self, response: str = "model response") -> None:
        self.prompts = []
        self.response = response

    def ask_stream(self, prompt: str, on_delta=None) -> str:
        self.prompts.append(prompt)
        chunks = (
            ["model ", "response"]
            if self.response == "model response"
            else [self.response]
        )
        for delta in chunks:
            if on_delta is not None:
                on_delta(delta)
        return self.response


class FakeCursesScreen:
    def __init__(self, height: int, width: int) -> None:
        self.height = height
        self.width = width
        self.writes: list[tuple[int, int, str]] = []
        self.refreshed = False

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        pass

    def addstr(self, y: int, x: int, text: str, attrs: int = 0) -> None:
        self.writes.append((y, x, text))

    def move(self, y: int, x: int) -> None:
        pass

    def refresh(self) -> None:
        self.refreshed = True


class ErrorModel:
    def ask_stream(self, prompt: str, on_delta=None) -> str:
        raise RuntimeError("boom")


class RuntimeParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = make_runner()

    def test_clean_message_line_strips_more_prompt(self) -> None:
        self.assertEqual(
            self.runner.clean_message_line("(east): a chest.--More--"),
            "(east): a chest.",
        )

    def test_parse_relative_location_combines_offsets(self) -> None:
        self.assertEqual(
            self.runner.parse_relative_location("2north,7east"),
            {
                "text": "2north,7east",
                "dx": 7,
                "dy": -2,
                "parts": [
                    {"direction": "north", "distance": 2},
                    {"direction": "east", "distance": 7},
                ],
            },
        )

    def test_parse_relative_location_defaults_distance_to_one(self) -> None:
        self.assertEqual(
            self.runner.parse_relative_location("west"),
            {
                "text": "west",
                "dx": -1,
                "dy": 0,
                "parts": [{"direction": "west", "distance": 1}],
            },
        )

    def test_format_relative_location_builds_same_shape(self) -> None:
        self.assertEqual(
            self.runner.format_relative_location(-2, 1),
            {
                "text": "south,2west",
                "dx": -2,
                "dy": 1,
                "parts": [
                    {"direction": "south", "distance": 1},
                    {"direction": "west", "distance": 2},
                ],
            },
        )

    def test_parse_scene_description_from_relative_description(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "Hello Arch, welcome to NetHack!  You are a chaotic male "
                "orcish Wizard.",
                "You are in a rectangular 11 by 6 room.  "
                "(2north): 3 gold pieces.",
                "(2north,7east): doorway.  (1north,5west): doorway.  "
                "(west): a chest.",
                "(east): tame kitten called Kitty.  "
                "(2south,1east): open door.",
                "orcish wizard called Arch (here)",
            ]
        )

        self.assertEqual(
            scene["room_description"],
            "You are in a rectangular 11 by 6 room.",
        )
        self.assertNotIn("player", scene)
        self.assertEqual(self.runner.player_identity, "orcish wizard called Arch")
        self.assertEqual(len(scene["elements"]), 6)
        self.assertIn(
            {
                "description": "a chest",
                "relative": {
                    "text": "west",
                    "dx": -1,
                    "dy": 0,
                    "parts": [{"direction": "west", "distance": 1}],
                },
            },
            scene["elements"],
        )

    def test_parse_scene_description_deduplicates_elements(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "You are in a square room.  (east): doorway.",
                "(east): doorway.",
            ]
        )

        self.assertEqual(scene["elements"], [
            {
                "description": "doorway",
                "relative": {
                    "text": "east",
                    "dx": 1,
                    "dy": 0,
                    "parts": [{"direction": "east", "distance": 1}],
                },
            }
        ])

    def test_room_description_fallback_excludes_elements_and_player(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "Unknown room text. (east): doorway.",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(scene["room_description"], "Unknown room text.")

    def test_room_description_prefers_dark_room_message_over_startup_greeting(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "Hello Agent, welcome to NetHack! You are a neutral male gnomish Wizard.",
                "You can't guess the size of this area.",
                "gnomish wizard called agent (here)",
            ]
        )

        self.assertEqual(
            scene["room_description"],
            "You can't guess the size of this area.",
        )

    def test_room_description_excludes_transient_message_text(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "You hear a door open.",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(scene["room_description"], "")

    def test_room_description_excludes_combat_message_text(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "Doggo bites the kobold.",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(scene["room_description"], "")

    def test_room_description_excludes_pet_farlook_fragment(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "og called Doggo.",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(scene["room_description"], "")

    def test_room_description_excludes_entity_farlook_text(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "tame little dog called Doggo (northwest)",
                "grid bug (east)",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(scene["room_description"], "")

    def test_room_description_excludes_item_farlook_text(self) -> None:
        scene = self.runner.parse_scene_description(
            [
                "gold pieces.",
                "some gold pieces (southeast)",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(scene["room_description"], "")

    def test_parse_player_from_startup_greeting(self) -> None:
        player = self.runner.parse_player(
            [
                "Hello Agent_323cca3e, welcome to NetHack! "
                "You are a chaotic male human Rogue.",
            ]
        )

        self.assertEqual(
            player,
            {"description": "human rogue called agent_323cca3e"},
        )

    def test_parse_status_identity_from_status_row(self) -> None:
        identity = self.runner.parse_status_identity(
            "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0"
        )

        self.assertEqual(identity, "samurai called agent_7012c1d2")


class RuntimeLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = make_runner()

    def test_runtime_debug_artifacts_are_isolated_from_project_root(self) -> None:
        self.assertEqual(runtime.LAST_EXECUTION_TRACE_PATH.parent, TEST_ARTIFACT_PATH)

    def test_prompted_text_lines_filters_status_and_map_prefix(self) -> None:
        screen = "\n".join(
            [
                "│o··u·······│                   When in this mode, press ESC.",
                "Lawful $:0 HP:16(16) Pw:3(3)",
                "Dlvl:1 T:1 Sword GAHS Stairs NetHack-5.0",
                "human knight called agent (here)",
            ]
        )

        self.assertEqual(
            self.runner.prompted_text_lines(screen),
            [
                "When in this mode, press ESC.",
                "human knight called agent (here)",
            ],
        )

    def test_record_prompted_text_deduplicates_lines(self) -> None:
        screen = "Pick a monster, object or location.\n" * 2

        self.runner.record_prompted_text(screen)
        self.runner.record_prompted_text(screen)

        self.assertEqual(
            self.runner.last_text_log,
            ["Pick a monster, object or location."],
        )

    def test_render_screen_can_suppress_prompted_text_capture(self) -> None:
        self.runner.terminal = FakeTerminal(["Pick a monster, object or location."])
        self.runner.capture_prompted_text = False

        self.runner.render_screen(print_output=False)

        self.assertEqual(self.runner.last_text_log, [])

        self.runner.capture_prompted_text = True
        self.runner.render_screen(print_output=False)

        self.assertEqual(
            self.runner.last_text_log,
            ["Pick a monster, object or location."],
        )

    def test_write_last_decision_trace_log_overwrites_latest_trace(self) -> None:
        trace_dir = Path(tempfile.mkdtemp())
        trace_path = trace_dir / "last_execution_trace.md"
        previous_path = runtime.LAST_EXECUTION_TRACE_PATH
        runtime.LAST_EXECUTION_TRACE_PATH = trace_path
        try:
            self.runner.last_trace_input = {
                "request_kind": "auto",
                "model_payload": {
                    "recent_actions": ["move(east)"],
                    "available_actions": [
                        {"id": "explore:exit:north", "label": "North", "type": "explore"}
                    ],
                },
                "scene_before_action": {"room_description": "room a"},
            }
            self.runner.last_trace_result = {
                "request_kind": "auto",
                "raw_model_response": '{"decision":"switch"}',
                "parsed_decision": {"decision": "switch"},
                "selected_action": {"action_id": "explore:exit:north", "label": "North"},
                "executed_low_level_action": "move(north)",
                "scene_after_action": {"room_description": "room a"},
            }

            self.runner.write_last_decision_trace_log()

            self.runner.last_trace_input = {
                "request_kind": "step",
                "model_payload": {
                    "recent_actions": ["move(north)", "move(east)"],
                    "available_actions": [
                        {"id": "fight:monster:jackal", "label": "Fight jackal", "type": "fight"}
                    ],
                },
                "scene_before_action": {"room_description": "room b before"},
            }
            self.runner.last_trace_result = {
                "request_kind": "step",
                "raw_model_response": '{"decision":"continue"}',
                "parsed_decision": {"decision": "continue"},
                "selected_action": {
                    "action_id": "fight:monster:jackal",
                    "label": "Fight jackal",
                },
                "executed_low_level_action": "move(west)",
                "scene_after_action": {"room_description": "room b"},
            }

            self.runner.write_last_decision_trace_log()

            content = trace_path.read_text(encoding="utf-8")
            self.assertIn("## Execution Result", content)
            self.assertIn("- Request kind: `step`", content)
            self.assertIn("- Decision: `continue`", content)
            self.assertIn("- Selected action: `fight:monster:jackal`", content)
            self.assertIn("- Executed: `move(west)`", content)
            self.assertIn("- After: room b", content)
            self.assertNotIn("decision_input", content)
            self.assertNotIn("decision_result", content)
        finally:
            runtime.LAST_EXECUTION_TRACE_PATH = previous_path

    def test_reset_runtime_logs_for_run_truncates_runtime_logs(self) -> None:
        trace_dir = Path(tempfile.mkdtemp())
        previous_trace = runtime.LAST_EXECUTION_TRACE_PATH
        runtime.LAST_EXECUTION_TRACE_PATH = trace_dir / "last_execution_trace.md"
        try:
            runtime.LAST_EXECUTION_TRACE_PATH.write_text("old log\n", encoding="utf-8")

            self.runner.reset_runtime_logs_for_run()

            self.assertEqual(
                runtime.LAST_EXECUTION_TRACE_PATH.read_text(encoding="utf-8"),
                "# NetHack Agent Last Execution Trace\n\n",
            )
        finally:
            runtime.LAST_EXECUTION_TRACE_PATH = previous_trace

    def test_trace_starts_with_first_model_handoff_entry(self) -> None:
        trace_dir = Path(tempfile.mkdtemp())
        previous_trace = runtime.LAST_EXECUTION_TRACE_PATH
        runtime.LAST_EXECUTION_TRACE_PATH = trace_dir / "last_execution_trace.md"
        try:
            self.runner.reset_runtime_logs_for_run()
            self.runner.screen = "screen before model"
            self.runner.last_payload = json.dumps({"decision": {"mode": "choose_action"}})

            self.runner.append_model_request_screen_snapshot("auto")

            content = runtime.LAST_EXECUTION_TRACE_PATH.read_text(encoding="utf-8")
            self.assertTrue(
                content.startswith("# NetHack Agent Last Execution Trace\n\n## Model Handoff")
            )
            self.assertNotIn("Game Launched", content)
            self.assertNotIn("Initial Parser Payload", content)
        finally:
            runtime.LAST_EXECUTION_TRACE_PATH = previous_trace

    def test_compact_trace_marks_runtime_action_owner(self) -> None:
        self.runner.last_trace_input = {
            "request_kind": "auto_continue_code",
            "scene_before_action": {"room_description": "corridor"},
        }
        self.runner.last_trace_result = {
            "request_kind": "auto_continue_code",
            "model_skipped": True,
            "selected_action": {"action_id": "explore_corridor:east"},
            "executed_low_level_action": "move(east)",
        }

        trace = self.runner.compact_decision_trace_log()

        self.assertEqual(trace["request"]["action_owner"], "runtime")

    def test_save_last_decision_trace_log_writes_named_curated_trace(self) -> None:
        trace_dir = Path(tempfile.mkdtemp())
        previous_trace_dir = runtime.SAVED_TRACE_DIR
        runtime.SAVED_TRACE_DIR = trace_dir / "saved"
        try:
            self.runner.last_trace_input = {
                "request_kind": "auto",
                "model_payload": {
                    "available_actions": [
                        {
                            "id": "explore_corridor:west",
                            "label": "Follow west",
                            "type": "explore_corridor",
                        }
                    ]
                },
            }
            self.runner.last_trace_result = {
                "request_kind": "auto",
                "raw_model_response": '{"decision":"switch"}',
                "parsed_decision": {"decision": "switch"},
                "selected_action": {"action_id": "explore_corridor:west"},
                "executed_low_level_action": "move(west)",
            }

            path = self.runner.save_last_decision_trace_log("corridor follow")

            self.assertEqual(path, runtime.SAVED_TRACE_DIR / "corridor_follow.trace.json")
            logged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(logged["request"]["kind"], "auto")
            self.assertEqual(
                logged["execution"]["selected_action"]["id"],
                "explore_corridor:west",
            )
        finally:
            runtime.SAVED_TRACE_DIR = previous_trace_dir

    def test_save_trace_command_saves_current_trace(self) -> None:
        trace_dir = Path(tempfile.mkdtemp())
        previous_trace_dir = runtime.SAVED_TRACE_DIR
        runtime.SAVED_TRACE_DIR = trace_dir / "saved"
        try:
            self.runner.last_trace_input = {
                "request_kind": "auto",
                "payload": {"payload": 1},
            }
            self.runner.last_trace_result = {
                "request_kind": "auto",
                "selected_action": {"action_id": "explore_corridor:west"},
            }

            self.runner.handle_tui_command("/save-trace corridor-follow")

            path = runtime.SAVED_TRACE_DIR / "corridor-follow.trace.json"
            self.assertTrue(path.exists())
            self.assertEqual(self.runner.last_response, f"Saved trace: {path}")
        finally:
            runtime.SAVED_TRACE_DIR = previous_trace_dir

    def test_write_debug_log_overwrites_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_path = runtime.LAST_EXECUTION_TRACE_PATH
            runtime.LAST_EXECUTION_TRACE_PATH = Path(temp_dir) / "trace.md"
            try:
                self.runner.last_scene = {"room_description": "old"}
                self.runner.last_text_log = ["old log"]
                self.runner.last_observation_cleanup = {
                    "farlook_used": True,
                    "returned_to_normal_mode": True,
                    "terminal_mode_after_cleanup": "normal",
                }
                self.runner.write_debug_log()

                self.runner.last_scene = {"room_description": "new"}
                self.runner.last_text_log = ["Pick a monster, object or location."]
                self.runner.last_observation_cleanup = {
                    "farlook_used": True,
                    "returned_to_normal_mode": True,
                    "terminal_mode_after_cleanup": "normal",
                }
                self.runner.write_debug_log()

                content = runtime.LAST_EXECUTION_TRACE_PATH.read_text(encoding="utf-8")
            finally:
                runtime.LAST_EXECUTION_TRACE_PATH = original_path

        self.assertIn('"room_description": "new"', content)
        self.assertIn("### Observation Cleanup", content)
        self.assertIn('"returned_to_normal_mode": true', content)
        self.assertIn("### Observation Text Log", content)
        self.assertIn("not necessarily the current terminal prompt", content)
        self.assertIn("Pick a monster, object or location.", content)
        self.assertIn('"room_description": "old"', content)
        self.assertIn("old log", content)

    def test_append_screen_snapshot_log_updates_screen_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_path = runtime.LAST_EXECUTION_TRACE_PATH
            runtime.LAST_EXECUTION_TRACE_PATH = Path(temp_dir) / "trace.md"
            try:
                self.runner.player_identity = "first hero"
                self.runner.last_scene = {
                    "room_description": "First room.",
                    "elements": [{"description": "a chest"}],
                }
                self.runner.screen = "first screen"
                self.runner.append_screen_snapshot_log()
                self.runner.player_identity = "second hero"
                self.runner.last_scene = {
                    "room_description": "Second room.",
                    "elements": [],
                }
                self.runner.screen = "second screen"
                self.runner.append_screen_snapshot_log()
                content = runtime.LAST_EXECUTION_TRACE_PATH.read_text(encoding="utf-8")
            finally:
                runtime.LAST_EXECUTION_TRACE_PATH = original_path

        self.assertIn("## Screen", content)
        self.assertIn("- Player: second hero", content)
        self.assertIn("- Parser room: Second room.", content)
        self.assertIn("second screen", content)


class RuntimeObservationTests(unittest.TestCase):
    def make_cell_rows(self, *rows: str) -> list[list[object]]:
        return [
            [
                SimpleNamespace(
                    char=char,
                    fg="default",
                    bg="default",
                    bold=False,
                    reverse=False,
                )
                for char in row
            ]
            for row in rows
        ]

    def test_consume_scene_pages_advances_more_prompts_silently(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            [
                "You are in a room. (east): doorway.--More--",
                "(west): a chest.",
            ]
        )

        runner.consume_scene_pages()

        self.assertEqual(runner.terminal.sent_keys, ["\n"])
        self.assertEqual(
            runner.last_text_log,
            [
                "You are in a room. (east): doorway.--More--",
                "(west): a chest.",
            ],
        )

    def test_begin_farlook_clears_tutorial_prompts(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            [
                "",
                "Pick a monster, object or location.--More--",
                "Tip: Farlooking or selecting a map location\n(end)",
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "human knight called agent (here)",
                    ]
                ),
            ]
        )

        screen = runner.begin_farlook()

        self.assertEqual(runner.terminal.sent_keys, [";", "\n", "\n"])
        self.assertIn("human knight called agent (here)", runner.last_text_log)
        self.assertIn("human knight called agent (here)", screen)

    def test_extract_current_look_description_reads_farlook_line(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                "Move cursor to a monster, object or location:",
                "tame kitten called Kitty (southwest)",
            ]
        )

        self.assertEqual(
            runner.extract_current_look_description(screen),
            ("tame kitten called Kitty", "southwest"),
        )

    def test_statue_description_is_classified_as_item_despite_monster_glyph(self) -> None:
        runner = make_runner()

        scene = runner.build_scene_from_observations(
            "You are in a room.",
            [
                (
                    runtime.ObservationTarget(1, 1, -2, 1, ":", 1),
                    "statue of a newt",
                ),
            ],
            exits=[],
        )

        self.assertEqual(scene["entities"], [])
        self.assertEqual(
            scene["items"],
            [{"description": "statue of a newt", "pos": [-2, 1]}],
        )
        self.assertEqual(
            scene["observations"],
            [{"description": "statue of a newt", "glyph": ":", "pos": [-2, 1]}],
        )

    def test_spellbook_description_is_classified_as_item_despite_door_glyph(self) -> None:
        runner = make_runner()

        scene = runner.build_scene_from_observations(
            "You are in a room.",
            [
                (
                    runtime.ObservationTarget(1, 1, 0, 1, "+", 1),
                    "a bronze spellbook",
                ),
            ],
            exits=[],
        )

        self.assertEqual(scene["features"], [])
        self.assertEqual(
            scene["items"],
            [{"description": "a bronze spellbook", "pos": [0, 1]}],
        )

    def test_spellbook_item_gets_pick_action(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "a bronze spellbook", "pos": [0, 1]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "...",
            ".@.",
            ".+.",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)

        payload = json.loads(runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = [action["id"] for action in payload["available_actions"]]

        self.assertIn("pick:item:spellbook", action_ids)

    def test_item_target_name_classifies_common_armor_before_ring(self) -> None:
        runner = make_runner()

        self.assertEqual(runner.item_target_name("a ring mail"), "armor")
        self.assertEqual(runner.item_target_name("an elven mithril-coat"), "armor")
        self.assertEqual(runner.item_target_name("a pair of leather gloves"), "armor")
        self.assertEqual(runner.item_target_name("a shiny ring"), "ring")

    def test_corpse_item_does_not_get_pick_action(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "a gnome corpse", "pos": [0, -3]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            ".%.",
            "...",
            "...",
            ".@.",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 3)

        payload = json.loads(runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertNotIn("pick:item:corpse", action_ids)

    def test_box_item_does_not_get_pick_action(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "a large box", "pos": [0, -1]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            ".(.",
            ".@.",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)

        payload = json.loads(runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}
        containers = [
            entry
            for entry in payload["scene"]["visible"]
            if isinstance(entry, dict) and entry.get("kind") == "container"
        ]

        self.assertNotIn("pick:item:container", action_ids)
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["label"], "container")
        self.assertNotEqual(containers[0]["id"], "item:container")
        self.assertTrue(containers[0]["not_pickup_target"])

    def test_chest_item_does_not_get_pick_action(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "a chest", "pos": [0, -1]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            ".(.",
            ".@.",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)

        payload = json.loads(runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}
        containers = [
            entry
            for entry in payload["scene"]["visible"]
            if isinstance(entry, dict) and entry.get("kind") == "container"
        ]

        self.assertNotIn("pick:item:container", action_ids)
        self.assertEqual(len(containers), 1)
        self.assertEqual(containers[0]["label"], "container")
        self.assertNotEqual(containers[0]["id"], "item:container")
        self.assertTrue(containers[0]["not_pickup_target"])

    def test_scan_observation_targets_skips_empty_floor_and_keeps_features(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                [
                    [SimpleNamespace(char=char, fg="default", bg="default", bold=False, reverse=False) for char in row]
                    for row in [
                        "-------",
                        "|.@$+.|",
                        "|..d..|",
                        "-------",
                    ]
                ]
            ],
        )

        targets = runner.scan_observation_targets(screen, player_x=2, player_y=1)

        self.assertEqual(targets[0].dx, 0)
        self.assertEqual(targets[0].dy, 0)
        self.assertTrue(
            any(
                target.glyph == "$" and target.dx == 1 and target.dy == 0
                for target in targets
            )
        )
        self.assertTrue(
            any(
                target.glyph == "+" and target.dx == 2 and target.dy == 0
                for target in targets
            )
        )
        self.assertTrue(
            any(
                target.glyph == "d" and target.dx == 1 and target.dy == 1
                for target in targets
            )
        )
        self.assertFalse(any(target.glyph == "." for target in targets))

    def test_render_map_glyph_rows_ignores_farlook_prompt_lines(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                    "Move cursor to a monster, object or location:",
                    "human knight called agent (here)",
                )
            ],
        )

        self.assertEqual(
            runner.render_map_glyph_rows(screen)[0],
            [
                "-------",
                "|.@$..|",
                "|...f.|",
                "-------",
                "",
                "",
            ],
        )

    def test_render_map_glyph_rows_ignores_text_above_map(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "What do you want to eat?",
                    "a - an apple",
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                    "Lawful $:0 HP:16(16) Pw:3(3)",
                )
            ],
        )

        self.assertEqual(
            runner.render_map_glyph_rows(screen)[0],
            [
                "",
                "",
                "-------",
                "|.@$..|",
                "|...f.|",
                "-------",
                "",
            ],
        )

    def test_render_map_glyph_rows_blanks_overlay_inside_viewport(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "What do you want to eat?",
                    "|...f.|",
                    "-------",
                    "Lawful $:0 HP:16(16) Pw:3(3)",
                )
            ],
        )

        self.assertEqual(
            runner.render_map_glyph_rows(screen)[0],
            [
                "-------",
                "|.@$..|",
                "",
                "|...f.|",
                "-------",
                "",
            ],
        )

    def test_render_map_glyph_rows_ignores_far_right_farlook_fragments(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "             ┌───┐",
                    "             │.@.│                                G··",
                    "             │...│                                  d",
                    "             └───┘",
                    "Lawful $:0 HP:16(16) Pw:3(3)",
                )
            ],
        )

        self.assertEqual(
            runner.render_map_glyph_rows(screen)[0],
            [
                "┌───┐",
                "│.@.│",
                "│...│",
                "└───┘",
                "",
            ],
        )

    def test_render_map_glyph_rows_keeps_attached_single_corridor_row(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "     --------",
                    "     .......|",
                    "     |......|",
                    "     |......|",
                    "     +...<%.|",
                    "     -----u--",
                    "          @",
                    "          #",
                    "Lawful $:0 HP:16(16) Pw:3(3)",
                )
            ],
        )

        self.assertEqual(
            runner.render_map_glyph_rows(screen)[0],
            [
                "--------",
                ".......|",
                "|......|",
                "|......|",
                "+...<%.|",
                "-----u--",
                "     @",
                "     #",
                "",
            ],
        )

    def test_render_map_glyph_rows_keeps_attached_monster_only_row(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "                             k",
                    "                             @",
                    "                             #",
                    "                             #",
                    "                            -d---",
                    "                            |....",
                    "                            |...|",
                    "                            |..<|",
                    "                            -----",
                    "Neutral $:0 HP:11(12) Pw:3(3)",
                )
            ],
        )

        map_rows, viewport = runner.render_map_glyph_rows(screen)
        player_pos = runner.resolve_player_screen_position(map_rows, viewport, None)
        assert player_pos is not None
        targets = runner.scan_observation_targets(screen, player_pos[0], player_pos[1])

        self.assertEqual(map_rows[0], " k")
        self.assertEqual(runner.map_char_at(map_rows, 1, 0), "k")
        self.assertTrue(
            any(
                target.glyph == "k" and target.dx == 0 and target.dy == -1
                for target in targets
            )
        )

    def test_render_map_glyph_rows_prefers_player_component_over_revealed_room(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "                                                                   ----",
                    "                                                                    ..|",
                    "                                                                    ..|",
                    "                                                                     .|",
                    "                                                                     .|",
                    "                                                                     .|",
                    "                                                                     .",
                    "",
                    "                                                                     #",
                    "                                                                     @",
                    "                                                                    ##",
                    "                                                                    #",
                    "                                                                    #",
                    "                                                                    #",
                    "                                                                 ---.--",
                    "                                                                 |<...|",
                    "                                                                 |.%..|",
                    "                                                                 ------",
                    "[Agent the Footpad    ] St:13 Dx:18 Co:12 In:11 Wi:10 Ch:11 S:0",
                    "Chaotic $:0 HP:12(12) Pw:2(2) AC:7 Xp:1/0",
                )
            ],
        )

        map_rows, viewport = runner.render_map_glyph_rows(screen)
        player_pos = runner.resolve_player_screen_position(map_rows, viewport, None)
        runner.last_map_lines = map_rows
        runner.last_viewport = viewport
        runner.last_player_screen_pos = player_pos

        self.assertIsNotNone(viewport)
        self.assertEqual(player_pos, (69, 9))
        self.assertEqual(runner.glyph_for_relative_pos((0, -1)), "#")
        self.assertEqual(runner.glyph_for_relative_pos((0, 1)), "#")

    def test_observation_targets_ignore_disconnected_room_fragment(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[
                self.make_cell_rows(
                    "                                   -+--+-                    |",
                    "                                   |.@.k.           .........|",
                    "                                   |d..$|                    |",
                    "                                   ------",
                    "[Agent the Troglodyte ] St:18 Dx:12 Co:19 In:9 Wi:10 Ch:7 S:0",
                    "Lawful $:0 HP:18(18) Pw:1(1) AC:8 Xp:1/0",
                )
            ],
        )

        map_lines, viewport = runner.render_map_glyph_rows(screen)
        player_pos = runner.resolve_player_screen_position(map_lines, viewport, None)
        assert player_pos is not None
        exits = runner.find_room_exits(map_lines, viewport, player_pos[0], player_pos[1])
        targets = runner.scan_observation_targets(screen, player_pos[0], player_pos[1])
        target_positions = {(target.dx, target.dy) for target in targets}

        self.assertEqual(exits, [])
        self.assertIn((2, 0), target_positions)
        self.assertIn((2, 1), target_positions)
        self.assertNotIn((23, 0), target_positions)
        self.assertTrue(all(target.dx < 10 for target in targets))

    def test_lightweight_refresh_anchors_on_visible_player_not_farlook_cursor(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        runner.terminal = FakeTerminal(
            [screen],
            cursor_positions=[(6, 3)],
            cell_screens=[
                self.make_cell_rows(
                    "       #",
                    "       @",
                    "      ##",
                    "      .",
                    "Neutral $:0 HP:10(10) Pw:2(2)",
                )
            ],
        )
        runner.last_scene = {"room_description": "Visible area.", "visibility": "normal"}

        scene = runner.refresh_lightweight_visible_scene_cache()

        self.assertEqual(runner.last_player_screen_pos, (7, 1))
        self.assertEqual(scene["location_context"]["area_type"], "corridor")
        self.assertIn("south", scene["location_context"]["adjacent_corridors"])

    def test_move_farlook_cursor_consumes_more_prompts(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            [
                "Move cursor to a monster, object or location:",
                "tame kitten called Kitty (east)--More--",
                "tame kitten called Kitty (east)",
            ]
        )

        screen = runner.move_farlook_cursor(0, 0, 1, 0)

        self.assertEqual(runner.terminal.sent_keys, ["l", "\n"])
        self.assertEqual(screen, "tame kitten called Kitty (east)")

    def test_ensure_normal_mode_exits_description_only_farlook(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            [
                "tame kitten called Kitty (east)",
                "normal game screen",
            ]
        )

        runner.ensure_normal_game_mode_before_action()

        self.assertEqual(runner.terminal.sent_keys, ["\x1b"])

    def test_move_farlook_cursor_does_not_move_when_farlook_is_inactive(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                "    -------",
                "    |<....|",
                "    |..@..|",
                "    -------",
                "Dlvl:1 T:1 Empty-hnd NetHack-5.0",
            ]
        )
        runner.terminal = FakeTerminal([screen])

        result = runner.move_farlook_cursor(0, 0, 1, 0)

        self.assertEqual(runner.terminal.sent_keys, [])
        self.assertEqual(result, screen)

    def test_look_does_not_send_cursor_keys_when_farlook_fails(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                "    -------",
                "    |<...$|",
                "    |..@..|",
                "    |...f.|",
                "    -------",
                "Dlvl:1 T:1 Empty-hnd NetHack-5.0",
            ]
        )
        runner.terminal = FakeTerminal(
            [screen, screen],
            cursor_positions=[(7, 2), (7, 2)],
            cell_screens=[
                self.make_cell_rows(
                    "    -------",
                    "    |<...$|",
                    "    |..@..|",
                    "    |...f.|",
                    "    -------",
                    "Dlvl:1 T:1 Empty-hnd NetHack-5.0",
                ),
                self.make_cell_rows(
                    "    -------",
                    "    |<...$|",
                    "    |..@..|",
                    "    |...f.|",
                    "    -------",
                    "Dlvl:1 T:1 Empty-hnd NetHack-5.0",
                ),
            ],
        )

        scene = runner.look()

        self.assertEqual(runner.terminal.sent_keys, [";"])
        self.assertEqual(scene["player"]["pos"], [0, 0])

    def test_resolve_room_description_falls_back_to_previous_scene(self) -> None:
        runner = make_runner()
        runner.last_scene = {"room_description": "You are in a corridor."}
        runner.last_text_log = []

        self.assertEqual(
            runner.resolve_room_description(),
            "You are in a corridor.",
        )

    def test_reconcile_room_description_drops_stale_here_staircase_text(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "There is a staircase up out of the dungeon here.",
            "features": [{"description": "branch staircase up", "pos": [1, 0]}],
            "items": [],
            "entities": [],
        }

        reconciled = runner.reconcile_room_description(scene)

        self.assertEqual(reconciled["room_description"], "Visible area.")

    def test_reconcile_room_description_keeps_matching_here_staircase_text(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "There is a staircase up out of the dungeon here.",
            "features": [{"description": "branch staircase up", "pos": [0, 0]}],
            "items": [],
            "entities": [],
        }

        reconciled = runner.reconcile_room_description(scene)

        self.assertEqual(
            reconciled["room_description"],
            "There is a staircase up out of the dungeon here.",
        )

    def test_scan_observation_targets_preserves_absolute_x_when_map_is_shifted(self) -> None:
        runner = make_runner()
        screen = "\n".join(["ignored"])
        shifted_rows = [
            "          -------",
            "          |.@$..|",
            "          |...d.|",
            "          -------",
            "Lawful $:0 HP:16(16) Pw:3(3)",
        ]
        runner.terminal = FakeTerminal(
            [screen],
            cell_screens=[self.make_cell_rows(*shifted_rows)],
        )

        targets = runner.scan_observation_targets(screen, player_x=12, player_y=1)

        self.assertTrue(
            any(
                target.glyph == "$"
                and target.screen_x == 13
                and target.dx == 1
                and target.dy == 0
                for target in targets
            )
        )
        self.assertTrue(
            any(
                target.glyph == "d"
                and target.screen_x == 14
                and target.dx == 2
                and target.dy == 1
                for target in targets
            )
        )

    def test_build_scene_groups_repeated_descriptions_into_positions(self) -> None:
        runner = make_runner()
        runner.player_identity = "human knight"
        scene = runner.build_scene_from_observations(
            "You are in a room.",
            [
                (runtime.ObservationTarget(0, 0, 0, 0, "@", 0), "human knight"),
                (
                    runtime.ObservationTarget(1, 0, 1, 0, "x", 1),
                    "unexplored area",
                ),
                (
                    runtime.ObservationTarget(2, 0, 2, 0, "x", 1),
                    "unexplored area",
                ),
                (
                    runtime.ObservationTarget(3, 1, 3, 1, "+", 2),
                    "closed door",
                ),
                (
                    runtime.ObservationTarget(4, 1, 4, 1, "+", 2),
                    "closed door",
                ),
            ],
        )

        self.assertEqual(
            scene,
            {
                "room_description": "You are in a room.",
                "visibility": "normal",
                "observations": [
                    {"description": "unexplored area", "glyph": "x", "pos": [1, 0]},
                    {"description": "unexplored area", "glyph": "x", "pos": [2, 0]},
                    {"description": "closed door", "glyph": "+", "pos": [3, 1]},
                    {"description": "closed door", "glyph": "+", "pos": [4, 1]},
                ],
                "features": [
                    {
                        "description": "closed door",
                        "positions": [[3, 1], [4, 1]],
                    }
                ],
                "items": [],
                "areas": [
                    {
                        "description": "unexplored areas",
                        "positions": [[1, 0], [2, 0]],
                    }
                ],
                "entities": [],
                "exits": [],
                "player": {
                    "identity": "human knight",
                    "pos": [0, 0],
                },
            },
        )
        self.assertEqual(runner.player_identity, "human knight")

    def test_find_room_exits_detects_boundary_gaps(self) -> None:
        runner = make_runner()
        map_lines = [
            "┌── ──┐",
            "│.@...│",
            "│.....│",
            "└─────┘",
        ]
        viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=6,
            overlay_rows=frozenset(),
        )

        exits = runner.find_room_exits(map_lines, viewport, player_x=2, player_y=1)

        self.assertEqual(
            exits,
            [
                {
                    "description": "exit",
                    "direction": "north",
                    "pos": [1, -1],
                }
            ],
        )

    def test_find_room_exits_skips_closed_doors_and_monster_tiles(self) -> None:
        runner = make_runner()
        map_lines = [
            "┌─────┐",
            "│.@..+│",
            "│....u│",
            "└─────┘",
        ]
        viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=6,
            overlay_rows=frozenset(),
        )

        exits = runner.find_room_exits(map_lines, viewport, player_x=2, player_y=1)

        self.assertEqual(exits, [])

    def test_find_room_exits_treats_cursor_glyph_as_room_interior(self) -> None:
        runner = make_runner()
        map_lines = [
            "┌───┐",
            "│··G·",
            "│···│",
            "│···│",
            "└───┘",
        ]
        viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )

        exits = runner.find_room_exits(map_lines, viewport, player_x=3, player_y=1)

        self.assertEqual(
            exits,
            [
                {
                    "description": "exit",
                    "direction": "east",
                    "pos": [1, 0],
                }
            ],
        )

    def test_find_room_exits_treats_entity_glyph_as_room_interior(self) -> None:
        runner = make_runner()
        map_lines = [
            "┌──────┐",
            "│<$····│",
            "·f@·····",
            "│······│",
            "│····(·│",
            "└──────┘",
        ]
        viewport = runtime.MapViewport(
            top=0,
            bottom=5,
            left=0,
            right=7,
            overlay_rows=frozenset(),
        )

        exits = runner.find_room_exits(map_lines, viewport, player_x=2, player_y=2)

        self.assertEqual(
            exits,
            [
                {
                    "description": "exit",
                    "direction": "west",
                    "pos": [-2, 0],
                },
                {
                    "description": "exit",
                    "direction": "east",
                    "pos": [5, 0],
                },
            ],
        )

    def test_find_room_exits_skips_room_below_when_player_is_in_corridor(self) -> None:
        runner = make_runner()
        map_lines = [
            "  ▒",
            " ▒@▒",
            "┌─ ───u───── ─┐",
            "│·············│",
            "│·$····<······│",
            "└─────────────┘",
        ]
        viewport = runtime.MapViewport(
            top=0,
            bottom=5,
            left=0,
            right=15,
            overlay_rows=frozenset(),
        )

        exits = runner.find_room_exits(map_lines, viewport, player_x=2, player_y=1)

        self.assertEqual(exits, [])

    def test_find_room_exits_skips_adjacent_room_when_player_is_in_corridor_tail(self) -> None:
        runner = make_runner()
        map_lines = [
            "----------",
            "|....<...+",
            "|........|",
            "|....{...|",
            "----.-----",
            "    #",
            "    #",
            "    #",
            "    #",
            "    ##@",
            "      .",
            "      .|",
            "     ...",
            "     .%|",
            "    ..x|",
            "   -----",
        ]
        viewport = runtime.MapViewport(
            top=0,
            bottom=15,
            left=0,
            right=9,
            overlay_rows=frozenset(),
        )

        self.assertFalse(
            runner.player_has_room_boundary_context(
                map_lines,
                viewport,
                player_x=7,
                player_y=9,
            )
        )
        exits = runner.find_room_exits(map_lines, viewport, player_x=7, player_y=9)

        self.assertEqual(exits, [])

    def test_look_does_not_infer_room_exits_in_dark_area(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                " ┌──",
                " │<·",
                " │@d",
                " │··",
                "Lawful $:0 HP:18(18) Pw:1(1)",
            ]
        )
        runner.terminal = FakeTerminal(
            [screen],
            cursor_positions=[(2, 2)],
            cell_screens=[
                self.make_cell_rows(
                    " ┌──",
                    " │<·",
                    " │@d",
                    " │··",
                    "Lawful $:0 HP:18(18) Pw:1(1)",
                )
            ],
        )
        runner.resolve_room_description = lambda: "You can't guess the size of this area."
        runner.begin_farlook = lambda: screen
        runner.end_farlook = lambda: None
        runner.inspect_farlook_targets = lambda *_args: []
        runner.append_screen_snapshot_log = lambda: None

        scene = runner.look()

        self.assertEqual(scene["visibility"], "dark")
        self.assertEqual(scene["exits"], [])

    def test_look_drops_stale_room_description_when_player_left_room(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                "  ▒",
                " ▒@▒",
                "┌─ ───u───── ─┐",
                "│·············│",
                "│·$····<······│",
                "└─────────────┘",
                "Lawful $:0 HP:16(16) Pw:4(4)",
            ]
        )
        runner.terminal = FakeTerminal(
            [screen],
            cursor_positions=[(2, 1)],
            cell_screens=[
                self.make_cell_rows(
                    "  ▒",
                    " ▒@▒",
                    "┌─ ───u───── ─┐",
                    "│·············│",
                    "│·$····<······│",
                    "└─────────────┘",
                    "Lawful $:0 HP:16(16) Pw:4(4)",
                )
            ],
        )
        runner.resolve_room_description = lambda: "You are in a rectangular 13 by 2 room."
        runner.begin_farlook = lambda: screen
        runner.end_farlook = lambda: None
        runner.inspect_farlook_targets = lambda *_args: []
        runner.append_screen_snapshot_log = lambda: None

        scene = runner.look()

        self.assertEqual(scene["room_description"], "Visible area.")
        self.assertEqual(scene["exits"], [])

    def test_look_drops_stale_here_description_in_corridor_tail(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                "----------",
                "|....<...+",
                "|........|",
                "|....{...|",
                "----.-----",
                "    #",
                "    #",
                "    #",
                "    #",
                "    ##@",
                "      .",
                "      .|",
                "     ...",
                "     .%|",
                "    ..x|",
                "   -----",
                "Lawful $:0 HP:15(15) Pw:2(2)",
            ]
        )
        runner.terminal = FakeTerminal(
            [screen],
            cursor_positions=[(7, 9)],
            cell_screens=[
                self.make_cell_rows(
                    "----------",
                    "|....<...+",
                    "|........|",
                    "|....{...|",
                    "----.-----",
                    "    #",
                    "    #",
                    "    #",
                    "    #",
                    "    ##@",
                    "      .",
                    "      .|",
                    "     ...",
                    "     .%|",
                    "    ..x|",
                    "   -----",
                    "Lawful $:0 HP:15(15) Pw:2(2)",
                )
            ],
        )
        runner.resolve_room_description = lambda: "There is a fountain here."
        runner.begin_farlook = lambda: screen
        runner.end_farlook = lambda: None
        runner.inspect_farlook_targets = lambda *_args: []
        runner.append_screen_snapshot_log = lambda: None

        scene = runner.look()

        self.assertEqual(scene["room_description"], "Visible area.")
        self.assertEqual(scene["location_context"]["area_type"], "corridor")
        self.assertEqual(scene["exits"], [])

    def test_merge_visible_map_fallbacks_adds_visible_door_and_stairs(self) -> None:
        runner = make_runner()
        viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        scene = {
            "room_description": "You can't guess the size of this area.",
            "visibility": "dark",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        map_lines = [
            "..<..",
            ".@+..",
            ".....",
        ]

        merged = runner.merge_visible_map_fallbacks(
            scene,
            map_lines,
            viewport,
            player_x=1,
            player_y=1,
        )

        self.assertEqual(
            merged["features"],
            [
                {"description": "visible staircase up", "pos": [1, -1]},
                {"description": "visible door", "pos": [1, 0]},
            ],
        )

    def test_merge_visible_map_fallbacks_adds_perpendicular_open_door(self) -> None:
        runner = make_runner()
        viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        map_lines = [
            "..|..",
            ".@-..",
            "..|..",
        ]

        merged = runner.merge_visible_map_fallbacks(
            scene,
            map_lines,
            viewport,
            player_x=1,
            player_y=1,
        )

        self.assertEqual(
            merged["features"],
            [{"description": "visible open door", "pos": [1, 0]}],
        )

    def test_scan_observation_targets_includes_perpendicular_open_door(self) -> None:
        runner = make_runner()
        screen = "\n".join(
            [
                "..|..",
                ".@-..",
                "..|..",
            ]
        )

        targets = runner.scan_observation_targets(screen, player_x=1, player_y=1)

        self.assertTrue(
            any(
                target.glyph == "-" and target.dx == 1 and target.dy == 0
                for target in targets
            )
        )

    def test_look_keeps_identity_from_startup_text_log(self) -> None:
        runner = make_runner()
        runner.last_text_log = [
            "Hello Agent, welcome to NetHack! You are a lawful human Knight.",
        ]
        runner.terminal = FakeTerminal(
            screens=[
                "You are in a rectangular 3 by 3 room.",
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "floor of a room (here)",
                    ]
                ),
                "\n".join(
                    [
                        "┌───┐",
                        "│.@.│",
                        "└───┘",
                    ]
                ),
            ],
            cursor_positions=[(0, 0), (2, 1), (2, 1)],
            cell_screens=[
                self.make_cell_rows("You are in a rectangular 3 by 3 room."),
                self.make_cell_rows(
                    "┌───┐",
                    "│.@.│",
                    "└───┘",
                    "Move cursor to a monster, object or location:",
                    "floor of a room (here)",
                ),
                self.make_cell_rows(
                    "┌───┐",
                    "│.@.│",
                    "└───┘",
                ),
            ],
        )

        scene = runner.look()

        self.assertEqual(runner.player_identity, "human knight called agent")
        self.assertEqual(
            scene["player"],
            {"identity": "human knight called agent", "pos": [0, 0]},
        )

    def test_look_keeps_identity_from_status_row(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            screens=[
                "\n".join(
                    [
                        "You are in a rectangular 3 by 3 room.",
                        "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0",
                        "Lawful $:0 HP:15(15) Pw:2(2) AC:4 Xp:1/0",
                        "Dlvl:1 T:1 Sword Suit Stairs                                                                      NetHack-5.0",
                    ]
                ),
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "floor of a room (here)",
                        "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0",
                        "Lawful $:0 HP:15(15) Pw:2(2) AC:4 Xp:1/0",
                    ]
                ),
                "\n".join(
                    [
                        "┌───┐",
                        "│.@.│",
                        "└───┘",
                        "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0",
                        "Lawful $:0 HP:15(15) Pw:2(2) AC:4 Xp:1/0",
                    ]
                ),
            ],
            cursor_positions=[(0, 0), (2, 1), (2, 1)],
            cell_screens=[
                self.make_cell_rows(
                    "You are in a rectangular 3 by 3 room.",
                    "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0",
                    "Lawful $:0 HP:15(15) Pw:2(2) AC:4 Xp:1/0",
                    "Dlvl:1 T:1 Sword Suit Stairs                                                                      NetHack-5.0",
                ),
                self.make_cell_rows(
                    "┌───┐",
                    "│.@.│",
                    "└───┘",
                    "Move cursor to a monster, object or location:",
                    "floor of a room (here)",
                    "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0",
                    "Lawful $:0 HP:15(15) Pw:2(2) AC:4 Xp:1/0",
                ),
                self.make_cell_rows(
                    "┌───┐",
                    "│.@.│",
                    "└───┘",
                    "[Agent_7012c1d2 the Hatamoto   ] St:18 Dx:15 Co:18 In:9 Wi:7 Ch:8 S:0",
                    "Lawful $:0 HP:15(15) Pw:2(2) AC:4 Xp:1/0",
                ),
            ],
        )

        scene = runner.look()

        self.assertEqual(runner.player_identity, "samurai called agent_7012c1d2")
        self.assertEqual(
            scene["player"],
            {"identity": "samurai called agent_7012c1d2", "pos": [0, 0]},
        )

    def test_look_builds_scene_from_farlook_targets(self) -> None:
        runner = make_runner()
        runner.last_text_log = ["human knight called agent (here)"]
        runner.terminal = FakeTerminal(
            screens=[
                "You are in a rectangular 7 by 5 room.",
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "human knight called agent (here)",
                    ]
                ),
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "an orange gem (east)",
                    ]
                ),
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "tame kitten called Kitty (south,2east)",
                    ]
                ),
                "\n".join(
                    [
                        "Move cursor to a monster, object or location:",
                        "an orange gem (east)",
                    ]
                ),
                "\n".join(
                    [
                        "-------",
                        "|.@$..|",
                        "|...f.|",
                        "-------",
                    ]
                ),
            ],
            cursor_positions=[
                (0, 0),
                (2, 1),
                (3, 1),
                (4, 2),
                (3, 1),
                (4, 2),
            ],
            cell_screens=[
                self.make_cell_rows("You are in a rectangular 7 by 5 room."),
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                    "Move cursor to a monster, object or location:",
                    "human knight called agent (here)",
                ),
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                    "Move cursor to a monster, object or location:",
                    "an orange gem (east)",
                ),
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                    "Move cursor to a monster, object or location:",
                    "tame kitten called Kitty (south,2east)",
                ),
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                    "Move cursor to a monster, object or location:",
                    "an orange gem (east)",
                ),
                self.make_cell_rows(
                    "-------",
                    "|.@$..|",
                    "|...f.|",
                    "-------",
                ),
            ],
        )

        scene = runner.look()

        self.assertEqual(
            scene["room_description"],
            "You are in a rectangular 7 by 5 room.",
        )
        self.assertEqual(runner.player_identity, "human knight called agent")
        self.assertEqual(
            scene["features"],
            [],
        )
        self.assertEqual(
            scene["items"],
            [
                {
                    "description": "an orange gem",
                    "pos": [1, 0],
                },
            ],
        )
        self.assertEqual(
            scene["areas"],
            [],
        )
        self.assertEqual(
            scene["entities"],
            [
                {
                    "description": "tame kitten called Kitty",
                    "pos": [2, 1],
                }
            ],
        )
        self.assertEqual(
            scene["player"],
            {
                "identity": "human knight called agent",
                "pos": [0, 0],
            },
        )
        self.assertEqual(runner.terminal.sent_keys, [";", "nl", "y", "\x1b"])

    def test_look_does_not_write_screen_snapshot_log(self) -> None:
        runner = make_runner()
        with tempfile.TemporaryDirectory() as temp_dir:
            original_path = runtime.LAST_EXECUTION_TRACE_PATH
            runtime.LAST_EXECUTION_TRACE_PATH = Path(temp_dir) / "trace.md"
            try:
                runner.terminal = FakeTerminal(
                    screens=["You are in a room."],
                    cursor_positions=[None],
                )

                runner.look()

                self.assertFalse(runtime.LAST_EXECUTION_TRACE_PATH.exists())
            finally:
                runtime.LAST_EXECUTION_TRACE_PATH = original_path


class RuntimePathfindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = make_runner()
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 2)

    def test_shortest_visible_path_finds_straight_line_exit(self) -> None:
        self.runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        scene = {
            "exits": [{"description": "exit", "direction": "north", "pos": [0, -2]}],
            "entities": [],
        }

        self.assertEqual(
            self.runner.shortest_visible_path(scene, [0, -2]),
            [[0, 0], [0, -1], [0, -2]],
        )

    def test_shortest_visible_path_prefers_diagonal_steps_in_open_space(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            ".....",
            "..@..",
            ".....",
            ".....",
        ]
        scene = {"exits": [], "entities": []}

        self.assertEqual(
            self.runner.shortest_visible_path(scene, [2, -2]),
            [[0, 0], [1, -1], [2, -2]],
        )

    def test_shortest_visible_path_avoids_walls(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            ".---.",
            "..@..",
            ".....",
            ".....",
        ]
        scene = {"exits": [], "entities": []}

        self.assertEqual(
            self.runner.shortest_visible_path(scene, [0, -2]),
            [[0, 0], [-1, 0], [-2, -1], [-1, -2], [0, -2]],
        )

    def test_shortest_visible_path_crosses_perpendicular_open_door(self) -> None:
        self.runner.last_player_screen_pos = (1, 2)
        self.runner.last_map_lines = [
            "..|..",
            "..|..",
            ".@-..",
            "..|..",
            "..|..",
        ]
        scene = {"exits": [], "entities": []}

        self.assertEqual(
            self.runner.shortest_visible_path(scene, [2, 0]),
            [[0, 0], [1, 0], [2, 0]],
        )

    def test_ordinary_wall_like_glyph_is_not_traversable(self) -> None:
        self.runner.last_player_screen_pos = (1, 2)
        self.runner.last_map_lines = [
            ".....",
            ".---.",
            ".@...",
            ".....",
            ".....",
        ]
        scene = {"exits": [], "entities": []}

        self.assertFalse(self.runner.is_traversable_scene_pos(scene, (1, -1)))

    def test_shortest_visible_path_returns_none_when_target_is_unreachable(self) -> None:
        self.runner.last_map_lines = [
            "-----",
            "--.--",
            "--@--",
            "--.--",
            "-----",
        ]
        scene = {"exits": [], "entities": []}

        self.assertIsNone(self.runner.shortest_visible_path(scene, [0, -2]))

    def test_nearest_reachable_target_skips_nearer_blocked_target(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            ".---.",
            "..@..",
            ".....",
            ".....",
        ]
        scene = {"exits": [], "entities": []}

        self.assertEqual(
            self.runner.nearest_reachable_target_path(scene, [[0, -2], [2, 0]]),
            ([2, 0], [[0, 0], [1, 0], [2, 0]]),
        )

    def test_path_to_action_converts_first_step(self) -> None:
        self.assertEqual(
            self.runner.path_to_action([[0, 0], [1, -1], [2, -2]]),
            "move(northeast)",
        )

    def test_walkable_neighbors_exclude_diagonal_closed_door_step(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            ".+...",
            "..@..",
            ".....",
            ".....",
        ]
        scene = {"exits": [], "entities": []}

        neighbors = dict(self.runner.walkable_neighbor_actions(scene))

        self.assertNotIn("move(northwest)", neighbors)
        self.assertIn("move(north)", neighbors)
        self.assertIn("move(west)", neighbors)

    def test_visible_path_enters_closed_door_cardinally(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            ".+...",
            "..@..",
            ".....",
            ".....",
        ]
        scene = {"exits": [], "entities": []}

        self.assertEqual(
            self.runner.shortest_visible_path(scene, [-1, -1]),
            [[0, 0], [0, -1], [-1, -1]],
        )

    def test_visible_entities_block_static_paths(self) -> None:
        self.runner.last_map_lines = [
            "┌─ ─┐",
            "│.d.│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        scene = {
            "exits": [{"description": "exit", "direction": "north", "pos": [0, -2]}],
            "entities": [{"description": "jackal", "pos": [0, -1]}],
        }

        self.assertEqual(
            self.runner.shortest_visible_path(scene, [0, -2]),
            [[0, 0], [-1, -1], [0, -2]],
        )

    def test_exploration_frontier_path_targets_visible_unknown_boundary(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            "..@. ",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        scene = {
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
        }

        result = self.runner.nearest_exploration_frontier_path(scene)

        self.assertEqual(result, ([1, 0], [[0, 0], [1, 0]]))

    def test_exploration_frontier_path_avoids_immediate_backtracking(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            "..@. ",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        self.runner.executed_actions = ["move(west)"]
        scene = {
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
        }

        result = self.runner.nearest_exploration_frontier_path(scene)

        self.assertIsNotNone(result)
        _target, path = result
        self.assertNotEqual(self.runner.path_first_delta(path), (1, 0))

    def test_exploration_frontier_avoids_hostile_adjacency(self) -> None:
        self.runner.last_map_lines = [
            ".....",
            "..@. ",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        scene = {
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "jackal", "pos": [2, 0]}],
            "exits": [],
        }

        self.assertIsNone(self.runner.nearest_exploration_frontier_path(scene))

    def test_exit_action_avoids_path_adjacent_to_hostile(self) -> None:
        self.runner.last_map_lines = [
            "..........",
            "..F.......",
            "..........",
            "..........",
            "......@...",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=9,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (6, 4)
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "lichen", "pos": [1, -3]}],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [2, -4]},
                {"description": "exit", "direction": "west", "pos": [-6, 0]},
            ],
            "player": {"identity": "archeologist called agent", "pos": [0, 0]},
        }

        actions = self.runner.build_available_actions(
            self.runner.copy_scene_with_refs(scene)
        )
        action_ids = {action["action_id"] for action in actions}

        self.assertNotIn("fight:monster:lichen", action_ids)
        self.assertNotIn("flee:exit:north", action_ids)
        self.assertIn("flee:exit:west", action_ids)


class RuntimeControllerTests(unittest.TestCase):
    def test_manual_prompt_writes_last_trace_log(self) -> None:
        runner = make_runner()
        model = FakeModel("model response")
        scene = {
            "room_description": "You are in a room.",
            "elements": [],
        }
        trace_dir = Path(tempfile.mkdtemp())
        trace_path = trace_dir / "last_execution_trace.md"
        previous_path = runtime.LAST_EXECUTION_TRACE_PATH
        runtime.LAST_EXECUTION_TRACE_PATH = trace_path
        try:
            runner.look = lambda: scene
            runner.get_model = lambda: model

            runner.handle_tui_command("Describe the scene.")

            content = trace_path.read_text(encoding="utf-8")
            self.assertIn("## Model Response", content)
            self.assertIn("model response", content)
            self.assertIn("## Execution Result", content)
            self.assertIn("- Request kind: `manual`", content)
            self.assertIn("- Action owner: `model`", content)
        finally:
            runtime.LAST_EXECUTION_TRACE_PATH = previous_path

    def test_model_request_writes_pre_request_screen_snapshot(self) -> None:
        runner = make_runner()
        model = FakeModel("model response")
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        trace_dir = Path(tempfile.mkdtemp())
        trace_path = trace_dir / "last_execution_trace.md"
        previous_path = runtime.LAST_EXECUTION_TRACE_PATH
        runtime.LAST_EXECUTION_TRACE_PATH = trace_path
        try:
            runner.screen = "screen before model"
            runner.look = lambda: scene
            runner.get_model = lambda: model

            runner.start_model_request("Describe the scene.", request_kind="manual")
            assert isinstance(runner.model_thread, threading.Thread)
            runner.model_thread.join(timeout=1)
            runner.drain_model_events()

            content = trace_path.read_text(encoding="utf-8")
        finally:
            runtime.LAST_EXECUTION_TRACE_PATH = previous_path

        self.assertIn("## Model Handoff", content)
        self.assertIn("- Action owner: `model`", content)
        self.assertIn("- Reason: `before_model_request`", content)
        self.assertIn("- Request kind: `manual`", content)
        self.assertIn("screen before model", content)

    def test_removed_keys_command_is_unknown(self) -> None:
        runner = make_runner()
        runner.auto_mode = True

        runner.handle_tui_command("/keys")

        self.assertFalse(runner.raw_keys_mode)
        self.assertTrue(runner.auto_mode)
        self.assertEqual(runner.last_response, "Unknown command: /keys")

    def test_ctrl_c_exits_raw_key_mode_without_sending_key(self) -> None:
        runner = make_runner()
        runner.raw_keys_mode = True
        runner.terminal = FakeTerminal([""])

        runner.handle_raw_key("\x03")

        self.assertFalse(runner.raw_keys_mode)
        self.assertEqual(runner.terminal.sent_keys, [])

    def test_raw_key_mode_sends_keys_to_terminal(self) -> None:
        runner = make_runner()
        runner.raw_keys_mode = True
        runner.terminal = FakeTerminal(["", "after"])

        runner.handle_raw_key("h")

        self.assertEqual(runner.terminal.sent_keys, ["h"])

    def test_quit_command_requests_exit(self) -> None:
        runner = make_runner()

        runner.handle_tui_command("/quit")

        self.assertTrue(runner.should_exit)

    def test_removed_look_command_is_unknown(self) -> None:
        runner = make_runner()
        model = FakeModel()
        runner.get_model = lambda: model

        runner.handle_tui_command("/look")

        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.last_response, "Unknown command: /look")

    def test_start_command_accepts_visible_start_and_enables_auto(self) -> None:
        runner = make_runner()

        runner.handle_tui_command("/start")

        self.assertTrue(runner.auto_mode)
        self.assertEqual(runner.last_response, "Start accepted. Auto mode enabled.")

    def test_roll_command_restarts_game_without_parsing(self) -> None:
        runner = make_runner()
        calls = []
        runner.start_new_game_for_roll = lambda: calls.append("rolled")

        runner.handle_tui_command("/roll")

        self.assertEqual(calls, ["rolled"])

    def test_skip_intro_dismisses_welcome_page_without_more_prompt(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            [
                "role prompt",
                "race prompt",
                "Hello agent_xxx! Welcome to NetHack",
                "------\n|.@..|\n------",
            ]
        )

        runner.skip_intro()

        self.assertEqual(runner.terminal.sent_keys, list(runtime.SKIP_INTRO_KEYS) + ["\n"])
        self.assertEqual(runner.screen, "------\n|.@..|\n------")

    def test_prompt_submission_updates_payload_and_response(self) -> None:
        runner = make_runner()
        model = FakeModel()
        scene = {
            "room_description": "You are in a room.",
            "elements": [],
        }
        runner.player_identity = "human knight called agent"
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.handle_tui_command("Describe the scene.")

        self.assertEqual(runner.last_response, "model response")
        self.assertEqual(runner.response_history, ["model response"])
        self.assertEqual(len(model.prompts), 1)
        payload = json.loads(model.prompts[0])
        self.assertEqual(
            payload["scene_state"]["identity"],
            "human knight called agent",
        )
        self.assertEqual(payload["scene_state"]["pet"], [])
        self.assertEqual(payload["user_question"], "Describe the scene.")
        self.assertEqual(payload["scene_state"]["player"], None)
        self.assertEqual(payload["scene_state"]["raw_scene"]["exits"], [])
        self.assertEqual(payload["scene_state"]["raw_scene"]["items"], [])
        self.assertEqual(payload["scene_state"]["raw_scene"]["entities"], [])
        self.assertEqual(payload["scene_state"]["raw_scene"]["features"], [])

    def test_prompt_submission_forwards_stream_deltas_to_callback(self) -> None:
        runner = make_runner()
        model = FakeModel()
        scene = {
            "room_description": "You are in a room.",
            "elements": [],
        }
        deltas = []
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.handle_tui_command(
            "Describe the scene.",
            on_response_delta=deltas.append,
        )

        self.assertEqual(deltas, ["model ", "response"])
        self.assertEqual(runner.last_response, "model response")

    def test_async_model_request_streams_through_event_queue(self) -> None:
        runner = make_runner()
        model = FakeModel()
        scene = {
            "room_description": "You are in a room.",
            "elements": [],
        }
        runner.look = lambda: scene
        runner.get_model = lambda: model
        notifications = []
        runner.notify_generation_finished_for_debug = lambda: notifications.append(
            "finished"
        )

        runner.start_model_request("Describe the scene.")
        self.assertTrue(runner.model_generating)
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertFalse(runner.model_generating)
        self.assertEqual(runner.last_response, "model response")
        self.assertEqual(runner.response_history, ["model response"])
        self.assertEqual(len(model.prompts), 1)
        self.assertEqual(notifications, ["finished"])

    def test_debug_generation_notification_uses_paplay(self) -> None:
        runner = make_runner()
        sound_path = Path(tempfile.mkdtemp()) / "complete.oga"
        sound_path.write_bytes(b"sound")
        runner.DEBUG_GENERATION_SOUND_PATH = sound_path

        with (
            patch("model.trace.shutil.which", return_value="/usr/bin/paplay"),
            patch("model.trace.subprocess.run") as run,
        ):
            runner.notify_generation_finished_for_debug()

        run.assert_called_once_with(
            ["/usr/bin/paplay", str(sound_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=1.0,
            check=False,
        )

    def test_response_pane_text_keeps_history_and_current_partial(self) -> None:
        runner = make_runner()
        runner.response_history = ["move(north)", "pickup()"]
        runner.last_response = "move(east"

        self.assertEqual(
            runner.response_pane_text(),
            "move(north)\n\npickup()\n\nmove(east",
        )

    def test_response_pane_text_formats_decision_json_for_humans(self) -> None:
        runner = make_runner()
        runner.response_history = [
            (
                '{"decision":"switch","chosen_action_id":"explore_corridor:west",'
                '"reason":"continue west"}\n\nAction: follow_corridor(west)'
            )
        ]

        self.assertEqual(
            runner.response_pane_text(),
            "\n".join(
                [
                    "- Decision: switch",
                    "- Chosen: explore_corridor:west",
                    "- Reason: continue west",
                    "- Executed: follow_corridor(west)",
                ]
            ),
        )

    def test_payload_pane_text_formats_compact_payload_for_humans(self) -> None:
        runner = make_runner()
        runner.last_payload = json.dumps(
            {
                "scene": {
                    "identity": "priest called agent",
                    "room_description": "Visible area.",
                    "visibility": "normal",
                    "location_context": {
                        "area_type": "corridor",
                        "in_corridor": True,
                        "adjacent_corridors": ["west", "north"],
                    },
                    "visible": [
                        {
                            "kind": "item",
                            "label": "a scroll",
                            "steps": 2,
                            "path": ["west", "west"],
                        }
                    ],
                },
                "recent_actions": ["move(west)"],
                "current_procedure": None,
                "available_actions": [
                    {
                        "id": "explore_corridor:west",
                        "label": "Follow corridor west",
                        "priority": "nearest_exploration",
                        "steps": 1,
                    }
                ],
                "decision": {"mode": "choose_action"},
            }
        )

        text = runner.payload_pane_text()

        self.assertIn("- Mode: choose_action", text)
        self.assertIn("- Player: priest called agent", text)
        self.assertIn("- Location: corridor, in corridor", text)
        self.assertIn("  - a scroll (item; 2 steps; via west, west)", text)
        self.assertIn(
            "  - Follow corridor west (explore_corridor:west; "
            "nearest_exploration; 1 steps)",
            text,
        )

    def test_removed_auto_command_is_unknown(self) -> None:
        runner = make_runner()

        runner.handle_tui_command("/auto")
        self.assertFalse(runner.auto_mode)
        self.assertEqual(runner.last_response, "Unknown command: /auto")

    def test_auto_mode_starts_next_action_request(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore:exit:north","reason":"leave"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.auto_mode = True
        runner.terminal = FakeTerminal(["before", "after"])
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertTrue(runner.model_generating)
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        payload = json.loads(model.prompts[0])
        self.assertEqual(payload["decision"]["mode"], "choose_action")
        self.assertIn("available_actions", payload)
        self.assertNotIn("user_question", payload)

    def test_auto_response_sends_translated_keys_to_game(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore:exit:north","reason":"leave"}'
        )
        observed_scenes = []
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: observed_scenes.append(scene) or scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertTrue(runner.auto_mode)
        self.assertGreater(runner.next_auto_request_at, 0.0)
        self.assertEqual(runner.current_action_id, "explore:exit:north")

    def test_auto_waits_before_starting_next_request_after_action(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore:exit:north","reason":"leave"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()
        runner.maybe_start_auto_request()

        self.assertFalse(runner.model_generating)
        self.assertEqual(len(model.prompts), 1)

    def test_auto_mode_continues_active_procedure_without_model_call(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -1]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.last_map_lines = [
            "...",
            ".@.",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertFalse(runner.model_generating)
        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertEqual(runner.last_response, "auto_continue: move(north)")
        self.assertTrue(runner.last_model_skipped)
        self.assertEqual(runner.last_trace_result["model_skipped"], True)

    def test_auto_mode_interrupts_when_non_pet_scene_event_happened(self) -> None:
        runner = make_runner()
        action = {
            "action_id": "pick:item:gem",
            "action_type": "pick_item",
            "target_key": "item:gem",
            "procedure_kind": "static",
            "next_action": "move(east)",
        }
        runner.current_action_id = "pick:item:gem"
        runner.current_procedure = {
            "action_id": "pick:item:gem",
            "action_type": "pick_item",
            "target_key": "item:gem",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_available_actions = [action]
        runner.last_available_actions_by_id = {"pick:item:gem": action}
        runner.last_scene_events = [
            {
                "type": "moved",
                "target_key": "monster:jackal",
                "text": "jackal moved 1 step west",
            }
        ]

        continued = runner.maybe_continue_auto_procedure()

        self.assertFalse(continued)
        self.assertEqual(runner.last_execution_outcome["reason"], "scene_moved")

    def test_pick_item_action_moves_then_pickups_as_one_runtime_procedure(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"pick:item:gold","reason":"safe nearby loot"}'
        )
        scene_before = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "7 gold pieces", "pos": [1, 0]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scene_on_item = {
            **scene_before,
            "items": [{"description": "7 gold pieces", "pos": [0, 0]}],
        }
        scene_after_pickup = {
            **scene_before,
            "items": [],
        }
        scenes = [scene_before, scene_on_item, scene_on_item, scene_after_pickup]
        runner.last_map_lines = ["@$."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (0, 0)
        runner.terminal = FakeTerminal(["before", "on item", "after pickup"])
        runner.auto_mode = True

        def look() -> dict[str, object]:
            scene = scenes.pop(0)
            if scene is scene_on_item:
                runner.last_map_lines = [".@."]
                runner.last_player_screen_pos = (1, 0)
            elif scene is scene_after_pickup:
                runner.last_map_lines = [".@."]
                runner.last_player_screen_pos = (1, 0)
            return scene

        runner.look = look
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()
        runner.next_auto_request_at = 0
        runner.maybe_start_auto_request()

        self.assertEqual(runner.terminal.sent_keys, ["l", ","])
        self.assertEqual(runner.last_execution_outcome["status"], "item_picked_up")
        self.assertEqual(runner.procedure_status, "completed")
        self.assertIsNone(runner.current_action_id)
        event_texts = [event["text"] for event in runner.procedure_events]
        self.assertTrue(any("Picked up gold" in text for text in event_texts))

    def test_gold_pickup_procedure_survives_quantity_description_change(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"pick:item:gold","reason":"safe nearby loot"}'
        )
        scene_before = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "some gold pieces", "pos": [2, 0]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scene_after_step = {
            **scene_before,
            "items": [{"description": "7 gold pieces", "pos": [1, 0]}],
        }
        scene_on_item = {
            **scene_before,
            "items": [{"description": "7 gold pieces", "pos": [0, 0]}],
        }
        scene_after_pickup = {
            **scene_before,
            "items": [],
        }
        scenes = [scene_before, scene_after_step, scene_on_item, scene_on_item, scene_after_pickup]
        runner.last_map_lines = ["@.$"]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (0, 0)
        runner.terminal = FakeTerminal(["before", "middle", "on item", "after pickup"])
        runner.auto_mode = True

        def look() -> dict[str, object]:
            scene = scenes.pop(0)
            if scene is scene_after_step:
                runner.last_map_lines = [".@$"]
                runner.last_player_screen_pos = (1, 0)
            elif scene is scene_on_item or scene is scene_after_pickup:
                runner.last_map_lines = ["..@"]
                runner.last_player_screen_pos = (2, 0)
            return scene

        runner.look = look
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()
        runner.next_auto_request_at = 0
        runner.maybe_start_auto_request()
        runner.next_auto_request_at = 0
        runner.maybe_start_auto_request()

        self.assertEqual(runner.terminal.sent_keys, ["l", "l", ","])
        self.assertEqual(runner.last_execution_outcome["status"], "item_picked_up")
        self.assertEqual(runner.procedure_status, "completed")
        self.assertIsNone(runner.current_action_id)

    def test_spellbook_pickup_survives_more_specific_description(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"pick:item:spellbook","reason":"useful nearby book"}'
        )
        scene_before = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "features": [],
            "items": [{"description": "a spellbook", "pos": [1, 0]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scene_on_item = {
            **scene_before,
            "items": [{"description": "a light green spellbook", "pos": [0, 0]}],
        }
        scene_after_pickup = {
            **scene_before,
            "items": [],
        }
        scenes = [scene_before, scene_on_item, scene_on_item, scene_after_pickup]
        runner.last_map_lines = ["@+."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (0, 0)
        runner.terminal = FakeTerminal(["before", "on item", "after pickup"])
        runner.auto_mode = True

        def look() -> dict[str, object]:
            scene = scenes.pop(0)
            if scene is scene_on_item or scene is scene_after_pickup:
                runner.last_map_lines = [".@."]
                runner.last_player_screen_pos = (1, 0)
            return scene

        runner.look = look
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()
        runner.next_auto_request_at = 0
        runner.maybe_start_auto_request()

        self.assertEqual(runner.terminal.sent_keys, ["l", ","])
        self.assertEqual(runner.last_execution_outcome["status"], "item_picked_up")
        self.assertEqual(runner.procedure_status, "completed")
        self.assertIsNone(runner.current_action_id)

    def test_auto_mode_interrupts_dynamic_exploration_loop(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:frontier"
        runner.current_procedure = {
            "action_id": "explore:frontier",
            "action_type": "explore",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.auto_exploration_action_id = "explore:frontier"
        runner.auto_exploration_positions = [(2, 1), (3, 1), (2, 1)]
        runner.last_map_lines = [
            ".....",
            "..@. ",
            ".....",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 1)
        runner.terminal = FakeTerminal(["screen"])
        runner.look = lambda: scene

        continued = runner.maybe_continue_auto_procedure()

        self.assertFalse(continued)
        self.assertIsNone(runner.current_action_id)
        self.assertEqual(runner.procedure_status, "completed")
        self.assertEqual(runner.terminal.sent_keys, [])

    def test_auto_mode_does_not_continue_explore_visible_area(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore_visible_area"
        runner.current_procedure = {
            "action_id": "explore_visible_area",
            "action_type": "explore",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_map_lines = [
            "...",
            ".@.",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["screen"])
        runner.look = lambda: scene

        continued = runner.maybe_continue_auto_procedure()

        self.assertFalse(continued)
        self.assertIsNone(runner.current_action_id)
        self.assertEqual(runner.procedure_status, "completed")
        self.assertEqual(runner.terminal.sent_keys, [])

    def test_auto_mode_hands_lost_exit_route_to_adjacent_corridor(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:west"
        runner.current_procedure = {
            "action_id": "explore:exit:west",
            "action_type": "explore",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_map_lines = ["#@."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.terminal.sent_keys, ["h"])
        self.assertEqual(
            runner.last_selected_action["action_id"],
            "explore_corridor:west",
        )
        self.assertEqual(runner.last_trace_result["model_skipped"], True)

    def test_lightweight_refresh_keeps_known_pet_from_becoming_hostile(self) -> None:
        cases = (
            ("d", "tame little dog"),
            ("f", "tame cat called Kitty"),
            ("f", "tame kitten"),
            ("u", "tame horse"),
            ("u", "tame saddled pony called Horse"),
        )
        for glyph, pet_description in cases:
            with self.subTest(glyph=glyph, pet_description=pet_description):
                runner = make_runner()
                runner.last_scene = {
                    "room_description": "Visible area.",
                    "visibility": "normal",
                    "entities": [
                        {
                            "description": pet_description,
                            "pos": [0, 1],
                        }
                    ],
                }
                runner.executed_actions = ["move(west)"]

                description = runner.lightweight_known_ally_description(
                    glyph,
                    [1, 0],
                    runner.last_scene,
                )

                self.assertEqual(description, pet_description)

    def test_auto_action_exits_farlook_before_sending_movement(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        action = {
            "action_id": "explore:exit:north",
            "action_type": "explore",
            "label": "Explore through north opening",
            "target_ref": "north_exit_1",
            "target_key": "exit:north",
            "procedure_kind": "static",
            "low_level_goal": "explore through the north opening",
            "next_action": "move(north)",
            "path_steps": ["north"],
        }
        runner.terminal = FakeTerminal(
            [
                "Pick a monster, object or location.",
                "normal game screen",
                "after move",
            ]
        )
        runner.refresh_scene_cache = lambda: scene

        runner.execute_selected_action(
            action=action,
            response="continue",
            request_kind="auto_continue_code",
            model_skipped=True,
        )

        self.assertEqual(runner.terminal.sent_keys[:2], ["\x1b", "k"])

    def test_invalid_model_decision_exits_farlook_prompt(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(
            [
                "Pick a monster, object or location.",
                "normal game screen",
            ]
        )
        runner.auto_mode = True
        runner.last_available_actions_by_id = {}

        runner.apply_decision_response(
            '{"decision":"switch","chosen_action_id":"missing","reason":"bad"}'
        )

        self.assertEqual(runner.terminal.sent_keys, ["\x1b"])
        self.assertFalse(runner.auto_mode)
        self.assertEqual(runner.last_execution_outcome["status"], "invalid_decision")

    def test_auto_mode_blocks_static_exploration_when_scene_does_not_change(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -1]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.current_procedure = {
            "action_id": "explore:exit:north",
            "action_type": "explore",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_map_lines = [
            "...",
            ".@.",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["same screen", "same screen"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.last_execution_outcome["status"], "movement_blocked")
        self.assertEqual(runner.blocked_action_id, "explore:exit:north")
        self.assertIsNone(runner.current_action_id)
        self.assertEqual(runner.procedure_status, "blocked")

    def test_blocked_movement_does_not_count_noisy_scene_change_as_progress(self) -> None:
        runner = make_runner()
        scene_before = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scene_after = {
            **scene_before,
            "features": [{"description": "door", "pos": [-1, -1]}],
        }
        action = {
            "action_id": "explore_visible_area",
            "action_type": "explore",
            "label": "Explore visible area",
            "target_ref": None,
            "target_key": "area:visible",
            "procedure_kind": "dynamic",
            "low_level_goal": "explore nearby visible tiles",
            "next_action": "move(northwest)",
            "path_steps": [],
            "distance_steps": 1,
            "completes_procedure_after_step": True,
        }
        runner.last_trace_input = {"scene_before_action": scene_before}
        runner.last_map_lines = [
            "...",
            ".@.",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["same screen", "same screen"])
        runner.refresh_scene_cache = lambda: scene_after

        runner.execute_selected_action(
            action=action,
            response='{"decision":"switch"}',
            request_kind="auto",
        )

        self.assertEqual(runner.terminal.sent_keys, ["y"])
        self.assertEqual(runner.last_execution_outcome["status"], "movement_blocked")
        self.assertFalse(runner.last_execution_outcome["scene_changed"])
        self.assertEqual(runner.procedure_status, "blocked")
        self.assertEqual(runner.blocked_action_id, "explore_visible_area")

    def test_auto_mode_ignores_pet_only_movement_without_model_call(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        previous_scene = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "player": {"identity": None, "pos": [0, 0]},
            "exits": [
                {
                    "description": "exit",
                    "direction": "north",
                    "pos": [0, -1],
                    "ref": "north_exit_1",
                    "target_key": "exit:north",
                    "display_name": "north exit",
                }
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "tame kitten called Kitty",
                    "pos": [1, 0],
                    "ref": "tame_kitten_called_kitty_1",
                    "target_key": "ally:tame_kitten_called_kitty",
                    "display_name": "tame kitten called Kitty",
                }
            ],
        }
        current_scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {"description": "tame kitten called Kitty", "pos": [1, 1]}
            ],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -1]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.last_scene = previous_scene
        runner.last_map_lines = [
            "...",
            ".@f",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: current_scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertTrue(runner.last_model_skipped)

    def test_auto_mode_calls_model_when_hostile_appears_during_procedure(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"flee:monster:goblin","reason":"danger"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "goblin", "pos": [1, 0]}],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -1]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.last_map_lines = [
            "...",
            ".@o",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()
        self.assertTrue(runner.model_generating)
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(len(model.prompts), 1)
        self.assertFalse(runner.last_model_skipped)

    def test_auto_mode_ignores_distant_stationary_hostile_during_procedure(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        previous_scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "player": {"identity": None, "pos": [0, 0]},
            "exits": [
                {
                    "description": "exit",
                    "direction": "north",
                    "pos": [0, -1],
                    "ref": "north_exit_1",
                    "target_key": "exit:north",
                    "display_name": "north exit",
                }
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "lichen",
                    "pos": [8, 1],
                    "ref": "lichen_9",
                    "target_key": "monster:lichen",
                    "display_name": "lichen",
                }
            ],
        }
        current_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "lichen", "pos": [9, 1]}],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -1]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.last_scene = previous_scene
        runner.executed_actions = ["move(west)"]
        runner.last_map_lines = [
            "...",
            ".@.",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: current_scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertTrue(runner.last_model_skipped)
        self.assertEqual(runner.last_scene_events, [])

    def test_auto_mode_continues_flee_when_target_is_stationary(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        previous_scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "player": {"identity": None, "pos": [0, 0]},
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "goblin",
                    "pos": [1, 0],
                    "ref": "goblin_1",
                    "target_key": "monster:goblin",
                    "display_name": "goblin",
                }
            ],
        }
        current_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "goblin", "pos": [2, 0]}],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "flee:monster:goblin"
        runner.last_scene = previous_scene
        runner.executed_actions = ["move(west)"]
        runner.last_map_lines = ["..@.o"]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 0)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: current_scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertEqual(model.prompts, [])
        self.assertTrue(runner.last_model_skipped)
        self.assertEqual(runner.last_scene_events, [])

    def test_auto_mode_calls_model_when_flee_target_moves(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"flee:monster:goblin","reason":"target moved"}'
        )
        previous_scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "player": {"identity": None, "pos": [0, 0]},
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "goblin",
                    "pos": [1, 0],
                    "ref": "goblin_1",
                    "target_key": "monster:goblin",
                    "display_name": "goblin",
                }
            ],
        }
        current_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "goblin", "pos": [1, 0]}],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "flee:monster:goblin"
        runner.last_scene = previous_scene
        runner.executed_actions = ["move(west)"]
        runner.last_map_lines = ["..@o."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 0)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: current_scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()
        self.assertTrue(runner.model_generating)
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(len(model.prompts), 1)
        self.assertFalse(runner.last_model_skipped)

    def test_auto_mode_stops_without_model_when_no_actions_are_available(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {"description": "tame kitten called Kitty", "pos": [1, 0]}
            ],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.terminal = FakeTerminal(["screen"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertFalse(runner.auto_mode)
        self.assertFalse(runner.model_generating)
        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.last_execution_outcome["status"], "no_available_actions")
        self.assertTrue(runner.last_model_skipped)
        self.assertEqual(
            runner.last_response,
            "Auto stopped: no enabled actions are available.",
        )

    def test_auto_mode_recovers_visible_hostile_before_stopping(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"flee:monster:goblin",'
            '"reason":"visible hostile"}'
        )
        full_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        lightweight_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "goblin", "pos": [1, 0]}],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.terminal = FakeTerminal(["screen"])
        runner.auto_mode = True
        runner.look = lambda: full_scene

        def recover_scene() -> dict[str, object]:
            runner.last_map_lines = [".@g"]
            runner.last_viewport = runtime.MapViewport(
                top=0,
                bottom=0,
                left=0,
                right=2,
                overlay_rows=frozenset(),
            )
            runner.last_player_screen_pos = (1, 0)
            return lightweight_scene

        runner.refresh_lightweight_visible_scene_cache = recover_scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertTrue(runner.model_generating)
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        self.assertEqual(len(model.prompts), 1)
        self.assertIn("flee:monster:goblin", model.prompts[0])
        outcome = runner.last_execution_outcome
        status = outcome.get("status") if isinstance(outcome, dict) else None
        self.assertNotEqual(status, "no_available_actions")

    def test_continue_decision_keeps_current_procedure(self) -> None:
        runner = make_runner()
        model = FakeModel("continue")
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertEqual(runner.current_action_id, "explore:exit:north")
        self.assertEqual(runner.last_response, "move(north)")
        self.assertEqual(runner.response_history, ["move(north)"])
        self.assertEqual(
            runner.last_parsed_decision,
            {
                "decision": "continue",
                "chosen_action_id": "explore:exit:north",
                "reason": None,
            },
        )

    def test_active_procedure_prompt_requests_bare_continue(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]},
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "agent", "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:north"
        runner.last_map_lines = [
            ".....",
            ".. ..",
            "..@..",
            ".....",
            ".....",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)

        payload = json.loads(runner.build_model_prompt(AUTO_PROMPT, scene))
        trace_payload = runner.last_trace_payload

        self.assertEqual(
            payload["decision"]["response_contract"],
            "When continuing the active current_procedure, output exactly: continue",
        )
        self.assertEqual(
            trace_payload["decision_request"]["instruction"],
            "If the current procedure is still safe and useful, return exactly "
            "`continue`. If switching, return JSON with decision=switch, "
            "chosen_action_id, and a reason of 6 words or fewer.",
        )

    def test_exit_step_through_completes_current_procedure(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"continue","chosen_action_id":"explore:exit:east","reason":"leave"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "east", "pos": [0, 0]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.current_action_id = "explore:exit:east"
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["l"])
        self.assertIsNone(runner.current_action_id)
        self.assertIsNone(runner.current_procedure)
        self.assertEqual(runner.procedure_status, "completed")

    def test_adjacent_door_action_retries_until_door_opens(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"go_to_door:door:west","reason":"open door"}'
        )
        closed_scene = {
            "room_description": "You are in a room.",
            "features": [{"description": "visible door", "pos": [-1, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        open_scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scenes = [closed_scene, closed_scene, open_scene]
        runner.last_map_lines = ["+@."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        runner.terminal = FakeTerminal(
            [
                "initial",
                "unused",
                "The door resists.",
                "unused",
                "The door opens.",
            ]
        )
        runner.auto_mode = True
        runner.look = lambda: scenes.pop(0)
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["oh", "oh"])
        self.assertEqual(runner.last_execution_outcome["status"], "door_opened")
        self.assertEqual(runner.last_execution_outcome["attempts"], 2)
        self.assertEqual(runner.procedure_status, "active")
        self.assertEqual(runner.current_action_id, "continue:opened_door")
        self.assertEqual(runner.pending_open_door_step, "move(west)")

    def test_diagonal_closed_door_approaches_before_opening(self) -> None:
        runner = make_runner()
        scene_before = {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": ["northwest"],
                "in_front_of_door": True,
            },
            "features": [
                {
                    "description": "closed door",
                    "pos": [-1, -1],
                    "ref": "closed_door_1",
                    "target_key": "door:northwest",
                    "display_name": "door",
                }
            ],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scene_after = {
            **scene_before,
            "location_context": {
                **scene_before["location_context"],
                "adjacent_doors": ["west"],
            },
            "features": [
                {
                    "description": "closed door",
                    "pos": [-1, 0],
                    "ref": "closed_door_1",
                    "target_key": "door:west",
                    "display_name": "door",
                }
            ],
        }
        runner.last_map_lines = [
            "-----",
            "+...|",
            "|@..|",
            "|...|",
            "-----",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 2)
        action = next(
            action
            for action in runner.build_available_actions(scene_before)
            if action["action_id"] == "explore_door:door:northwest"
        )

        def look_after_approach() -> dict[str, object]:
            runner.last_map_lines = [
                "-----",
                "+@..|",
                "|...|",
                "|...|",
                "-----",
            ]
            runner.last_player_screen_pos = (1, 1)
            return scene_after

        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = look_after_approach
        runner.last_trace_input = {"scene_before_action": scene_before}

        runner.execute_selected_action(
            action=action,
            response="continue",
            request_kind="auto",
        )

        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertEqual(runner.last_executed_low_level_action, "move(north)")
        self.assertEqual(runner.current_action_id, "explore_door:door:west")
        self.assertEqual(runner.procedure_status, "active")
        self.assertEqual(runner.current_procedure["next_action"], "move(west)")

    def test_door_approach_rebinds_when_relative_door_name_changes(self) -> None:
        runner = make_runner()
        runner.current_action_id = "explore_door:door:southeast"
        runner.current_procedure = {
            "action_id": "explore_door:door:southeast",
            "action_type": "explore_door",
            "target_key": "door:southeast",
            "procedure_kind": "static",
            "next_action": "move(southeast)",
            "path_steps": ["southeast", "east", "east"],
            "door_pos": [6, 1],
            "approach_door": True,
            "requires_open": True,
            "status": "active",
        }
        runner.procedure_status = "active"

        runner.project_active_door_target_after_move((1, 1))
        snapshot = runner.current_procedure_snapshot(
            [
                {
                    "action_id": "explore:exit:east",
                    "action_type": "explore",
                    "target_key": "exit:east",
                    "next_action": "move(east)",
                    "path_steps": ["east"],
                },
                {
                    "action_id": "explore_door:door:east",
                    "action_type": "explore_door",
                    "target_key": "door:east",
                    "procedure_kind": "static",
                    "low_level_goal": "explore through closed door",
                    "next_action": "move(east)",
                    "path_steps": ["east", "east"],
                    "door_pos": [5, 0],
                    "approach_door": True,
                    "requires_open": True,
                },
            ]
        )

        self.assertIsNotNone(snapshot)
        self.assertEqual(runner.current_action_id, "explore_door:door:east")
        self.assertEqual(runner.procedure_status, "active")
        self.assertEqual(runner.current_procedure["target_key"], "door:east")
        self.assertEqual(runner.current_procedure["door_pos"], [5, 0])
        self.assertEqual(runner.current_procedure["next_action"], "move(east)")

    def test_adjacent_door_action_follows_closed_resists_opens_flow(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"go_to_door:door:west","reason":"open door"}'
        )
        closed_scene = {
            "room_description": "You are in a room.",
            "features": [{"description": "visible door", "pos": [-1, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        open_scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scenes = [closed_scene, closed_scene, closed_scene, open_scene]
        runner.last_map_lines = ["+@."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        runner.terminal = FakeTerminal(
            [
                "initial",
                "unused",
                "The door is closed.",
                "unused",
                "The door resists.",
                "unused",
                "The door opens.",
            ]
        )
        runner.auto_mode = True
        runner.look = lambda: scenes.pop(0)
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["oh", "oh", "oh"])
        self.assertEqual(runner.last_execution_outcome["status"], "door_opened")
        self.assertEqual(runner.last_execution_outcome["attempts"], 3)
        self.assertEqual(runner.procedure_status, "active")
        self.assertEqual(runner.current_action_id, "continue:opened_door")
        self.assertEqual(runner.pending_open_door_step, "move(west)")

    def test_door_open_retry_exits_farlook_before_each_attempt(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "features": [{"description": "visible door", "pos": [-1, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scenes = [scene, scene, scene]
        runner.last_map_lines = ["+@."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        runner.terminal = FakeTerminal(
            [
                "normal",
                "The door resists.",
                "Pick a monster, object or location.",
                "normal again",
                "The door opens.",
            ]
        )
        runner.look = lambda: scenes.pop(0) if scenes else scene

        runner.run_door_open_procedure(
            action={
                "action_id": "go_to_door:door:west",
                "action_type": "go_to_door",
                "target_key": "door:west",
                "requires_open": True,
                "distance_steps": 1,
                "next_action": "move(west)",
                "path_steps": ["west"],
            },
            response="continue",
            request_kind="auto_continue_code",
            scene_before_action=scene,
        )

        self.assertEqual(runner.terminal.sent_keys, ["oh", "\x1b", "oh"])
        self.assertEqual(runner.last_execution_outcome["status"], "door_opened")

    def test_adjacent_locked_door_stops_without_unlocking(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"go_to_door:door:west","reason":"open door"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [{"description": "visible door", "pos": [-1, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scenes = [scene, scene]
        runner.last_map_lines = ["+@."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        runner.terminal = FakeTerminal(["initial", "unused", "The door is locked."])
        runner.auto_mode = True
        runner.look = lambda: scenes.pop(0)
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["oh"])
        self.assertEqual(runner.last_execution_outcome["status"], "door_open_locked")
        self.assertEqual(runner.last_execution_outcome["attempts"], 1)
        self.assertFalse(runner.auto_mode)
        self.assertEqual(runner.procedure_status, "blocked")

    def test_adjacent_open_door_moves_through_without_open_retry(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore_door:door:west","reason":"go through"}'
        )
        scene = {
            "room_description": "Visible area.",
            "features": [{"description": "open door", "pos": [-1, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            ".|.",
            ".-@",
            ".|.",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 1)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["h"])
        self.assertEqual(runner.last_executed_low_level_action, "move(west)")
        self.assertNotEqual(
            runner.last_execution_outcome["status"],
            "door_open_retry_limit",
        )

    def test_stale_pending_opened_door_does_not_preempt_corridor_auto_continue(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["before", "after"])
        runner.render_screen = lambda print_output=False: "after"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.auto_mode = True
        runner.pending_open_door_step = "move(east)"
        runner.pending_open_door_direction = "east"
        runner.current_action_id = "explore_corridor:east"
        runner.current_procedure = {
            "action_id": "explore_corridor:east",
            "action_type": "explore_corridor",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_available_actions = [
            {
                "action_id": "continue:opened_door",
                "action_type": "explore_door",
                "target_key": "door:east:opened",
                "procedure_kind": "static",
                "low_level_goal": "move through the opened east doorway",
                "next_action": "move(east)",
                "path_steps": ["east"],
                "distance_steps": 1,
                "completes_procedure_after_step": True,
                "post_open_door_step": True,
            },
            {
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(east)",
                "path_steps": ["east"],
                "distance_steps": 1,
            },
        ]
        runner.last_available_actions_by_id = {
            action["action_id"]: action for action in runner.last_available_actions
        }
        runner.last_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_trace_input = {
            "scene_before_action": {
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }
        }
        runner.refresh_scene_cache = lambda: {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        self.assertTrue(runner.maybe_continue_auto_procedure())

        self.assertEqual(runner.terminal.sent_keys, ["l"])
        self.assertEqual(
            runner.last_selected_action["action_id"],
            "explore_corridor:east",
        )
        self.assertEqual(
            runner.last_executed_low_level_action,
            "move(east)",
        )
        self.assertEqual(runner.pending_open_door_step, "move(east)")
        self.assertEqual(runner.pending_open_door_direction, "east")

    def test_pending_opened_door_still_interrupts_for_hostile(self) -> None:
        runner = make_runner()
        runner.auto_mode = True
        runner.pending_open_door_step = "move(east)"
        runner.pending_open_door_direction = "east"
        runner.current_action_id = "continue:opened_door"
        runner.current_procedure = {
            "action_id": "continue:opened_door",
            "action_type": "explore_door",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_available_actions = [
            {
                "action_id": "continue:opened_door",
                "action_type": "explore_door",
                "target_key": "door:east:opened",
                "procedure_kind": "static",
                "low_level_goal": "move through the opened east doorway",
                "next_action": "move(east)",
                "path_steps": ["east"],
                "distance_steps": 1,
                "completes_procedure_after_step": True,
                "post_open_door_step": True,
            }
        ]
        runner.last_available_actions_by_id = {
            "continue:opened_door": runner.last_available_actions[0]
        }
        runner.last_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "goblin", "pos": [1, 0]}],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        self.assertFalse(runner.maybe_continue_auto_procedure())

        self.assertEqual(
            runner.last_execution_outcome["reason"],
            "hostile_nearby",
        )
        self.assertEqual(runner.pending_open_door_step, "move(east)")

    def test_pending_opened_door_ignores_harmless_scene_events(self) -> None:
        runner = make_runner()
        runner.pending_open_door_step = "move(east)"
        runner.pending_open_door_direction = "east"
        runner.current_action_id = "continue:opened_door"
        runner.current_procedure = {
            "action_id": "continue:opened_door",
            "action_type": "explore_door",
            "status": "active",
        }
        runner.procedure_status = "active"
        action = {
            "action_id": "continue:opened_door",
            "action_type": "explore_door",
            "target_key": "door:east:opened",
            "procedure_kind": "static",
            "low_level_goal": "move through the opened east doorway",
            "next_action": "move(east)",
            "path_steps": ["east"],
            "distance_steps": 1,
            "completes_procedure_after_step": True,
            "post_open_door_step": True,
        }
        runner.last_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_scene_events = [
            {
                "type": "procedure",
                "procedure": "door_exploration",
                "status": "opened_door",
                "text": "The door opened.",
            },
            {"type": "moved", "text": "A tame pet moved."},
        ]

        self.assertIsNone(runner.auto_continue_interrupt_reason(action))

    def test_pending_opened_door_steps_through_wall_rendered_opening(self) -> None:
        runner = make_runner()
        runner.pending_open_door_step = "move(east)"
        runner.pending_open_door_direction = "east"
        runner.last_map_lines = [
            "...",
            ".@-",
            "...",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        scene = {
            "room_description": "Visible area.",
            "features": [{"description": "visible door", "pos": [1, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        actions = runner.build_available_actions(scene)
        action_map = {action["action_id"]: action for action in actions}

        self.assertIn("continue:opened_door", action_map)
        self.assertEqual(
            action_map["continue:opened_door"]["next_action"],
            "move(east)",
        )

    def test_opened_door_step_into_corridor_starts_corridor_follow(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
            },
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.build_available_actions = lambda _scene: [
            {
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "target_ref": None,
                "target_key": "corridor:east",
                "procedure_kind": "dynamic",
                "low_level_goal": "follow the corridor east",
                "next_action": "follow_corridor(east)",
                "path_steps": ["east"],
                "distance_steps": 1,
                "interruptible": True,
            }
        ]

        self.assertTrue(runner.start_corridor_after_opened_door(scene, "east"))

        self.assertEqual(runner.current_action_id, "explore_corridor:east")
        self.assertEqual(runner.procedure_status, "active")
        self.assertEqual(
            runner.current_procedure["next_action"],
            "follow_corridor(east)",
        )

    def test_opened_door_step_adopts_adjacent_corridor_from_room_context(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "adjacent_corridors": ["north"],
            },
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.build_available_actions = lambda _scene: [
            {
                "action_id": "explore_corridor:north",
                "action_type": "explore_corridor",
                "target_ref": None,
                "target_key": "corridor:north",
                "procedure_kind": "dynamic",
                "low_level_goal": "follow the corridor north",
                "next_action": "follow_corridor(north)",
                "path_steps": ["north"],
                "distance_steps": 1,
                "interruptible": True,
            }
        ]

        self.assertTrue(runner.start_corridor_after_opened_door(scene, "north"))

        self.assertEqual(runner.current_action_id, "explore_corridor:north")
        self.assertEqual(runner.procedure_status, "active")
        self.assertEqual(
            runner.current_procedure["next_action"],
            "follow_corridor(north)",
        )

    def test_door_open_retry_stops_when_hostile_moves(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"flee:door:west","reason":"open door"}'
        )
        scene_before = {
            "room_description": "You are in a room.",
            "features": [{"description": "visible door", "pos": [-1, 0]}],
            "items": [],
            "areas": [],
            "entities": [{"description": "goblin", "pos": [3, 0]}],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        scene_after = {
            **scene_before,
            "entities": [{"description": "goblin", "pos": [2, 0]}],
        }
        scenes = [scene_before, scene_after]
        runner.last_map_lines = ["+@..o"]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        runner.terminal = FakeTerminal(["initial", "unused", "The door resists."])
        runner.auto_mode = True
        runner.look = lambda: scenes.pop(0)
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["oh"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "stopped_hostile_moved",
        )
        self.assertFalse(runner.auto_mode)
        self.assertEqual(runner.procedure_status, "blocked")

    def test_corridor_action_follows_straight_corridor_until_end(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore_corridor:east","reason":"follow corridor"}'
        )
        scenes = [
            (
                {"room_description": "Visible area.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [".@##"],
                (1, 0),
            ),
            (
                {"room_description": "Visible area step 1.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [".#@#"],
                (2, 0),
            ),
            (
                {"room_description": "Visible area step 2.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [".##@"],
                (3, 0),
            ),
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=3,
            overlay_rows=frozenset(),
        )

        def look():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.look = look
        runner.get_model = lambda: model
        runner.auto_mode = True

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["l", "l"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_end",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 2)
        self.assertEqual(
            runner.corridor_backtrack_steps,
            ["move(west)", "move(west)"],
        )
        self.assertEqual(runner.procedure_events[0]["status"], "corridor_follow_end")
        payload = json.loads(
            runner.build_model_prompt(
                AUTO_PROMPT,
                {"room_description": "Visible area step 2.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
            )
        )
        self.assertIn("Corridor following stopped", payload["events"][0])
        self.assertIn("no forward corridor continuation", payload["events"][0])

    def test_corridor_action_exits_farlook_before_each_step(self) -> None:
        runner = make_runner()
        runner.current_action_id = "explore_corridor:south"
        runner.current_procedure = {
            "action_id": "explore_corridor:south",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=0,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = ["@", "#", "#", "#"]
        runner.last_player_screen_pos = (0, 0)
        runner.terminal = FakeTerminal(
            [
                "normal",
                "Pick a monster, object or location.",
                "normal again",
                "after second",
            ]
        )
        scenes = [
            (["#", "@", "#", "#"], (0, 1)),
            (["#", "#", "@"], (0, 2)),
        ]

        def lightweight_scene():
            map_lines, player_pos = scenes.pop(0)
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return {
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:south",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(south)",
            },
            response="continue",
            request_kind="auto_continue_code",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["j", "\x1b", "j"])

    def test_corridor_action_stops_at_intersection(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore_corridor:east","reason":"follow corridor"}'
        )
        scenes = [
            (
                {"room_description": "Visible area.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [".@#."],
                (1, 0),
            ),
            (
                {"room_description": "Visible intersection.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                ["..#.", ".#@#"],
                (2, 1),
            ),
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=3,
            overlay_rows=frozenset(),
        )

        def look():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.look = look
        runner.get_model = lambda: model
        runner.auto_mode = True

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["l"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_intersection",
        )
        self.assertEqual(
            runner.corridor_intersection_avoid_steps,
            [{"origin": [2, 1], "delta": [-1, 0]}],
        )

    def test_corridor_action_follows_diagonal_turn(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore_corridor:east","reason":"follow corridor"}'
        )
        scenes = [
            (
                {"room_description": "Visible area.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [" @# "],
                (1, 0),
            ),
            (
                {"room_description": "Visible turn.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                ["    ", " #@ ", "   #"],
                (2, 1),
            ),
            (
                {"room_description": "Visible end.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                ["    ", "  # ", "   @"],
                (3, 2),
            ),
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=3,
            overlay_rows=frozenset(),
        )

        def look():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.look = look
        runner.get_model = lambda: model
        runner.auto_mode = True

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["l", "n"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_end",
        )

    def test_corridor_action_uses_raw_adjacent_hash_when_scene_alignment_misses_it(self) -> None:
        from navigation.corridor_topology import corridor_follow_decision

        runner = make_runner()
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            "...",
            "@..",
            "...",
        ]
        runner.last_player_screen_pos = (0, 1)
        full_screen = "\n".join(
            [
                "       ",
                " #@    ",
                "       ",
            ]
        )
        runner.screen = full_screen
        runner.terminal = FakeTerminal([full_screen])
        runner.is_traversable_scene_pos = lambda scene, pos: False
        scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        decision = corridor_follow_decision(
            runner=runner,
            scene=scene,
            previous_delta=(0, 1),
        )

        self.assertEqual(decision.status, "continue")
        self.assertEqual(decision.delta, (-1, 0))
        self.assertEqual(decision.reason, "raw adjacent corridor glyph")

    def test_corridor_action_detects_closed_door_at_corridor_end(self) -> None:
        from navigation.corridor_topology import corridor_follow_decision

        runner = make_runner()
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            " + ",
            " @ ",
            " # ",
        ]
        runner.last_player_screen_pos = (1, 1)
        scene = {
            "room_description": "Visible area.",
            "features": [{"description": "closed door", "pos": [0, -1]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
            },
        }

        decision = corridor_follow_decision(
            runner=runner,
            scene=scene,
            previous_delta=(0, -1),
        )

        self.assertEqual(decision.status, "corridor_follow_closed_door")
        self.assertEqual(decision.delta, (0, -1))

    def test_corridor_action_opens_door_at_corridor_end_and_continues(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "The door opens."
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:north"
        runner.current_procedure = {
            "action_id": "explore_corridor:north",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            " + ",
            " # ",
            " @ ",
        ]
        runner.last_player_screen_pos = (1, 2)
        corridor_scene = {
            "room_description": "Visible area.",
            "features": [{"description": "closed door", "pos": [0, -1]}],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
            },
        }
        room_scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
            },
        }
        states = [
            (
                corridor_scene,
                [
                    " + ",
                    " @ ",
                    " # ",
                ],
                (1, 1),
            ),
            (
                room_scene,
                [
                    " @ ",
                    " - ",
                    " # ",
                ],
                (1, 0),
            ),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = states.pop(0)
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        full_scenes = [corridor_scene, room_scene]
        runner.refresh_scene_cache = lambda: full_scenes.pop(0) if full_scenes else room_scene

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:north",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(north)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["k", "ok", "k"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_room_entrance",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 2)
        self.assertEqual(runner.corridor_backtrack_steps, [])

    def test_corridor_action_treats_multiple_raw_adjacent_hashes_as_intersection(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:south"
        runner.current_procedure = {
            "action_id": "explore_corridor:south",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            ".@.",
            ".#.",
        ]
        runner.last_player_screen_pos = (1, 0)

        def lightweight_scene():
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = [
                "...",
                "#@#",
                "...",
            ]
            runner.last_player_screen_pos = (1, 1)
            return {
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lightweight_scene

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:south",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(south)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["j"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_intersection",
        )
        self.assertEqual(runner.corridor_backtrack_steps, [])

    def test_corridor_action_continues_diagonal_bend_instead_of_intersection(self) -> None:
        from navigation.corridor_topology import corridor_follow_decision

        runner = make_runner()
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=5,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            "     ",
            " #   ",
            "  #@ ",
            "   # ",
            "   # ",
            "  -.-",
        ]
        runner.last_player_screen_pos = (3, 2)
        scene = {
            "room_description": "",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
            },
        }

        decision = corridor_follow_decision(
            runner=runner,
            scene=scene,
            previous_delta=(1, 1),
        )

        self.assertEqual(decision.status, "continue")
        self.assertEqual(decision.delta, (0, 1))
        self.assertEqual(decision.reason, "clear corridor continuation through bend")

    def test_corridor_action_ignores_backward_diagonal_bend_artifact(self) -> None:
        from navigation.corridor_topology import corridor_follow_decision

        runner = make_runner()
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            "  #",
            " d@",
            " # ",
        ]
        runner.last_player_screen_pos = (2, 1)
        scene = {
            "room_description": "",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "tame little dog called Doggo",
                    "pos": [-1, 0],
                }
            ],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
            },
        }

        decision = corridor_follow_decision(
            runner=runner,
            scene=scene,
            previous_delta=(1, 0),
        )

        self.assertEqual(decision.status, "continue")
        self.assertEqual(decision.delta, (0, -1))
        self.assertEqual(decision.reason, "single safe corridor continuation")

    def test_corridor_action_keeps_real_intersection_after_artifact_filter(self) -> None:
        from navigation.corridor_topology import corridor_follow_decision

        runner = make_runner()
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            "  # ",
            "  @#",
            "  # ",
        ]
        runner.last_player_screen_pos = (2, 1)
        scene = {
            "room_description": "",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
            },
        }

        decision = corridor_follow_decision(
            runner=runner,
            scene=scene,
            previous_delta=(1, 0),
        )

        self.assertEqual(decision.status, "corridor_follow_intersection")
        self.assertEqual(decision.reason, "multiple corridor continuations")

    def test_corridor_action_steps_onto_room_floor_before_handoff(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:east"
        runner.current_procedure = {
            "action_id": "explore_corridor:east",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=3,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = ["@##."]
        runner.last_player_screen_pos = (0, 0)
        states = [
            (
                {
                    "room_description": "Visible area.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                ["#@#."],
                (1, 0),
            ),
            (
                {
                    "room_description": "Visible area.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                ["##@."],
                (2, 0),
            ),
            (
                {
                    "room_description": "Visible area.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                    "location_context": {
                        "area_type": "visible_area",
                        "in_corridor": False,
                        "in_room": False,
                        "dark": False,
                        "adjacent_corridors": ["west"],
                        "adjacent_doors": [],
                        "in_front_of_door": False,
                    },
                },
                ["###@"],
                (3, 0),
            ),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = states.pop(0)
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": ["west"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
        }

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(east)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["l", "l", "l"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_room_entrance",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 3)
        self.assertEqual(runner.corridor_backtrack_steps, [])
        self.assertEqual(runner.blocked_corridor_entries, [])

    def test_corridor_action_requires_confirmed_room_before_handoff(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:east"
        runner.current_procedure = {
            "action_id": "explore_corridor:east",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = ["@##. "]
        runner.last_player_screen_pos = (0, 0)
        corridor_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
                "dark": False,
                "adjacent_corridors": ["east"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
        }
        states = [
            (corridor_scene, ["#@#. "], (1, 0)),
            (corridor_scene, ["##@. "], (2, 0)),
            (corridor_scene, ["###@#"], (3, 0)),
            (corridor_scene, ["####@"], (4, 0)),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = states.pop(0)
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: corridor_scene

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(east)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["l", "l", "l", "l"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_end",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 4)
        self.assertNotEqual(runner.procedure_status, "completed")

    def test_room_entrance_confirmation_rejects_corridor_ahead(self) -> None:
        runner = make_runner()
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=3,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = ["#@#."]
        runner.last_player_screen_pos = (1, 0)
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": ["east"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
        }

        self.assertFalse(
            runner.scene_confirms_room_entrance(
                scene,
                (1, 0),
                stepped_on_glyph="#",
            )
        )

    def test_room_entrance_confirmation_requires_stepping_on_floor(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": ["north", "southeast"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
        }

        self.assertFalse(
            runner.scene_confirms_room_entrance(
                scene,
                (-1, -1),
                stepped_on_glyph=" ",
            )
        )
        self.assertTrue(
            runner.scene_confirms_room_entrance(
                scene,
                (-1, -1),
                stepped_on_glyph=".",
            )
        )

    def test_corridor_action_continues_bend_when_farlook_cursor_is_elsewhere(self) -> None:
        runner = make_runner()
        initial_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_scene = initial_scene
        runner.last_trace_input = {"scene_before_action": initial_scene}
        runner.terminal = FakeTerminal(
            [
                " @\n #\nNeutral $:0 HP:10(10) Pw:2(2)",
                " #\n @\n##\n \nNeutral $:0 HP:10(10) Pw:2(2)",
                " #\n #\n#@\n \nNeutral $:0 HP:10(10) Pw:2(2)",
                "  \n  \n@ \n  \nNeutral $:0 HP:10(10) Pw:2(2)",
            ],
            cursor_positions=[
                (1, 0),
                (0, 3),
                (0, 3),
                (0, 3),
            ],
        )
        runner.last_map_lines = [" @", " #"]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=1,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 0)
        action = {
            "action_id": "explore_corridor:south",
            "action_type": "explore_corridor",
            "next_action": "follow_corridor(south)",
        }

        runner.execute_selected_action(
            action=action,
            response="model chose corridor",
            request_kind="auto_continue_code",
            model_skipped=True,
        )

        self.assertEqual(runner.terminal.sent_keys[:3], ["j", "j", "h"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_end",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 3)

    def test_corridor_action_stops_when_hostile_appears(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore_corridor:east","reason":"follow corridor"}'
        )
        scenes = [
            (
                {"room_description": "Visible area.", "features": [], "items": [], "areas": [], "entities": [], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [".@##"],
                (1, 0),
            ),
            (
                {"room_description": "Visible danger.", "features": [], "items": [], "areas": [], "entities": [{"description": "goblin", "pos": [2, 0]}], "exits": [], "player": {"identity": None, "pos": [0, 0]}},
                [".#@#"],
                (2, 0),
            ),
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=3,
            overlay_rows=frozenset(),
        )

        def look():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.look = look
        runner.get_model = lambda: model
        runner.auto_mode = True

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["l"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_monster_seen",
        )

    def test_action_catalog_offers_backtrack_after_corridor_dead_end(self) -> None:
        runner = make_runner()
        runner.corridor_backtrack_steps = ["move(south)", "move(south)"]
        runner.last_map_lines = [".....", "..@..", "....."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 1)
        scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "location_context": {
                "area_type": "visible_area",
                "in_corridor": False,
                "in_room": False,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        actions = runner.build_available_actions(scene)
        action_map = {action["action_id"]: action for action in actions}

        self.assertIn("backtrack:corridor", action_map)
        self.assertEqual(
            action_map["backtrack:corridor"]["next_action"],
            "backtrack_corridor(move(south))",
        )
        self.assertEqual(
            action_map["backtrack:corridor"]["path_steps"],
            ["move(south)", "move(south)"],
        )

    def test_corridor_action_flags_runtime_backtracking_choice(self) -> None:
        runner = make_runner()
        runner.corridor_recent_path_positions = [[1, 2]]
        runner.last_map_lines = [
            ".#.",
            ".@#",
            ".#.",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (1, 1)
        scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
                "dark": False,
                "adjacent_corridors": ["north", "south", "east"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        payload = json.loads(runner.build_model_prompt(AUTO_PROMPT, scene))
        action_map = {action["id"]: action for action in payload["available_actions"]}

        self.assertTrue(action_map["explore_corridor:south"]["runtime_backtracking"])
        self.assertEqual(
            action_map["explore_corridor:south"]["priority"],
            "runtime_backtracking",
        )
        self.assertIn(
            "Runtime marks this corridor as backtracking",
            action_map["explore_corridor:south"]["tactical_notes"][0],
        )
        self.assertNotIn(
            "runtime_backtracking",
            action_map["explore_corridor:east"],
        )

    def test_corridor_backtrack_executes_reverse_steps_until_room(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "backtrack:corridor"
        runner.current_procedure = {
            "action_id": "backtrack:corridor",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.corridor_backtrack_steps = ["move(south)", "move(south)"]
        runner.last_map_lines = ["..@", "..."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 0)
        scenes = [
            (
                {
                    "room_description": "Visible area.",
                    "visibility": "normal",
                    "location_context": {
                        "area_type": "visible_area",
                        "in_corridor": False,
                        "in_room": False,
                        "dark": False,
                        "adjacent_corridors": [],
                        "adjacent_doors": [],
                        "in_front_of_door": False,
                    },
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                ["...", "..@"],
                (2, 1),
            ),
            (
                {
                    "room_description": "You are in a room.",
                    "visibility": "normal",
                    "location_context": {
                        "area_type": "room",
                        "in_corridor": False,
                        "in_room": True,
                        "dark": False,
                        "adjacent_corridors": [],
                        "adjacent_doors": [],
                        "in_front_of_door": False,
                    },
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                ["...", "...", "..@"],
                (2, 2),
            ),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: {
            "room_description": "You are in a room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        runner.run_corridor_backtrack_procedure(
            action={
                "action_id": "backtrack:corridor",
                "action_type": "backtrack_corridor",
                "next_action": "backtrack_corridor(move(south))",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["j", "j"])
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_backtrack_completed",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 2)
        self.assertEqual(runner.corridor_backtrack_steps, [])

    def test_corridor_backtrack_exits_farlook_before_each_step(self) -> None:
        runner = make_runner()
        runner.current_action_id = "backtrack:corridor"
        runner.current_procedure = {
            "action_id": "backtrack:corridor",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.corridor_backtrack_steps = ["move(south)", "move(south)"]
        runner.last_map_lines = ["@", ".", "."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=0,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (0, 0)
        runner.terminal = FakeTerminal(
            [
                "normal",
                "Pick a monster, object or location.",
                "normal again",
                "after second",
            ]
        )
        scenes = [
            (
                {
                    "room_description": "Visible area.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                [".", "@", "."],
                (0, 1),
            ),
            (
                {
                    "room_description": "You are in a room.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                    "location_context": {
                        "area_type": "room",
                        "in_corridor": False,
                        "in_room": True,
                    },
                },
                [".", ".", "@"],
                (0, 2),
            ),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: scenes[-1][0] if scenes else {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }

        runner.run_corridor_backtrack_procedure(
            action={
                "action_id": "backtrack:corridor",
                "action_type": "backtrack_corridor",
                "next_action": "backtrack_corridor(move(south))",
            },
            response="continue",
            request_kind="auto_continue_code",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["j", "\x1b", "j"])

    def test_auto_mode_backtracks_corridor_dead_end_without_model(self) -> None:
        runner = make_runner()
        model = FakeModel("should not be called")
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.auto_mode = True
        runner.corridor_backtrack_steps = ["move(south)"]
        runner.last_scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = ["..@", "..."]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=1,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 0)

        def lightweight_scene():
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = ["...", "..@"]
            runner.last_player_screen_pos = (2, 1)
            return {
                "room_description": "You are in a room.",
                "visibility": "normal",
                "location_context": {
                    "area_type": "room",
                    "in_corridor": False,
                    "in_room": True,
                    "dark": False,
                    "adjacent_corridors": [],
                    "adjacent_doors": [],
                    "in_front_of_door": False,
                },
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lightweight_scene
        runner.get_model = lambda: model

        runner.maybe_start_auto_request()

        self.assertEqual(model.prompts, [])
        self.assertEqual(runner.terminal.sent_keys, ["j"])
        self.assertTrue(runner.last_model_skipped)
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_backtrack_completed",
        )
        self.assertEqual(runner.corridor_backtrack_steps, [])
        self.assertEqual(runner.last_trace_input["request_kind"], "auto_continue_code")
        self.assertEqual(
            runner.procedure_events[-1]["text"],
            "The previous corridor was a dead end; runtime backtracked 1 step "
            "and returned to the last useful room or junction.",
        )

    def test_corridor_action_confirms_lightweight_hostile_with_farlook(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:east"
        runner.current_procedure = {
            "action_id": "explore_corridor:east",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = ["@##"]
        runner.last_player_screen_pos = (0, 0)

        def lightweight_scene():
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = ["#@d"]
            runner.last_player_screen_pos = (1, 0)
            return {
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [{"description": "visible monster", "pos": [1, 0]}],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }

        def farlook_scene():
            runner.last_map_lines = ["#@d"]
            runner.last_player_screen_pos = (1, 0)
            return {
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [
                    {"description": "tame little dog called Doggo", "pos": [1, 0]}
                ],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = farlook_scene

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(east)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["l"] * 10)
        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_blocked_by_ally",
        )
        self.assertEqual(runner.last_execution_outcome["steps"], 1)
        self.assertEqual(runner.last_execution_outcome["pet_block_retries"], 8)
        self.assertEqual(runner.corridor_backtrack_steps, [])
        self.assertNotEqual(
            runner.procedure_events[0]["status"],
            "corridor_follow_monster_seen",
        )

    def test_corridor_action_retries_pet_block_without_counting_step(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:east"
        runner.current_procedure = {
            "action_id": "explore_corridor:east",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = ["@#d"]
        runner.last_player_screen_pos = (0, 0)
        blocked_scene = {
            "room_description": "Visible area.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {"description": "tame little dog called Doggo", "pos": [1, 0]}
            ],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
        }
        current_scene = {"value": None}
        lightweight_states = [
            (
                {
                    "room_description": "Visible area.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [
                        {"description": "tame little dog called Doggo", "pos": [1, 0]}
                    ],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                ["#@d"],
                (1, 0),
            ),
            (blocked_scene, ["#@d"], (1, 0)),
            (
                {
                    "room_description": "Visible area.",
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                },
                ["##@"],
                (2, 0),
            ),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = lightweight_states.pop(0)
            current_scene["value"] = scene
            runner.last_lightweight_refresh_was_full = False
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: current_scene["value"] or blocked_scene

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(east)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(runner.terminal.sent_keys, ["l", "l", "l"])
        self.assertEqual(runner.last_execution_outcome["status"], "corridor_follow_end")
        self.assertEqual(runner.last_execution_outcome["steps"], 2)
        self.assertEqual(runner.last_execution_outcome["pet_block_retries"], 1)

    def test_corridor_action_does_not_memorize_dead_end_when_corridor_visible_ahead(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:north"
        runner.current_procedure = {
            "action_id": "explore_corridor:north",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=6,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [
            " # ",
            "   ",
            "   ",
            " # ",
            " @ ",
            "   ",
        ]
        runner.last_player_screen_pos = (1, 4)

        def observed_scene():
            runner.last_lightweight_refresh_was_full = True
            runner.last_map_lines = [
                " # ",
                "   ",
                " @ ",
                " # ",
                " # ",
                " # ",
                "   ",
            ]
            runner.last_player_screen_pos = (1, 2)
            return {
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            }

        runner.refresh_lightweight_visible_scene_cache = observed_scene
        runner.refresh_scene_cache = observed_scene

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:north",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(north)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_lost_topology",
        )
        self.assertEqual(runner.corridor_backtrack_steps, [])
        self.assertEqual(runner.blocked_corridor_entries, [])

    def test_corridor_action_buffers_revealed_room_facts_until_stop(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["corridor"])
        runner.render_screen = lambda print_output=False: "corridor"
        runner.ensure_normal_game_mode_before_action = lambda: None
        runner.current_action_id = "explore_corridor:east"
        runner.current_procedure = {
            "action_id": "explore_corridor:east",
            "status": "active",
        }
        runner.procedure_status = "active"
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_map_lines = [".....", ".@##.", "....."]
        runner.last_player_screen_pos = (1, 1)
        scenes = [
            (
                {
                    "room_description": "Visible area.",
                    "features": [{"description": "staircase down", "pos": [1, -2]}],
                    "items": [{"description": "some gold pieces", "pos": [1, -3]}],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                    "location_context": {
                        "area_type": "corridor",
                        "in_corridor": True,
                        "in_room": False,
                        "dark": False,
                        "adjacent_corridors": ["west", "east"],
                        "adjacent_doors": [],
                        "in_front_of_door": False,
                    },
                },
                [".>...", ".$...", ".#@#."],
                (2, 2),
            ),
            (
                {
                    "room_description": "You are in a room.",
                    "features": [{"description": "staircase down", "pos": [0, -1]}],
                    "items": [{"description": "some gold pieces", "pos": [0, -2]}],
                    "areas": [],
                    "entities": [],
                    "exits": [],
                    "player": {"identity": None, "pos": [0, 0]},
                    "location_context": {
                        "area_type": "room",
                        "in_corridor": False,
                        "in_room": True,
                        "dark": False,
                        "adjacent_corridors": ["west"],
                        "adjacent_doors": [],
                        "in_front_of_door": False,
                    },
                },
                [".>...", ".$...", ".#.@."],
                (3, 2),
            ),
        ]

        def lightweight_scene():
            scene, map_lines, player_pos = scenes.pop(0)
            runner.last_lightweight_refresh_was_full = True
            runner.last_map_lines = map_lines
            runner.last_player_screen_pos = player_pos
            return scene

        runner.refresh_lightweight_visible_scene_cache = lightweight_scene
        runner.refresh_scene_cache = lambda: {
            "room_description": "You are in a room.",
            "features": [{"description": "staircase down", "pos": [0, -1]}],
            "items": [{"description": "some gold pieces", "pos": [0, -2]}],
            "areas": [],
            "entities": [],
            "exits": [],
            "player": {"identity": None, "pos": [0, 0]},
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": ["west"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
        }

        runner.run_corridor_follow_procedure(
            action={
                "action_id": "explore_corridor:east",
                "action_type": "explore_corridor",
                "next_action": "follow_corridor(east)",
            },
            response="continue",
            request_kind="auto",
            scene_before_action={
                "room_description": "Visible area.",
                "features": [],
                "items": [],
                "areas": [],
                "entities": [],
                "exits": [],
                "player": {"identity": None, "pos": [0, 0]},
            },
        )

        self.assertEqual(
            runner.last_execution_outcome["status"],
            "corridor_follow_room_entrance",
        )
        self.assertEqual(runner.procedure_status, "completed")
        self.assertIsNone(runner.current_action_id)
        self.assertIsNone(runner.current_procedure)
        self.assertIsNone(runner.blocked_action_id)
        self.assertEqual(runner.corridor_backtrack_steps, [])
        event_texts = [event["text"] for event in runner.procedure_events]
        self.assertTrue(
            any("Entered a new room after following the corridor" in text for text in event_texts)
        )
        self.assertTrue(
            any("do not immediately backtrack" in text for text in event_texts)
        )
        self.assertTrue(
            any("Newly revealed while following corridor" in text for text in event_texts)
        )
        self.assertTrue(any("some gold pieces" in text for text in event_texts))
        self.assertTrue(any("staircase down" in text for text in event_texts))

    def test_continue_with_chosen_action_starts_action_when_idle(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":{"decision":"continue","chosen_action_id":"explore:exit:north","reason":"leave"}}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertEqual(runner.current_action_id, "explore:exit:north")
        self.assertEqual(runner.last_execution_outcome["status"], "scene_unchanged")
        self.assertIn('"reason":"leave"', runner.last_response)
        self.assertIn("Action: move(north)", runner.last_response)

    def test_switch_decision_keeps_reason_visible_with_executed_action(self) -> None:
        runner = make_runner()
        response = (
            '{"decision":"switch","chosen_action_id":"explore:exit:north",'
            '"reason":"blocked route, switching"}'
        )
        model = FakeModel(response)
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertIn("blocked route, switching", runner.last_response)
        self.assertIn("Action: move(north)", runner.last_response)
        self.assertEqual(runner.response_history, [runner.last_response])

    def test_auto_response_stops_auto_when_untranslated(self) -> None:
        runner = make_runner()
        model = FakeModel("Describe the room.")
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before"])
        runner.auto_mode = True
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.start_model_request(AUTO_PROMPT, request_kind="auto")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, [])
        self.assertFalse(runner.auto_mode)
        self.assertIn("Auto stopped", runner.last_response)

    def test_step_command_applies_one_action_without_enabling_auto(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore:exit:north","reason":"leave"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.terminal = FakeTerminal(["before", "after"])
        runner.look = lambda: scene
        runner.get_model = lambda: model

        runner.handle_tui_command("/step")
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertEqual(runner.terminal.sent_keys, ["k"])
        self.assertFalse(runner.auto_mode)

    def test_auto_decision_writes_last_trace_log(self) -> None:
        runner = make_runner()
        model = FakeModel(
            '{"decision":"switch","chosen_action_id":"explore:exit:north","reason":"leave"}'
        )
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        trace_dir = Path(tempfile.mkdtemp())
        trace_path = trace_dir / "last_execution_trace.md"
        previous_path = runtime.LAST_EXECUTION_TRACE_PATH
        runtime.LAST_EXECUTION_TRACE_PATH = trace_path
        try:
            runner.last_map_lines = [
                "┌─ ─┐",
                "│...│",
                "│.@.│",
                "│...│",
                "└───┘",
            ]
            runner.last_viewport = runtime.MapViewport(
                top=0,
                bottom=4,
                left=0,
                right=4,
                overlay_rows=frozenset(),
            )
            runner.last_player_screen_pos = (2, 2)
            runner.terminal = FakeTerminal(["before", "after"])
            runner.auto_mode = True
            runner.look = lambda: scene
            runner.get_model = lambda: model

            runner.start_model_request(AUTO_PROMPT, request_kind="auto")
            assert isinstance(runner.model_thread, threading.Thread)
            runner.model_thread.join(timeout=1)
            runner.drain_model_events()

            content = trace_path.read_text(encoding="utf-8")
            self.assertIn("- Decision: `switch` `explore:exit:north`", content)
            self.assertIn("- Selected action: `explore:exit:north`", content)
            self.assertIn("- Executed: `move(north)`", content)
            self.assertIn("- Action owner: `model`", content)
            self.assertIn("status `scene_unchanged`", content)
        finally:
            runtime.LAST_EXECUTION_TRACE_PATH = previous_path

    def test_auto_mode_stops_on_model_error(self) -> None:
        runner = make_runner()
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]}
            ],
            "player": {"identity": None, "pos": [0, 0]},
        }
        runner.auto_mode = True
        runner.last_map_lines = [
            "┌─ ─┐",
            "│...│",
            "│.@.│",
            "│...│",
            "└───┘",
        ]
        runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        runner.last_player_screen_pos = (2, 2)
        runner.look = lambda: scene
        runner.get_model = lambda: ErrorModel()
        notifications = []
        runner.notify_generation_finished_for_debug = lambda: notifications.append(
            "finished"
        )

        runner.maybe_start_auto_request()
        assert isinstance(runner.model_thread, threading.Thread)
        runner.model_thread.join(timeout=1)
        runner.drain_model_events()

        self.assertFalse(runner.auto_mode)
        self.assertEqual(runner.last_response, "Model error: boom")
        self.assertEqual(notifications, ["finished"])

    def test_scroll_offsets_do_not_go_below_zero(self) -> None:
        runner = make_runner()

        runner.scroll_payload(4)
        runner.scroll_payload(-10)
        runner.scroll_response(3)
        runner.scroll_response(-10)

        self.assertEqual(runner.payload_scroll, 0)
        self.assertEqual(runner.response_scroll, 0)

    def test_point_in_view_detects_pane_bounds(self) -> None:
        runner = make_runner()
        view = (2, 10, 5, 20)

        self.assertTrue(runner.point_in_view(10, 2, view))
        self.assertTrue(runner.point_in_view(29, 6, view))
        self.assertFalse(runner.point_in_view(30, 6, view))
        self.assertFalse(runner.point_in_view(29, 7, view))

    def test_resize_game_view_resizes_terminal_and_rerenders(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["screen"])

        runner.resize_game_view(12, 80)

        self.assertEqual(runner.terminal.resize_calls, [(12, 80)])
        self.assertEqual(runner.screen, "screen")

    def test_draw_tui_refuses_to_shrink_nethack_below_minimum_size(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["screen"])
        screen = FakeCursesScreen(
            MIN_GAME_ROWS + 3,
            MIN_GAME_COLS + MIN_SIDE_PANE_COLS,
        )

        runner.draw_tui(screen, "")

        self.assertEqual(runner.terminal.resize_calls, [])
        self.assertTrue(screen.refreshed)
        self.assertIn("Terminal too small", screen.writes[0][2])

    def test_draw_tui_keeps_game_terminal_at_least_eighty_columns(self) -> None:
        runner = make_runner()
        runner.terminal = FakeTerminal(["screen"])
        runner.draw_game_cells = lambda *args, **kwargs: None
        screen = FakeCursesScreen(
            MIN_GAME_ROWS + 4,
            MIN_GAME_COLS + MIN_SIDE_PANE_COLS + 1,
        )

        runner.draw_tui(screen, "")

        self.assertEqual(
            runner.terminal.resize_calls,
            [(MIN_GAME_ROWS, MIN_GAME_COLS)],
        )


class RuntimePromptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = make_runner()

    def test_build_model_prompt_is_json_payload(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "human knight called agent", "pos": [0, 0]},
        }
        self.runner.player_identity = "human knight called agent"

        payload = json.loads(
            self.runner.build_model_prompt("Describe the scene.", scene)
        )

        self.assertEqual(
            payload["scene_state"]["identity"],
            "human knight called agent",
        )
        self.assertEqual(payload["scene_state"]["pet"], [])
        self.assertEqual(payload["user_question"], "Describe the scene.")
        self.assertEqual(
            payload["scene_state"]["scene_summary"],
            "You are in a room.",
        )
        self.assertEqual(
            payload["scene_state"]["player"],
            {"identity": "human knight called agent", "pos": [0, 0]},
        )
        self.assertEqual(payload["scene_state"]["raw_scene"]["exits"], [])
        self.assertEqual(payload["scene_state"]["raw_scene"]["items"], [])
        self.assertEqual(payload["scene_state"]["raw_scene"]["entities"], [])
        self.assertEqual(payload["scene_state"]["raw_scene"]["features"], [])

    def test_build_model_prompt_includes_tabletop_style_scene_summary(self) -> None:
        scene = {
            "room_description": "You are in a rectangular 7 by 5 room.",
            "exits": [
                {
                    "description": "exit",
                    "direction": "north",
                    "pos": [1, -1],
                }
            ],
            "features": [],
            "items": [
                {"description": "an orange gem", "pos": [1, 0]},
            ],
            "areas": [],
            "entities": [
                {"description": "tame kitten called Kitty", "pos": [2, 1]},
            ],
            "player": {
                "identity": "samurai called agent_7012c1d2",
                "pos": [0, 0],
            },
        }
        self.runner.player_identity = "samurai called agent_7012c1d2"

        payload = json.loads(self.runner.build_model_prompt("", scene))

        self.assertEqual(
            payload["scene_state"]["scene_summary"],
            "You are in a rectangular 7 by 5 room. Visible facts: "
            "tame kitten called Kitty is 2 steps away via southeast, east; "
            "an orange gem is 1 step away via east; "
            "exit is 1 step away via northeast.",
        )
        self.assertEqual(
            payload["scene_state"]["identity"],
            "samurai called agent_7012c1d2",
        )
        self.assertEqual(
            payload["scene_state"]["pet"],
            [
                {
                    "description": "tame kitten called Kitty",
                    "relationship": "ally",
                    "pos": [2, 1],
                }
            ],
        )
        self.assertEqual(payload["scene_state"]["player"], scene["player"])
        self.assertEqual(
            payload["scene_state"]["raw_scene"]["exits"][0]["direction"],
            "north",
        )
        self.assertEqual(
            payload["scene_state"]["raw_scene"]["items"][0]["description"],
            "an orange gem",
        )
        self.assertEqual(
            payload["scene_state"]["raw_scene"]["entities"][0]["description"],
            "tame kitten called Kitty",
        )
        self.assertEqual(payload["decision_request"]["mode"], "answer_question")

    def test_build_model_prompt_reports_scene_events_and_actions(self) -> None:
        first_scene = {
            "room_description": "You are in a room.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "jackal", "pos": [0, -2]}],
            "player": {"identity": "agent", "pos": [0, 0]},
        }
        second_scene = {
            "room_description": "You are in a room.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "jackal", "pos": [0, -1]}],
            "player": {"identity": "agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            "..d..",
            "..@..",
            ".....",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 2)

        json.loads(self.runner.build_model_prompt(AUTO_PROMPT, first_scene))
        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, second_scene))

        self.assertEqual(payload["decision"]["mode"], "choose_action")
        self.assertIn("jackal moved 1 step south toward the player", payload["events"][0])
        self.assertTrue(payload["non_pet_scene_changed"])
        action_ids = {
            action["id"]
            for action in payload["available_actions"]
        }
        self.assertIn("fight:monster:jackal", action_ids)
        self.assertIn("flee:monster:jackal", action_ids)

    def test_auto_payload_omits_far_visible_periphery_outside_room(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "location_context": {
                "area_type": "visible_area",
                "in_corridor": False,
                "in_room": False,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [
                {
                    "description": "closed door",
                    "pos": [14, 4],
                    "ref": "closed_door_1",
                    "target_key": "door:southeast",
                    "display_name": "door",
                }
            ],
            "items": [
                {
                    "description": "an elven mithril-coat",
                    "pos": [11, 3],
                    "ref": "an_elven_mithril_coat_1",
                    "target_key": "item:an_elven_mithril_coat",
                    "display_name": "an elven mithril-coat",
                }
            ],
            "areas": [],
            "entities": [
                {
                    "description": "tame little dog called Doggo",
                    "pos": [-1, 0],
                    "ref": "tame_little_dog_called_doggo_1",
                    "target_key": "ally:tame_little_dog_called_doggo",
                    "display_name": "tame little dog called Doggo",
                }
            ],
            "player": {"identity": "barbarian called agent", "pos": [0, 0]},
        }
        self.runner.player_identity = "barbarian called agent"

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        visible_ids = {
            entry["id"]
            for entry in payload["scene"].get("visible", [])
            if isinstance(entry.get("id"), str)
        }

        self.assertIn("ally:tame_little_dog_called_doggo", visible_ids)
        self.assertNotIn("item:an_elven_mithril_coat", visible_ids)
        self.assertNotIn("door:southeast", visible_ids)

    def test_build_model_prompt_uses_compact_payload_for_auto(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, -2]},
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "jackal", "pos": [0, -1]}],
            "player": {"identity": "agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            "..d..",
            "..@..",
            ".....",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 2)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertEqual(payload["decision"]["mode"], "choose_action")
        self.assertIn("available_actions", payload)
        self.assertIn("events", payload)
        self.assertIn("hazards", payload)
        self.assertNotIn("system_context", payload)
        self.assertNotIn("scene_state", payload)
        self.assertNotIn("user_question", payload)

    def test_build_model_prompt_dark_scene_includes_high_level_door_action(self) -> None:
        scene = {
            "room_description": "You can't guess the size of this area.",
            "visibility": "dark",
            "exits": [],
            "features": [{"description": "visible door", "pos": [1, 0]}],
            "items": [],
            "areas": [],
            "entities": [{"description": "tame kitten called Kitty", "pos": [0, 1]}],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            "..@+.",
            "..f..",
            ".....",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertEqual(payload["scene"]["visibility"], "dark")
        action_ids = {action["id"] for action in payload["available_actions"]}
        self.assertIn("explore_door:door:east", action_ids)
        action_map = {
            action["id"]: action for action in payload["available_actions"]
        }
        self.assertEqual(
            action_map["explore_door:door:east"]["label"],
            "Explore east door",
        )
        self.assertNotIn("next", action_map["explore_door:door:east"])

    def test_room_exit_door_is_offered_before_visible_area_fallback(self) -> None:
        scene = {
            "room_description": "You are in a rectangular 6 by 5 room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [
                {
                    "description": "closed door",
                    "pos": [2, -3],
                    "ref": "closed_door_1",
                    "target_key": "door:northeast",
                    "display_name": "door",
                }
            ],
            "items": [{"description": "5 gold pieces", "pos": [1, -1]}],
            "areas": [],
            "entities": [
                {"description": "tame little dog called Doggo", "pos": [0, 1]}
            ],
            "player": {"identity": "rogue called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "--------",
            "|......+",
            "|......|",
            "|.....$|",
            "|....@.|",
            "|....d.|",
            "--------",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=6,
            left=0,
            right=7,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (5, 4)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = [action["id"] for action in payload["available_actions"]]
        action_map = {
            action["action_id"]: action for action in self.runner.last_available_actions
        }

        self.assertIn("pick:item:gold", action_ids)
        self.assertIn("explore_door:door:northeast", action_ids)
        self.assertNotIn("explore:frontier", action_ids)
        self.assertIn(
            action_map["explore_door:door:northeast"]["next_action"],
            {"move(north)", "move(northeast)"},
        )
        self.assertEqual(
            action_map["explore_door:door:northeast"]["selection_priority"],
            "nearest_exploration",
        )

    def test_safe_distance_hostile_does_not_hide_room_exit_door(self) -> None:
        scene = {
            "room_description": "You are in a rectangular 9 by 4 room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [
                {
                    "description": "closed door",
                    "pos": [3, 1],
                    "ref": "closed_door_1",
                    "target_key": "door:southeast",
                    "display_name": "door",
                }
            ],
            "items": [
                {
                    "description": "some gold pieces",
                    "pos": [3, -1],
                    "ref": "gold_1",
                    "target_key": "item:gold",
                    "display_name": "some gold pieces",
                }
            ],
            "areas": [],
            "entities": [
                {
                    "description": "tame kitten called Kitty",
                    "pos": [-1, 0],
                    "ref": "kitty_1",
                    "target_key": "ally:kitty",
                    "display_name": "tame kitten called Kitty",
                },
                {
                    "description": "lichen",
                    "pos": [2, -3],
                    "ref": "lichen_1",
                    "target_key": "monster:lichen",
                    "display_name": "lichen",
                },
            ],
            "player": {"identity": "tourist called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "-----------",
            "|..F......|",
            "|.........|",
            "|<..$.....|",
            "f@........|",
            "----+------",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=5,
            left=0,
            right=10,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (1, 4)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = [action["id"] for action in payload["available_actions"]]
        action_map = {
            action["action_id"]: action for action in self.runner.last_available_actions
        }

        self.assertIn("explore_door:door:southeast", action_ids)
        self.assertNotIn("explore:frontier", action_ids)
        self.assertNotIn("explore_visible_area", action_ids)
        self.assertEqual(
            action_map["explore_door:door:southeast"]["next_action"],
            "move(east)",
        )
        self.assertEqual(payload["hazards"], [])

    def test_distance_three_hostile_does_not_reset_corridor_to_frontier(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "visibility": "normal",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "in_room": False,
                "dark": False,
                "adjacent_corridors": ["east", "west"],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "remembered, unseen, creature",
                    "pos": [-3, -2],
                    "ref": "remembered_unseen_creature_1",
                    "target_key": "monster:remembered_unseen_creature",
                    "display_name": "remembered, unseen, creature",
                }
            ],
            "player": {"identity": "samurai called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "#####",
            "##@##",
            "#####",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = [action["id"] for action in payload["available_actions"]]

        self.assertTrue(
            any(action_id.startswith("explore_corridor:") for action_id in action_ids)
        )
        self.assertNotIn("explore:frontier", action_ids)
        self.assertEqual(payload["hazards"], [])

    def test_pick_item_action_notes_when_first_step_also_flees_hostile(self) -> None:
        scene = {
            "room_description": "You are in a rectangular 5 by 5 room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [],
            "items": [
                {
                    "description": "some gold pieces",
                    "pos": [-2, 2],
                    "ref": "gold_1",
                    "target_key": "item:gold",
                    "display_name": "some gold pieces",
                }
            ],
            "areas": [],
            "entities": [
                {
                    "description": "lichen",
                    "pos": [-1, -1],
                    "ref": "lichen_1",
                    "target_key": "monster:lichen",
                    "display_name": "lichen",
                }
            ],
            "player": {"identity": "priest called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "F....",
            ".....",
            "..@..",
            ".....",
            "$....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 2)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_map = {
            action["id"]: action for action in payload["available_actions"]
        }

        pick_action = action_map["pick:item:gold"]
        self.assertTrue(pick_action["compatible_with_flee"])
        self.assertEqual(
            pick_action["tactical_notes"],
            ["first step also increases distance from lichen"],
        )

    def test_pick_item_action_warns_when_first_step_moves_closer_to_hostile(self) -> None:
        scene = {
            "room_description": "You are in a rectangular 5 by 5 room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [],
            "items": [
                {
                    "description": "some gold pieces",
                    "pos": [-1, -1],
                    "ref": "gold_1",
                    "target_key": "item:gold",
                    "display_name": "some gold pieces",
                }
            ],
            "areas": [],
            "entities": [
                {
                    "description": "lichen",
                    "pos": [-3, -3],
                    "ref": "lichen_1",
                    "target_key": "monster:lichen",
                    "display_name": "lichen",
                }
            ],
            "player": {"identity": "priest called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "F......",
            ".......",
            "..$....",
            "...@...",
            ".......",
            ".......",
            ".......",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=6,
            left=0,
            right=6,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (3, 3)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_map = {
            action["id"]: action for action in payload["available_actions"]
        }

        pick_action = action_map["pick:item:gold"]
        self.assertNotIn("compatible_with_flee", pick_action)
        self.assertEqual(
            pick_action["tactical_notes"],
            ["first step moves closer to lichen"],
        )

    def test_bear_trap_suppresses_generic_explore_action(self) -> None:
        scene = {
            "room_description": "You are caught in a bear trap.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "tame little dog called Doggo",
                    "pos": [0, 2],
                    "ref": "doggo_1",
                    "target_key": "ally:doggo",
                    "display_name": "tame little dog called Doggo",
                }
            ],
            "player": {"identity": "samurai called agent", "pos": [0, 0]},
        }
        self.runner.executed_actions = ["move(north)"]
        self.runner.last_map_lines = [
            ".....",
            "..@..",
            ".....",
            "..d..",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertIn("escape:bear_trap", action_ids)
        self.assertNotIn("explore_visible_area", action_ids)
        self.assertEqual(
            self.runner.last_available_actions_by_id["escape:bear_trap"]["next_action"],
            "move(north)",
        )

    def test_flee_route_prefers_safer_diagonal_first_step(self) -> None:
        scene = {
            "room_description": "You are in a square room.",
            "visibility": "normal",
            "location_context": {
                "area_type": "room",
                "in_corridor": False,
                "in_room": True,
                "dark": False,
                "adjacent_corridors": [],
                "adjacent_doors": [],
                "in_front_of_door": False,
            },
            "exits": [
                {
                    "description": "exit",
                    "direction": "south",
                    "pos": [0, 4],
                    "ref": "south_exit_1",
                    "target_key": "exit:south",
                    "display_name": "south exit",
                }
            ],
            "features": [
                {
                    "description": "closed door",
                    "pos": [2, 2],
                    "ref": "door_1",
                    "target_key": "door:southeast",
                    "display_name": "door",
                }
            ],
            "items": [],
            "areas": [],
            "entities": [
                {
                    "description": "jackal",
                    "pos": [-1, 1],
                    "ref": "jackal_1",
                    "target_key": "monster:jackal",
                    "display_name": "jackal",
                },
                {
                    "description": "tame kitten called Kitty",
                    "pos": [-1, 2],
                    "ref": "kitty_1",
                    "target_key": "ally:kitty",
                    "display_name": "tame kitten called Kitty",
                },
            ],
            "player": {"identity": "archeologist called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "------",
            "|..@.|",
            "|.d..|",
            "|.f.++",
            "|..<.|",
            "---.--",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=5,
            left=0,
            right=5,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (3, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertIn("flee:exit:south", action_ids)
        self.assertEqual(
            self.runner.last_available_actions_by_id["flee:exit:south"][
                "next_action"
            ],
            "move(southeast)",
        )
        self.assertEqual(
            self.runner.last_available_actions_by_id["flee:door:southeast"][
                "next_action"
            ],
            "move(southeast)",
        )

    def test_door_action_is_not_offered_for_diagonal_door_entry(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [{"description": "open door", "pos": [-1, -1]}],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "valkyrie called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "-..",
            ".@.",
            "...",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (1, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertNotIn("explore_door:door:northwest", action_ids)

    def test_build_model_prompt_does_not_mark_tame_entity_as_threat(self) -> None:
        descriptions = (
            "tame little dog",
            "tame dog called Doggo",
            "tame kitten",
            "tame cat called Kitty",
            "tame horse",
            "tame saddled pony called Horse",
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.runner = make_runner()
                scene = {
                    "room_description": "You are in a room.",
                    "exits": [],
                    "features": [],
                    "items": [],
                    "areas": [],
                    "entities": [
                        {"description": description, "pos": [1, 1]}
                    ],
                    "player": {"identity": "knight called agent", "pos": [0, 0]},
                }

                payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

                self.assertEqual(payload["events"], [])
                self.assertEqual(payload["hazards"], [])
                self.assertEqual(payload["available_actions"], [])

    def test_build_model_prompt_keeps_stable_refs_across_identical_turns(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "exits": [
                {"description": "exit", "direction": "east", "pos": [2, 0]},
                {"description": "exit", "direction": "east", "pos": [1, 1]},
            ],
            "features": [
                {"description": "closed door", "pos": [1, -1]},
            ],
            "items": [],
            "areas": [],
            "entities": [
                {"description": "jackal", "pos": [-6, 1]},
                {"description": "tame saddled pony called Horse", "pos": [1, 1]},
            ],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }

        json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        first_trace = self.runner.last_trace_payload
        second_payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        second_trace = self.runner.last_trace_payload

        self.assertEqual(
            first_trace["scene_state"]["raw_scene"]["exits"],
            second_trace["scene_state"]["raw_scene"]["exits"],
        )
        self.assertEqual(
            first_trace["scene_state"]["raw_scene"]["entities"],
            second_trace["scene_state"]["raw_scene"]["entities"],
        )
        self.assertEqual(
            second_payload["events"],
            [],
        )

    def test_current_procedure_migrates_legacy_exit_ref_to_semantic_action(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "exits": [
                {"description": "exit", "direction": "east", "pos": [2, 0]},
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            "..@..",
            ".... ",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        self.runner.current_action_id = "explore:east_exit_1"

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertEqual(
            payload["current_procedure"]["action_id"],
            "explore:exit:east",
        )
        self.assertEqual(payload["current_procedure"]["status"], "active")
        self.assertEqual(self.runner.current_action_id, "explore:exit:east")

    def test_current_exit_procedure_continues_when_player_is_on_exit_tile(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "exits": [
                {"description": "exit", "direction": "north", "pos": [0, 0]},
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }
        self.runner.current_action_id = "explore:exit:north"

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertEqual(
            payload["current_procedure"]["action_id"],
            "explore:exit:north",
        )
        self.assertEqual(payload["current_procedure"]["status"], "active")
        action_map = {
            action["id"]: action for action in payload["available_actions"]
        }
        self.assertEqual(action_map["explore:exit:north"]["steps"], 1)

    def test_semantic_actions_hide_volatile_entity_refs_and_collapse_exits(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "exits": [
                {"description": "exit", "direction": "east", "pos": [1, -1]},
                {"description": "exit", "direction": "east", "pos": [1, 1]},
            ],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "lichen", "pos": [-1, 0]}],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            ".F@. ",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_map = {
            action["id"]: action for action in payload["available_actions"]
        }

        self.assertIn("flee:exit:east", action_map)
        self.assertEqual(action_map["flee:exit:east"]["steps"], 1)
        self.assertEqual(
            [
                action["id"]
                for action in payload["available_actions"]
                if action["id"].startswith("flee:exit:")
            ],
            ["flee:exit:east"],
        )
        self.assertEqual(action_map["fight:monster:lichen"]["label"], "Attack lichen")
        self.assertNotIn("lichen_", json.dumps(payload["available_actions"]))

    def test_build_model_prompt_includes_dynamic_frontier_exploration_action(self) -> None:
        scene = {
            "room_description": "You are exploring the level.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            "..@. ",
            ".....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_map = {
            action["id"]: action for action in payload["available_actions"]
        }

        self.assertEqual(
            payload["goals"][0],
            "Collect useful items to improve survival and progress.",
        )
        self.assertIn("explore:frontier", action_map)
        self.assertEqual(action_map["explore:frontier"]["label"], "Explore unknown area")
        self.assertEqual(action_map["explore:frontier"]["steps"], 1)

    def test_up_staircase_is_hidden_and_exploration_remains_available(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [{"description": "branch staircase up", "pos": [-2, -1]}],
            "items": [{"description": "a chest", "pos": [-3, 0]}],
            "areas": [],
            "entities": [],
            "player": {"identity": "healer called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "-----",
            "|.<@|",
            "|(..|",
            "-----",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (3, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        payload_text = json.dumps(payload)
        trace_text = json.dumps(self.runner.last_trace_payload)
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertNotIn("staircase:up", payload_text)
        self.assertNotIn("branch staircase up", trace_text)
        self.assertNotIn("go_to_staircase:staircase:up", action_ids)
        self.assertIn("explore_visible_area", action_ids)

    def test_no_monsters_make_exits_exploration_actions(self) -> None:
        scene = {
            "room_description": "You are in a room.",
            "exits": [{"description": "exit", "direction": "east", "pos": [1, 0]}],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = ["..@. "]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 0)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertIn("explore:exit:east", action_ids)
        self.assertNotIn("flee:exit:east", action_ids)

    def test_fallback_exploration_continues_recent_direction_over_backtracking(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "barbarian called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "-----",
            "|...|",
            "|.@.|",
            "|...|",
            "-----",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=4,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 2)
        self.runner.executed_actions = ["move(north)"]

        json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertEqual(
            self.runner.last_available_actions_by_id["explore_visible_area"][
                "next_action"
            ],
            "move(north)",
        )

    def test_fallback_exploration_penalizes_recent_reverse_steps(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [
                {"description": "tame little dog called Doggo", "pos": [0, 1]}
            ],
            "player": {"identity": "rogue called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "-----",
            "|.@.|",
            "|...|",
            "-----",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        self.runner.executed_actions = [
            "move(east)",
            "move(northeast)",
            "move(southeast)",
            "move(southeast)",
            "move(north)",
        ]

        json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertNotEqual(
            self.runner.last_available_actions_by_id["explore_visible_area"][
                "next_action"
            ],
            "move(west)",
        )

    def test_corridor_run_action_is_prioritized_when_corridor_is_available(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [{"description": "exit", "direction": "west", "pos": [-2, 0]}],
            "features": [{"description": "closed door", "pos": [4, 0]}],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = ["##@...+"]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=6,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 0)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertEqual(
            payload["available_actions"][0]["id"],
            "explore_corridor:west",
        )
        self.assertEqual(
            payload["available_actions"][0]["priority"],
            "nearest_exploration",
        )
        self.assertEqual(
            self.runner.last_available_actions_by_id["explore_corridor:west"][
                "next_action"
            ],
            "follow_corridor(west)",
        )

    def test_corridor_context_only_exposes_managed_corridor_exploration(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "adjacent_corridors": ["west", "east"],
            },
            "exits": [],
            "features": [{"description": "closed door", "pos": [4, 0]}],
            "items": [{"description": "a gem", "pos": [3, 0]}],
            "areas": [],
            "entities": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = ["##@##+"]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=5,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 0)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertEqual(
            action_ids,
            {"explore_corridor:west", "explore_corridor:east"},
        )

    def test_corridor_catalog_does_not_offer_step_toward_visible_hostile(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "adjacent_corridors": ["east", "southwest"],
            },
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [{"description": "kobold zombie", "pos": [3, 0]}],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "......",
            "..@##Z",
            ".#....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=5,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = [action["id"] for action in payload["available_actions"]]

        self.assertIn("explore_corridor:southwest", action_ids)
        self.assertNotIn("explore_corridor:east", action_ids)

    def test_corridor_catalog_collapses_same_bend_entries(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "adjacent_corridors": ["east", "northeast"],
            },
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "..#",
            ".@#",
            "...",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (1, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertIn("explore_corridor:east", action_ids)
        self.assertNotIn("explore_corridor:northeast", action_ids)

    def test_corridor_catalog_keeps_distinct_branches(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "adjacent_corridors": ["north", "east"],
            },
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".#.",
            ".@#",
            "...",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=2,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (1, 1)

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertIn("explore_corridor:north", action_ids)
        self.assertIn("explore_corridor:east", action_ids)

    def test_corridor_action_skips_immediate_backtracking_direction(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = ["##@##"]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=0,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 0)
        self.runner.executed_actions = ["move(east)"]

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertNotIn("explore_corridor:west", action_ids)
        self.assertIn("explore_corridor:east", action_ids)

    def test_corridor_intersection_memory_suppresses_reverse_branch(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "location_context": {
                "area_type": "corridor",
                "in_corridor": True,
                "adjacent_corridors": ["west", "east", "south"],
            },
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "wizard called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "..#..",
            "##@##",
            "..#..",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=2,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        self.runner.corridor_intersection_avoid_steps = [
            {"origin": [2, 1], "delta": [-1, 0]}
        ]

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = {action["id"] for action in payload["available_actions"]}

        self.assertNotIn("explore_corridor:west", action_ids)
        self.assertIn("explore_corridor:east", action_ids)
        self.assertIn("explore_corridor:north", action_ids)
        self.assertIn("explore_corridor:south", action_ids)

    def test_corridor_action_continuing_recent_direction_sorts_before_turns(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "rogue called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            "...#.",
            "...#.",
            "..@..",
            "..#..",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 2)
        self.runner.executed_actions = ["move(south)"]

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))
        action_ids = [action["id"] for action in payload["available_actions"]]

        self.assertLess(
            action_ids.index("explore_corridor:south"),
            action_ids.index("explore_corridor:northeast"),
        )

    def test_blocked_exit_recovery_promotes_matching_frontier(self) -> None:
        scene = {
            "room_description": "Visible area.",
            "exits": [],
            "features": [{"description": "visible door", "pos": [-2, 1]}],
            "items": [],
            "areas": [],
            "entities": [],
            "player": {"identity": "knight called agent", "pos": [0, 0]},
        }
        self.runner.last_map_lines = [
            ".....",
            "..@. ",
            ".....",
            "+....",
        ]
        self.runner.last_viewport = runtime.MapViewport(
            top=0,
            bottom=3,
            left=0,
            right=4,
            overlay_rows=frozenset(),
        )
        self.runner.last_player_screen_pos = (2, 1)
        self.runner.current_action_id = "explore:exit:east"
        self.runner.procedure_status = "blocked"

        payload = json.loads(self.runner.build_model_prompt(AUTO_PROMPT, scene))

        self.assertIsNone(payload["decision"]["current_action_id"])
        self.assertEqual(
            payload["current_procedure"],
            {
                "action_id": "explore:exit:east",
                "label": None,
                "status": "blocked",
                "goal": None,
                "next_action": None,
            },
        )
        self.assertEqual(payload["available_actions"][0]["id"], "explore:frontier")
        self.assertEqual(
            payload["available_actions"][0]["label"],
            "Recover blocked east route",
        )
        self.assertEqual(
            payload["available_actions"][0]["recovers_blocked_action"],
            "explore:exit:east",
        )
        self.assertIsNone(self.runner.current_action_id)


class ActionTests(unittest.TestCase):
    def test_action_to_keys_known_and_unknown_actions(self) -> None:
        self.assertEqual(action_to_keys("move(north)"), "k")
        self.assertEqual(action_to_keys("run(west)"), "gh")
        self.assertEqual(action_to_keys("Action: run(east)."), "gl")
        self.assertEqual(action_to_keys(" pickup() "), ",")
        self.assertEqual(action_to_keys("Action: move(east)."), "l")
        self.assertEqual(action_to_keys("```move(south)```"), "j")
        self.assertIsNone(action_to_keys("skip()"))
        self.assertIsNone(action_to_keys("describe the scene"))
        self.assertIsNone(action_to_keys(None))


if __name__ == "__main__":
    unittest.main()
