from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import get_state
from app.core.state import NexusState
from app.services.audit import audit
from app.voice.service import VoiceError
from app.voice.stt import KNOWN_MODELS, faster_whisper_installed
from app.voice.tts import SapiTTS
from app.voice.wakeword import openwakeword_installed
from app.voice.worker_client import SpeechEngineError, whisper_model_downloaded

router = APIRouter(prefix="/api/voice")
MAX_UPLOAD = 15 * 1024 * 1024


def _wrap(fn: Any) -> dict[str, Any]:
    try:
        fn()
    except VoiceError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.get("/status")
def status(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    st = state.voice.status()
    st["components"] = {"faster_whisper": faster_whisper_installed(), "openwakeword": openwakeword_installed()}
    st["stt_models"] = [
        {"name": n, "downloaded": whisper_model_downloaded(state.settings.models_dir, n)} for n in KNOWN_MODELS
    ]
    return st


@router.get("/devices")
def devices(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    try:
        return {"available": True, "devices": state.voice.audio.devices()}
    except Exception as e:
        return {"available": False, "devices": [], "error": str(e)}


_voices_cache: tuple[float, list[str]] | None = None


@router.get("/tts/voices")
async def voices() -> list[str]:
    """Installed Windows voices. Listing spawns PowerShell (slow), so the answer is cached for 5 minutes."""
    global _voices_cache
    now = time.monotonic()
    if _voices_cache and now - _voices_cache[0] < 300:
        return _voices_cache[1]
    found = await asyncio.to_thread(SapiTTS.voices)
    if found:
        _voices_cache = (now, found)
    return found


@router.post("/listen/start")
def listen_start(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(state.voice.start_listening)


@router.post("/listen/stop")
def listen_stop(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(state.voice.stop_listening)


@router.post("/mute")
def mute(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(lambda: state.voice.set_muted(True))


@router.post("/unmute")
def unmute(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(lambda: state.voice.set_muted(False))


@router.post("/ptt/start")
def ptt_start(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(state.voice.ptt_start)


@router.post("/ptt/stop")
def ptt_stop(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(state.voice.ptt_stop)


@router.post("/interrupt")
def interrupt(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    return _wrap(state.voice.interrupt)


@router.post("/transcribe")
async def transcribe(request: Request, state: NexusState = Depends(get_state)) -> dict[str, str]:
    """Transcribe audio recorded by the browser (push-to-talk in the UI). Processed in memory only."""
    data = await request.body()
    if not data:
        raise HTTPException(400, "No audio received")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Recording too long")
    try:
        text = await state.voice.transcribe_upload(data)
    except VoiceError as e:
        raise HTTPException(409, str(e)) from e
    except SpeechEngineError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"Transcription failed: {e}") from e
    return {"text": text}


class SpeakIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)
    wait: bool = True  # False = queue it and return at once (used to speak a reply sentence by sentence)


@router.post("/speak")
async def speak(body: SpeakIn, state: NexusState = Depends(get_state)) -> dict[str, bool]:
    try:
        await state.voice.speak(body.text, wait=body.wait)
    except Exception as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


class DownloadIn(BaseModel):
    model: str


@router.post("/stt/download")
async def download_stt(body: DownloadIn, state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Explicit, user-initiated download of a Whisper model (requires internet once)."""
    if body.model not in KNOWN_MODELS:
        raise HTTPException(422, f"Choose one of: {', '.join(KNOWN_MODELS)}")
    with state.db.session() as s:
        audit(s, "user", "voice.model_download", body.model)
    try:
        path = await asyncio.to_thread(state.voice.download_model, body.model)
    except SpeechEngineError as e:
        raise HTTPException(409, str(e)) from e
    except Exception as e:
        raise HTTPException(502, f"Download failed: {e}") from e
    state.voice.reload()
    return {"path": str(path)}


@router.post("/test")
async def test_microphone(state: NexusState = Depends(get_state)) -> dict[str, Any]:
    """Record a short phrase and report level, noise and what was heard (nothing is saved)."""
    try:
        return await asyncio.to_thread(state.voice.test_microphone)
    except VoiceError as e:
        raise HTTPException(409, str(e)) from e
