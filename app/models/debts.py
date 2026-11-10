from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin
from app.models.enums import DebtDirection, DebtStatus, TxSource, sql_in


class Person(TimestampMixin, Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)

    debts: Mapped[list[Debt]] = relationship(back_populates="person")


class Debt(TimestampMixin, Base):
    __tablename__ = "debts"
    __table_args__ = (
        CheckConstraint(f"direction IN ({sql_in(DebtDirection)})", name="direction"),
        CheckConstraint(f"status IN ({sql_in(DebtStatus)})", name="status"),
        CheckConstraint(f"source IN ({sql_in(TxSource)})", name="source"),
        CheckConstraint("original_amount > 0", name="amount_positive"),
        Index("ix_debts_person_direction", "person_id", "direction"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    original_amount: Mapped[Decimal] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # What the debt was for ("Food › Snacks › Coca-Cola"). Repaying it then counts as that spending.
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    subcategory_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=DebtStatus.OPEN)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(String(500))

    person: Mapped[Person] = relationship(back_populates="debts")
    category = relationship("Category", foreign_keys=[category_id], viewonly=True)
    subcategory = relationship("Category", foreign_keys=[subcategory_id], viewonly=True)
    item = relationship("Category", foreign_keys=[item_id], viewonly=True)
    payments: Mapped[list[DebtPayment]] = relationship(back_populates="debt", order_by="DebtPayment.payment_date")


class DebtPayment(CreatedAtMixin, Base):
    __tablename__ = "debt_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        UniqueConstraint("debt_id", "idempotency_key", name="uq_debt_payments_debt_key"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    debt_id: Mapped[int] = mapped_column(ForeignKey("debts.id", ondelete="RESTRICT"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(nullable=False)
    payment_date: Mapped[datetime] = mapped_column(nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    cancelled_at: Mapped[datetime | None]

    debt: Mapped[Debt] = relationship(back_populates="payments")
