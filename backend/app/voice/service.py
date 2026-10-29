"""Voice orchestration: wake word / push-to-talk -> local STT -> assistant -> local TTS.

Every component is optional. If anything is missing, the reason is reported in `status()` and typed
chat keeps working. Audio is processed in memory only; it is never written to disk or stored.
"""

from __future__ import annotations

import asyncio
import logging
import math
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

from app.core.events import EventBus
from app.database.db import Database
from app.database.models import Conversation
from app.services.conversation import ConversationService, TurnRequest
from app.services.llm import LLMService
from app.services.preferences import VoicePrefs, load_preferences
from app.voice.audio import SAMPLE_RATE, AudioInput, MicUnavailable, SoundDeviceInput, Utterance, rms
from app.voice.endpoint import looks_complete
from app.voice.lexicon import Lexicon, build_lexicon
from app.voice.stt import STT
from app.voice.tts import TTS, NullTTS, PiperTTS, SapiTTS, SentenceChunker, play_ack, speakable
from app.voice.wakeword import WakeDetector, WhisperKeywordDetector
from app.voice.worker_client import SpeechWorker, WorkerOpenWakeWord, WorkerSTT, whisper_model_downloaded

log = logging.getLogger(__name__)

VOICE_CONVERSATION_TITLE = "Voice conversation"
MAX_EXTENSIONS = 2  # how many times an unfinished-sounding sentence may extend the wait
EXTENSION_S = 1.6  # extra silence required each time
MAX_FOLLOW_UPS = 8  # replies in a row without the wake phrase, so a conversation can flow but never runs away
FOLLOW_UP_MIN_CONFIDENCE = 0.3  # below this a follow-up is probably noise, not you
CAPTION_EVERY_S = 1.2  # live-caption refresh, in seconds of speech
END_PHRASES = {"thanks", "thank you", "thanks nexus", "thank you nexus", "that's all", "thats all", "that is all", "goodbye",
               "bye", "bye nexus", "stop listening", "never mind", "nevermind", "okay thanks", "ok thanks", "okay thank you",
               "no thanks", "that's it", "thats it", "all done", "i'm done", "im done"}


class VoiceError(Exception):
    pass


def make_tts(p: VoicePrefs) -> TTS:
    if p.tts_engine == "sapi":
        return SapiTTS(p.tts_rate, p.tts_voice)
    if p.tts_engine == "piper":
        return PiperTTS(p.piper_exe, p.piper_voice_model, p.tts_rate)
    return NullTTS()


def _close_tts(tts: TTS) -> None:
    close = getattr(tts, "close", None)
    (close if callable(close) else tts.stop)()


