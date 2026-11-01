"""Optional game-state snapshot. Failures are reported, never raised."""

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from grumble.config import GameStateConfig


def fetch_gamestate(config: GameStateConfig) -> tuple[Any, str | None]:
    """Return (state, error). Both are None when no source is configured."""
    try:
        if config.type == "http":
            with urllib.request.urlopen(config.url, timeout=config.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8")), None
        if config.type == "file":
            return json.loads(Path(config.path).read_text(encoding="utf-8")), None
    except (OSError, urllib.error.URLError, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"
    return None, None
