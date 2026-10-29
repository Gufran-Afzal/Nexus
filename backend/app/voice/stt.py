"""Local speech-to-text via faster-whisper (MIT). Models are only downloaded on explicit request."""

from __future__ import annotations

import io
import logging
import re
import threading
from pathlib import Path
from typing import Any, Protocol

from app.voice.probe import package_installed

log = logging.getLogger(__name__)

KNOWN_MODELS = ["tiny.en", "base.en", "small.en", "distil-small.en", "distil-medium.en", "distil-large-v3",
                "tiny", "base", "small"]
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


class STTUnavailable(Exception):
    pass


class STT(Protocol):
    def available(self) -> tuple[bool, str]: ...
    def transcribe_pcm(self, pcm: bytes) -> str: ...
    def transcribe_file(self, data: bytes) -> str: ...


def faster_whisper_installed() -> bool:
    return package_installed("faster_whisper")  # find_spec only: importing native libs here could crash the server


class FasterWhisperSTT:
    def __init__(self, models_dir: Path, model_name: str = "base.en", compute_type: str = "int8",
                 cpu_threads: int = 4) -> None:
        if not _SAFE_NAME.match(model_name):
            raise ValueError("Invalid model name")
        self.compute_type, self.cpu_threads = compute_type, cpu_threads
        self.models_dir = models_dir / "whisper"
        self.model_name = model_name
        self._model: Any = None
        self._lock = threading.Lock()

    @property
    def model_path(self) -> Path:
        return self.models_dir / self.model_name

    def downloaded(self) -> bool:
        return (self.model_path / "model.bin").exists()

    def available(self) -> tuple[bool, str]:
        if not faster_whisper_installed():
            return False, "faster-whisper is not installed (pip install faster-whisper)"
        if not self.downloaded():
            return False, f"Speech model '{self.model_name}' has not been downloaded yet"
        return True, "ok"

    def download(self) -> Path:
        """Explicit, user-initiated download from Hugging Face (the only network use of this module)."""
        if not faster_whisper_installed():
            raise STTUnavailable("faster-whisper is not installed")
        from faster_whisper import download_model

        self.model_path.mkdir(parents=True, exist_ok=True)
        download_model(self.model_name, output_dir=str(self.model_path))
        return self.model_path

    def _load(self) -> Any:
        ok, why = self.available()
        if not ok:
            raise STTUnavailable(why)
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                # Loading from a local directory never touches the network.
                self._model = WhisperModel(str(self.model_path), device="cpu", compute_type=self.compute_type,
                                           cpu_threads=self.cpu_threads, local_files_only=True)
            return self._model

    def unload(self) -> None:
        with self._lock:
            self._model = None

    def _run(self, audio: Any, mode: str = "final", phrase: str = "", vocab: str = "") -> tuple[str, float | None]:
        model = self._load()
        lang = "en" if self.model_name.endswith(".en") or self.model_name == "distil-large-v3" else None
        audio = normalize_audio(audio)
        segments, _info = model.transcribe(audio, language=lang, **decode_options(mode, phrase, vocab or self.vocab))
        segs = list(segments)
        text = clean_transcript(" ".join(seg.text.strip() for seg in segs).strip(), mode)
        return text, confidence_of(segs) if text else None

    vocab: str = ""  # the user's own words (apps, people), set by the voice service

    def set_vocab(self, vocab: str) -> None:
        self.vocab = vocab

    def transcribe_detail(self, pcm: bytes, mode: str = "final", phrase: str = "", vocab: str = "") -> tuple[str, float | None]:
        import numpy as np

        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        return self._run(audio, mode, phrase, vocab)

    def transcribe_pcm(self, pcm: bytes, mode: str = "final", phrase: str = "", vocab: str = "") -> str:
        return self.transcribe_detail(pcm, mode, phrase, vocab)[0]

    def transcribe_file(self, data: bytes) -> str:
        return self._run(decode_audio_bytes(data))[0]


# Words NEXUS should expect. Whisper uses this as context ("initial prompt"), which makes it far more likely to write
# "Nexus" instead of "next us" and to spell the everyday assistant vocabulary correctly. It is not an instruction.
VOCAB_PROMPT = (
    "Hey Nexus. Set a timer for ten minutes. Remind me tomorrow at 9 a.m. to call the dentist. "
    "Take a note. Add milk to my to-do list. What's on my agenda today? Open Notepad."
)

