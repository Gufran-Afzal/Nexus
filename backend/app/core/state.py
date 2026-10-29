from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.tasks import TaskEngine
from app.core.config import Settings
from app.core.events import EventBus
from app.database.db import Database
from app.providers.base import LLMProvider
from app.retrieval.semantic import SemanticIndexer
from app.services.conversation import ConversationService
from app.services.llm import LLMService
from app.services.scheduler import Scheduler
from app.tools.command_service import CommandManager
from app.voice.service import VoiceService
from app.windows.startup import StartupManager


@dataclass
class NexusState:
    settings: Settings
    db: Database
    bus: EventBus
    provider: LLMProvider
    llm: LLMService
    conversations: ConversationService
    commands: CommandManager
    tasks: TaskEngine
    voice: VoiceService
    startup: StartupManager
    indexer: SemanticIndexer
    scheduler: Scheduler | None = None
    catalog: Any = None  # app.windows.appcatalog.AppCatalog
    paused: bool = False
    started_at: float = field(default_factory=time.time)
    ollama_status: dict[str, Any] = field(default_factory=dict)
    # Set by the desktop launcher: performs a full, graceful exit (same as tray > Exit).
    request_shutdown: Callable[[], None] | None = None
