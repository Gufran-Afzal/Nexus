"""Microphone capture and audio helpers. All audio stays in memory; nothing is written to disk."""

from __future__ import annotations

import array
import io
import math
import queue
import wave
from collections.abc import Iterator
from typing import Any, Protocol

from app.voice.probe import DeviceProbe

SAMPLE_RATE = 16_000
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz (the frame size openWakeWord expects)


class MicUnavailable(Exception):
    pass


def rms(frame: bytes) -> float:
    """Root-mean-square level of 16-bit little-endian mono PCM, 0..32768."""
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) - (len(frame) % 2)])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def pcm_to_wav(pcm: bytes, rate: int = SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class AudioInput(Protocol):
    def available(self) -> tuple[bool, str]: ...
    def devices(self) -> list[dict[str, Any]]: ...
    def frames(self, device: int | None, stop: Any) -> Iterator[bytes]: ...  # a FrameStream, or any iterator


class SoundDeviceInput:
    """Captures 16 kHz mono int16 frames with the `sounddevice` package (PortAudio).

    Listing devices happens in a throw-away child process (see voice/probe.py): the Voice page asks often,
    and a PortAudio/driver crash must never take the NEXUS server down with it.
    """

    def __init__(self, probe: DeviceProbe | None = None) -> None:
        self.probe = probe or DeviceProbe()

    def _sd(self) -> Any:
        try:
            import sounddevice as sd
        except Exception as e:  # ImportError or PortAudio load failure
            raise MicUnavailable(f"sounddevice is not installed or PortAudio failed to load: {e}") from e
        return sd

    def available(self) -> tuple[bool, str]:
        res = self.probe.run()
        if not res["ok"]:
            return False, res["error"]
        if not res["devices"]:
            return False, "No microphone found"
        return True, "ok"

    def devices(self) -> list[dict[str, Any]]:
        res = self.probe.run()
        if not res["ok"]:
            raise MicUnavailable(res["error"])
        return list(res["devices"])

    def frames(self, device: int | None, stop: Any) -> FrameStream:
        """Open the microphone and return a stream of 80 ms frames. Iteration ends when stop.is_set()."""
        sd = self._sd()
        q: queue.Queue[bytes] = queue.Queue(maxsize=400)

        def cb(indata: Any, _frames: int, _t: Any, _status: Any) -> None:
            try:
                q.put_nowait(bytes(indata))
            except queue.Full:
                pass

        try:
            stream = sd.RawInputStream(samplerate=SAMPLE_RATE, blocksize=FRAME_SAMPLES, channels=1,
                                       dtype="int16", device=device, callback=cb)
            stream.start()
        except Exception as e:
            raise MicUnavailable(f"Could not open microphone: {e}") from e
        return FrameStream(stream, q, stop)


class FrameStream:
    """An open microphone. Iterate it for 80 ms frames. Unlike a generator it can be iterated again after a `break`
    (so wake-word listening and the request that follows share ONE stream and no audio is lost between them), can
    discard audio that piled up while NEXUS was busy, and releases the device on close()."""

    def __init__(self, stream: Any, q: queue.Queue[bytes], stop: Any) -> None:
        self._stream, self._q, self._stop = stream, q, stop

    def __iter__(self) -> FrameStream:
        return self

    def __next__(self) -> bytes:
        while not self._stop.is_set():
            try:
                return self._q.get(timeout=0.25)
            except queue.Empty:
                continue
        raise StopIteration

    def drain(self) -> int:
        n = 0
        while True:
            try:
                self._q.get_nowait()
                n += 1
            except queue.Empty:
                return n

    def close(self) -> None:
        for fn in (self._stream.stop, self._stream.close):
            try:
                fn()
            except Exception:  # noqa: S110 - closing a device that already failed must not raise
                pass


class Utterance:
    """End-of-speech detection for push-to-talk and wake-word follow-up capture.

    Speech is any frame clearly louder than the room: the limit adapts to background noise (a slowly tracked noise
    floor), so a fan or a quiet speaker does not break it. `extend()` lets the caller wait longer when the sentence
    sounds unfinished; new speech resets the wait to the normal pause length.
    """

    def __init__(self, threshold: float = 500.0, silence_s: float = 1.0, max_s: float = 20.0,
                 min_speech_s: float = 0.3, frame_s: float = FRAME_SAMPLES / SAMPLE_RATE,
                 adaptive: bool = False, wait_s: float | None = None) -> None:
        self.threshold, self.silence_s, self.max_s, self.min_speech_s, self.frame_s = (
            threshold, silence_s, max_s, min_speech_s, frame_s)
        self.base_silence_s = silence_s
        self.wait_s = wait_s  # give up if nobody speaks within this long
        self.adaptive = adaptive
        self.noise: float | None = None
        self.extensions = 0
        self.buf = bytearray()
        self.speech_s = 0.0
        self.silent_s = 0.0
        self.total_s = 0.0

    def _limit(self, level: float) -> float:
        if not self.adaptive:
            return self.threshold
        if self.noise is None:
            self.noise = min(level, 100.0)  # never start from a loud first frame: the user may already be talking
        elif level < self.noise:
            self.noise = level
        else:
            self.noise = min(level, self.noise * 1.01 + 1.0)  # drifts up slowly so speech never raises it much
        return max(200.0, self.noise * 3.0)

    def extend(self, extra_s: float) -> None:
        """The sentence sounds unfinished: require `extra_s` more silence before declaring it complete."""
        self.extensions += 1
        self.silence_s = self.silent_s + extra_s

    def feed(self, frame: bytes) -> bool:
        """Returns True when the utterance is complete."""
        self.buf.extend(frame)
        self.total_s += self.frame_s
        level = rms(frame)
        if level >= self._limit(level):
            self.speech_s += self.frame_s
            self.silent_s = 0.0
            self.silence_s = self.base_silence_s  # speech resumed: back to the normal pause length
        else:
            self.silent_s += self.frame_s
        if self.total_s >= self.max_s:
            return True
        if self.wait_s is not None and self.speech_s < self.min_speech_s and self.total_s >= self.wait_s:
            return True
        return self.speech_s >= self.min_speech_s and self.silent_s >= self.silence_s

    @property
    def has_speech(self) -> bool:
        return self.speech_s >= self.min_speech_s
