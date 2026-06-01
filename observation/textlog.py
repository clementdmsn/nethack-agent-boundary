from __future__ import annotations

from observation.constants import (
    END_FLAG,
    MAP_PREFIX_PATTERN,
    MORE_FLAG,
    PROMPT_PREFIXES,
    RELATIVE_ELEMENT_PATTERN,
)


class ScreenTextLogMixin:
    def reset_observation_log(self) -> None:
        # Clears the per-observation prompted text log.
        self.last_text_log = []
        self.seen_text_log_lines = set()

    def record_prompted_text(self, screen: str) -> None:
        # Appends newly seen prompted text lines from the rendered screen.
        for line in self.prompted_text_lines(screen):
            if line in self.seen_text_log_lines:
                continue

            self.last_text_log.append(line)
            self.seen_text_log_lines.add(line)

    def prompted_text_lines(self, screen: str) -> list[str]:
        # Extracts message-like lines worth keeping in the observation text log.
        lines = []

        for line in screen.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            cleaned = self.clean_log_line(cleaned)
            if not cleaned:
                continue
            if self.row_has_status_text(cleaned):
                continue
            if self.is_prompted_text_line(cleaned):
                lines.append(cleaned)

        return lines

    def clean_log_line(self, line: str) -> str:
        # Removes map prefixes from mixed map/message lines before logging them.
        return MAP_PREFIX_PATTERN.sub("", line).strip()

    def is_prompted_text_line(self, line: str) -> bool:
        # Classifies a cleaned line as prompted text instead of map content.
        if MORE_FLAG in line or END_FLAG in line:
            return True
        if RELATIVE_ELEMENT_PATTERN.search(line) or "(here)" in line:
            return True

        if line.startswith(PROMPT_PREFIXES):
            return True

        tutorial_fragments = (
            "farlook",
            "movement keys",
            "not your character",
            "select a location",
            "return to normal game mode",
            "key help",
        )
        return any(fragment in line for fragment in tutorial_fragments)
