from __future__ import annotations


class GoalsMixin:
    def high_level_goals(self) -> list[str]:
        """Return stable top-level goals for the current prototype."""
        return [
            "Collect useful items to improve survival and progress.",
            "Survive immediate threats while pursuing items.",
            "Explore rooms, exits, doors, corridors, and frontiers to find more items and information.",
        ]

    def medium_level_goals(self) -> list[str]:
        """Return tactical priorities beneath the top-level goal."""
        return [
            "Avoid staying adjacent to a hostile when a safe move exists.",
            "When immediate danger is absent, prefer useful reachable item pickup over pure exploration.",
            "Choose pick:item:* once for an item; runtime will move to it, pick it up, and hand control back after completion or interruption.",
            "A distant hostile does not automatically forbid item pickup; weigh item distance, item value, hostile distance, and route safety.",
            "When no useful item is available, choose the nearest exploration action first; prefer corridor traversal, frontiers, exits, and doors that reveal new map over incidental movement.",
            "Corridor traversal is runtime-managed; after choosing a corridor, reassess when the stop event reports an end, intersection, blockage, or hostile.",
            "Avoid corridor actions marked runtime_backtracking unless all non-backtracking choices are unsafe or exhausted.",
            "Use visible paths to exits, doors, corridors, items, and frontiers instead of inventing routes.",
            "Interrupt the current action when the scene becomes unsafe.",
        ]