class VoiceService:
    def __init__(self, db: Database, bus: EventBus, conversations: ConversationService, llm: LLMService,
                 models_dir: Any, audio: AudioInput | None = None,
                 stt_factory: Callable[[VoicePrefs], STT] | None = None,
                 tts_factory: Callable[[VoicePrefs], TTS] | None = None,
                 wake_factory: Callable[[VoicePrefs, STT], WakeDetector] | None = None,
                 ack: Callable[[], None] = play_ack, worker: SpeechWorker | None = None) -> None:
        self.db, self.bus, self.conversations, self.llm = db, bus, conversations, llm
        self.audio = audio or SoundDeviceInput()
        self.models_dir = models_dir
        self.worker = worker or SpeechWorker()  # speech/wake-word native code runs in this separate process
        self.stt_factory = stt_factory or (lambda p: WorkerSTT(models_dir, p.stt_model, self.worker))
        self.tts_factory = tts_factory or make_tts
        self.wake_factory = wake_factory or self._default_wake
        self.ack = ack
        self.state = "disabled"
        self.muted = False
        self.paused = False
        self.last_transcript: str | None = None
        self.last_error: str | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._listen_stop = threading.Event()
        self._listen_thread: threading.Thread | None = None
        self._ptt_stop = threading.Event()
        self._busy = threading.Lock()
        self._tts: TTS = NullTTS()
        self._alert_tts: TTS | None = None  # used for reminders when normal voice output is off
        self._stt: STT | None = None
        self._current_gen: str | None = None
        self.prefs = VoicePrefs()
        # Speech output: one speaker thread plays queued pieces in order, so a reply can start being spoken while
        # the model is still writing it. `_speech_epoch` invalidates everything queued when the user interrupts.
        self._speech_q: queue.Queue[tuple[int, str]] = queue.Queue()
        self._speech_epoch = 0
        self._speech_pending = 0
        self._speech_cv = threading.Condition()
        self._speaker: threading.Thread | None = None
        self.last_confidence: float | None = None
        self._partial_stt: STT | None = None
        self._partial_name = ""
        self._caption_busy = threading.Event()
        self.wake_attempts: deque[dict[str, Any]] = deque(maxlen=12)  # what the wake detector heard, newest last

    # ---------- lifecycle ----------
    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.reload()

    def reload(self) -> None:
        """Apply new voice settings. A running wake-word listener is restarted with the new settings."""
        with self.db.session() as s:
            self.prefs = load_preferences(s).voice
        self.worker.reset_breaker()  # applying settings is also the user's "try again"
        was_listening = bool(self._listen_thread and self._listen_thread.is_alive())
        if was_listening:
            self.stop_listening()
        self.cancel_speech()
        _close_tts(self._tts)
        self._stt = self.stt_factory(self.prefs)
        self._tts = self.tts_factory(self.prefs)
        if not self.prefs.enabled:
            self._set("disabled")
            return
        if self.prefs.speak_responses and hasattr(self._tts, "prewarm"):
            self._tts.prewarm()  # start the speech host now, not when the first answer is ready
        newly_enabled = self.state == "disabled"
        if newly_enabled:
            self._set("idle")
        auto = newly_enabled and self.prefs.listening_mode == "wake_word"
        if (was_listening or auto) and not self.muted and not self.paused:
            try:
                self.start_listening()
            except VoiceError as e:
                self._error(str(e))

    def announce(self, text: str) -> bool:
        """Speak an alert (a reminder ringing) even when the microphone side of voice is switched off. Returns whether
        anything could be spoken."""
        text = speakable(text)
        if not text:
            return False
        if self._tts.available()[0]:
            self.say(text)
            return True
        try:
            if self._alert_tts is None:
                self._alert_tts = make_tts(self.prefs)
            if self._alert_tts.available()[0]:
                threading.Thread(target=self._alert_tts.speak, args=(text,), name="nexus-alert-speech", daemon=True).start()
                return True
        except Exception as e:
            log.info("could not speak the alert: %s", e)
        return False

    def shutdown(self) -> None:
        if self._alert_tts is not None:
            _close_tts(self._alert_tts)
        self.stop_listening()
        self._ptt_stop.set()
        self.cancel_speech()
        _close_tts(self._tts)
        self.worker.close()

    def download_model(self, name: str) -> str:
        """Explicit, user-initiated Whisper download; runs inside the speech worker."""
        return str(WorkerSTT(self.models_dir, name, self.worker).download())

    def _set(self, state: str) -> None:
        self.state = state
        self.bus.publish("voice", state=state, muted=self.muted, paused=self.paused)

    def _error(self, msg: str) -> None:
        self.last_error = msg
        log.warning("voice: %s", msg)
        self.bus.publish("voice", state=self.state, error=msg)

    def status(self) -> dict[str, Any]:
        mic_ok, mic_why = self.audio.available()
        stt_ok, stt_why = self._stt.available() if self._stt else (False, "not configured")
        tts_ok, tts_why = self._tts.available()
        return {
            "enabled": self.prefs.enabled, "state": self.state, "muted": self.muted, "paused": self.paused,
            "listening_mode": self.prefs.listening_mode,
            "wake_listening": bool(self._listen_thread and self._listen_thread.is_alive()),
            "microphone": {"available": mic_ok, "detail": mic_why},
            "stt": {"available": stt_ok, "detail": stt_why, "model": self.prefs.stt_model},
            "tts": {"available": tts_ok, "detail": tts_why, "engine": self.prefs.tts_engine},
            "wake": {"engine": self.prefs.wake_engine, "phrase": self.prefs.wake_phrase},
            "last_transcript": self.last_transcript, "last_error": self.last_error, "last_confidence": self.last_confidence,
            "wake_attempts": list(reversed(self.wake_attempts)),
        }

    # ---------- controls ----------
    def _check_ready(self) -> None:
        if not self.prefs.enabled:
            raise VoiceError("Voice is disabled. Enable it in Voice settings.")
        if self.muted:
            raise VoiceError("Microphone is muted")
        if self.paused:
            raise VoiceError("Assistant is paused")

    def set_muted(self, muted: bool) -> None:
        self.muted = muted
        if muted:
            self.stop_listening()
            self._ptt_stop.set()
        self._set("muted" if muted else ("idle" if self.prefs.enabled else "disabled"))

    def set_paused(self, paused: bool) -> None:
        self.paused = paused
        if paused:
            self.stop_listening()
            self.interrupt()
        self._set("paused" if paused else ("idle" if self.prefs.enabled else "disabled"))

    def start_listening(self) -> None:
        self._check_ready()
        if self._listen_thread and self._listen_thread.is_alive():
            return
        stt = self._stt
        assert stt is not None
        try:
            detector = self.wake_factory(self.prefs, stt)
        except Exception as e:
            raise VoiceError(f"Wake word unavailable: {e}. Push-to-talk and typing still work.") from e
        ok, why = self.audio.available()
        if not ok:
            raise VoiceError(f"Microphone unavailable: {why}")
        self._listen_stop.clear()
        threading.Thread(target=self._prewarm_stt, args=(stt,), name="nexus-stt-warm", daemon=True).start()
        self._listen_thread = threading.Thread(target=self._wake_loop, args=(detector,), name="nexus-wake",
                                               daemon=True)
        self._listen_thread.start()
        self._set("listening_for_wake")

    def stop_listening(self) -> None:
        self._listen_stop.set()
        t = self._listen_thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3)
        self._listen_thread = None
        if self.state == "listening_for_wake":
            self._set("idle")

    def interrupt(self) -> None:
        self.cancel_speech()
        if self._current_gen:
            self.llm.stop(self._current_gen)
        self._ptt_stop.set()

    def _prewarm_stt(self, stt: STT) -> None:
        """Load the speech model while the user is not talking yet, so the first request is not slow."""
        try:
            if stt.available()[0]:
                stt.transcribe_pcm(b"\x00\x00" * 8000)
        except Exception as e:
            log.info("speech model warm-up failed: %s", e)

    def _default_wake(self, p: VoicePrefs, stt: STT) -> WakeDetector:
        if p.wake_engine == "openwakeword":
            return WorkerOpenWakeWord(self.worker, p.openwakeword_model, p.wake_threshold)
        ok, why = stt.available()
        if not ok:
            raise RuntimeError(why)
        # Optional lighter model just for spotting the phrase (off by default: accuracy matters more than a little CPU).
        light = p.wake_stt_model
        if light and light != p.stt_model and whisper_model_downloaded(self.models_dir, light):
            stt = WorkerSTT(self.models_dir, light, self.worker)
        return WhisperKeywordDetector(stt, p.wake_phrase, on_attempt=self._note_wake_attempt)

    def _note_wake_attempt(self, text: str, score: float, accepted: bool) -> None:
        self.wake_attempts.append({"ts": time.time(), "heard": text, "score": round(score, 2), "accepted": accepted})
        self.bus.publish("voice", state="wake_attempt", heard=text, accepted=accepted)

    # ---------- capture ----------
    def _wake_loop(self, detector: WakeDetector) -> None:
        backoff = 1.0
        while not self._listen_stop.is_set():
            stream: Any = None
            try:
                # ONE microphone stream serves both wake-word listening and the request that follows. Opening a second
                # stream after the wake phrase lost whatever you said while the phrase was being recognised.
                stream = self.audio.frames(self.prefs.input_device, self._listen_stop)
                for frame in stream:
                    hit = detector.process(frame)
                    if hit is None:
                        continue
                    if self.prefs.ack_sound:
                        try:
                            self.ack()
                        except Exception:
                            log.debug("ack sound failed", exc_info=True)
                    self.bus.publish("voice", state="wake_detected", score=hit.score)
                    # Words spoken in the same breath as the wake phrase are only the start of the request: keep
                    # listening so a pause mid-sentence does not cut the request short.
                    prefix = hit.remainder if hit.remainder and len(hit.remainder.split()) >= 2 else ""
                    text = self._record_utterance(self._listen_stop, prefix=prefix, frames=stream)
                    if text:
                        self._dispatch(text)
                        self._follow_up(stream)
                    detector.reset()
                    self._drain(stream)
                    if not self._listen_stop.is_set():
                        self._set("listening_for_wake")
                backoff = 1.0
            except MicUnavailable as e:
                self._error(f"Microphone error: {e}. Retrying in {backoff:.0f}s")
                self._listen_stop.wait(backoff)
                backoff = min(backoff * 2, 30)
            except Exception as e:
                log.exception("wake loop error")
                self._error(f"Listening error: {e}")
                self._listen_stop.wait(backoff)
                backoff = min(backoff * 2, 30)
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()

    @staticmethod
    def _drain(stream: Any) -> None:
        drain = getattr(stream, "drain", None)
        if callable(drain):
            drain()  # audio that piled up while NEXUS was answering (including its own voice) is not input

    def _follow_up(self, stream: Any) -> None:
        """Like a voice chat: after an answer, keep listening for a few seconds so you can just reply, with no wake phrase.
        Stops on silence, on "thanks"/"that's all", or when what it hears is probably noise."""
        p = self.prefs
        turns = 0
        while p.follow_up_s > 0 and turns < MAX_FOLLOW_UPS and not self._listen_stop.is_set() and not self.paused:
            self._drain(stream)  # NEXUS's own voice must not count as your reply
            self.bus.publish("voice", state="following_up", seconds=p.follow_up_s)
            text = self._record_utterance(self._listen_stop, frames=stream, wait_override=p.follow_up_s)
            words = re.sub(r"[^a-z' ]", "", text.lower()).split()
            if not words:
                return
            if " ".join(words) in END_PHRASES:
                self.bus.publish("voice", state="follow_up_ended", text=text)
                return
            if self.last_confidence is not None and self.last_confidence < FOLLOW_UP_MIN_CONFIDENCE:
                return  # TV, music or a cough: do not answer it
            self._dispatch(text)
            turns += 1

    def _caption(self, pcm: bytes, lex: Lexicon | None) -> None:
        """Show what you have said so far while you are still speaking (a light model, one at a time, never blocks recording)."""
        if self._caption_busy.is_set():
            return
        stt = self._partial_stt_for()
        if stt is None:
            return
        self._caption_busy.set()

        def run() -> None:
            try:
                text = stt.transcribe_pcm(pcm).strip()
                if text and not self._listen_stop.is_set():
                    self.bus.publish("caption", text=lex.snap(text) if lex else text, final=False)
            except Exception:
                log.debug("live caption failed", exc_info=True)
            finally:
                self._caption_busy.clear()

        threading.Thread(target=run, name="nexus-caption", daemon=True).start()

    def _partial_stt_for(self) -> STT | None:
        p = self.prefs
        if not p.live_captions or not p.partial_stt_model or p.partial_stt_model == p.stt_model:
            return None
        if self._partial_stt is None or self._partial_name != p.partial_stt_model:
            if not whisper_model_downloaded(self.models_dir, p.partial_stt_model):
                return None  # never trigger a download in the middle of a sentence
            self._partial_stt = WorkerSTT(self.models_dir, p.partial_stt_model, self.worker)
            self._partial_name = p.partial_stt_model
        return self._partial_stt

    def _record_utterance(self, outer_stop: threading.Event, until_released: bool = False, prefix: str = "",
                          frames: Iterator[bytes] | None = None, wait_override: float | None = None) -> str:
        """Record until you are really done (or until PTT release), then transcribe. Audio stays in memory.

        "Done" is not just silence: after the pause, a quick transcript is checked and, if the sentence sounds
        unfinished ("...and", "...because", a trailing comma), NEXUS keeps listening a little longer (smart_endpoint).
        `prefix` is text already heard (words said right after the wake phrase); it is joined to what follows.
        """
        p = self.prefs
        self._set("recording")
        stop = threading.Event()
        pause = p.pause_tolerance_s
        if prefix:  # short confirm window if the prefix already reads as finished, normal wait otherwise
            pause = min(pause, 0.9) if looks_complete(prefix) else pause
        # How long to wait for speech to begin: just "Hey Nexus" -> 6 s; the phrase plus a finished command -> a short
        # confirm window (nothing more is coming); the phrase plus half a command -> 4 s for the rest.
        wait: float | None = None
        if not until_released:
            wait = (min(pause, 0.9) + 0.5) if prefix and looks_complete(prefix) else 4.0 if prefix else 6.0
        if wait_override is not None and not until_released:
            wait = wait_override
        utt = Utterance(silence_s=3600 if until_released else pause, max_s=3600 if until_released else p.max_utterance_s,
                        min_speech_s=0.3, adaptive=True, wait_s=wait)
        text: str | None = None
        source: Any = frames
        captions = p.live_captions and self._partial_stt_for() is not None
        cap_lex = self._lexicon() if captions else None
        last_cap = 0.0
        try:
            if source is None:
                source = self.audio.frames(p.input_device, stop)
            for frame in source:
                done = utt.feed(frame)
                if captions and not done and utt.has_speech and utt.total_s - last_cap >= CAPTION_EVERY_S:
                    last_cap = utt.total_s
                    self._caption(bytes(utt.buf), cap_lex)
                if outer_stop.is_set() or (until_released and self._ptt_stop.is_set()):
                    stop.set()
                    break
                if not done:
                    continue
                if until_released or utt.total_s >= utt.max_s or not utt.has_speech:
                    stop.set()
                    break
                heard = self._transcribe_quiet(lambda: self._final_text(bytes(utt.buf)))  # type: ignore[union-attr]
                self._set("recording")
                full = f"{prefix} {heard}".strip()
                if not p.smart_endpoint or looks_complete(full) or utt.extensions >= MAX_EXTENSIONS:
                    text = heard
                    stop.set()
                    break
                utt.extend(EXTENSION_S)  # sounds unfinished: wait a little longer
        except MicUnavailable as e:
            self._error(str(e))
            self._set("idle")
            return prefix
        finally:
            if frames is None:  # only a stream opened here is closed here; the wake loop owns the shared one
                close = getattr(source, "close", None)
                if callable(close):
                    close()
        if text is None:
            if not utt.has_speech:
                self._set("idle")
                return prefix
            text = self._transcribe(lambda: self._final_text(bytes(utt.buf)))  # type: ignore[union-attr]
        else:
            self.last_transcript = f"{prefix} {text}".strip()
            self.bus.publish("voice", state="transcribed", text=self.last_transcript)
        return f"{prefix} {text}".strip()

    def _lexicon(self) -> Lexicon:
        """The user's own words (apps, people, custom words), rebuilt per request so changes apply at once."""
        from app.database.models import Contact
        from app.services import appcontrol

        with self.db.session() as s:
            prefs = load_preferences(s)
            names = [c.name for c in s.query(Contact).all()]
            aliases = [a for c in s.query(Contact).all() for a in (c.aliases or [])]
        return build_lexicon(sorted(appcontrol.app_names(prefs)), [*names, *aliases], prefs.voice.custom_words,
                             prefs.assistant_name)

    def _final_text(self, pcm: bytes) -> str:
        """Full-quality transcription of a request: vocabulary hints in, mishearings repaired, confidence remembered."""
        stt = self._stt
        assert stt is not None
        lx = self._lexicon()
        setter = getattr(stt, "set_vocab", None)
        if callable(setter):
            setter(lx.prompt())
        detail = getattr(stt, "transcribe_detail", None)
        if callable(detail):
            text, conf = detail(pcm)
        else:
            text, conf = stt.transcribe_pcm(pcm), None
        self.last_confidence = conf
        return lx.snap(text)

    def _transcribe_quiet(self, fn: Callable[[], str]) -> str:
        """Transcribe without announcing it (used for end-of-sentence checks; errors surface in the final pass)."""
        try:
            return fn().strip()
        except Exception as e:
            self._error(f"Speech recognition failed: {e}")
            return ""

    def _transcribe(self, fn: Callable[[], str], reraise: bool = False) -> str:
        self._set("transcribing")
        try:
            text = fn().strip()
        except Exception as e:
            self._error(f"Speech recognition failed: {e}")
            self._set("idle")
            if reraise:  # browser push-to-talk: the user must see the real reason, not "no speech recognised"
                raise
            return ""
        self.last_transcript = text
        self.bus.publish("voice", state="transcribed", text=text)
        return text

    def ptt_start(self) -> None:
        """Server-side push-to-talk (tray / hotkey). The browser UI can also upload recorded audio."""
        self._check_ready()
        if not self._busy.acquire(blocking=False):
            raise VoiceError("Voice is busy")
        self._ptt_stop.clear()

        def run() -> None:
            try:
                text = self._record_utterance(threading.Event(), until_released=True)
                if text:
                    self._dispatch(text)
            finally:
                self._busy.release()
                if self.state not in ("disabled", "muted", "paused"):
                    self._set("listening_for_wake" if self._listen_thread else "idle")

        threading.Thread(target=run, name="nexus-ptt", daemon=True).start()

    def ptt_stop(self) -> None:
        self._ptt_stop.set()

    async def transcribe_upload(self, data: bytes) -> str:
        self._check_ready()
        stt = self._stt
        assert stt is not None
        ok, why = stt.available()
        if not ok:
            raise VoiceError(why)
        text = await asyncio.to_thread(self._transcribe, lambda: self._lexicon().snap(stt.transcribe_file(data)), True)
        if self.state not in ("disabled", "muted", "paused"):
            self._set("idle")
        return text

    # ---------- microphone test ----------
    def test_microphone(self, max_s: float = 8.0) -> dict[str, Any]:
        """Record one short phrase and report what NEXUS hears: level, background noise, the transcript and how long
        recognition took, with plain advice. Audio is kept in memory only. Runs on a worker thread."""
        if not self._busy.acquire(blocking=False):
            raise VoiceError("NEXUS is busy listening or answering. Try again in a moment.")
        was_listening = bool(self._listen_thread and self._listen_thread.is_alive())
        try:
            self._check_ready()
            ok, why = self.audio.available()
            if not ok:
                raise VoiceError(f"Microphone unavailable: {why}")
            if self._stt is None or not self._stt.available()[0]:
                raise VoiceError(self._stt.available()[1] if self._stt else "Speech recognition is not set up")
            if was_listening:
                self.stop_listening()  # one program holding the microphone at a time
            self._set("recording")
            stop = threading.Event()
            utt = Utterance(silence_s=1.0, max_s=max_s, adaptive=True, wait_s=6.0)
            stream: Any = self.audio.frames(self.prefs.input_device, stop)
            noise_levels: list[float] = []
            try:
                for frame in stream:
                    if not utt.has_speech:
                        noise_levels.append(rms(frame))
                    if utt.feed(frame):
                        break
            finally:
                stop.set()
                close = getattr(stream, "close", None)
                if callable(close):
                    close()
            pcm = bytes(utt.buf)
            return self._analyse_test(pcm, utt, noise_levels)
        finally:
            self._busy.release()
            if self.state not in ("disabled", "muted", "paused"):
                self._set("idle")
            if was_listening and not self.muted and not self.paused:
                try:
                    self.start_listening()
                except VoiceError as e:
                    self._error(str(e))

    def _analyse_test(self, pcm: bytes, utt: Utterance, noise_levels: list[float]) -> dict[str, Any]:
        import array

        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        peak = max((abs(x) for x in samples), default=0) / 32768.0
        level = rms(pcm) / 32768.0
        db = round(20 * math.log10(level), 1) if level > 0 else -90.0
        noise = (sorted(noise_levels)[len(noise_levels) // 2] / 32768.0) if noise_levels else 0.0
        seconds = round(len(samples) / SAMPLE_RATE, 1)
        out: dict[str, Any] = {"peak": round(peak, 3), "level_db": db, "noise": round(noise, 4), "seconds": seconds,
                               "heard": "", "recognise_s": None, "advice": []}
        advice: list[str] = out["advice"]
        if not utt.has_speech:
            advice.append("I didn't hear anyone speak. Check the right microphone is selected, that it isn't muted in "
                          "Windows, and that Windows lets desktop apps use it (Settings -> Privacy -> Microphone).")
            return out
        t0 = time.time()
        out["heard"] = self._transcribe_quiet(lambda: self._final_text(pcm))
        out["confidence"] = self.last_confidence
        out["recognise_s"] = round(time.time() - t0, 1)
        self.last_transcript = out["heard"] or self.last_transcript
        if peak < 0.05:
            advice.append("Your voice is very quiet. Raise the input level: Windows Settings -> System -> Sound -> Input "
                          "-> your microphone -> Input volume (aim for 70-90), or sit closer. NEXUS boosts quiet audio, "
                          "but a very weak signal still hurts accuracy.")
        elif peak < 0.15:
            advice.append("Your voice is a little quiet. Raising the Windows input volume or moving closer will help.")
        if peak > 0.97:
            advice.append("The signal is clipping (too loud). Lower the Windows input volume or turn off 'microphone boost'.")
        if noise > 0.03:
            advice.append("There is a lot of background noise (fan, music, TV). Move away from it or use a headset microphone.")
        if not out["heard"]:
            advice.append("Speech was detected but nothing was recognised. Try speaking a little slower and closer, or "
                          "switch to a larger recognition model (Voice -> Speech recognition models -> small.en).")
        if out["recognise_s"] and seconds and out["recognise_s"] > max(3.0, seconds * 1.5):
            advice.append(f"Recognition took {out['recognise_s']} s for {seconds} s of speech, which is slow. A smaller model "
                          "(tiny.en or base.en) is faster; small.en is more accurate but slower on this CPU.")
        if not advice:
            advice.append("Level and recognition look good.")
        return out

    # ---------- respond ----------
    def _dispatch(self, text: str) -> None:
        if self._loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(self.respond(text), self._loop)
        try:
            fut.result(timeout=900)
        except Exception as e:
            self._error(f"Could not answer: {e}")

    def voice_conversation_id(self) -> int:
        with self.db.session() as s:
            conv = (s.query(Conversation).filter(Conversation.title == VOICE_CONVERSATION_TITLE,
                                                 Conversation.mode == "assistant")
                    .order_by(Conversation.id.desc()).first())
            if not conv:
                conv = Conversation(title=VOICE_CONVERSATION_TITLE, mode="assistant")
                s.add(conv)
                s.flush()
            return conv.id

    async def respond(self, text: str, conversation_id: int | None = None, speak: bool | None = None) -> str:
        """Answer a spoken request. Voice can only trigger non-destructive assistant actions.

        The reply is spoken sentence by sentence while the model is still writing it."""
        conv_id = conversation_id if conversation_id is not None else await asyncio.to_thread(self.voice_conversation_id)
        self._set("thinking")
        should_speak = self.prefs.speak_responses if speak is None else speak
        speaking = bool(should_speak and not self.paused and self._tts.available()[0])
        chunker = SentenceChunker()
        epoch = self._speech_epoch
        parts: list[str] = []
        error = None
        turn = TurnRequest(conversation_id=conv_id, content=text, source="voice", confidence=self.last_confidence)
        async for ev in self.conversations.respond(turn):
            if ev["event"] == "meta":
                self._current_gen = ev.get("generation_id")
                self.bus.publish("voice", state="thinking", conversation_id=conv_id)
            elif ev["event"] == "token":
                parts.append(ev["text"])
                if speaking:
                    for piece in chunker.feed(ev["text"]):
                        self.say(piece, epoch)
            elif ev["event"] == "error":
                error = ev["message"]
        self._current_gen = None
        answer = "".join(parts).strip() or (error or "")
        self.bus.publish("voice", state="answered", conversation_id=conv_id, text=answer[:4000])
        if speaking and not parts and error:
            chunker.feed(error)
        if speaking:
            for piece in chunker.flush():
                self.say(piece, epoch)
            await asyncio.to_thread(self.wait_speech)
        if self.state not in ("disabled", "muted", "paused"):
            self._set("idle")
        return answer

    # ---------- speech output queue ----------
    def say(self, text: str, epoch: int | None = None) -> None:
        """Queue text to be spoken in order. Returns immediately."""
        ok, _ = self._tts.available()
        if not ok or not text.strip():
            return
        with self._speech_cv:
            self._speech_pending += 1
        self._speech_q.put((self._speech_epoch if epoch is None else epoch, text))
        if self._speaker is None or not self._speaker.is_alive():
            self._speaker = threading.Thread(target=self._speak_loop, name="nexus-speaker", daemon=True)
            self._speaker.start()

    def _speak_loop(self) -> None:
        while True:
            try:
                epoch, text = self._speech_q.get(timeout=30)
            except queue.Empty:
                return  # idle: the thread exits and is started again on demand
            try:
                if epoch == self._speech_epoch and not self.paused:
                    if self.state not in ("disabled", "muted", "paused"):
                        self._set("speaking")
                    self._tts.speak(text)
            except Exception as e:
                self._error(f"Speech output failed: {e}")
                self.cancel_speech()
            finally:
                with self._speech_cv:
                    self._speech_pending -= 1
                    self._speech_cv.notify_all()
                if self._speech_pending <= 0 and self.state == "speaking":
                    listening = bool(self._listen_thread and self._listen_thread.is_alive())
                    self._set("listening_for_wake" if listening else "idle")

    def wait_speech(self, timeout: float = 600.0) -> None:
        """Block until everything queued has been spoken (or dropped)."""
        with self._speech_cv:
            self._speech_cv.wait_for(lambda: self._speech_pending <= 0, timeout=timeout)

    def cancel_speech(self) -> None:
        """Stop talking now and drop everything still queued."""
        self._speech_epoch += 1
        self._tts.stop()

    async def speak(self, text: str, wait: bool = True) -> None:
        ok, why = self._tts.available()
        if not ok:
            raise VoiceError(why)
        self.say(speakable(text))
        if wait:
            await asyncio.to_thread(self.wait_speech)
            if self.state == "speaking":
                self._set("idle")
