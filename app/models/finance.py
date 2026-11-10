from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, CreatedAtMixin, TimestampMixin
from app.models.enums import (
    CategoryType,
    ClassificationStatus,
    MsgKind,
    MsgStatus,
    TxDirection,
    TxSource,
    TxType,
    sql_in,
)


class Category(CreatedAtMixin, Base):
    __tablename__ = "categories"
    __table_args__ = (
        CheckConstraint(f"category_type IN ({sql_in(CategoryType)})", name="category_type"),
        # Unique per parent; top-level categories share parent "0".
        Index(
            "uq_categories_parent_name",
            func.coalesce(literal_column("parent_id"), 0),
            "normalized_name",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(60), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="RESTRICT"))
    category_type: Mapped[str] = mapped_column(String(16), nullable=False, default=CategoryType.EXPENSE)

    parent: Mapped[Category | None] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list[Category]] = relationship(back_populates="parent")


class Merchant(TimestampMixin, Base):
    """Merchant memory: classification learned from AI or set by the owner."""

    __tablename__ = "merchants"
    # Same name can mean different things for money in vs money out (e.g. P2P services).
    __table_args__ = (UniqueConstraint("normalized_name", "direction", name="uq_merchants_name_direction"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False, default="out", server_default="out")
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    subcategory_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    transaction_type: Mapped[str | None] = mapped_column(String(20))
    is_spending: Mapped[bool | None] = mapped_column(Boolean)
    description: Mapped[str | None] = mapped_column(String(255))
    # confirmed = set/approved by the owner; always wins over AI.
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ai_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    last_ai_at: Mapped[datetime | None]

    category: Mapped[Category | None] = relationship(foreign_keys=[category_id])
    subcategory: Mapped[Category | None] = relationship(foreign_keys=[subcategory_id])


class TelegramMessage(TimestampMixin, Base):
    """Original message exactly as received; processing state lives here."""

    __tablename__ = "telegram_messages"
    __table_args__ = (
        CheckConstraint(f"processing_status IN ({sql_in(MsgStatus)})", name="processing_status"),
        CheckConstraint(f"kind IN ({sql_in(MsgKind)})", name="kind"),
        Index("uq_telegram_messages_chat_msg", "chat_id", "telegram_message_id", unique=True),
        Index("ix_telegram_messages_status_next", "processing_status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    telegram_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sender_id: Mapped[int | None] = mapped_column(BigInteger)
    message_text: Mapped[str] = mapped_column(Text, nullable=False)
    message_date: Mapped[datetime] = mapped_column(nullable=False)
    edit_date: Mapped[datetime | None]
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=MsgKind.UNKNOWN)
    processing_status: Mapped[str] = mapped_column(String(16), nullable=False, default=MsgStatus.RECEIVED)
    processing_error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None]


class Transaction(TimestampMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(f"transaction_type IN ({sql_in(TxType)})", name="transaction_type"),
        CheckConstraint(f"direction IN ({sql_in(TxDirection)})", name="direction"),
        CheckConstraint(f"source IN ({sql_in(TxSource)})", name="source"),
        CheckConstraint(f"classification_status IN ({sql_in(ClassificationStatus)})", name="classification_status"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("ai_confidence IS NULL OR (ai_confidence >= 0 AND ai_confidence <= 1)", name="confidence"),
        Index("ix_transactions_datetime", "transaction_datetime"),
        Index("ix_transactions_card", "card_id"),
        Index("ix_transactions_category", "category_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    card_id: Mapped[int | None] = mapped_column(ForeignKey("cards.id", ondelete="RESTRICT"))
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    merchant_id: Mapped[int | None] = mapped_column(ForeignKey("merchants.id", ondelete="SET NULL"))
    merchant: Mapped[str | None] = mapped_column(String(255))
    operation_title: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(String(500))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    subcategory_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    # Third level: the specific thing ("Hot dog" under Food › Fast Food, "Kurtka" under Shopping › Clothing)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    is_spending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    transaction_datetime: Mapped[datetime] = mapped_column(nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    ai_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    classification_status: Mapped[str] = mapped_column(String(16), nullable=False, default=ClassificationStatus.PENDING)
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reported_balance: Mapped[Decimal | None]
    # One financial record per original message, enforced by the database.
    original_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("telegram_messages.id", ondelete="RESTRICT"), unique=True
    )
    # Idempotency for records created from bot/dashboard actions.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    notified_at: Mapped[datetime | None]
    deleted_at: Mapped[datetime | None]
    has_splits: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    card = relationship("Card")
    category: Mapped[Category | None] = relationship(foreign_keys=[category_id])
    subcategory: Mapped[Category | None] = relationship(foreign_keys=[subcategory_id])
    item: Mapped[Category | None] = relationship(foreign_keys=[item_id])
    original_message: Mapped[TelegramMessage | None] = relationship()
    splits: Mapped[list["TransactionSplit"]] = relationship(order_by="TransactionSplit.position", viewonly=True)


class AIProcessingLog(CreatedAtMixin, Base):
    __tablename__ = "ai_processing_logs"
    __table_args__ = (Index("ix_ai_processing_logs_source", "source_type", "source_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[int | None] = mapped_column(BigInteger)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class TransactionSplit(CreatedAtMixin, Base):
    """Parts of one bank transaction as explained by the owner ("20k food, the rest taxi").

    When a transaction has splits, analytics use the splits instead of the transaction's own category.
    Debt parts link to the debt ledger (a repayment's payment rows share `payment_key`).
    """

    __tablename__ = "transaction_splits"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("kind IN ('expense','income','transfer','refund','debt_repay','debt_new')", name="kind"),
        CheckConstraint(f"transaction_type IN ({sql_in(TxType)})", name="transaction_type"),
        Index("ix_transaction_splits_tx", "transaction_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    position: Mapped[int] = mapped_column(nullable=False, default=0)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    transaction_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(nullable=False)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    subcategory_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    item_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"))
    # True when the owner didn't say this part's price and it was estimated (shown with "≈")
    estimated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    description: Mapped[str | None] = mapped_column(String(300))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    debt_id: Mapped[int | None] = mapped_column(ForeignKey("debts.id", ondelete="SET NULL"))
    payment_key: Mapped[str | None] = mapped_column(String(128))

    category: Mapped[Category | None] = relationship(foreign_keys=[category_id])
    subcategory: Mapped[Category | None] = relationship(foreign_keys=[subcategory_id])
    item: Mapped[Category | None] = relationship(foreign_keys=[item_id])
    person = relationship("Person", viewonly=True)


class TransactionQuestion(CreatedAtMixin, Base):
    """A bot message asking "what was this money for?"; replies to it explain the transaction."""

    __tablename__ = "transaction_questions"
    __table_args__ = (
        UniqueConstraint("chat_id", "message_id", name="uq_transaction_questions_msg"),
        Index("ix_transaction_questions_tx", "transaction_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    answered_at: Mapped[datetime | None]
