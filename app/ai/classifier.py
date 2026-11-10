from __future__ import annotations

from dataclasses import dataclass
from zoneinfo import ZoneInfo

from app.ai.gemini import AIClient
from app.ai.prompts import CLASSIFIER_SYSTEM, classifier_prompt
from app.ai.schemas import TransactionClassification
from app.transactions.parser import ParsedTransaction


@dataclass
class ClassificationOutcome:
    result: TransactionClassification
    raw: str
    warnings: list[str]


def describe_facts(parsed: ParsedTransaction) -> str:
    return "\n".join(
        [
            f"direction: {'money out' if parsed.direction == 'out' else 'money in'}",
            f"amount: {parsed.amount} {parsed.currency}",
            f"merchant: {parsed.merchant or 'unknown'}",
            f"card_last_four: {parsed.card_last_four or 'unknown'}",
            f"datetime: {parsed.transaction_datetime.isoformat() if parsed.transaction_datetime else 'unknown'}",
            f"operation: {parsed.operation_title or 'unknown'}",
        ]
    )


async def classify_transaction(
    ai: AIClient, message_text: str, parsed: ParsedTransaction, categories_text: str, tz: ZoneInfo
) -> ClassificationOutcome:
    prompt = classifier_prompt(message_text, describe_facts(parsed), categories_text, str(tz))
    result, raw = await ai.generate_json(prompt, TransactionClassification, system=CLASSIFIER_SYSTEM, background=True)
    return ClassificationOutcome(result, raw, validate_against_facts(result, parsed))


def validate_against_facts(result: TransactionClassification, parsed: ParsedTransaction) -> list[str]:
    """Cross-check AI output against parser facts. Any mismatch forces a manual review."""
    warnings: list[str] = []
    if result.amount is not None and result.amount != parsed.amount:
        warnings.append(f"AI amount {result.amount} != parsed {parsed.amount}")
    if result.currency and result.currency != parsed.currency:
        warnings.append(f"AI currency {result.currency} != parsed {parsed.currency}")
    if result.card_last_four and parsed.card_last_four and result.card_last_four != parsed.card_last_four:
        warnings.append("AI card differs from parsed card")
    # Direction consistency: money in cannot be an expense; money out cannot be income/refund.
    if parsed.direction == "in" and result.transaction_type in ("expense", "cash_withdrawal"):
        warnings.append(f"AI says {result.transaction_type} but money came in")
    if parsed.direction == "out" and result.transaction_type in ("income", "refund"):
        warnings.append(f"AI says {result.transaction_type} but money went out")
    return warnings
