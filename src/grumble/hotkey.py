"""Global hotkey through Win32 RegisterHotKey.

RegisterHotKey installs no per-keystroke hook, so it adds no input latency. It
also keeps the key from the game while registered.
"""

import ctypes
import threading
from collections.abc import Callable
from ctypes import wintypes

_MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
_MOD_NOREPEAT = 0x4000
_WM_HOTKEY = 0x0312
_WM_QUIT = 0x0012


def parse_hotkey(text: str) -> tuple[int, int]:
    """Turn 'ctrl+f9' into (modifier flags, virtual key code)."""
    *modifiers, key = [part.strip().lower() for part in text.split("+")]
    flags = 0
    for modifier in modifiers:
        if modifier not in _MODIFIERS:
            raise ValueError(f"Unknown hotkey modifier '{modifier}' in '{text}'")
        flags |= _MODIFIERS[modifier]

    if key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        return flags, 0x6F + int(key[1:])
    if len(key) == 1 and key.isascii() and key.isalnum():
        return flags, ord(key.upper())
    raise ValueError(f"Unknown hotkey key '{key}' in '{text}'")


def is_safe_global_hotkey(text: str) -> bool:
    """A global hotkey takes its key from every app, so a bare letter would stop typing it."""
    modifiers, key = parse_hotkey(text)
    return modifiers != 0 or 0x70 <= key <= 0x87


class Hotkey:
    def __init__(self, text: str, callback: Callable[[], None]):
        self._text = text
        self._modifiers, self._key = parse_hotkey(text)
        self._callback = callback
        self._ready = threading.Event()
        self._error: str | None = None
        self._thread_id = 0
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait()
        if self._error:
            raise RuntimeError(self._error)

    def stop(self) -> None:
        if self._thread.is_alive():
            ctypes.WinDLL("user32").PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
            self._thread.join(timeout=2)

    def _run(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
        self._thread_id = ctypes.WinDLL("kernel32").GetCurrentThreadId()

        # The registration and the message loop must share a thread.
        if not user32.RegisterHotKey(None, 1, self._modifiers | _MOD_NOREPEAT, self._key):
            code = ctypes.get_last_error()
            self._error = f"Could not register hotkey '{self._text}' (Windows error {code}). Another app probably owns it."
            self._ready.set()
            return
        self._ready.set()

        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == _WM_HOTKEY:
                    self._callback()
        finally:
            user32.UnregisterHotKey(None, 1)
