"""Async SQLAlchemy engine/session + ORM models (single module keeps the schema in one place)."""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from sqlalchemy import JSON, BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


# ---------------------------------------------------------------- enums
class HostKind(str, enum.Enum):
    linux = "linux"
    act = "act"


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"


class LabState(str, enum.Enum):
    draft = "draft"
    built = "built"
    deployed = "deployed"
    validated = "validated"
    destroyed = "destroyed"


class SecretKind(str, enum.Enum):
    ssh_key = "ssh_key"
    password = "password"
    arista_token = "arista_token"
    cv_token = "cv_token"
    netbox_token = "netbox_token"
    generic = "generic"


# ---------------------------------------------------------------- models
class Secret(Base):
    __tablename__ = "secrets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[SecretKind] = mapped_column(Enum(SecretKind))
    ciphertext: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # e.g. public key, expiry, username
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Host(Base):
    __tablename__ = "hosts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[HostKind] = mapped_column(Enum(HostKind), default=HostKind.linux)
    address: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(64), default="ubuntu")
    # 1.0.1: optional SSH jump host "[user@]host[:port]" (same key/password as the target) — e.g. the Mac in front of
    # its OrbStack VM, or a Windows PC's sshd in front of its WSL distro
    jump_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 1.0.2: what the user says this host is — linux | mac (OrbStack VM, beta) | windows (WSL2, beta); the probe checks it
    platform: Mapped[str] = mapped_column(String(16), default="linux")
    ssh_key_secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)
    password_secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)
    sudo_password_secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)  # falls back to password_secret_id
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # result of probe
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    labs: Mapped[list["Lab"]] = relationship(back_populates="host")


class Image(Base):
    __tablename__ = "images"
    __table_args__ = (UniqueConstraint("fmt", "version", name="uq_image_fmt_version"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    fmt: Mapped[str] = mapped_column(String(32), default="cEOS")  # cEOS | vEOS64-lab | ...
    version: Mapped[str] = mapped_column(String(32))  # 4.34.2F
    docker_tag: Mapped[str] = mapped_column(String(120))  # arista/ceos:4.34.2F
    local_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    present_on_hosts: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [host_id,...]
    arch: Mapped[str | None] = mapped_column(String(16), nullable=True, default="x86_64")  # x86_64 | arm64 (from fmt)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Lab(Base):
    __tablename__ = "labs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    template: Mapped[str] = mapped_column(String(64))
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"), nullable=True)
    target: Mapped[str] = mapped_column(String(16), default="clab")  # clab | act
    state: Mapped[LabState] = mapped_column(Enum(LabState), default=LabState.draft)
    ceos_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # knob values, cv target, serial scheme
    cv_target_id: Mapped[str | None] = mapped_column(ForeignKey("cv_targets.id"), nullable=True)  # CloudVision binding
    netbox_target_id: Mapped[str | None] = mapped_column(ForeignKey("netbox_targets.id"), nullable=True)  # phase 4 (B)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    host: Mapped[Host | None] = relationship(back_populates="labs")
    jobs: Mapped[list["Job"]] = relationship(back_populates="lab")


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    type: Mapped[str] = mapped_column(String(64))  # host.probe, host.bootstrap, lab.build, lab.deploy ...
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus), default=JobStatus.queued)
    lab_id: Mapped[str | None] = mapped_column(ForeignKey("labs.id"), nullable=True)
    host_id: Mapped[str | None] = mapped_column(ForeignKey("hosts.id"), nullable=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    artefacts: Mapped[list[Any]] = mapped_column(JSON, default=list)  # [{"name","path"}]
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lab: Mapped[Lab | None] = relationship(back_populates="jobs")


class ValidationRun(Base):
    __tablename__ = "validation_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    lab_id: Mapped[str] = mapped_column(ForeignKey("labs.id"))
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    totals: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"success": n, "failure": n, ...}
    report_path: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CVTarget(Base):
    __tablename__ = "cv_targets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    kind: Mapped[str] = mapped_column(String(16))  # cvaas | onprem
    server: Mapped[str] = mapped_column(String(255))
    token_secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)  # service account
    verify_certs: Mapped[bool] = mapped_column(default=True)
    onboarding_token_secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)  # TerminAttr


class TrafficRun(Base):
    """One ping / iperf run of the client traffic generator (Module 5)."""
    __tablename__ = "traffic_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    lab_id: Mapped[str] = mapped_column(ForeignKey("labs.id"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(16))  # ping | iperf
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- phase 3 (backend B): host monitoring samples, pruned to 7 days by hosts.monitor
class HostMetric(Base):
    __tablename__ = "host_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    host_id: Mapped[str] = mapped_column(String(32), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {point, containers}


