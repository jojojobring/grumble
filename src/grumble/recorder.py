"""Mic capture to fixed-length 16 kHz FLAC chunks.

Every chunk except the last holds exactly chunk_seconds of audio, so a chunk's
index gives its place on the session timeline even if another chunk is damaged.
"""

import ctypes
import queue
import threading
from pathlib import Path
from typing import Any

import av
import numpy as np
import sounddevice as sd
import soundfile as sf
import soxr

SAMPLE_RATE = 16000
_LATENCY_SECONDS = 0.1  # also bounds how far a marker can lag the audio timeline


def _prepare_audio_thread() -> None:
    # WASAPI streams only start on a thread with COM set up, which PortAudio does only for its importer.
    ctypes.windll.ole32.CoInitializeEx(None, 0)


def pick_device(name: str | None, devices: list[dict[str, Any]], hostapis: list[dict[str, Any]]) -> int:
    """Choose an input device, preferring WASAPI because MME truncates names."""
    wasapi = next((i for i, api in enumerate(hostapis) if "WASAPI" in api["name"]), None)
    inputs = [i for i, device in enumerate(devices) if device["max_input_channels"] > 0]

    if name is None:
        if wasapi is not None and hostapis[wasapi]["default_input_device"] >= 0:
            return hostapis[wasapi]["default_input_device"]
        raise ValueError("No default input device. Set audio.device in the project config.")

    matches = [i for i in inputs if name.lower() in devices[i]["name"].lower()]
    if not matches:
        options = sorted({devices[i]["name"] for i in inputs})
        raise ValueError(f"No input device matches '{name}'. Available: {options}")
    return next((i for i in matches if devices[i]["hostapi"] == wasapi), matches[0])


def list_input_devices() -> list[tuple[str, bool]]:
    """Return (name, is default) for each WASAPI input device."""
    hostapis = list(sd.query_hostapis())
    wasapi = next((i for i, api in enumerate(hostapis) if "WASAPI" in api["name"]), None)
    default = hostapis[wasapi]["default_input_device"] if wasapi is not None else -1
    return [
        (device["name"], i == default)
        for i, device in enumerate(sd.query_devices())
        if device["max_input_channels"] > 0 and device["hostapi"] == wasapi
    ]


def measure_level(name: str | None, seconds: float = 3.0) -> float:
    """Listen briefly, and return the peak level from 0 to 1. Nothing is saved."""
    devices = list(sd.query_devices())
    index = pick_device(name, devices, list(sd.query_hostapis()))
    info = devices[index]
    rate = int(info["default_samplerate"])
    _prepare_audio_thread()
    try:
        with sd.InputStream(device=index, samplerate=rate, channels=min(2, info["max_input_channels"]), dtype="float32") as stream:
            data, _ = stream.read(int(seconds * rate))
    except sd.PortAudioError as error:
        raise RuntimeError(f"Could not open mic '{info['name']}'. Check that it's connected, or choose another one. ({error})") from error
    return float(np.abs(data).max(initial=0.0))


class ChunkWriter:
    def __init__(self, directory: Path, chunk_seconds: int, sample_rate: int = SAMPLE_RATE):
        self._directory = directory
        self._sample_rate = sample_rate
        self._chunk_frames = chunk_seconds * sample_rate
        self._file: sf.SoundFile | None = None
        self._index = 0
        self._frames_in_chunk = 0
        self.total_frames = 0

    def write(self, samples: np.ndarray) -> None:
        while len(samples):
            if self._file is None:
                path = self._directory / f"chunk_{self._index:05d}.flac"
                self._file = sf.SoundFile(path, "w", self._sample_rate, 1, "PCM_16", format="FLAC")
            room = self._chunk_frames - self._frames_in_chunk
            part, samples = samples[:room], samples[room:]
            self._file.write(part)
            self._frames_in_chunk += len(part)
            self.total_frames += len(part)
            if self._frames_in_chunk == self._chunk_frames:
                self._close_chunk()

    def close(self) -> None:
        if self._file is not None:
            self._close_chunk()

    def _close_chunk(self) -> None:
        self._file.close()
        self._file = None
        self._index += 1
        self._frames_in_chunk = 0


