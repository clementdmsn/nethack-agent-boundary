from __future__ import annotations

import re


FARLOOK_KEY = ";"
ESCAPE_KEY = "\x1b"
MORE_FLAG = "--More--"
END_FLAG = "(end)"
MORE_PATTERN = re.compile(r"\s*--More--.*$")
LOOK_DESCRIPTION_PATTERN = re.compile(
    r"(?P<description>.+?)\s+\((?P<location>[^)]+)\)$"
)
RELATIVE_ELEMENT_PATTERN = re.compile(
    r"\((?P<location>[^)]+)\):\s*(?P<text>[^()]+)"
)
RELATIVE_PART_PATTERN = re.compile(
    r"(?P<count>\d+)?\s*(?P<direction>north|south|east|west)"
)
PLAYER_HERE_PATTERN = re.compile(r"(?P<description>.+?)\s+\(here\)")
STARTUP_IDENTITY_PATTERN = re.compile(
    r"Hello\s+(?P<name>[^,]+),\s+welcome to NetHack!\s+You are\s+"
    r"(?:a|an)\s+(?P<role_text>[^.]+)\."
)
STATUS_IDENTITY_PATTERN = re.compile(
    r"^\[(?P<name>[^\]]+?)\s+the\s+(?P<title>[^\]]+?)\s*\]\s+St:"
)
STATUS_PATTERN = re.compile(
    r"^(?:Lawful|Neutral|Chaotic)\s+\$|^\[.*\]|^Dlvl:"
)
MAP_PREFIX_PATTERN = re.compile(
    r"^[\s┌┐└┘─│·■+@A-Za-z$)($!?%/=*`0-9]+?\s{2,}(?=\S)"
)
ROOM_DESCRIPTION_PATTERN = re.compile(r"You are in (?:a|an) [^.]+\.")
DARK_ROOM_PATTERN = re.compile(r"You can't guess the size of this area\.")
TRANSIENT_MESSAGE_PREFIXES = (
    "You hear ",
)
TRANSIENT_MESSAGE_PATTERN = re.compile(
    r"^(?:[A-Z][A-Za-z0-9_ -]*|The [A-Za-z0-9_ -]+) "
    r"(?:bites?|hits?|misses?|scratches?|kicks?|stings?|shoots?|throws?|"
    r"zaps?|casts?|dies?|is killed|falls?|moves?|opens?|closes?)\b"
)
ENTITY_DESCRIPTION_FRAGMENT_PATTERN = re.compile(
    r"\b(?:called\s+(?:Kitty|Doggo|Horse)|"
    r"(?:kitten|cat|dog|horse|pony|grid bug|goblin|kobold|newt|lichen|jackal))\b",
    re.IGNORECASE,
)
ITEM_DESCRIPTION_FRAGMENT_PATTERN = re.compile(
    r"\b(?:gold pieces?|coins?|spellbook|scroll|potion|ring|wand|gem|"
    r"amulet|armor|helmet|boots|cloak|shield|weapon|dagger|sword|"
    r"food ration|corpse|large box|chest)\b",
    re.IGNORECASE,
)
PET_NAMES = ("Kitty", "Doggo", "Horse")
PET_SPECIES_WORDS = ("kitten", "cat", "dog", "horse", "pony")
PROMPT_PREFIXES = (
    "Hello ",
    "You are ",
    "Pick ",
    "Tip:",
    "Move cursor ",
    "When ",
    "Game time ",
    "This mode ",
    "You can ",
    "You will ",
    "Press ",
    "There ",
)
FARLOOK_NOISE_PREFIXES = (
    "Hello ",
    "Pick ",
    "Tip:",
    "Move cursor ",
    "When ",
    "Game time ",
    'You are now in a "farlook" mode',
    "This mode ",
    "You can ",
    "You will ",
    "Press ",
)
FARLOOK_ACTIVE_MARKERS = (
    "Pick a monster, object or location",
    "Move cursor",
    'You are now in a "farlook" mode',
    "return to normal game mode",
)
FLOOR_GLYPHS = {".", "·"}
WALL_GLYPHS = {"|", "-", "│", "─", "┌", "┐", "└", "┘"}
FEATURE_GLYPHS = {"+", "<", ">", "_", "{", "\\", "^", "#"}
ITEM_GLYPHS = {'"', "%", "?", "!", "=", "/", "(", "[", "*", "$"}
MONSTER_GLYPHS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ&;':"
)
PLAYER_GLYPHS = {"@"}
SKIP_GLYPHS = FLOOR_GLYPHS | WALL_GLYPHS | {" ", ""}
GENERIC_TERRAIN_DESCRIPTIONS = {
    "dark part of a room",
    "floor of a room",
    "floor of a corridor",
    "lit corridor",
    "corridor",
    "wall",
    "stone",
}
