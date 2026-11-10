from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, func, Boolean, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin
from app.models.enums import SnapshotSource, sql_in


class User(TimestampMixin, Base):
    """The single owner. Kept as a row for clean relationships and settings."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(100))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Tashkent")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="UZS")
    notify_transactions: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Card(TimestampMixin, Base):
    __tablename__ = "cards"
    __table_args__ = (UniqueConstraint("owner_id", "card_last_four", name="uq_cards_owner_last_four"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(100))
    card_last_four: Mapped[str] = mapped_column(String(4), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="UZS")
    current_balance: Mapped[Decimal | None]
    last_balance_sync_at: Mapped[datetime | None]
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    snapshots: Mapped[list[BalanceSnapshot]] = relationship(back_populates="card")

    @property
    def label(self) -> str:
        return f"{self.bank_name or 'HUMO'} *{self.card_last_four}"


class BalanceSnapshot(Base):
    """A point-in-time balance reported by the bank. Never a financial transaction."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (
        CheckConstraint(f"source IN ({sql_in(SnapshotSource)})", name="source"),
        UniqueConstraint("card_id", "source_message_id", "recorded_at", name="uq_balance_snapshots_msg"),
        Index("ix_balance_snapshots_card_recorded", "card_id", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    balance: Mapped[Decimal] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(nullable=False)
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_messages.id", ondelete="SET NULL"))
    sync_request_id: Mapped[int | None] = mapped_column(ForeignKey("balance_sync_requests.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(nullable=False, server_default=func.now())

    card: Mapped[Card] = relationship(back_populates="snapshots")


class BalanceSyncRequest(CreatedAtMixin, Base):
    __tablename__ = "balance_sync_requests"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'completed', 'failed', 'timeout')", name="status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    requested_via: Mapped[str] = mapped_column(String(16), nullable=False)
    marker_message_id: Mapped[int | None] = mapped_column(BigInteger)
    response_message_id: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None]