def _read_chunk(path: Path, max_frames: int) -> np.ndarray:
    try:
        with sf.SoundFile(path) as file:
            return file.read(frames=max_frames, dtype="float32")
    except (sf.LibsndfileError, RuntimeError, ValueError):
        pass

    # A chunk unfinished by a crash has a bad header, but FFmpeg can still decode its frames.
    parts = []
    with av.open(str(path)) as container:
        try:
            for frame in container.decode(audio=0):
                parts.append(frame.to_ndarray().reshape(-1))
        except av.FFmpegError:
            if not parts:
                raise  # otherwise keep what came before the torn final frame
    if not parts:
        raise RuntimeError("no decodable audio")
    return (np.concatenate(parts).astype("float32") / 32768)[:max_frames]


def read_audio(directory: Path, chunk_seconds: int, sample_rate: int = SAMPLE_RATE) -> tuple[np.ndarray, list[str]]:
    """Rebuild the session timeline. Damaged chunks become silence, with a warning."""
    chunk_frames = chunk_seconds * sample_rate
    paths = {int(path.stem.split("_")[1]): path for path in directory.glob("chunk_*.flac")}
    warnings: list[str] = []
    if not paths:
        return np.zeros(0, dtype="float32"), warnings

    last = max(paths)
    parts = []
    for index in range(last + 1):
        data = np.zeros(0, dtype="float32")
        path = paths.get(index)
        if path is None:
            warnings.append(f"chunk {index} is missing; treating it as silence")
        else:
            try:
                data = _read_chunk(path, chunk_frames)
            except Exception as error:  # one bad chunk must not lose the session
                warnings.append(f"{path.name} is unreadable ({error}); treating it as silence")
        if index < last and len(data) < chunk_frames:
            if path is not None and len(data):
                warnings.append(f"{path.name} is short; padding it with silence")
            data = np.pad(data, (0, chunk_frames - len(data)))
        parts.append(data[:chunk_frames])
    return np.concatenate(parts), warnings


class Recorder:
    def __init__(self, directory: Path, device: str | None, chunk_seconds: int):
        devices = list(sd.query_devices())
        hostapis = list(sd.query_hostapis())
        self._device = pick_device(device, devices, hostapis)
        info = devices[self._device]
        self.device_name: str = info["name"]
        self._rate = int(info["default_samplerate"])
        self._channels = min(2, info["max_input_channels"])
        self._writer = ChunkWriter(directory, chunk_seconds)
        self._queue: queue.Queue[np.ndarray | None] = queue.Queue()
        self._thread = threading.Thread(target=self._write_loop, name="audio-writer", daemon=True)
        self._stream: sd.InputStream | None = None
        self._frames_captured = 0
        self._peak = 0.0
        self.overflow_count = 0
        self.write_error: Exception | None = None

    @property
    def position_seconds(self) -> float:
        """Current place on the audio timeline, so markers line up with the transcript."""
        return self._frames_captured / self._rate

    @property
    def duration_seconds(self) -> float:
        return self._writer.total_frames / SAMPLE_RATE

    @property
    def running(self) -> bool:
        return self._stream is not None and self._stream.active

    def take_peak(self) -> float:
        peak, self._peak = self._peak, 0.0
        return peak

    def start(self) -> None:
        _prepare_audio_thread()
        try:
            self._stream = sd.InputStream(
                device=self._device,
                samplerate=self._rate,
                channels=self._channels,
                dtype="float32",
                latency=_LATENCY_SECONDS,
                callback=self._on_audio,
            )
            self._thread.start()
            self._stream.start()
        except sd.PortAudioError as error:
            raise RuntimeError(f"Could not open mic '{self.device_name}'. Check that it's connected, or choose another one in Setup. ({error})") from error

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
        if self._thread.is_alive():
            self._queue.put(None)
            self._thread.join()

    def _on_audio(self, data: np.ndarray, frames: int, _time: Any, status: sd.CallbackFlags) -> None:
        if status.input_overflow:
            self.overflow_count += 1
        self._frames_captured += frames
        self._queue.put_nowait(data.copy())

    def _write_loop(self) -> None:
        resampler = soxr.ResampleStream(self._rate, SAMPLE_RATE, 1, dtype="float32")
        try:
            while (block := self._queue.get()) is not None:
                mono = block.mean(axis=1)
                self._peak = max(self._peak, float(np.abs(mono).max(initial=0.0)))
                self._writer.write(resampler.resample_chunk(mono))
            self._writer.write(resampler.resample_chunk(np.zeros(0, dtype="float32"), last=True))
        except Exception as error:  # surfaced by the capture loop
            self.write_error = error
        finally:
            self._writer.close()
