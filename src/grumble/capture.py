"""Record a play session. No speech models run here, so the game keeps the GPU."""

import ctypes
import math
import time
import winsound
from dataclasses import dataclass

from grumble.config import ProjectConfig
from grumble.hotkey import Hotkey
from grumble.markers import MarkerWorker
from grumble.recorder import Recorder
from grumble.session import ActiveLock, Session, create_session, now_iso

_SILENT_MIC_WARNING_SECONDS = 15


@dataclass
class CaptureResult:
    session: Session
    duration_seconds: float
    markers: int


def _set_dpi_aware() -> None:
    # Without this, window and monitor coordinates disagree on scaled displays.
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
    except OSError:
        pass


def _meter(peak: float) -> str:
    if peak <= 0:
        return "silent"
    return f"{20 * math.log10(peak):5.0f} dB"


def run_capture(config: ProjectConfig, max_seconds: float | None = None) -> CaptureResult:
    _set_dpi_aware()
    lock = ActiveLock()
    if active := lock.read():
        raise RuntimeError(f"Session {active['session']} is already recording (pid {active['pid']})")

    session = create_session(config)
    lock.acquire(session.id)
    recorder: Recorder | None = None
    markers: MarkerWorker | None = None
    hotkey: Hotkey | None = None
    started = False
    try:
        recorder = Recorder(session.audio_dir, config.audio.device, config.audio.chunk_seconds)
        markers = MarkerWorker(session, config.gamestate)

        def on_hotkey() -> None:
            markers.mark(recorder.position_seconds)
            if config.marker_beep:
                winsound.MessageBeep(winsound.MB_ICONASTERISK)

        hotkey = Hotkey(config.hotkey, on_hotkey)
        hotkey.start()
        markers.start()
        recorder.start()
        started = True
        session.update_manifest(device=recorder.device_name)

        print(f"Recording session {session.id}")
        print(f"  mic:    {recorder.device_name}")
        print(f"  marker: {config.hotkey.upper()}")
        print("  stop:   Ctrl+C here, or `grumble stop` from another terminal")
        _wait(lock, recorder, markers, max_seconds)
    except BaseException:
        if not started:
            session.update_manifest(status="failed", ended_at=now_iso())
        raise
    finally:
        print()
        if hotkey is not None:
            hotkey.stop()
        if recorder is not None:
            recorder.stop()
        if markers is not None and started:
            markers.stop()
        lock.release()
        if started:
            session.update_manifest(
                status="failed" if recorder.write_error else "recorded",
                ended_at=now_iso(),
                duration_seconds=round(recorder.duration_seconds, 3),
                overflow_count=recorder.overflow_count,
            )

    if recorder.overflow_count:
        print(f"Warning: {recorder.overflow_count} audio overflow(s); marker times may drift slightly.")
    return CaptureResult(session, recorder.duration_seconds, markers.count)


def _wait(lock: ActiveLock, recorder: Recorder, markers: MarkerWorker, max_seconds: float | None) -> None:
    started = time.monotonic()
    loudest = 0.0
    warned_silent = False
    try:
        while not lock.stop_requested():
            time.sleep(0.25)
            elapsed = time.monotonic() - started
            if max_seconds is not None and elapsed >= max_seconds:
                return
            if recorder.write_error:
                raise RuntimeError(f"Could not write audio: {recorder.write_error}")
            if not recorder.running:
                raise RuntimeError("The mic stream stopped. Was the device disconnected? Audio so far is saved.")

            peak = recorder.take_peak()
            loudest = max(loudest, peak)
            minutes, seconds = divmod(int(elapsed), 60)
            status = f"\r  {minutes:02d}:{seconds:02d}  mic {_meter(peak):>8}  markers {markers.count}  "
            if markers.last_warning:
                status += "[screenshot warning] "
            print(status, end="", flush=True)

            if not warned_silent and elapsed > _SILENT_MIC_WARNING_SECONDS and loudest < 1e-4:
                warned_silent = True
                print(f"\nWarning: no signal from '{recorder.device_name}' in {_SILENT_MIC_WARNING_SECONDS}s. Check the mic.")
    except KeyboardInterrupt:
        pass
