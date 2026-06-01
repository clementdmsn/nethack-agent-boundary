from __future__ import annotations

import time

from constants.runtime import AUTO_ACTION_DELAY, DOOR_OPEN_MAX_ATTEMPTS
from model.action_contract import ActionContract
from navigation.corridor_topology import corridor_follow_decision


CORRIDOR_FOLLOW_MAX_STEPS = 30
CORRIDOR_PET_BLOCK_RETRY_LIMIT = 8


class CorridorProcedureMixin:
    def should_run_corridor_follow_procedure(self, action: dict[str, object]) -> bool:
        """Return whether an action should be handled by the corridor loop."""
        return ActionContract(action).needs_corridor_follow_procedure()

    def should_run_corridor_backtrack_procedure(self, action: dict[str, object]) -> bool:
        """Return whether an action should be handled by the backtrack loop."""
        return ActionContract(action).needs_corridor_backtrack_procedure()

    def corridor_stop_event(self, status: str, steps: int) -> dict[str, object]:
        """Build the model-facing event emitted after corridor following stops."""
        if status == "corridor_follow_room_entrance":
            suffix = "" if steps == 1 else "s"
            return {
                "type": "procedure",
                "procedure": "corridor_follow",
                "status": status,
                "text": (
                    f"Entered a new room after following the corridor for {steps} "
                    f"step{suffix}. Analyze the room and choose the next "
                    "exploration or tactical action; do not immediately "
                    "backtrack unless the room is unsafe or exhausted."
                ),
            }
        reason = {
            "corridor_follow_intersection": "intersection reached",
            "corridor_follow_monster_seen": "hostile appeared",
            "corridor_follow_end": "no forward corridor continuation",
            "corridor_follow_room_entrance": "room entrance reached",
            "corridor_follow_closed_door": "closed door reached",
            "corridor_follow_blocked_by_ally": "allied pet blocked the route",
            "corridor_follow_lost_topology": "visible corridor topology became uncertain",
            "corridor_follow_blocked": "movement was blocked",
            "corridor_follow_step_limit": "safety step limit reached",
            "corridor_follow_invalid_direction": "starting direction was invalid",
            "corridor_follow_untranslatable_direction": "starting direction could not be translated",
        }.get(status, "corridor following stopped")
        suffix = "" if steps == 1 else "s"
        return {
            "type": "procedure",
            "procedure": "corridor_follow",
            "status": status,
            "text": f"Corridor following stopped after {steps} step{suffix}: {reason}.",
        }

    def discovery_entry_key(
        self,
        bucket: str,
        entry: dict[str, object],
    ) -> tuple[str, str, tuple[int, int] | None]:
        """Return a stable-enough key for discoveries seen during corridor follow."""
        target_key = entry.get("target_key")
        description = entry.get("description")
        if isinstance(target_key, str):
            name = target_key
        elif isinstance(description, str):
            name = description
        else:
            name = bucket
        pos = self.entry_position(entry)
        return (bucket, name, (pos[0], pos[1]) if pos is not None else None)

    def corridor_discovery_text(
        self,
        bucket: str,
        entry: dict[str, object],
    ) -> str | None:
        """Format one non-tactical fact revealed while corridor automation runs."""
        description = entry.get("description")
        if not isinstance(description, str):
            return None
        pos = self.entry_position(entry)
        if pos is None:
            return description
        steps = self.direction_sequence_for_pos(pos)
        direction = "-".join(steps) if steps else "here"
        return f"{description} {direction}"

    def collect_corridor_discoveries(
        self,
        scene: dict[str, object] | None,
    ) -> None:
        """Accumulate newly visible room facts without interrupting corridor follow."""
        if not isinstance(scene, dict):
            return
        existing = {
            (
                item.get("bucket"),
                item.get("name"),
                tuple(item["pos"]) if isinstance(item.get("pos"), list) else None,
            )
            for item in self.corridor_pending_discoveries
            if isinstance(item, dict)
        }
        for bucket in ("items", "features"):
            entries = scene.get(bucket)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                text = self.corridor_discovery_text(bucket, entry)
                if text is None:
                    continue
                key = self.discovery_entry_key(bucket, entry)
                if key in existing:
                    continue
                pos = key[2]
                self.corridor_pending_discoveries.append(
                    {
                        "bucket": bucket,
                        "name": key[1],
                        "pos": [pos[0], pos[1]] if pos is not None else None,
                        "text": text,
                    }
                )
                existing.add(key)
        self.corridor_pending_discoveries = self.corridor_pending_discoveries[-12:]

    def flush_corridor_discoveries_event(self) -> None:
        """Emit pending corridor discoveries at the next real decision point."""
        discoveries = [
            item.get("text")
            for item in self.corridor_pending_discoveries
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        ]
        self.corridor_pending_discoveries = []
        if not discoveries:
            return
        self.procedure_events.append(
            {
                "type": "procedure",
                "procedure": "corridor_follow",
                "status": "corridor_follow_discoveries",
                "text": "Newly revealed while following corridor: "
                + "; ".join(discoveries)
                + ".",
            }
        )

    def direction_delta_from_action(self, action: str) -> tuple[int, int] | None:
        """Extract a movement delta from move/follow_corridor action text."""
        if action.startswith("follow_corridor(") and action.endswith(")"):
            direction = action.removeprefix("follow_corridor(").removesuffix(")")
        elif action.startswith("move(") and action.endswith(")"):
            direction = action.removeprefix("move(").removesuffix(")")
        else:
            return None
        for delta, name in getattr(self, "DIRECTION_NAMES", {}).items():
            if name == direction:
                return delta
        return None

    def move_action_from_delta(self, delta: tuple[int, int]) -> str | None:
        """Convert one movement delta into a low-level move action."""
        return getattr(self, "DELTA_TO_ACTION", {}).get(delta)

    def reverse_move_action(self, action: str) -> str | None:
        """Return the opposite move(...) action for one executed movement."""
        delta = self.direction_delta_from_action(action)
        if delta is None:
            return None
        return self.move_action_from_delta((-delta[0], -delta[1]))

    def backtrack_steps_from_moves(self, moves: list[str]) -> list[str]:
        """Build a reversed movement list that can leave a dead-end corridor."""
        steps = []
        for action in reversed(moves):
            reverse = self.reverse_move_action(action)
            if reverse is not None:
                steps.append(reverse)
        return steps

    def remember_dead_end_corridor_entry(
        self,
        origin: tuple[int, int] | None,
        delta: tuple[int, int] | None,
    ) -> None:
        """Remember the local corridor entry that just proved to be a dead end."""
        if origin is None or delta is None:
            return
        entry = {
            "origin": [origin[0], origin[1]],
            "delta": [delta[0], delta[1]],
        }
        entries = [
            existing
            for existing in self.blocked_corridor_entries
            if existing != entry
        ]
        entries.append(entry)
        self.blocked_corridor_entries = entries[-8:]

    def remember_corridor_intersection_reverse(
        self,
        origin: tuple[int, int] | None,
        entry_delta: tuple[int, int] | None,
    ) -> None:
        """Remember the reverse step that should not be offered at an intersection."""
        if origin is None or entry_delta is None:
            return
        reverse = (-entry_delta[0], -entry_delta[1])
        entry = {
            "origin": [origin[0], origin[1]],
            "delta": [reverse[0], reverse[1]],
        }
        entries = [
            existing
            for existing in self.corridor_intersection_avoid_steps
            if existing != entry
        ]
        entries.append(entry)
        self.corridor_intersection_avoid_steps = entries[-8:]

    def remember_corridor_intersection_path(
        self,
        path_positions: list[tuple[int, int]],
    ) -> None:
        """Remember the just-followed corridor path for handoff annotations."""
        unique_positions: list[list[int]] = []
        seen: set[tuple[int, int]] = set()
        for pos in path_positions[-24:]:
            if pos in seen:
                continue
            seen.add(pos)
            unique_positions.append([pos[0], pos[1]])
        self.corridor_recent_path_positions = unique_positions

    def should_stop_corridor_backtrack(
        self,
        scene: dict[str, object],
        remaining_steps: list[str],
    ) -> bool:
        """Return whether backtracking has reached a useful decision point."""
        if self.has_hostile_pressure(scene):
            return True
        context = scene.get("location_context")
        if isinstance(context, dict) and context.get("in_room") is True:
            return True
        if not remaining_steps:
            return True
        return False

    def scene_confirms_room_entrance(
        self,
        scene: dict[str, object] | None,
        previous_delta: tuple[int, int] | None = None,
        stepped_on_glyph: str | None = None,
    ) -> bool:
        """Return whether observation confirms the player is now in a room."""
        if not isinstance(scene, dict):
            return False
        if stepped_on_glyph not in {".", "·"}:
            return False
        context = scene.get("location_context")
        if not isinstance(context, dict) or context.get("in_room") is not True:
            return False
        if context.get("in_corridor") is True:
            return False
        if previous_delta is None:
            return True
        if self.glyph_for_relative_pos(previous_delta) == "#":
            return False
        direction = getattr(self, "DIRECTION_NAMES", {}).get(previous_delta)
        adjacent_corridors = context.get("adjacent_corridors")
        if (
            isinstance(direction, str)
            and isinstance(adjacent_corridors, list)
            and direction in adjacent_corridors
        ):
            return False
        return True

    def step_is_blocked_by_ally(
        self,
        scene: dict[str, object] | None,
        delta: tuple[int, int] | None,
    ) -> bool:
        """Return whether the intended movement tile contains an allied pet."""
        if delta is None or not isinstance(scene, dict):
            return False
        return delta in self.ally_positions(scene)

    def open_corridor_door(
        self,
        *,
        delta: tuple[int, int],
        sent_actions: list[str],
        sent_keys: list[str],
    ) -> tuple[str | None, dict[str, object] | None]:
        """Open an adjacent door encountered while following a corridor."""
        direction = getattr(self, "DIRECTION_NAMES", {}).get(delta)
        move_action = self.move_action_from_delta(delta)
        keys = self.translate_action(move_action)
        if not isinstance(direction, str) or keys is None:
            return "corridor_follow_untranslatable_direction", None

        scene_after_action = None
        for _attempt in range(DOOR_OPEN_MAX_ATTEMPTS):
            low_level_action = f"open_door({direction})"
            sent_actions.append(low_level_action)
            sent_keys.append("o" + keys)
            self.ensure_normal_game_mode_before_action()
            self.terminal.send_keys("o" + keys)
            self.record_executed_action(low_level_action)
            self.last_executed_low_level_action = low_level_action
            screen = self.render_screen(print_output=False)
            message_status = self.door_message_status(screen)
            scene_after_action = self.refresh_scene_cache()
            self.collect_corridor_discoveries(scene_after_action)
            if self.hostile_positions(scene_after_action):
                return "corridor_follow_monster_seen", scene_after_action
            if message_status == "opened" or message_status is None:
                return None, scene_after_action
            if message_status in {"locked", "failed"}:
                return "corridor_follow_blocked", scene_after_action
        return "corridor_follow_blocked", scene_after_action

    def run_corridor_follow_procedure(
        self,
        *,
        action: dict[str, object],
        response: str,
        request_kind: str,
        scene_before_action: dict[str, object] | None,
    ) -> None:
        """Follow a corridor one step at a time until a stop condition appears."""
        self.ensure_normal_game_mode_before_action()
        low_level_action = action.get("next_action")
        if not isinstance(low_level_action, str):
            return

        previous_delta = self.direction_delta_from_action(low_level_action)
        entry_origin = self.current_screen_position_key()
        entry_delta = previous_delta
        first_move = self.move_action_from_delta(previous_delta) if previous_delta else None
        if previous_delta is None or first_move is None:
            self.auto_mode = False
            status = "corridor_follow_invalid_direction"
            self.last_executed_low_level_action = low_level_action
            self.last_execution_outcome = {
                "status": status,
                "scene_changed": False,
                "steps": 0,
            }
            self.procedure_events.append(self.corridor_stop_event(status, 0))
            self.last_response = (
                f"{response}\n\nAuto stopped: corridor direction was invalid."
            )
            self.append_response_history(self.last_response)
            self.write_trace_result(request_kind=request_kind, scene_after_action=None)
            return

        scene_after_action = scene_before_action
        steps = 0
        status = "corridor_follow_step_limit"
        next_move = first_move
        next_move_enters_room = False
        followed_moves: list[str] = []
        used_full_refresh = False
        pet_block_retries = 0
        total_pet_block_retries = 0
        sent_actions: list[str] = []
        sent_keys: list[str] = []
        path_positions: list[tuple[int, int]] = []
        start_position = self.current_screen_position_key()
        if start_position is not None:
            path_positions.append(start_position)
        self.corridor_pending_discoveries = []

        for _attempt in range(CORRIDOR_FOLLOW_MAX_STEPS):
            attempted_delta = self.direction_delta_from_action(next_move)
            destination_glyph = (
                self.glyph_for_relative_pos(attempted_delta)
                if attempted_delta is not None
                else None
            )
            keys = self.translate_action(next_move)
            if keys is None:
                status = "corridor_follow_untranslatable_direction"
                break

            self.ensure_normal_game_mode_before_action()
            before = self.screen_signature()
            sent_actions.append(next_move)
            sent_keys.append(keys)
            self.terminal.send_keys(keys)
            self.last_executed_low_level_action = next_move
            scene_after_action = self.refresh_lightweight_visible_scene_cache()
            self.collect_corridor_discoveries(scene_after_action)
            used_full_refresh = (
                used_full_refresh or self.last_lightweight_refresh_was_full
            )
            after = self.screen_signature()
            if before == after:
                if not self.last_lightweight_refresh_was_full:
                    scene_after_action = self.refresh_scene_cache()
                    used_full_refresh = True
                    self.collect_corridor_discoveries(scene_after_action)
                if (
                    self.step_is_blocked_by_ally(scene_after_action, attempted_delta)
                    and pet_block_retries < CORRIDOR_PET_BLOCK_RETRY_LIMIT
                ):
                    pet_block_retries += 1
                    total_pet_block_retries += 1
                    continue
                if self.step_is_blocked_by_ally(scene_after_action, attempted_delta):
                    status = "corridor_follow_blocked_by_ally"
                    break
                status = "corridor_follow_blocked"
                break

            self.record_executed_action(next_move)
            self.last_player_underlying_glyph = destination_glyph
            current_position = self.current_screen_position_key()
            if current_position is not None:
                path_positions.append(current_position)
            followed_moves.append(next_move)
            steps += 1
            pet_block_retries = 0
            if next_move_enters_room:
                if not self.last_lightweight_refresh_was_full:
                    scene_after_action = self.refresh_scene_cache()
                    used_full_refresh = True
                    self.collect_corridor_discoveries(scene_after_action)
                if self.scene_confirms_room_entrance(
                    scene_after_action,
                    previous_delta,
                    stepped_on_glyph=self.last_player_underlying_glyph,
                ):
                    status = "corridor_follow_room_entrance"
                    break
                next_move_enters_room = False
            decision = corridor_follow_decision(
                runner=self,
                scene=scene_after_action,
                previous_delta=previous_delta,
            )
            if (
                decision.status
                in {
                    "corridor_follow_monster_seen",
                    "corridor_follow_end",
                    "corridor_follow_room_entrance",
                    "corridor_follow_blocked_by_ally",
                    "corridor_follow_lost_topology",
                    "corridor_follow_closed_door",
                }
                and not self.last_lightweight_refresh_was_full
            ):
                scene_after_action = self.refresh_scene_cache()
                used_full_refresh = True
                self.collect_corridor_discoveries(scene_after_action)
                decision = corridor_follow_decision(
                    runner=self,
                    scene=scene_after_action,
                    previous_delta=previous_delta,
                )
            if decision.status == "corridor_follow_closed_door":
                if decision.delta is None:
                    status = "corridor_follow_blocked"
                    break
                door_status, opened_scene = self.open_corridor_door(
                    delta=decision.delta,
                    sent_actions=sent_actions,
                    sent_keys=sent_keys,
                )
                used_full_refresh = True
                if opened_scene is not None:
                    scene_after_action = opened_scene
                if door_status is not None:
                    status = door_status
                    break
                previous_delta = decision.delta
                next_move = self.move_action_from_delta(decision.delta)
                next_move_enters_room = False
                if next_move is None:
                    status = "corridor_follow_untranslatable_direction"
                    break
                continue
            if decision.status != "continue":
                status = decision.status
                break
            if decision.delta is None:
                status = "corridor_follow_blocked"
                break

            previous_delta = decision.delta
            next_move = self.move_action_from_delta(decision.delta)
            next_move_enters_room = decision.reason in {
                "room floor entrance",
                "raw adjacent room entrance",
            }
            if next_move is None:
                status = "corridor_follow_untranslatable_direction"
                break

        if not used_full_refresh:
            scene_after_action = self.refresh_scene_cache()
            self.collect_corridor_discoveries(scene_after_action)
        self.append_runtime_input_summary_log(
            owner="runtime",
            procedure="corridor_follow",
            actions=sent_actions,
            keys=sent_keys,
        )
        scene_changed = self.canonical_scene(scene_after_action) != self.canonical_scene(
            scene_before_action
        )
        self.last_execution_outcome = {
            "status": status,
            "scene_changed": scene_changed,
            "steps": steps,
        }
        if total_pet_block_retries:
            self.last_execution_outcome["pet_block_retries"] = total_pet_block_retries
        if status == "corridor_follow_end":
            self.corridor_backtrack_steps = self.backtrack_steps_from_moves(
                followed_moves
            )
            self.remember_dead_end_corridor_entry(entry_origin, entry_delta)
        else:
            self.corridor_backtrack_steps = []
        if status == "corridor_follow_intersection":
            self.remember_corridor_intersection_reverse(
                self.current_screen_position_key(),
                previous_delta,
            )
            self.remember_corridor_intersection_path(path_positions)
        else:
            self.corridor_recent_path_positions = []
        if status == "corridor_follow_room_entrance":
            self.complete_current_procedure()
        else:
            blocked_action_id = self.current_action_id
            self.blocked_action_id = blocked_action_id
            self.current_action_id = None
            self.current_target_ref = None
            self.current_procedure = {
                "action_id": blocked_action_id,
                "status": "blocked",
                "next_action": None,
                "low_level_goal": None,
            }
            self.procedure_status = "blocked"
        self.procedure_events.append(self.corridor_stop_event(status, steps))
        self.flush_corridor_discoveries_event()
        self.last_response = f"follow_corridor: {status} after {steps} step"
        if steps != 1:
            self.last_response += "s"
        self.append_response_history(self.last_response)
        self.next_auto_request_at = time.monotonic() + AUTO_ACTION_DELAY
        self.write_trace_result(
            request_kind=request_kind,
            scene_after_action=scene_after_action,
        )

    def run_corridor_backtrack_procedure(
        self,
        *,
        action: dict[str, object],
        response: str,
        request_kind: str,
        scene_before_action: dict[str, object] | None,
    ) -> None:
        """Walk back along the stored corridor route until a useful point appears."""
        self.ensure_normal_game_mode_before_action()
        steps = 0
        status = "corridor_backtrack_completed"
        scene_after_action = scene_before_action
        sent_actions: list[str] = []
        sent_keys: list[str] = []

        while self.corridor_backtrack_steps:
            low_level_action = self.corridor_backtrack_steps.pop(0)
            keys = self.translate_action(low_level_action)
            if keys is None:
                status = "corridor_backtrack_untranslatable_direction"
                break

            self.ensure_normal_game_mode_before_action()
            before = self.screen_signature()
            sent_actions.append(low_level_action)
            sent_keys.append(keys)
            self.terminal.send_keys(keys)
            self.record_executed_action(low_level_action)
            self.last_executed_low_level_action = low_level_action
            steps += 1
            scene_after_action = self.refresh_lightweight_visible_scene_cache()
            after = self.screen_signature()
            if before == after:
                status = "corridor_backtrack_blocked"
                break
            if self.should_stop_corridor_backtrack(
                scene_after_action,
                self.corridor_backtrack_steps,
            ):
                break

        if scene_after_action is None or not self.last_lightweight_refresh_was_full:
            scene_after_action = self.refresh_scene_cache()
        self.append_runtime_input_summary_log(
            owner="runtime",
            procedure="corridor_backtrack",
            actions=sent_actions,
            keys=sent_keys,
        )

        scene_changed = self.canonical_scene(scene_after_action) != self.canonical_scene(
            scene_before_action
        )
        self.last_execution_outcome = {
            "status": status,
            "scene_changed": scene_changed,
            "steps": steps,
            "remaining_backtrack_steps": len(self.corridor_backtrack_steps),
        }
        if status == "corridor_backtrack_completed":
            self.complete_current_procedure()
            suffix = "" if steps == 1 else "s"
            self.procedure_events.append(
                {
                    "type": "procedure",
                    "procedure": "corridor_backtrack",
                    "status": status,
                    "text": (
                        "The previous corridor was a dead end; runtime "
                        f"backtracked {steps} step{suffix} and returned to "
                        "the last useful room or junction."
                    ),
                }
            )
        else:
            self.mark_current_procedure_blocked()
        self.last_response = f"backtrack_corridor: {status} after {steps} step"
        if steps != 1:
            self.last_response += "s"
        self.append_response_history(self.last_response)
        self.next_auto_request_at = time.monotonic() + AUTO_ACTION_DELAY
        self.write_trace_result(
            request_kind=request_kind,
            scene_after_action=scene_after_action,
        )