# Phrases Whisper invents from silence or noise. Never a real request.
_HALLUCINATIONS = (
    "thanks for watching", "thank you for watching", "please subscribe", "subtitles by", "amara org", "like and subscribe",
)
_WAKE_NOISE = {"you", "thank you", "thanks", "bye", "bye bye", "so", "the", "okay", "ok", "uh", "um", "hmm", "oh"}


def confidence_of(segments: list[Any]) -> float | None:
    """0..1: how sure Whisper was, from its own average log-probability (duration-weighted over the segments), reduced
    when it thinks part of the clip may not be speech. Roughly: above 0.75 reliable, below 0.5 worth double-checking."""
    import math

    total = weight = 0.0
    for seg in segments:
        lp = getattr(seg, "avg_logprob", None)
        if lp is None:
            continue
        w = max(0.1, float(getattr(seg, "end", 0.0)) - float(getattr(seg, "start", 0.0)))
        ns = float(getattr(seg, "no_speech_prob", 0.0) or 0.0)
        total += math.exp(min(0.0, float(lp))) * (1.0 - 0.5 * ns) * w
        weight += w
    return round(total / weight, 3) if weight else None


def decode_options(mode: str, phrase: str = "", vocab: str = "") -> dict[str, Any]:
    """faster-whisper settings. "final" (a whole request) favours accuracy; "wake" (a short clip that may contain the
    wake phrase) is cheap, primed with the phrase, and skips the speech filter because the caller already found speech."""
    if mode == "wake":
        first = (phrase.split(",")[0].strip() or "Hey Nexus").title()
        return {"beam_size": 3, "best_of": 1, "temperature": 0.0, "vad_filter": False,
                "condition_on_previous_text": False, "without_timestamps": True,
                "initial_prompt": f"{first}. {first}, what time is it?", "hotwords": first}
    opts = {"beam_size": 5, "best_of": 1, "temperature": (0.0, 0.2, 0.4), "condition_on_previous_text": False,
            "without_timestamps": True, "no_speech_threshold": 0.6, "compression_ratio_threshold": 2.4,
            "log_prob_threshold": -1.0, "suppress_blank": True, "initial_prompt": vocab or VOCAB_PROMPT,
            # The default speech filter is tuned for clean audio and can cut quiet speech; this one is gentler.
            "vad_filter": True, "vad_parameters": {"threshold": 0.35, "min_silence_duration_ms": 700, "speech_pad_ms": 500}}
    if vocab:  # the user's own words are also given as hotwords, which biases decoding towards their spelling
        opts["hotwords"] = " ".join(vocab.split("Names and apps:")[-1].replace(".", " ").replace(",", " ").split()[:24])
    return opts


def normalize_audio(audio: Any) -> Any:
    """Bring quiet recordings up to a normal level and remove DC offset. Laptop microphones are often far too quiet
    for Whisper; a clip peaking at 5% of full scale is transcribed much worse than the same clip at 60%."""
    import numpy as np

    if audio.size == 0:
        return audio
    a = audio.astype(np.float32, copy=False)
    a = a - float(np.mean(a))
    peak = float(np.percentile(np.abs(a), 99.5))
    if peak < 1e-4:  # digital silence: amplifying it would only create noise
        return a
    gain = min(25.0, max(1.0, 0.6 / peak))
    return np.clip(a * gain, -1.0, 1.0)


def clean_transcript(text: str, mode: str = "final") -> str:
    t = text.strip()
    low = re.sub(r"[^a-z ]+", " ", t.lower()).strip()
    low = " ".join(low.split())
    if any(h in low for h in _HALLUCINATIONS):
        return ""
    if mode == "wake" and low in _WAKE_NOISE:
        return ""
    return t


def decode_audio_bytes(data: bytes, rate: int = 16_000) -> Any:
    """Decode wav/webm/ogg/mp3 (e.g. a browser MediaRecorder upload) to mono float32 at 16 kHz, locally.

    NEXUS decodes with PyAV itself instead of faster_whisper.decode_audio, because faster-whisper 1.2.1
    passes an argument (`metadata_errors`) that PyAV 19 removed, which breaks every file transcription.
    """
    import av
    import numpy as np

    chunks: list[Any] = []
    with av.open(io.BytesIO(data), mode="r") as container:
        stream = next((st for st in container.streams if st.type == "audio"), None)
        if stream is None:
            raise ValueError("The recording contains no audio")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in container.decode(stream):
            if not isinstance(frame, av.AudioFrame):
                continue
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0
