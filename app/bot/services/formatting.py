from __future__ import annotations

from zoneinfo import ZoneInfo

from app.models import Card, Transaction
from app.models.enums import TxType
from app.utils.money import format_money
from app.utils.text import escape_html

TYPE_ICONS = {
    TxType.EXPENSE: "💸",
    TxType.INCOME: "💰",
    TxType.TRANSFER: "🔁",
    TxType.REFUND: "↩️",
    TxType.CASH_WITHDRAWAL: "🏧",
    TxType.OTHER: "▫️",
}

TYPE_LABELS = {
    TxType.EXPENSE: "Expense",
    TxType.INCOME: "Income",
    TxType.TRANSFER: "Transfer",
    TxType.REFUND: "Refund",
    TxType.CASH_WITHDRAWAL: "Cash withdrawal",
    TxType.OTHER: "Other",
}


def signed_amount(tx: Transaction) -> str:
    sign = "-" if tx.direction == "out" else "+"
    return f"{sign}{format_money(tx.amount, tx.currency)}"


def category_label(tx: Transaction) -> str:
    parts = [c.name for c in (tx.category, tx.subcategory) if c is not None]
    return " / ".join(parts) or "Other"


def tx_line(tx: Transaction, tz: ZoneInfo) -> str:
    icon = TYPE_ICONS.get(tx.transaction_type, "▫️")
    who = escape_html(tx.merchant or tx.description or "—")
    review = " ⚠️" if tx.needs_review else ""
    return (
        f"{icon} <b>{signed_amount(tx)}</b> · {who}\n"
        f"    {escape_html(category_label(tx))} · {tx.transaction_datetime.astimezone(tz):%d.%m %H:%M}{review}  /tx_{tx.id}"
    )


def tx_details(tx: Transaction, tz: ZoneInfo) -> str:
    card = f"{tx.card.bank_name or 'HUMO'} *{tx.card.card_last_four}" if tx.card else "— (cash/manual)"
    conf = f"{float(tx.ai_confidence):.0%}" if tx.ai_confidence is not None else "—"
    lines = [
        f"{TYPE_ICONS.get(tx.transaction_type, '▫️')} <b>{signed_amount(tx)}</b>",
        f"Type: {TYPE_LABELS.get(tx.transaction_type, tx.transaction_type)}",
        f"Merchant: {escape_html(tx.merchant or '—')}",
        f"Category: {escape_html(category_label(tx))}",
        f"Description: {escape_html(tx.description or '—')}",
        f"Card: {escape_html(card)}",
        f"Time: {tx.transaction_datetime.astimezone(tz):%d.%m.%Y %H:%M}",
        f"Classified by: {tx.classification_status} (confidence {conf})",
    ]
    if tx.reported_balance is not None:
        lines.append(f"Balance after: {format_money(tx.reported_balance, tx.currency)}")
    if tx.needs_review:
        lines.append("⚠️ <b>Needs review</b>")
    return "\n".join(lines)


def card_line(card: Card, tz: ZoneInfo) -> str:
    bal = format_money(card.current_balance, card.currency) if card.current_balance is not None else "unknown"
    when = f"{card.last_balance_sync_at.astimezone(tz):%d.%m %H:%M}" if card.last_balance_sync_at else "never"
    return f"💳 <b>{escape_html(card.bank_name or 'HUMO')}</b> *{card.card_last_four}\n    {bal}  <i>(updated {when})</i>"
