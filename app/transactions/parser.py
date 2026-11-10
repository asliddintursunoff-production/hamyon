"""Deterministic parsing of HUMO Card bot messages.

The parser is the source of truth for amounts, currency, card, time and reported balance.
AI is used only for semantic classification and is never allowed to change these facts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.models.enums import MsgKind, TxDirection
from app.utils.money import AMOUNT_TOKEN_RE, AmountParseError, parse_amount
from app.utils.text import unify_apostrophes

CURRENCY_RE = re.compile(r"\b(UZS|USD|EUR|RUB|SO'M|SUM|СУМ)\b", re.IGNORECASE)
CARD_RE = re.compile(r"\*\s?(\d{4})\b")
DATETIME_RE = re.compile(r"(\d{1,2}):(\d{2})\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")
DATETIME_RE_ALT = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}):(\d{2})")

MINUS_MARKERS = ("➖", "−", "—")
PLUS_MARKERS = ("➕",)
MERCHANT_MARKERS = ("📍",)
CARD_MARKERS = ("💳",)
TIME_MARKERS = ("🕓", "🕐", "🕑", "🕒", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛", "⏰", "🕰")
BALANCE_MARKERS = ("💰",)
BALANCE_LIST_CARD_MARKERS = ("🔹", "🔸", "▫️", "▪️")
BALANCE_LIST_AMOUNT_MARKERS = ("💵",)
# Unsigned amount line, e.g. "ℹ️ Karta hisobi o'zgardi / 💸 4.337.505,00 UZS" (money arrived on the card).
UNSIGNED_AMOUNT_MARKERS = ("💸",)
INCOMING_TITLE_HINTS = ("o'zgardi", "to'ldir", "tushdi", "kirim", "popoln", "zachisl", "поступ", "пополн", "credit")


def _has_amount(line: str) -> bool:
    return AMOUNT_TOKEN_RE.search(line) is not None and CURRENCY_RE.search(line) is not None


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedTransaction:
    operation_title: str | None
    direction: TxDirection
    amount: Decimal
    currency: str
    merchant: str | None
    card_last_four: str | None
    card_label: str | None
    transaction_datetime: datetime | None
    reported_balance: Decimal | None
    balance_currency: str | None
    # True for short "card balance changed" notices without merchant; HUMO sometimes also sends a
    # detailed message for the same event, so these must be de-duplicated against it.
    generic: bool = False


@dataclass(frozen=True)
class CardBalance:
    card_last_four: str
    bank_name: str | None
    balance: Decimal
    currency: str


@dataclass(frozen=True)
class ParsedBalanceList:
    balances: list[CardBalance] = field(default_factory=list)


def _norm_currency(raw: str | None, default: str = "UZS") -> str:
    if not raw:
        return default
    raw = raw.upper().replace("'", "")
    if raw in {"SOM", "SUM", "СУМ"}:
        return "UZS"
    return raw


def _amount_and_currency(line: str, default_currency: str) -> tuple[Decimal, str]:
    m = AMOUNT_TOKEN_RE.search(line)
    if not m:
        raise ParseError(f"no amount in line: {line!r}")
    try:
        amount = parse_amount(m.group(0))
    except AmountParseError as exc:
        raise ParseError(str(exc)) from exc
    cur = CURRENCY_RE.search(line[m.end():]) or CURRENCY_RE.search(line)
    return amount, _norm_currency(cur.group(1) if cur else None, default_currency)


def _strip_marker(line: str, markers: tuple[str, ...]) -> str:
    for mk in markers:
        if line.startswith(mk):
            return line[len(mk):].strip()
    return line.strip()


def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]


def detect_kind(text: str) -> MsgKind:
    lines = _lines(text)
    if not lines:
        return MsgKind.UNKNOWN
    has_card = any(CARD_RE.search(ln) for ln in lines)
    has_signed_amount = any(ln.startswith(MINUS_MARKERS + PLUS_MARKERS) for ln in lines)
    has_unsigned_amount = any(ln.startswith(UNSIGNED_AMOUNT_MARKERS) and _has_amount(ln) for ln in lines)
    if (has_signed_amount or has_unsigned_amount) and has_card:
        return MsgKind.TRANSACTION
    card_lines = [ln for ln in lines if CARD_RE.search(ln) and ln.startswith(BALANCE_LIST_CARD_MARKERS)]
    amount_lines = [ln for ln in lines if ln.startswith(BALANCE_LIST_AMOUNT_MARKERS)]
    if card_lines and amount_lines:
        return MsgKind.BALANCE
    return MsgKind.UNKNOWN


def parse_datetime(text: str, tz: ZoneInfo) -> datetime | None:
    m = DATETIME_RE.search(text)
    if m:
        hh, mm, dd, mo, yy = map(int, m.groups())
    else:
        m = DATETIME_RE_ALT.search(text)
        if not m:
            return None
        dd, mo, yy, hh, mm = map(int, m.groups())
    try:
        return datetime(yy, mo, dd, hh, mm, tzinfo=tz)
    except ValueError:
        return None


def parse_transaction(text: str, tz: ZoneInfo, default_currency: str = "UZS") -> ParsedTransaction:
    lines = _lines(text)
    if not lines:
        raise ParseError("empty message")

    direction: TxDirection | None = None
    amount: Decimal | None = None
    currency = default_currency
    merchant = card_last4 = card_label = None
    tx_dt = None
    balance = balance_currency = None
    title = None
    unsigned_amount: tuple[Decimal, str] | None = None

    for idx, ln in enumerate(lines):
        if ln.startswith(MINUS_MARKERS) and amount is None:
            direction = TxDirection.OUT
            amount, currency = _amount_and_currency(_strip_marker(ln, MINUS_MARKERS), default_currency)
        elif ln.startswith(PLUS_MARKERS) and amount is None:
            direction = TxDirection.IN
            amount, currency = _amount_and_currency(_strip_marker(ln, PLUS_MARKERS), default_currency)
        elif ln.startswith(MERCHANT_MARKERS):
            merchant = _strip_marker(ln, MERCHANT_MARKERS) or None
        elif ln.startswith(CARD_MARKERS):
            card_label = _strip_marker(ln, CARD_MARKERS)
            m = CARD_RE.search(ln)
            card_last4 = m.group(1) if m else None
        elif ln.startswith(TIME_MARKERS):
            tx_dt = parse_datetime(ln, tz)
        elif ln.startswith(BALANCE_MARKERS):
            balance, balance_currency = _amount_and_currency(_strip_marker(ln, BALANCE_MARKERS), default_currency)
        elif ln.startswith(UNSIGNED_AMOUNT_MARKERS) and amount is None and _has_amount(ln):
            unsigned_amount = _amount_and_currency(_strip_marker(ln, UNSIGNED_AMOUNT_MARKERS), default_currency)
        elif idx == 0:
            title = re.sub(r"^[^\w]+", "", ln.replace("ℹ", "")).strip() or None

    generic = False
    if amount is None and unsigned_amount is not None:
        # "Karta hisobi o'zgardi": the owner receives these when money arrives (salary, top-ups).
        amount, currency = unsigned_amount
        # The owner confirmed these notices arrive when money comes in; INCOMING_TITLE_HINTS documents
        # the known wordings. AI classification still sees the full text afterwards.
        direction = TxDirection.IN
        generic = merchant is None

    if amount is None or direction is None:
        raise ParseError("no amount found")
    if amount <= 0:
        raise ParseError("amount must be positive")
    if card_last4 is None:
        m = CARD_RE.search(text)
        card_last4 = m.group(1) if m else None
    if tx_dt is None:
        tx_dt = parse_datetime(text, tz)

    return ParsedTransaction(
        operation_title=title,
        direction=direction,
        amount=amount,
        currency=currency,
        merchant=merchant,
        card_last_four=card_last4,
        card_label=card_label,
        transaction_datetime=tx_dt,
        reported_balance=balance,
        balance_currency=balance_currency,
        generic=generic,
    )


def _bank_name_from_card_line(line: str) -> str | None:
    body = _strip_marker(line, BALANCE_LIST_CARD_MARKERS)
    body = CARD_RE.split(body)[0] if CARD_RE.search(body) else body
    body = re.sub(r"^\s*HUMO\s*CARD\b", "", body, flags=re.IGNORECASE).strip(" *-")
    return body or None


def parse_balance_list(text: str, default_currency: str = "UZS") -> ParsedBalanceList:
    lines = _lines(text)
    balances: list[CardBalance] = []
    pending: tuple[str, str | None] | None = None
    for ln in lines:
        m = CARD_RE.search(ln)
        if m and not ln.startswith(BALANCE_LIST_AMOUNT_MARKERS):
            pending = (m.group(1), _bank_name_from_card_line(ln))
            continue
        if ln.startswith(BALANCE_LIST_AMOUNT_MARKERS) and pending:
            amount, cur = _amount_and_currency(_strip_marker(ln, BALANCE_LIST_AMOUNT_MARKERS), default_currency)
            balances.append(CardBalance(pending[0], pending[1], amount, cur))
            pending = None
    if not balances:
        raise ParseError("no card balances found")
    seen = set()
    for b in balances:
        if b.card_last_four in seen:
            raise ParseError(f"duplicate card {b.card_last_four} in balance message")
        seen.add(b.card_last_four)
    return ParsedBalanceList(balances)
