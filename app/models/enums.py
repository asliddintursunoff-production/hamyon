from enum import StrEnum


class TxType(StrEnum):
    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    REFUND = "refund"
    CASH_WITHDRAWAL = "cash_withdrawal"
    OTHER = "other"


class TxDirection(StrEnum):
    IN = "in"
    OUT = "out"


class TxSource(StrEnum):
    HUMO = "humo"
    BOT_TEXT = "bot_text"
    BOT_VOICE = "bot_voice"
    DASHBOARD = "dashboard"


class ClassificationStatus(StrEnum):
    PENDING = "pending"      # waiting for rule/AI
    RULE = "rule"            # merchant memory
    AI = "ai"                # Gemini
    MANUAL = "manual"        # owner edited
    FALLBACK = "fallback"    # AI unavailable / unknown merchant


class MsgStatus(StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    AWAITING_AI = "awaiting_ai"
    CLASSIFIED = "classified"
    SAVED = "saved"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    IGNORED = "ignored"


class MsgKind(StrEnum):
    TRANSACTION = "transaction"
    BALANCE = "balance"
    UNKNOWN = "unknown"


class DebtDirection(StrEnum):
    I_OWE_THEM = "i_owe_them"
    THEY_OWE_ME = "they_owe_me"


class DebtStatus(StrEnum):
    OPEN = "open"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class CategoryType(StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    ANY = "any"


class SnapshotSource(StrEnum):
    TRANSACTION_MESSAGE = "transaction_message"
    BALANCE_MESSAGE = "balance_message"
    BALANCE_REFRESH = "balance_refresh"


def sql_in(enum_cls) -> str:
    return ", ".join(f"'{m.value}'" for m in enum_cls)
