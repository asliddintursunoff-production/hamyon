"""Strict schemas for everything Gemini returns. AI output is untrusted until validated here."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.money import AmountParseError, to_decimal

TxTypeLiteral = Literal["income", "expense", "transfer", "refund", "cash_withdrawal", "other"]
DirectionLiteral = Literal["i_owe_them", "they_owe_me"]
IntentLiteral = Literal["create_debt", "repay_debt", "add_expense", "add_income", "multiple", "unknown"]


def _clean_str(v, max_len: int):
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("must be a string")
    v = " ".join(v.split())
    if not v or v.lower() in {"null", "none", "n/a", "unknown"}:
        return None
    return v[:max_len]


def _amount(v):
    if v is None or v == "":
        return None
    try:
        d = to_decimal(v)
    except AmountParseError as exc:
        raise ValueError(str(exc)) from exc
    return d


class TransactionClassification(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_type: TxTypeLiteral
    amount: Decimal | None = None
    currency: str | None = None
    merchant: str | None = None
    card_last_four: str | None = None
    transaction_datetime: datetime | None = None
    category: str = Field(min_length=1, max_length=60)
    subcategory: str | None = None
    description: str | None = None
    is_spending: bool
    confidence: float = Field(ge=0.0, le=1.0)
    needs_review: bool = False
    ambiguity: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_v(cls, v):
        return _amount(v)

    @field_validator("merchant", "description", "ambiguity", mode="before")
    @classmethod
    def _strings(cls, v):
        return _clean_str(v, 255)

    @field_validator("category", mode="before")
    @classmethod
    def _category(cls, v):
        return _clean_str(v, 60) or "Other"

    @field_validator("subcategory", mode="before")
    @classmethod
    def _subcategory(cls, v):
        return _clean_str(v, 60)

    @field_validator("card_last_four", mode="before")
    @classmethod
    def _card(cls, v):
        if v is None:
            return None
        v = str(v).strip()[-4:]
        return v if v.isdigit() and len(v) == 4 else None

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v):
        v = _clean_str(v, 3)
        return v.upper() if v else None


class AssistantIntent(BaseModel):
    model_config = ConfigDict(extra="ignore")

    intent: IntentLiteral
    person_name: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    direction: DirectionLiteral | None = None
    description: str | None = None
    category: str | None = None
    subcategory: str | None = None
    item: str | None = None
    transaction_datetime: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    needs_confirmation: bool = False
    clarification_question: str | None = None
    transcript: str | None = None  # set when the input was a voice note
    # Several operations in one message ("A, B and C each owe me 4000"): intent "multiple" + this list.
    operations: list["AssistantIntent"] = Field(default_factory=list, max_length=10)

    def all_operations(self) -> list["AssistantIntent"]:
        ops = [op for op in self.operations if op.intent not in ("multiple", "unknown")]
        return ops or [self]

    @field_validator("operations", mode="before")
    @classmethod
    def _ops(cls, v):
        return [] if v in (None, "", "null") else v

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        return 0.5 if v in (None, "", "null") else v

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_v(cls, v):
        return _amount(v)

    @field_validator("transcript", mode="before")
    @classmethod
    def _transcript(cls, v):
        return _clean_str(v, 2000)

    @field_validator("person_name", mode="before")
    @classmethod
    def _person(cls, v):
        return _clean_str(v, 100)

    @field_validator("description", "clarification_question", mode="before")
    @classmethod
    def _desc(cls, v):
        return _clean_str(v, 300)

    @field_validator("category", "subcategory", "item", mode="before")
    @classmethod
    def _cat(cls, v):
        return _clean_str(v, 60)

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v):
        v = _clean_str(v, 3)
        return v.upper() if v else None

    @field_validator("direction", mode="before")
    @classmethod
    def _direction(cls, v):
        if v in ("", "null", "unknown", None):
            return None
        return v


AssistantIntent.model_rebuild()


class Transcription(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transcript: str = Field(max_length=2000)
    language: str | None = None


SplitKindLiteral = Literal["expense", "income", "transfer", "refund", "debt_repay", "debt_new"]


class SplitLine(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: SplitKindLiteral
    amount: Decimal | None = None  # null = "the rest"
    estimated: bool = False  # the owner didn't say this price; the AI estimated it
    category: str | None = None
    subcategory: str | None = None
    item: str | None = None
    person_name: str | None = None
    description: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_v(cls, v):
        return _amount(v)

    @field_validator("category", "subcategory", "item", mode="before")
    @classmethod
    def _cat(cls, v):
        return _clean_str(v, 60)

    @field_validator("person_name", mode="before")
    @classmethod
    def _person(cls, v):
        return _clean_str(v, 100)

    @field_validator("description", mode="before")
    @classmethod
    def _desc(cls, v):
        return _clean_str(v, 300)


class SplitAnswer(BaseModel):
    """The owner's explanation of what one bank transaction was for."""

    model_config = ConfigDict(extra="ignore")

    transcript: str | None = None
    lines: list[SplitLine] = Field(min_length=1, max_length=10)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_confirmation: bool = False
    clarification_question: str | None = None

    @field_validator("transcript", "clarification_question", mode="before")
    @classmethod
    def _text(cls, v):
        return _clean_str(v, 2000)
