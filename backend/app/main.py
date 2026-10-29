"""NEXUS backend application factory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.agent.tasks import TaskEngine
from app.api import (
    routes_agent,
    routes_apps,
    routes_chat,
    routes_models,
    routes_people,
    routes_personal,
    routes_runs,
    routes_system,
    routes_voice,
)
from app.api.routes_system import refresh_ollama
from app.core.config import Settings, get_settings
from app.core.events import EventBus
from app.core.logging_setup import configure_logging
from app.core.state import NexusState
from app.database.db import Database
from app.providers.base import LLMProvider
from app.providers.ollama import OllamaProvider
from app.retrieval.semantic import SemanticIndexer
from app.services import actions
from app.services.conversation import ConversationService, mark_interrupted_messages
from app.services.llm import LLMService
from app.services.preferences import load_preferences
from app.services.scheduler import Scheduler
from app.services.warmup import warm_up
from app.tools.command_service import CommandManager
from app.voice.service import VoiceService
from app.voice.worker_client import SpeechWorker
from app.windows.startup import RegistryBackend, StartupManager

log = logging.getLogger("nexus")

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}


def _hostname(host_header: str) -> str:
    if host_header.startswith("["):  # IPv6 literal, e.g. [::1]:8765
        return host_header.split("]")[0] + "]"
    return host_header.rsplit(":", 1)[0]


def _find_frontend(settings: Settings) -> Path | None:
    if settings.frontend_dist:
        return settings.frontend_dist if settings.frontend_dist.is_dir() else None
    candidate = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    return candidate if (candidate / "index.html").is_file() else None


def create_app(
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    voice_options: dict[str, Any] | None = None,
    startup_backend: RegistryBackend | None = None,
    launcher: actions.Launcher | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    configure_logging(settings.logs_dir)

    db = Database(settings.db_path)
    bus = EventBus()
    provider = provider or OllamaProvider(settings.ollama_url, settings.request_timeout_s)
    llm = LLMService(provider, settings.max_concurrent_inference)
    commands = CommandManager(settings, db, bus)
    conversations = ConversationService(settings, db, llm, launcher or actions.default_launcher, commands)
    tasks = TaskEngine(settings, db, bus, llm, commands)
    indexer = SemanticIndexer(db, provider, bus)
    conversations.indexer = indexer
    conversations.assistant.bus = bus
    profile_file = settings.models_dir / "whisper" / "engine-profile.json"
    voice_kwargs = {"worker": SpeechWorker(settings.logs_dir, state_file=profile_file), **(voice_options or {})}
    voice = VoiceService(db, bus, conversations, llm, settings.models_dir, **voice_kwargs)
    if settings.background_tasks:  # sound + spoken alert + "then open app" when a reminder rings
        from app.services.alerts import AlertHandler
        from app.voice.tts import play_alert

        bus.add_listener(AlertHandler(db, bus, voice, launcher or actions.default_launcher, play_alert))
    state = NexusState(settings=settings, db=db, bus=bus, provider=provider, llm=llm, conversations=conversations,
                       commands=commands, tasks=tasks, voice=voice, startup=StartupManager(startup_backend),
                       indexer=indexer)
    from app.windows.appcatalog import AppCatalog

    state.catalog = AppCatalog(settings.data_dir / "apps-cache.json")

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        bus.bind_loop(loop)
        commands.mark_interrupted()
        tasks.mark_interrupted()
        mark_interrupted_messages(db)
        try:
            voice.bind(loop)
        except Exception:
            log.exception("voice initialisation failed; voice disabled for this session")
        monitor: asyncio.Task | None = None
        warm_task: asyncio.Task | None = None
        if settings.background_tasks:
            async def on_resume() -> None:
                await refresh_ollama(state)

            state.scheduler = Scheduler(db, bus, on_resume=on_resume)
            state.scheduler.start()
            monitor = asyncio.create_task(_ollama_monitor(state))
            warm_task = asyncio.create_task(_warm_model(state))
        log.info("NEXUS %s started on http://%s:%s (data: %s)", __version__, settings.host, settings.port,
                 settings.data_dir)
        try:
            yield
        finally:
            log.info("NEXUS shutting down")
            llm.stop_all()
            voice.shutdown()
            await commands.shutdown()
            await tasks.shutdown()
            await indexer.shutdown()
            if state.scheduler:
                await state.scheduler.stop()
            if warm_task:
                warm_task.cancel()
            if monitor:
                monitor.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await monitor
            db.dispose()

    app = FastAPI(title="NEXUS", version=__version__, lifespan=lifespan, docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json")
    app.state.nexus = state

    allowed_origins = {f"http://{h}:{settings.port}" for h in ("127.0.0.1", "localhost")} | set(settings.dev_origins)

    @app.middleware("http")
    async def local_only_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        # DNS-rebinding protection: only accept requests addressed to a local host name.
        if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS:
            return JSONResponse({"detail": "Forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin not in allowed_origins:
            return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
        # CSRF protection: state-changing requests must carry a custom header, which browsers only allow
        # cross-origin after a CORS preflight that we reject for unknown origins.
        if request.method not in SAFE_METHODS and request.url.path.startswith("/api") \
                and not request.headers.get("x-nexus-client"):
            return JSONResponse({"detail": "Missing X-Nexus-Client header"}, status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith("/api"):
            # The UI only talks to this origin; block third-party scripts and framing.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
            )
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_middleware(CORSMiddleware, allow_origins=sorted(allowed_origins), allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
                       allow_headers=["content-type", "x-nexus-client"])

    for r in (routes_system, routes_models, routes_chat, routes_agent, routes_personal, routes_voice, routes_apps, routes_people,
              routes_runs):
        app.include_router(r.router)
    from app.api import routes_workspaces

    app.include_router(routes_workspaces.router)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> JSONResponse:
        log.exception("Unhandled error: %s", exc)
        return JSONResponse({"detail": f"Internal error: {exc.__class__.__name__}"}, status_code=500)

    dist = _find_frontend(settings)
    if dist:
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    else:
        @app.get("/")
        def no_frontend() -> dict[str, str]:
            return {"detail": "NEXUS API is running. The UI is not built yet: run scripts\\build-frontend.ps1"}

    return app


async def _warm_model(state: NexusState) -> None:
    """Preload the model shortly after start so the first question does not pay the load time."""
    try:
        await asyncio.sleep(2)
        await refresh_ollama(state)
        if not state.ollama_status.get("available"):
            return
        with state.db.session() as s:
            prefs = load_preferences(s)
        await warm_up(state.llm, prefs)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("model warm-up failed", exc_info=True)


async def _ollama_monitor(state: NexusState, interval: float = 30.0) -> None:
    """Lightweight periodic health check so the UI reflects Ollama starting/stopping/restarting."""
    while True:
        try:
            await refresh_ollama(state)
        except Exception:
            log.debug("ollama health check failed", exc_info=True)
        await asyncio.sleep(interval)


def run() -> None:
    import uvicorn

    s = get_settings()
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_config=None)


if __name__ == "__main__":
    run()
