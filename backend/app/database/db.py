"""SQLite engine, sessions, versioned migrations, integrity check/recovery and backup."""

from __future__ import annotations

import datetime as dt
import logging
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.database.models import Base, SchemaVersion

log = logging.getLogger(__name__)


def _m1_initial(engine: Engine) -> None:
    Base.metadata.create_all(engine)


# Append new migrations here. Never edit an applied migration.
# Each migration receives the engine; additive migrations can usually just call create_all
# (which only creates missing tables) or run explicit ALTER TABLE statements.
def _m2_semantic_index(engine: Engine) -> None:
    from app.database.models import SemanticChunk

    SemanticChunk.__table__.create(engine, checkfirst=True)  # type: ignore[attr-defined]


def _m3_smart_reminders(engine: Engine) -> None:
    """Recurrence, ringing and on-fire actions. A fresh database already has these columns (migration 1 creates
    tables from the current models), so only add the ones that are missing."""
    cols = {c["name"] for c in inspect(engine).get_columns("reminders")}
    wanted = {"repeat": "VARCHAR(24) NOT NULL DEFAULT ''", "persistent": "BOOLEAN NOT NULL DEFAULT 1",
              "ring_count": "INTEGER NOT NULL DEFAULT 0", "action": "JSON"}
    with engine.begin() as con:
        for name, ddl in wanted.items():
            if name not in cols:
                con.execute(text(f"ALTER TABLE reminders ADD COLUMN {name} {ddl}"))
        con.execute(text("UPDATE reminders SET repeat = 'daily' WHERE repeat_daily = 1 AND repeat = ''"))


def _m4_contacts(engine: Engine) -> None:
    from app.database.models import Contact

    Contact.__table__.create(engine, checkfirst=True)  # type: ignore[attr-defined]


def _m5_agent_runtime(engine: Engine) -> None:
    from app.database.models import AgentLesson, AgentRun, Workflow

    for t in (AgentRun, Workflow, AgentLesson):
        t.__table__.create(engine, checkfirst=True)  # type: ignore[attr-defined]


MIGRATIONS: list[tuple[int, Callable[[Engine], None]]] = [
    (1, _m1_initial),
    (2, _m2_semantic_index),
    (3, _m3_smart_reminders),
    (4, _m4_contacts),
    (5, _m5_agent_runtime),
]

LATEST_VERSION = MIGRATIONS[-1][0]


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.recovered_from: Path | None = None
        path.parent.mkdir(parents=True, exist_ok=True)
        self._check_integrity()
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False, "timeout": 30}
        )

        @event.listens_for(self.engine, "connect")
        def _pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.migrate()

    def _check_integrity(self) -> None:
        """If the DB file is corrupt, move it aside (never delete) and start fresh."""
        if not self.path.exists():
            return
        ok = False
        try:
            con = sqlite3.connect(self.path)
            try:
                row = con.execute("PRAGMA integrity_check").fetchone()
                ok = bool(row) and row[0] == "ok"
            finally:
                con.close()
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = self.path.with_name(f"{self.path.stem}.corrupt-{stamp}{self.path.suffix}")
            shutil.move(str(self.path), dest)
            for suffix in ("-wal", "-shm"):
                side = Path(str(self.path) + suffix)
                if side.exists():
                    shutil.move(str(side), str(dest) + suffix)
            self.recovered_from = dest
            log.error("Database failed integrity check; moved to %s and created a new one", dest)

    def current_version(self) -> int:
        if not inspect(self.engine).has_table("schema_version"):
            return 0
        with self.session() as s:
            v = s.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
            return int(v or 0)

    def migrate(self) -> None:
        current = self.current_version()
        for version, fn in MIGRATIONS:
            if version > current:
                log.info("Applying database migration %s", version)
                fn(self.engine)
                with self.session() as s:
                    s.add(SchemaVersion(version=version))

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def backup(self, dest: Path) -> Path:
        """Consistent online backup using SQLite's backup API."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(self.path)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return dest

    def dispose(self) -> None:
        self.engine.dispose()
