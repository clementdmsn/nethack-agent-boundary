from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_NETHACK_BIN = "nethack"


API_KEY = os.environ.get("OPENAI_API_KEY", "local")
MODEL = os.environ.get("NETHACK_AGENT_MODEL", "unsloth/qwen3-4b-instruct-2507")
BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://localhost:1234/v1")

NETHACK_BIN = os.environ.get("NETHACK_BIN", DEFAULT_NETHACK_BIN)
NETHACK_OPTIONS = os.environ.get(
    "NETHACK_OPTIONS",
    f"@{PROJECT_ROOT / '.nethackrc'}",
)
TERMINAL_ROWS = int(os.environ.get("NETHACK_TERMINAL_ROWS", "50"))
TERMINAL_COLS = int(os.environ.get("NETHACK_TERMINAL_COLS", "160"))
TERMINAL_TIMEOUT = float(os.environ.get("NETHACK_TERMINAL_TIMEOUT", "0.10"))
