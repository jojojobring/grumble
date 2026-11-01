import subprocess
import sys

import numpy as np
import soundfile as sf

from grumble.recorder import ChunkWriter, pick_device, read_audio

RATE = 100  # small rate keeps these tests instant


def ramp(frames):
    return (np.arange(frames, dtype="float32") % 100) / 200


def write_all(directory, samples, block=37):
    writer = ChunkWriter(directory, chunk_seconds=1, sample_rate=RATE)
    for start in range(0, len(samples), block):
        writer.write(samples[start : start + block])
    writer.close()
    return writer


def test_chunks_are_exact_length_and_round_trip(tmp_path):
    samples = ramp(250)
    writer = write_all(tmp_path, samples)
    assert writer.total_frames == 250
    assert [sf.info(p).frames for p in sorted(tmp_path.glob("*.flac"))] == [100, 100, 50]

    audio, warnings = read_audio(tmp_path, 1, RATE)
    assert warnings == []
    assert np.allclose(audio, samples, atol=1e-3)


def test_exact_multiple_leaves_no_empty_chunk(tmp_path):
    write_all(tmp_path, ramp(200))
    assert len(list(tmp_path.glob("*.flac"))) == 2


def test_damaged_chunk_keeps_later_audio_in_place(tmp_path):
    samples = ramp(250)
    write_all(tmp_path, samples)
    (tmp_path / "chunk_00001.flac").write_bytes(b"not flac")

    audio, warnings = read_audio(tmp_path, 1, RATE)
    assert len(audio) == 250
    assert len(warnings) == 1
    assert np.all(audio[100:200] == 0)
    assert np.allclose(audio[200:], samples[200:], atol=1e-3)


def test_missing_chunk_keeps_later_audio_in_place(tmp_path):
    samples = ramp(250)
    write_all(tmp_path, samples)
    (tmp_path / "chunk_00000.flac").unlink()

    audio, warnings = read_audio(tmp_path, 1, RATE)
    assert len(audio) == 250 and "missing" in warnings[0]
    assert np.allclose(audio[200:], samples[200:], atol=1e-3)


def test_chunk_left_open_by_a_crash_is_recovered(tmp_path):
    code = f"""
import os
import numpy as np
from pathlib import Path
from grumble.recorder import ChunkWriter
writer = ChunkWriter(Path(r"{tmp_path}"), chunk_seconds=30, sample_rate=16000)
writer.write(np.full(16000 * 40, 0.25, dtype="float32"))
writer._file.flush()
os._exit(1)
"""
    assert subprocess.run([sys.executable, "-c", code]).returncode == 1

    audio, _ = read_audio(tmp_path, 30, 16000)
    assert 16000 * 31 < len(audio) <= 16000 * 40
    assert np.allclose(audio[16000 * 30 : 16000 * 31], 0.25, atol=1e-3)


def test_no_audio_returns_empty(tmp_path):
    audio, warnings = read_audio(tmp_path, 1, RATE)
    assert len(audio) == 0 and warnings == []


HOSTAPIS = [{"name": "MME", "default_input_device": 0}, {"name": "Windows WASAPI", "default_input_device": 2}]
DEVICES = [
    {"name": "HyperX Cloud", "hostapi": 0, "max_input_channels": 1},
    {"name": "Speakers", "hostapi": 1, "max_input_channels": 0},
    {"name": "Webcam mic", "hostapi": 1, "max_input_channels": 1},
    {"name": "HyperX Cloud II Wireless", "hostapi": 1, "max_input_channels": 1},
]


def test_pick_device_prefers_wasapi_and_matches_substring():
    assert pick_device("hyperx", DEVICES, HOSTAPIS) == 3
    assert pick_device(None, DEVICES, HOSTAPIS) == 2


def test_pick_device_lists_options_when_nothing_matches():
    try:
        pick_device("focusrite", DEVICES, HOSTAPIS)
    except ValueError as error:
        assert "Webcam mic" in str(error)
    else:
        raise AssertionError("expected ValueError")
