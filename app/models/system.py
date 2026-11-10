from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, CreatedAtMixin, TimestampMixin


class AuditLog(CreatedAtMixin, Base):
    """Append-only history of manual corrections to financial data."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    changes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(32), nullable=False)


class BotAuthSession(TimestampMixin, Base):
    """Access-code state of the owner in the Telegram bot (brute force protection + unlock expiry)."""

    __tablename__ = "bot_auth_sessions"

    telegram_user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    authenticated_until: Mapped[datetime | None]
    failed_attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    locked_until: Mapped[datetime | None]
    # HMAC fingerprint of the access code used to unlock; a changed code invalidates the session.
    code_fingerprint: Mapped[str | None] = mapped_column(String(64))


class DashboardLoginToken(CreatedAtMixin, Base):
    """One-time dashboard login links issued by the bot. Only the SHA-256 hash is stored."""

    __tablename__ = "dashboard_login_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    used_at: Mapped[datetime | None]


class PendingAction(TimestampMixin, Base):
    """An assistant operation waiting for the owner's confirmation or clarification."""

    __tablename__ = "pending_actions"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'confirmed', 'cancelled', 'expired')", name="status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
