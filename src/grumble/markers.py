"""Marker handling. The hotkey thread only queues; slow work happens here."""

import ctypes
import queue
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

import mss
import numpy as np
from PIL import Image

from grumble.config import GameStateConfig
from grumble.gamestate import fetch_gamestate
from grumble.session import Session, now_iso, write_json

_BLACK_LEVEL = 8


def pick_monitor(monitors: list[dict[str, int]], rect: tuple[int, int, int, int] | None) -> dict[str, int]:
    """Return the monitor holding the centre of rect. monitors[0] is mss's whole desktop."""
    if rect:
        x, y = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
        for monitor in monitors[1:]:
            if (
                monitor["left"] <= x < monitor["left"] + monitor["width"]
                and monitor["top"] <= y < monitor["top"] + monitor["height"]
            ):
                return monitor
    return monitors[1] if len(monitors) > 1 else monitors[0]


def is_black(image: Image.Image) -> bool:
    return int(np.asarray(image.resize((64, 36))).max()) <= _BLACK_LEVEL


def _foreground_window() -> tuple[str | None, tuple[int, int, int, int] | None]:
    user32 = ctypes.WinDLL("user32")
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

    window = user32.GetForegroundWindow()
    if not window:
        return None, None
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(window, buffer, 256)
    rect = wintypes.RECT()
    found = user32.GetWindowRect(window, ctypes.byref(rect))
    return buffer.value or None, (rect.left, rect.top, rect.right, rect.bottom) if found else None


class MarkerWorker:
    def __init__(self, session: Session, gamestate: GameStateConfig):
        self._session = session
        self._gamestate = gamestate
        self._queue: queue.Queue[tuple[float, str] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="markers", daemon=True)
        self.count = 0
        self.last_warning: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)
        self._thread.join()

    def mark(self, t: float) -> None:
        self._queue.put((t, now_iso()))

    def _run(self) -> None:
        # mss handles belong to the thread that opened them.
        with mss.mss() as capture:
            while (item := self._queue.get()) is not None:
                self.count += 1
                self._session.append_marker(self._build(self.count, *item, capture))

    def _build(self, marker_id: int, t: float, wall_time: str, capture: Any) -> dict[str, Any]:
        title, rect = _foreground_window()
        marker: dict[str, Any] = {
            "id": marker_id,
            "t": round(t, 3),
            "wall_time": wall_time,
            "window_title": title,
            "screenshot": None,
            "screenshot_warning": None,
            "gamestate": None,
            "gamestate_error": None,
        }

        try:
            shot = capture.grab(pick_monitor(capture.monitors, rect))
            image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            name = f"marker_{marker_id:03d}.jpg"
            image.save(self._session.screenshots_dir / name, quality=85)
            marker["screenshot"] = f"screenshots/{name}"
            if is_black(image):
                marker["screenshot_warning"] = "black frame; the game may be in exclusive fullscreen, so use borderless"
        except Exception as error:  # a failed screenshot must not end the session
            marker["screenshot_warning"] = f"screenshot failed: {error}"
        self.last_warning = marker["screenshot_warning"]

        state, error = fetch_gamestate(self._gamestate)
        marker["gamestate_error"] = error
        if state is not None:
            path = Path("gamestate") / f"marker_{marker_id:03d}.json"
            write_json(self._session.path / path, state)
            marker["gamestate"] = path.as_posix()
        return marker
