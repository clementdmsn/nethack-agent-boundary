from __future__ import annotations

import json

from constants.runtime import AUTO_PROMPT
from prompting.action_catalog import ActionCatalogMixin
from prompting.actions.combat import CombatActionsMixin
from prompting.actions.exploration import ExplorationActionsMixin
from prompting.actions.navigation import NavigationActionsMixin
from prompting.goals import GoalsMixin
from prompting.payload import PayloadMixin
from prompting.procedure import ProcedureMixin
from prompting.scene.events import SceneEventsMixin
from prompting.scene.refs import SceneRefsMixin
from prompting.scene.text import SceneTextMixin


class PromptContextMixin(
    GoalsMixin,
    SceneTextMixin,
    SceneRefsMixin,
    SceneEventsMixin,
    NavigationActionsMixin,
    CombatActionsMixin,
    ExplorationActionsMixin,
    ActionCatalogMixin,
    PayloadMixin,
    ProcedureMixin,
):
    ACTION_BUCKETS = ("exits", "items", "entities", "features", "areas")
    DIRECTION_NAMES = {
        (0, -1): "north",
        (0, 1): "south",
        (-1, 0): "west",
        (1, 0): "east",
        (-1, -1): "northwest",
        (1, -1): "northeast",
        (-1, 1): "southwest",
        (1, 1): "southeast",
    }
    CARDINAL_DIRECTION_DELTAS = {
        "north": (0, -1),
        "south": (0, 1),
        "west": (-1, 0),
        "east": (1, 0),
    }

    def build_model_prompt(self, user_input: str, scene: dict[str, object]) -> str:
        """Build the model payload and update trace/procedure context."""
        normalized_scene = self.copy_scene_with_refs(scene)
        model_scene = self.model_visible_scene(normalized_scene)
        scene_summary = self.describe_scene_for_model(model_scene)
        previous_scene_summary = self.last_scene_summary
        scene_events = (
            self.consume_procedure_events()
            + self.build_scene_events(self.last_scene, model_scene)
        )
        available_actions = self.build_available_actions(model_scene)
        current_procedure = self.current_procedure_snapshot(available_actions)
        full_payload = self.full_payload(
            user_input=user_input,
            normalized_scene=model_scene,
            scene_summary=scene_summary,
            previous_scene_summary=previous_scene_summary,
            scene_events=scene_events,
            available_actions=available_actions,
            current_procedure=current_procedure,
        )
        if user_input == AUTO_PROMPT:
            payload = self.decision_payload(
                normalized_scene=model_scene,
                scene_events=scene_events,
                available_actions=available_actions,
                current_procedure=current_procedure,
            )
        else:
            payload = full_payload

        self.last_scene = model_scene
        self.last_scene_summary = scene_summary
        self.last_scene_events = scene_events
        self.last_available_actions = available_actions
        self.last_available_actions_by_id = {
            action["action_id"]: action
            for action in available_actions
            if isinstance(action.get("action_id"), str)
        }
        self.last_decision_context = {
            "available_actions": available_actions,
            "current_procedure": current_procedure,
        }
        self.last_trace_payload = full_payload

        return json.dumps(payload, indent=2)
