"""User preferences stored in SQLite, validated by a Pydantic schema."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.database.models import Preference

DEFAULT_PERSONA = (
    "You are NEXUS, a calm, direct and technically strong local AI assistant running entirely on the "
    "user's own computer. Be concise and precise. Say plainly when you are unsure. "
    "You have no internet access and no live information; your general knowledge comes from training "
    "data and may be outdated. Never claim to have performed an action, run a command or tested code "
    "unless the application has reported the result to you. You are not conscious and cannot perceive "
    "anything beyond what is in this conversation."
)

DEFAULT_ENGINEER = (
    "You are acting as a senior software engineer. Before proposing changes, reason from the code you "
    "were actually shown; if something you need was not provided, say so instead of guessing. Follow the "
    "project's existing conventions, prefer simple maintainable solutions, avoid unnecessary dependencies "
    "and rewrites, call out edge cases and security risks, separate verified facts from assumptions, and "
    "explain significant trade-offs. Never state that tests pass or that a change works unless a test run "
    "result was provided to you."
)


class GenerationSettings(BaseModel):
    temperature: float = Field(0.3, ge=0, le=2)
    top_p: float = Field(0.9, gt=0, le=1)
    # 4096 keeps prompt processing and memory small on a CPU. The value must be the same for every request:
    # Ollama reloads the model whenever num_ctx changes.
    num_ctx: int = Field(4096, ge=1024, le=131072)
    max_output_tokens: int = Field(2048, ge=64, le=32768)
    assistant_max_tokens: int = Field(400, ge=64, le=4096)  # cap for assistant/voice answers (short replies are fast)
    keep_alive: str = "60m"  # how long Ollama keeps the model in RAM after a request (reloading takes many seconds)


class VoicePrefs(BaseModel):
    enabled: bool = False
    listening_mode: Literal["push_to_talk", "wake_word"] = "wake_word"  # once voice is enabled, listen for the wake phrase
    wake_engine: Literal["openwakeword", "whisper_keyword"] = "whisper_keyword"
    wake_phrase: str = "hey nexus, ok nexus"
    openwakeword_model: str = ""  # path to a custom .onnx/.tflite or a built-in model name
    wake_threshold: float = Field(0.5, gt=0, lt=1)
    stt_model: str = "base.en"
    wake_stt_model: str = ""  # optional separate model for wake spotting; empty = use the main model (more accurate)
    input_device: int | None = None
    tts_engine: Literal["sapi", "piper", "none"] = "sapi"
    tts_voice: str = ""
    tts_rate: int = Field(180, ge=80, le=400)
    piper_exe: str = ""
    piper_voice_model: str = ""
    speak_responses: bool = True
    ack_sound: bool = True
    pause_tolerance_s: float = Field(0.9, ge=0.6, le=5.0)  # silence after speech before NEXUS considers you done
    smart_endpoint: bool = True  # if the sentence sounds unfinished ("... and"), keep listening a bit longer
    max_utterance_s: int = Field(60, ge=10, le=180)
    store_transcripts: bool = True  # transcribed text is saved as normal chat messages; audio never is
    custom_words: list[str] = Field(default_factory=list)  # your own vocabulary (names, products, jargon) for recognition
    confirm_when_unsure: bool = True  # an action NEXUS isn't sure it heard right is read back and confirmed first
    unsure_threshold: float = Field(0.55, ge=0.1, le=0.95)
    follow_up_s: float = Field(8.0, ge=0, le=30)  # after an answer, keep listening this long without "Hey Nexus" (0 = off)
    live_captions: bool = True  # show your words while you are still speaking (needs a small model)
    partial_stt_model: str = "tiny.en"  # model for live captions; only used if already downloaded


def _default_model() -> str:
    from app.core.config import get_settings

    return get_settings().default_model


class ModelRoles(BaseModel):
    """Optional per-role models. Empty = use the active model. Only one chat model is used per request."""

    coding: str = ""
    tasks: str = ""
    fast: str = ""  # quick model for the assistant and voice (e.g. a 3B model); empty = the active model
    embedding: str = "nomic-embed-text"


class IntegratedApp(BaseModel):
    """An application NEXUS may open, close and drive. Only these (and the legacy allowed_apps) can be launched by
    voice or text. `kind`: exe = a program (target is its path or file name), uwp = a Microsoft Store app (target is
    its AppUserModelID), shortcut = a Start Menu shortcut or other file opened with Windows, uri = a protocol link."""

    name: str
    aliases: list[str] = Field(default_factory=list)
    kind: Literal["exe", "uwp", "shortcut", "uri"] = "exe"
    target: str
    args: list[str] = Field(default_factory=list)
    process: str = ""  # image name (chrome.exe) used to close the app; empty when unknown
    enabled: bool = True
    source: str = ""  # where it was found: start-menu, store, registry, essentials, manual


class CustomSkill(BaseModel):
    """Your own "say X, open this site" command. `phrase` contains {q}; `url` is https and contains {q}."""

    name: str = Field(min_length=1, max_length=40)
    phrase: str = Field(min_length=3, max_length=80)  # e.g. "search amazon for {q}"
    url: str = Field(min_length=10, max_length=400)  # e.g. "https://www.amazon.com/s?k={q}"

    @field_validator("url")
    @classmethod
    def _https(cls, v: str) -> str:
        if not v.startswith("https://") or "{q}" not in v:
            raise ValueError("The address must start with https:// and contain {q} where the spoken words go.")
        return v

    @field_validator("phrase")
    @classmethod
    def _has_q(cls, v: str) -> str:
        if "{q}" not in v:
            raise ValueError("The phrase must contain {q} where the spoken words go.")
        return v


class AgentPrefs(BaseModel):
    """The automation runtime: whether it plans, how much it may do alone, and its budgets."""

    enabled: bool = True
    auto_approve_risk: Literal["low", "medium"] = "medium"  # high always asks
    max_steps: int = Field(8, ge=1, le=20)
    max_tool_calls: int = Field(30, ge=1, le=100)
    max_llm_calls: int = Field(3, ge=1, le=10)
    max_seconds: int = Field(180, ge=10, le=1800)
    max_retries: int = Field(2, ge=0, le=5)
    max_replans: int = Field(1, ge=0, le=3)
    planner_timeout_s: int = Field(90, ge=5, le=600)
    remember_lessons: bool = True


class Preferences(BaseModel):
    active_model: str = Field(default_factory=_default_model)
    assistant_name: str = "NEXUS"
    persona_prompt: str = DEFAULT_PERSONA
    engineer_prompt: str = DEFAULT_ENGINEER
    generation: GenerationSettings = GenerationSettings()
    model_roles: ModelRoles = ModelRoles()
    voice: VoicePrefs = VoicePrefs()
    retention_days: int = Field(0, ge=0)  # 0 = keep conversations forever
    allowed_apps: dict[str, str] = Field(
        default_factory=lambda: {"notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe"}
    )
    integrated_apps: list[IntegratedApp] = Field(default_factory=list)
    search_url: str = "https://duckduckgo.com/?q={q}"  # used by "search for ..." (must contain {q})
    default_message_app: Literal["whatsapp", "teams"] = "whatsapp"
    email_app: Literal["gmail", "mailto"] = "gmail"  # gmail = Gmail compose in your browser; mailto = your default mail app
    smart_routing: bool = True  # let the model understand free-form commands that no fixed phrase matched
    disabled_skills: list[str] = Field(default_factory=list)
    custom_skills: list[CustomSkill] = Field(default_factory=list)
    send_key_delay_s: float = Field(3.0, ge=0.5, le=15)  # wait this long for a chat/compose window before pressing Send
    reminders_speak: bool = True  # say reminders out loud (needs a speech voice; works with the microphone off)
    allowed_url_domains: list[str] = Field(default_factory=lambda: ["github.com", "docs.python.org"])
    approved_folders: list[str] = Field(default_factory=list)
    agent: AgentPrefs = AgentPrefs()
    onboarding_complete: bool = False


def model_for(prefs: Preferences, role: str) -> str:
    return (getattr(prefs.model_roles, role, "") or "").strip() or prefs.active_model


def load_preferences(s: Session) -> Preferences:
    rows = {p.key: p.value for p in s.query(Preference).all()}
    base = Preferences().model_dump()
    for k, v in rows.items():
        if k in base:
            base[k] = v
    try:
        return Preferences.model_validate(base)
    except Exception:
        # Corrupt/old values: fall back field by field.
        prefs = Preferences()
        for k, v in rows.items():
            try:
                prefs = Preferences.model_validate({**prefs.model_dump(), k: v})
            except Exception:  # noqa: S112 - skip invalid stored value
                continue
        return prefs


# Dict preferences that are replaced as a whole (a merge could never remove an entry).
REPLACE_KEYS = {"allowed_apps"}


def save_preferences(s: Session, updates: dict[str, Any]) -> Preferences:
    current = load_preferences(s).model_dump()
    for k, v in updates.items():
        if k not in current:
            raise KeyError(k)
        if isinstance(current[k], dict) and isinstance(v, dict) and k not in REPLACE_KEYS:
            current[k] = {**current[k], **v}
        else:
            current[k] = v
    prefs = Preferences.model_validate(current)
    dumped = prefs.model_dump()
    for k in updates:
        row = s.get(Preference, k)
        if row is None:
            s.add(Preference(key=k, value=dumped[k]))
        else:
            row.value = dumped[k]
    return prefs
