"""
Async SQLAlchemy 2.0 database layer.

Two tables:
  - audit_logs   : append-mostly ledger of every decision the Policy Engine
                    has ever made, sanitized and structured for compliance
                    review.
  - hitl_requests: tracks the lifecycle of any tool call gated behind a
                    human-in-the-loop review (PENDING -> APPROVED/REJECTED).

Uses an async engine (aiosqlite by default) so the FastAPI layer can await
DB I/O without blocking the event loop, which matters once many agents are
hitting /v1/execute-tool concurrently.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Optional

from sqlalchemy import DateTime, Integer, String, Text, Boolean, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from config.settings import settings

# Keys that should never be persisted verbatim in the audit trail, even
# though these are mock tools. This mirrors how a real deployment would
# redact secrets/PII before writing to a durable log.
_SENSITIVE_KEYS = {"password", "api_key", "secret", "token", "ssn"}


def sanitize_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Redact obviously sensitive fields before they hit the audit log."""
    clean: dict[str, Any] = {}
    for key, value in parameters.items():
        if key.lower() in _SENSITIVE_KEYS:
            clean[key] = "***REDACTED***"
        else:
            clean[key] = value
    return clean


class Base(DeclarativeBase):
    pass


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    tool_name: Mapped[str] = mapped_column(String(255), index=True)
    parameters_json: Mapped[str] = mapped_column(Text)
    rule_matched: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(32))
    outcome: Mapped[str] = mapped_column(String(64))
    dry_run: Mapped[bool] = mapped_column(Boolean, default=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "parameters": json.loads(self.parameters_json),
            "rule_matched": self.rule_matched,
            "action": self.action,
            "outcome": self.outcome,
            "dry_run": self.dry_run,
        }


class HITLRequest(Base):
    __tablename__ = "hitl_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(255), index=True)
    tool_name: Mapped[str] = mapped_column(String(255))
    parameters_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewer: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "agent_id": self.agent_id,
            "tool_name": self.tool_name,
            "parameters": json.loads(self.parameters_json),
            "status": self.status,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
        }


def _build_engine():
    # SQLite ":memory:" databases are per-connection: with the default pool,
    # every new checkout gets its own blank database and "loses" tables
    # created by init_db(). Pin in-memory URLs to a single shared connection
    # via StaticPool (used by the test harness); file-based/production URLs
    # use SQLAlchemy's normal pooling untouched.
    if ":memory:" in settings.database_url:
        return create_async_engine(
            settings.database_url,
            future=True,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(settings.database_url, future=True)


engine = _build_engine()
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# -----------------------------------------------------------------------------
# Convenience write/read helpers used by main.py
# -----------------------------------------------------------------------------
async def write_audit_log(
    session: AsyncSession,
    *,
    request_id: str,
    agent_id: str,
    tool_name: str,
    parameters: dict[str, Any],
    rule_matched: str,
    action: str,
    outcome: str,
    dry_run: bool,
) -> AuditLog:
    row = AuditLog(
        request_id=request_id,
        agent_id=agent_id,
        tool_name=tool_name,
        parameters_json=json.dumps(sanitize_parameters(parameters)),
        rule_matched=rule_matched,
        action=action,
        outcome=outcome,
        dry_run=dry_run,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_audit_outcome(
    session: AsyncSession, *, request_id: str, outcome: str
) -> None:
    result = await session.execute(
        select(AuditLog).where(AuditLog.request_id == request_id)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        row.outcome = outcome
        await session.commit()


async def create_hitl_request(
    session: AsyncSession,
    *,
    request_id: str,
    agent_id: str,
    tool_name: str,
    parameters: dict[str, Any],
) -> HITLRequest:
    row = HITLRequest(
        request_id=request_id,
        agent_id=agent_id,
        tool_name=tool_name,
        parameters_json=json.dumps(sanitize_parameters(parameters)),
        status="PENDING",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_hitl_request(
    session: AsyncSession, request_id: str
) -> Optional[HITLRequest]:
    result = await session.execute(
        select(HITLRequest).where(HITLRequest.request_id == request_id)
    )
    return result.scalar_one_or_none()


async def list_hitl_requests(
    session: AsyncSession, status: Optional[str] = None
) -> list[HITLRequest]:
    stmt = select(HITLRequest)
    if status:
        stmt = stmt.where(HITLRequest.status == status)
    stmt = stmt.order_by(HITLRequest.created_at.desc())
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_audit_logs(session: AsyncSession, limit: int = 50) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())
