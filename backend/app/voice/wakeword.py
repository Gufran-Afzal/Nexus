"""Wake-word detection engines.

- OpenWakeWordDetector: openWakeWord (Apache-2.0 code). Very light on CPU. Needs a model: either a
  custom-trained "hey nexus" model file, or one of openWakeWord's pre-trained phrases (e.g. "hey_jarvis").
  Note: openWakeWord's pre-trained models are CC BY-NC-SA 4.0 (non-commercial use).
- WhisperKeywordDetector: works with ANY phrase (default "hey nexus") without training. A cheap energy
  gate finds short bursts of speech; only those are transcribed by the local Whisper model and matched
  against the phrase. Costs more CPU than openWakeWord when there is a lot of speech nearby.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.voice.audio import FRAME_SAMPLES, SAMPLE_RATE, rms
from app.voice.probe import package_installed


@dataclass
class WakeHit:
    score: float
    remainder: str = ""  # words spoken right after the wake phrase in the same breath


class WakeDetector(Protocol):
    def process(self, frame: bytes) -> WakeHit | None: ...
    def reset(self) -> None: ...


def openwakeword_installed() -> bool:
    return package_installed("openwakeword")  # find_spec only: importing native libs here could crash the server


class OpenWakeWordDetector:
    def __init__(self, model: str, threshold: float = 0.5) -> None:
        if not openwakeword_installed():
            raise RuntimeError("openwakeword is not installed (pip install openwakeword)")
        from openwakeword.model import Model

        if not model:
            raise RuntimeError("No wake-word model configured. Set a custom model path or a built-in name.")
        is_file = Path(model).suffix.lower() in (".onnx", ".tflite")
        if is_file and not Path(model).is_file():
            raise RuntimeError(f"Wake-word model not found: {model}")
        framework = "tflite" if model.lower().endswith(".tflite") else "onnx"
        self._model: Any = Model(wakeword_models=[model], inference_framework=framework)
        self.threshold = threshold

    def process(self, frame: bytes) -> WakeHit | None:
        import numpy as np

        scores = self._model.predict(np.frombuffer(frame, dtype=np.int16))
        best = max(scores.values()) if scores else 0.0
        if best >= self.threshold:
            self.reset()
            return WakeHit(score=float(best))
        return None

    def reset(self) -> None:
        self._model.reset()


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z' ]+", " ", text.lower().replace("’", "'")).split())


# Ways Whisper writes the "hey" of "hey nexus".
_HEY = {"a", "he", "hay", "hi", "ay", "eh", "heh", "hey"}


def phrases_of(phrase: str) -> list[str]:
    """"hey nexus, ok nexus" -> ["hey nexus", "ok nexus"]. Several phrases can be set, separated by commas."""
    out = [_norm(p) for p in phrase.split(",")]
    return [p for p in out if p] or ["hey nexus"]


def match_phrase(phrase: str, text: str, threshold: float = 0.78) -> tuple[float, str] | None:
    """Fuzzy-match a wake phrase (or any of several) at the start of a transcript. Returns (score, remainder).

    Spaces are ignored in the comparison, so "next us" and "nexus" are the same word and "hey, nexus" and "hey nexus"
    are too; "hay/hi/a nexus" count as "hey nexus". The phrase may start within the first few words ("uh, hey nexus").
    """
    words = _norm(text).split()
    best: tuple[float, int] = (0.0, 0)
    for p in phrases_of(phrase):
        pw = p.split()
        target = p.replace(" ", "").replace("'", "")
        for start in range(0, min(3, len(words))):
            for length in range(max(1, len(pw) - 1), len(pw) + 3):  # "next us" is two words for one
                span = words[start:start + length]
                if not span:
                    continue
                if pw[0] == "hey" and span[0] in _HEY:
                    span = ["hey", *span[1:]]
                cand = "".join(span).replace("'", "")
                score = difflib.SequenceMatcher(None, target, cand).ratio()
                if score > best[0] or (score == best[0] and start + length > best[1]):
                    best = (score, start + length)
    if best[0] >= threshold:
        return best[0], " ".join(words[best[1]:])
    return None


class WhisperKeywordDetector:
    """Finds the wake phrase with the speech model. Everything is adaptive: the loudness gate follows the room's
    background noise (so quiet voices and noisy rooms both work), and a burst keeps 400 ms of audio from before it
    started so the first syllable is never cut off. `on_attempt(text, score, accepted)` reports what was heard."""

    def __init__(self, stt: Any, phrase: str = "hey nexus", energy_threshold: float = 0.0,
                 max_burst_s: float = 4.0, end_silence_s: float = 0.5, min_speech_s: float = 0.24,
                 on_attempt: Callable[[str, float, bool], None] | None = None) -> None:
        self.stt, self.phrase = stt, phrase
        self.fixed_threshold = energy_threshold  # > 0 forces a fixed gate (tests); 0 = adaptive
        self.frame_s = FRAME_SAMPLES / SAMPLE_RATE
        self.max_burst_s, self.end_silence_s, self.min_speech_s = max_burst_s, end_silence_s, min_speech_s
        self.on_attempt = on_attempt
        self.noise: float | None = None
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._pre: list[bytes] = []
        self._active = False
        self._silence = 0.0
        self._dur = 0.0
        self._speech = 0.0

    def _limit(self, level: float) -> float:
        if self.fixed_threshold > 0:
            return self.fixed_threshold
        # Track the quietest recent level as "the room"; never start from a loud first frame.
        if self.noise is None:
            self.noise = min(level, 100.0)
        elif level < self.noise:
            self.noise = level
        else:
            self.noise = min(level, self.noise * 1.005 + 0.5)
        return max(150.0, self.noise * 3.0)

    def process(self, frame: bytes) -> WakeHit | None:
        level = rms(frame)
        loud = level >= self._limit(level)
        if not self._active:
            self._pre = [*self._pre[-4:], frame]  # keep ~400 ms of pre-roll
            if loud:
                self._active = True
                self._buf = bytearray(b"".join(self._pre))
                self._dur = len(self._pre) * self.frame_s
                self._silence = 0.0
                self._speech = self.frame_s
            return None
        self._buf.extend(frame)
        self._dur += self.frame_s
        if loud:
            self._silence = 0.0
            self._speech += self.frame_s
        else:
            self._silence += self.frame_s
        if self._silence < self.end_silence_s and self._dur < self.max_burst_s:
            return None
        pcm, speech = bytes(self._buf), self._speech
        self.reset()
        if speech < self.min_speech_s:  # a click or a cough, not speech
            return None
        wake = getattr(self.stt, "transcribe_wake", None)
        text = wake(pcm, self.phrase) if callable(wake) else self.stt.transcribe_pcm(pcm)
        m = match_phrase(self.phrase, text) if text else None
        if self.on_attempt and text:
            self.on_attempt(text, m[0] if m else 0.0, m is not None)
        if m:
            return WakeHit(score=m[0], remainder=m[1])
        return None
