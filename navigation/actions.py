from __future__ import annotations

import re


ACTION_TO_KEYS = {
    "move(north)": "k",
    "move(south)": "j",
    "move(west)": "h",
    "move(east)": "l",
    "move(northwest)": "y",
    "move(northeast)": "u",
    "move(southwest)": "b",
    "move(southeast)": "n",
    "run(north)": "gk",
    "run(south)": "gj",
    "run(west)": "gh",
    "run(east)": "gl",
    "pickup()": ",",
}

ACTION_PATTERN = re.compile(
    r"(?:move\((?:north|south|west|east|northwest|northeast|southwest|southeast)\)|run\((?:north|south|east|west)\)|pickup\(\))"
)


def action_to_keys(action: str | None) -> str | None:
    """Normalize a model action string and map it to NetHack keystrokes."""
    if not action:
        return None

    action = action.strip()
    exact_match = ACTION_TO_KEYS.get(action)
    if exact_match is not None:
        return exact_match

    match = ACTION_PATTERN.search(action)
    if match is None:
        return None

    return ACTION_TO_KEYS.get(match.group(0))
