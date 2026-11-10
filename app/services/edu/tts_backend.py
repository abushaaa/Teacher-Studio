"""Narration audio for one scene.

Default backend reuses MoneyPrinterTurbo's own TTS layer (`app.services.voice`),
so every voice provider already supported there (Edge TTS - free, Azure,
ElevenLabs, ...) works here with the user's existing config.toml.

An `espeak` backend (offline, robotic) exists for development and tests when no
network/TTS account is available.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional

from loguru import logger

# Edge TTS voices (free). "-Female"/"-Male" suffix is the naming MoneyPrinterTurbo uses.
DEFAULT_VOICES = {
    "en": "en-US-AriaNeural-Female",
    "ar": "ar-AE-FatimaNeural-Female",
}
_ESPEAK_VOICE = {"en": "en-us", "ar": "ar"}


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path],
        capture_output=True,
        text=True,
    )
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise RuntimeError(f"could not read audio duration of {path}: {out.stderr.strip()}") from exc


def _espeak(text: str, language: str, out_path: str, rate: float) -> None:
    exe = shutil.which("espeak-ng") or shutil.which("espeak")
    if not exe:
        raise RuntimeError("espeak backend selected but espeak-ng is not installed")
    wav = out_path + ".wav"
    wpm = int(150 * rate)
    subprocess.run([exe, "-v", _ESPEAK_VOICE.get(language, "en-us"), "-s", str(wpm), "-w", wav, text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", wav, "-ar", "44100", "-ac", "2", out_path], check=True)
    os.remove(wav)


def synthesize(
    text: str,
    language: str,
    out_path: str,
    backend: str = "app",
    voice_name: Optional[str] = None,
    rate: float = 0.95,
    volume: float = 1.0,
) -> float:
    """Write narration audio to `out_path`; return its duration in seconds.

    rate < 1.0 slows the voice slightly: students need time to read the diagram.
    """
    if backend == "espeak":
        _espeak(text, language, out_path, rate)
    elif backend == "app":
        from app.services import voice  # lazy: heavy imports + config

        name = voice_name or DEFAULT_VOICES.get(language, DEFAULT_VOICES["en"])
        result = voice.tts(text=text, voice_name=name, voice_rate=rate, voice_file=out_path, voice_volume=volume)
        if result is None or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError(
                f"TTS failed for voice '{name}'. Check your network / TTS settings in config.toml "
                f"(or choose the offline 'espeak' backend for a quick local test)."
            )
    else:
        raise ValueError(f"unknown tts backend: {backend}")
    duration = probe_duration(out_path)
    logger.info(f"tts[{backend}] {duration:.1f}s for {len(text)} chars")
    return duration
