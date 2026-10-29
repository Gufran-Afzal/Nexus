from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Preference(Base):
    __tablename__ = "preferences"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text, unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_opened_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Read-only by default; the user must explicitly enable editing/commands per workspace.
    allow_edits: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_commands: Mapped[bool] = mapped_column(Boolean, default=False)
    exclude_globs: Mapped[list[Any]] = mapped_column(JSON, default=list)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)  # user-approved project summary
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    mode: Mapped[str] = mapped_column(String(20), default="assistant")  # assistant | coding
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20))  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="complete")  # complete|stopped|error|streaming
    context_files: Mapped[list[Any]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ChangeSet(Base):
    __tablename__ = "changesets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    # proposed | applied | partially_applied | rejected | failed | reverted
    status: Mapped[str] = mapped_column(String(30), default="proposed")
    source: Mapped[str] = mapped_column(String(30), default="user")  # user | model | task
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    applied_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    files: Mapped[list[FileChange]] = relationship(
        back_populates="changeset", cascade="all, delete-orphan", order_by="FileChange.id"
    )


class FileChange(Base):
    __tablename__ = "file_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    changeset_id: Mapped[int] = mapped_column(ForeignKey("changesets.id", ondelete="CASCADE"))
    rel_path: Mapped[str] = mapped_column(Text)
    base_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)  # None => new file
    base_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_content: Mapped[str | None] = mapped_column(Text, nullable=True)  # None => delete file
    # proposed | accepted | rejected | applied | conflict | failed | reverted
    status: Mapped[str] = mapped_column(String(20), default="proposed")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    backup_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    changeset: Mapped[ChangeSet] = relationship(back_populates="files")


class CommandRun(Base):
    __tablename__ = "command_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    argv: Mapped[list[Any]] = mapped_column(JSON)
    cwd: Mapped[str] = mapped_column(Text, default=".")
    risk: Mapped[str] = mapped_column(String(30))  # verification|unfamiliar|destructive|package_install|network
    reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)
    requested_by: Mapped[str] = mapped_column(String(30), default="user")
    # pending_approval|rejected|running|succeeded|failed|timed_out|cancelled|error
    status: Mapped[str] = mapped_column(String(30), default="pending_approval")
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    timeout_s: Mapped[int] = mapped_column(Integer, default=300)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True
    )
    template: Mapped[str] = mapped_column(String(50))
    title: Mapped[str] = mapped_column(String(300))
    goal: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # pending|running|waiting_approval|succeeded|failed|cancelled
    status: Mapped[str] = mapped_column(String(30), default="pending")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    steps: Mapped[list[TaskStep]] = relationship(
        back_populates="task", cascade="all, delete-orphan", order_by="TaskStep.idx"
    )


class TaskStep(Base):
    __tablename__ = "task_steps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("agent_tasks.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text)
    depends_on: Mapped[list[Any]] = mapped_column(JSON, default=list)
    # pending|running|waiting_approval|succeeded|failed|skipped|cancelled
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # e.g. {"changeset_id": 3}
    log: Mapped[list[Any]] = mapped_column(JSON, default=list)
    task: Mapped[AgentTask] = relationship(back_populates="steps")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(30))  # user | model | system | voice
    action: Mapped[str] = mapped_column(String(80))
    target: Mapped[str] = mapped_column(Text, default="")
    outcome: Mapped[str] = mapped_column(String(30), default="ok")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Todo(Base):
    __tablename__ = "todos"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(Boolean, default=False)
    due_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Reminder(Base):
    __tablename__ = "reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), default="reminder")  # reminder | timer | alarm
    text: Mapped[str] = mapped_column(Text)
    due_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)  # local naive time
    repeat_daily: Mapped[bool] = mapped_column(Boolean, default=False)  # legacy; repeat="daily" is the same
    repeat: Mapped[str] = mapped_column(String(24), default="")  # "" | daily | weekdays | weekly | monthly
    persistent: Mapped[bool] = mapped_column(Boolean, default=True)  # keeps ringing until dismissed or snoozed
    ring_count: Mapped[int] = mapped_column(Integer, default=0)
    action: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)  # e.g. {"type":"open_app","name":"teams"}
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending|fired|dismissed|cancelled
    fired_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class Contact(Base):
    """A person NEXUS may message or email for you. Stored only on this computer."""

    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    aliases: Mapped[list[Any]] = mapped_column(JSON, default=list)  # "mom", "mum", "the boss"
    phone: Mapped[str] = mapped_column(String(32), default="")  # digits with country code, e.g. 447700900123
    email: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class AssistantAction(Base):
    """Actions recognised from typed/spoken requests (timers, app launches...), with an explicit lifecycle."""

    __tablename__ = "assistant_actions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(40))
    summary: Mapped[str] = mapped_column(Text)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # proposed|awaiting_permission|running|succeeded|failed|unsupported|rejected
    status: Mapped[str] = mapped_column(String(30))
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="text")  # text | voice
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class SemanticChunk(Base):
    """Optional embedding index (see retrieval/semantic.py). Vectors are L2-normalised float32."""

    __tablename__ = "semantic_chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workspace_id: Mapped[int] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    file_mtime: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(200))
    vector: Mapped[bytes] = mapped_column(LargeBinary)


class PerfSample(Base):
    __tablename__ = "perf_samples"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    model: Mapped[str] = mapped_column(String(200))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    load_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_s: Mapped[float | None] = mapped_column(Float, nullable=True)


class AgentRun(Base):
    """One goal handled by the automation runtime: plan, per-step state, checkpoint and metrics, all in one durable row
    so a restart can resume from the last valid checkpoint."""

    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal: Mapped[str] = mapped_column(Text)
    # queued|planning|running|waiting_for_approval|waiting_for_input|retrying|completed|partially_completed|failed|cancelled
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    source: Mapped[str] = mapped_column(String(20), default="text")
    workspace_id: Mapped[int | None] = mapped_column(nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(nullable=True)
    workflow: Mapped[str] = mapped_column(String(80), default="")  # name of the template it came from, if any
    constraints: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # budgets and the autonomy level in force
    criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)  # what "done" means, in words
    plan: Mapped[list[Any]] = mapped_column(JSON, default=list)  # validated steps
    state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # per-step results, checkpoint, pending decision
    metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # llm calls, tokens, tool calls, retries, seconds
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Workflow(Base):
    """A reusable, typed sequence of steps (built in, or saved from a run that completed and verified)."""

    __tablename__ = "workflows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[list[Any]] = mapped_column(JSON, default=list)
    steps: Mapped[list[Any]] = mapped_column(JSON, default=list)
    criteria: Mapped[list[Any]] = mapped_column(JSON, default=list)
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    uses: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_used: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class AgentLesson(Base):
    """A verified fix for a failure NEXUS met before (tool + kind of error -> what worked), offered to the planner."""

    __tablename__ = "agent_lessons"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tool: Mapped[str] = mapped_column(String(80), index=True)
    error_class: Mapped[str] = mapped_column(String(40))
    fix: Mapped[str] = mapped_column(Text)
    uses: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