# ---- phase 4 (backend B): NetBox targets (Module 6); the API token lives in `secrets` (kind netbox_token)
class NetBoxTarget(Base):
    __tablename__ = "netbox_targets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    url: Mapped[str] = mapped_column(String(255))
    token_secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id"), nullable=True)
    verify_certs: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- phase 5: in-app feedback (bug | suggestion | question), optionally mirrored to a GitHub issue
class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(16))  # bug | suggestion | question
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    page: Mapped[str | None] = mapped_column(String(512), nullable=True)
    diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="new")  # new | triaged | done
    github_issue_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- phase 6 (backend B): in-app learning — one row per passed lesson checkpoint (latest pass wins)
class LearnProgress(Base):
    __tablename__ = "learn_progress"
    __table_args__ = (UniqueConstraint("username", "lesson_id", "checkpoint_id", name="uq_learn_progress"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(120), default="")
    lesson_id: Mapped[str] = mapped_column(String(120), index=True)
    checkpoint_id: Mapped[str] = mapped_column(String(120))
    lab_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    passed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---- v0.8.0 (stream A): multi-user accounts, roles, TOTP MFA + auth audit trail
class UserRole(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)  # always lower-case
    password_hash: Mapped[str] = mapped_column(String(255))  # argon2id
    role: Mapped[str] = mapped_column(String(16), default=UserRole.viewer.value)  # admin | operator | viewer
    disabled: Mapped[bool] = mapped_column(default=False)
    must_change_password: Mapped[bool] = mapped_column(default=False)
    totp_secret: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet ciphertext (secret store key)
    mfa_enabled: Mapped[bool] = mapped_column(default=False)
    recovery_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)  # argon2 hashes, removed when used
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    session_version: Mapped[int] = mapped_column(Integer, default=1)  # bumped → every other session is invalid
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    username: Mapped[str] = mapped_column(String(120), default="")
    event: Mapped[str] = mapped_column(String(64))  # login.ok, login.failed, login.locked, mfa.enabled, ...
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)


# ---- v0.9.0 (stream A): public account requests + admin-editable app settings (key → JSON value)
class AccountRequest(Base):
    __tablename__ = "account_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), index=True)  # lower-case, validated at submission
    email: Mapped[str] = mapped_column(String(254))
    full_name: Mapped[str] = mapped_column(String(120), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending | approved | rejected
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    role: Mapped[str | None] = mapped_column(String(16), nullable=True)  # granted role (approved)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # created user (approved)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---- v0.10.0: AI lab designer — one linked model per user; the API key lives in `secrets` (write-only)
class AILink(Base):
    __tablename__ = "ai_links"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(16))  # anthropic | openai (OpenAI-compatible: Ollama, LM Studio, vLLM, Azure)
    base_url: Mapped[str] = mapped_column(String(512), default="")
    model: Mapped[str] = mapped_column(String(200))
    api_key_secret_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


# ---------------------------------------------------------------- engine / session
engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for d in (settings.labs_dir, settings.images_dir, settings.jobs_dir):
        d.mkdir(parents=True, exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_light_migrations)


# Columns added after the first release; create_all never alters existing tables, so add them here (SQLite + Postgres).
_ADDED_COLUMNS: list[tuple[str, str, str]] = [
    ("hosts", "sudo_password_secret_id", "VARCHAR(32)"),
    ("hosts", "jump_host", "VARCHAR(255)"),  # 1.0.1
    ("hosts", "platform", "VARCHAR(16) DEFAULT 'linux'"),  # 1.0.2
    ("labs", "cv_target_id", "VARCHAR(32)"),
    ("cv_targets", "onboarding_token_secret_id", "VARCHAR(32)"),
    ("images", "arch", "VARCHAR(16)"),  # phase 3 (backend B)
    ("labs", "netbox_target_id", "VARCHAR(32)"),  # phase 4 (backend B)
    # v0.8.0 (stream A): users created by a 0.8.0 pre-release before these columns existed
    ("users", "session_version", "INTEGER DEFAULT 1"),
    ("users", "recovery_codes", "JSON"),
]


def _apply_light_migrations(conn) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import inspect, text

    insp = inspect(conn)
    for table, column, ddl in _ADDED_COLUMNS:
        if table in insp.get_table_names() and column not in {c["name"] for c in insp.get_columns(table)}:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
