"""Saved audio -> VAD segments -> transcript. Safe to re-run on any session."""

import ctypes.util
import dataclasses
import os
import sysconfig
import time
from pathlib import Path
from typing import Any

import numpy as np

from grumble import schemas
from grumble.config import ProjectConfig, TranscribeConfig, VadConfig
from grumble.recorder import SAMPLE_RATE, read_audio
from grumble.session import Session, now_iso, write_json

_PROMPT_TOKEN_LIMIT = 220


def build_initial_prompt(vocabulary: list[str]) -> str | None:
    return f"Glossary: {', '.join(vocabulary)}." if vocabulary else None


def find_speech(audio: np.ndarray, config: VadConfig) -> list[tuple[int, int]]:
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    if len(audio) == 0:
        return []
    options = VadOptions(
        threshold=config.threshold,
        min_speech_duration_ms=config.min_speech_ms,
        min_silence_duration_ms=config.min_silence_ms,
        speech_pad_ms=config.speech_pad_ms,
        max_speech_duration_s=config.max_speech_seconds,
    )
    return [(span["start"], span["end"]) for span in get_speech_timestamps(audio, options)]


def _add_cuda_to_path() -> None:
    # The nvidia-* wheels install DLLs where CTranslate2 will not look on its own.
    nvidia = Path(sysconfig.get_path("purelib")) / "nvidia"
    for directory in nvidia.glob("*/bin"):
        os.add_dll_directory(str(directory))
        os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ['PATH']}"


def gpu_ready() -> bool:
    """True with an NVIDIA GPU and the optional CUDA libraries both present."""
    import ctranslate2

    _add_cuda_to_path()
    return ctranslate2.get_cuda_device_count() > 0 and ctypes.util.find_library("cublas64_12") is not None


def resolve_settings(config: TranscribeConfig, has_gpu: bool) -> TranscribeConfig:
    """Fill in anything set to 'auto'. The CPU gets a model small enough to run at a usable speed."""
    device = config.device if config.device != "auto" else ("cuda" if has_gpu else "cpu")
    on_gpu = device == "cuda"
    compute_type = config.compute_type if config.compute_type != "auto" else ("float16" if on_gpu else "int8")
    if config.model != "auto":
        model = config.model
    elif on_gpu:
        model = "large-v3-turbo"
    else:
        model = "small.en" if config.language == "en" else "small"
    return dataclasses.replace(config, device=device, compute_type=compute_type, model=model)


def load_model(config: TranscribeConfig) -> Any:
    if config.device == "cuda" and not gpu_ready():
        raise RuntimeError("CUDA isn't available. Install grumble[cuda], or set transcribe.device to cpu.")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    from faster_whisper import WhisperModel

    return WhisperModel(config.model, device=config.device, compute_type=config.compute_type)


def format_time(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 3600:02d}:{whole % 3600 // 60:02d}:{whole % 60:02d}"


def render_text(segments: list[dict[str, Any]], markers: list[dict[str, Any]]) -> str:
    """A readable transcript with markers in place, for checking quality by eye."""
    lines: list[tuple[float, int, str]] = []
    for segment in segments:
        lines.append((segment["start"], 1, f"[{format_time(segment['start'])}] {segment['text']}"))
    for marker in markers:
        note = f" ({marker['screenshot']})" if marker.get("screenshot") else ""
        lines.append((marker["t"], 0, f"[{format_time(marker['t'])}] ***** MARKER {marker['id']}{note}"))
    return "\n".join(line for _, _, line in sorted(lines, key=lambda item: item[:2])) + "\n"


def transcribe_session(session: Session, config: ProjectConfig) -> dict[str, Any]:
    manifest = session.read_manifest()
    audio, warnings = read_audio(session.audio_dir, manifest["chunk_seconds"])
    for warning in warnings:
        print(f"Warning: {warning}")

    spans = find_speech(audio, config.vad)
    speech_seconds = sum(end - start for start, end in spans) / SAMPLE_RATE
    write_json(
        session.path / "segments.json",
        {
            "vad": vars(config.vad),
            "segments": [{"start": start / SAMPLE_RATE, "end": end / SAMPLE_RATE} for start, end in spans],
        },
    )
    print(f"VAD: {speech_seconds:.0f}s of speech in {len(spans)} segment(s), from {len(audio) / SAMPLE_RATE:.0f}s of audio")

    settings = resolve_settings(config.transcribe, gpu_ready())
    if settings.device == "cpu" and config.transcribe.device == "auto":
        print("No usable GPU, so transcribing on the CPU. NVIDIA GPU owners can install grumble[cuda] for better results.")
    prompt = build_initial_prompt(config.vocabulary)
    began = time.monotonic()
    segments: list[dict[str, Any]] = []
    if spans:
        print(f"Loading {settings.model} on {settings.device}...")
        model = load_model(settings)
        if prompt and (count := len(model.hf_tokenizer.encode(prompt).ids)) > _PROMPT_TOKEN_LIMIT:
            print(f"Warning: vocabulary is {count} tokens; Whisper only uses about {_PROMPT_TOKEN_LIMIT}.")
        for index, (start, end) in enumerate(spans, 1):
            print(f"\r  segment {index}/{len(spans)}", end="", flush=True)
            results, _ = model.transcribe(
                audio[start:end],
                language=settings.language,
                beam_size=settings.beam_size,
                initial_prompt=prompt,
                condition_on_previous_text=False,
                vad_filter=False,
            )
            offset = start / SAMPLE_RATE
            for result in results:
                text = result.text.strip()
                if text:
                    segments.append(
                        {
                            "start": round(offset + result.start, 2),
                            "end": round(offset + result.end, 2),
                            "text": text,
                            "avg_logprob": round(result.avg_logprob, 3),
                            "no_speech_prob": round(result.no_speech_prob, 3),
                        }
                    )
        print()

    transcript = {
        "session": session.id,
        "model": settings.model,
        "language": settings.language,
        "initial_prompt": prompt,
        "created_at": now_iso(),
        "audio_seconds": round(len(audio) / SAMPLE_RATE, 3),
        "speech_seconds": round(speech_seconds, 3),
        "transcribe_seconds": round(time.monotonic() - began, 2),
        "segments": segments,
    }
    schemas.validate(transcript, schemas.TRANSCRIPT)
    write_json(session.path / "transcript.json", transcript)
    (session.path / "transcript.txt").write_text(render_text(segments, session.read_markers()), encoding="utf-8")
    return transcript
