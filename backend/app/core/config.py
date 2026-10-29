"""Static configuration (environment / .env). User-editable preferences live in the DB (see services/preferences.py)."""

from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "NEXUS"
    return Path.home() / ".nexus"


PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # Optional overrides: environment variables or a .env file in the NEXUS folder (see .env.example).
    model_config = SettingsConfigDict(env_prefix="NEXUS_", env_file=PROJECT_ROOT / ".env", extra="ignore")

    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = default_data_dir()

    ollama_url: str = "http://localhost:11434"
    default_model: str = "qwen2.5-coder:7b"
    max_concurrent_inference: int = 1
    request_timeout_s: float = 600.0

    # Command runner limits
    command_timeout_s: int = 300
    command_output_limit: int = 200_000

    # Workspace limits
    max_file_bytes: int = 1_000_000
    max_tree_entries: int = 5_000
    max_search_files: int = 20_000

    # Extra allowed browser origins (Vite dev server).
    dev_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Directory containing the built frontend (served at /). None -> auto-detect.
    frontend_dist: Path | None = None

    # Disable background loops (reminder scheduler, health monitor) e.g. in tests.
    background_tasks: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nexus.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.logs_dir, self.backups_dir, self.models_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
