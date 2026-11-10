from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from app.models.enums import MsgKind, TxDirection
from app.transactions.parser import ParseError, detect_kind, parse_balance_list, parse_transaction
from app.utils.money import AmountParseError, candidate_amounts, parse_amount
from tests.conftest import BALANCES, HUMO_A, HUMO_B, HUMO_C, HUMO_D

TZ = ZoneInfo("Asia/Tashkent")


@pytest.mark.parametrize(
    "text, amount, merchant, balance, dt",
    [
        (HUMO_A, "3000.00", "MCHJ NAVROZ ABDUAHAT", "13299141.60", datetime(2026, 11, 5, 22, 23, tzinfo=TZ)),
        (HUMO_B, "10070.00", "Octo Mobile P2P H2U>", "13289071.60", datetime(2026, 11, 5, 22, 24, tzinfo=TZ)),
        (HUMO_C, "16213.00", "OOO WB TAXI OPLATA>T", "13272858.60", datetime(2026, 11, 6, 9, 59, tzinfo=TZ)),
        (HUMO_D, "100300.00", "Octo Mobile P2P H2H>", "13170158.60", datetime(2026, 11, 6, 12, 12, tzinfo=TZ)),
    ],
)
def test_parse_humo_transactions(text, amount, merchant, balance, dt):
    assert detect_kind(text) == MsgKind.TRANSACTION
    p = parse_transaction(text, TZ)
    assert p.amount == Decimal(amount)
    assert p.direction == TxDirection.OUT
    assert p.currency == "UZS"
    assert p.merchant == merchant
    assert p.card_last_four == "5592"
    assert p.reported_balance == Decimal(balance)
    assert p.transaction_datetime == dt
    assert p.transaction_datetime.utcoffset().total_seconds() == 5 * 3600


def test_parse_income_message():
    text = "💸 To'ldirish\n➕ 1.500.000,00 UZS\n📍 SALARY LLC\n💳 HUMOCARD *3798\n🕓 10:00 01.11.2026\n💰 1.500.796,22 UZS"
    p = parse_transaction(text, TZ)
    assert p.direction == TxDirection.IN
    assert p.amount == Decimal("1500000.00")
    assert p.card_last_four == "3798"


def test_parse_balance_list():
    assert detect_kind(BALANCES) == MsgKind.BALANCE
    parsed = parse_balance_list(BALANCES)
    got = {b.card_last_four: (b.bank_name, b.balance) for b in parsed.balances}
    assert got == {
        "8549": ("AO ANOR BANK", Decimal("0.00")),
        "3798": ("TBCBANK", Decimal("796.22")),
        "5592": ("OCTOBANK", Decimal("12359504.85")),
    }


def test_unknown_messages():
    assert detect_kind("Assalomu alaykum! Yangi aksiya 🎉") == MsgKind.UNKNOWN
    with pytest.raises(ParseError):
        parse_transaction("hello", TZ)
    with pytest.raises(ParseError):
        parse_balance_list("nothing here")


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3.000,00", "3000.00"),
        ("13.299.141,60", "13299141.60"),
        ("12'359'504.85", "12359504.85"),
        ("12’359’504.85", "12359504.85"),
        ("1,234,567.89", "1234567.89"),
        ("1 234 567,89", "1234567.89"),
        ("19 000", "19000.00"),
        ("19.000", "19000.00"),
        ("0.00", "0.00"),
        ("796.22", "796.22"),
        ("1,5", "1.50"),
        ("100", "100.00"),
    ],
)
def test_amount_separators(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["1.2.3,4.5", "abc", "", "12,34,5.6", "1.23.456"])
def test_invalid_amounts(raw):
    with pytest.raises(AmountParseError):
        parse_amount(raw)


def test_candidate_amounts_understand_ming_and_mln():
    assert Decimal("20000") in candidate_amounts("Orzubek mendan 20 ming qarzi bo'ldi")
    assert Decimal("19000") in candidate_amounts("19 000 Orzubekdan qarz bo'ldim")
    assert Decimal("1500000") in candidate_amounts("1,5 mln oldim")
    assert Decimal("50000") in candidate_amounts("I borrowed 50,000 UZS from Ali")
