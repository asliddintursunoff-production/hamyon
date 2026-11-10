import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.debts.service import (
    NothingToRepay,
    OverpaymentError,
    cancel_debt,
    cancel_payment,
    correct_debt,
    create_debt,
    debt_overview,
    find_or_create_person,
    record_repayment,
)
from app.models import AuditLog, DebtPayment, Person, Transaction
from app.models.enums import DebtDirection, DebtStatus
from app.utils.timeutil import utcnow

D = Decimal


async def add(sf, name, amount, direction, key=None):
    async with sf.begin() as s:
        debt, created = await create_debt(
            s, person_name=name, direction=direction, amount=D(amount), currency="UZS", description=None,
            occurred_at=utcnow(), source="bot_text", idempotency_key=key,
        )
        return debt


async def repay(sf, person_id, amount, direction, key):
    async with sf.begin() as s:
        return await record_repayment(
            s, person_id=person_id, direction=direction, amount=D(amount), currency="UZS", paid_at=utcnow(),
            description=None, idempotency_key=key,
        )


async def balance_of(sf, person_id):
    async with sf() as s:
        ov = await debt_overview(s, include_settled=True)
    return next(p for p in ov.people if p.person_id == person_id)


async def test_both_directions_and_net(sf, owner_id):
    d1 = await add(sf, "Orzubek", "19000", DebtDirection.I_OWE_THEM)
    await add(sf, "Orzubek", "20000", DebtDirection.THEY_OWE_ME)
    b = await balance_of(sf, d1.person_id)
    assert b.i_owe_them == D("19000.00") and b.they_owe_me == D("20000.00") and b.net == D("1000.00")
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(Person)) == 1
        assert await s.scalar(select(func.count()).select_from(Transaction)) == 0  # debts are not income/expense


async def test_person_reused_with_uzbek_suffix_and_case(sf, owner_id):
    async with sf.begin() as s:
        p1, c1 = await find_or_create_person(s, "Orzubek")
    async with sf.begin() as s:
        p2, c2 = await find_or_create_person(s, "orzubekdan")
    async with sf.begin() as s:
        p3, c3 = await find_or_create_person(s, "ORZUBEK")
    assert c1 and not c2 and not c3 and p1.id == p2.id == p3.id
    async with sf.begin() as s:
        p4, c4 = await find_or_create_person(s, "Ali")
    assert c4 and p4.id != p1.id


async def test_partial_and_full_repayment(sf, owner_id):
    d1 = await add(sf, "Ali", "50000", DebtDirection.THEY_OWE_ME)
    d2 = await add(sf, "Ali", "30000", DebtDirection.THEY_OWE_ME)  # increasing the debt
    pid = d1.person_id
    assert (await balance_of(sf, pid)).they_owe_me == D("80000.00")
    await repay(sf, pid, "20000", DebtDirection.THEY_OWE_ME, "k1")
    assert (await balance_of(sf, pid)).they_owe_me == D("60000.00")
    payments = await repay(sf, pid, "60000", DebtDirection.THEY_OWE_ME, "k2")  # spans both debts (FIFO)
    assert sorted(p.amount for p in payments) == [D("30000.00"), D("30000.00")]
    assert (await balance_of(sf, pid)).they_owe_me == D("0.00")
    async with sf() as s:
        from app.models import Debt
        statuses = {d.id: d.status for d in (await s.scalars(select(Debt))).all()}
    assert statuses == {d1.id: DebtStatus.SETTLED, d2.id: DebtStatus.SETTLED}


async def test_overpayment_and_wrong_direction_rejected(sf, owner_id):
    d = await add(sf, "Bob", "100000", DebtDirection.THEY_OWE_ME)
    with pytest.raises(OverpaymentError) as e:
        await repay(sf, d.person_id, "100000.01", DebtDirection.THEY_OWE_ME, "x1")
    assert e.value.outstanding == D("100000.00")
    with pytest.raises(NothingToRepay):
        await repay(sf, d.person_id, "10", DebtDirection.I_OWE_THEM, "x2")


async def test_repayment_idempotent(sf, owner_id):
    d = await add(sf, "Bob", "100000", DebtDirection.THEY_OWE_ME)
    await repay(sf, d.person_id, "40000", DebtDirection.THEY_OWE_ME, "same")
    await repay(sf, d.person_id, "40000", DebtDirection.THEY_OWE_ME, "same")
    assert (await balance_of(sf, d.person_id)).they_owe_me == D("60000.00")


async def test_debt_creation_idempotent(sf, owner_id):
    await add(sf, "Bob", "100", DebtDirection.I_OWE_THEM, key="bot:1:5")
    await add(sf, "Bob", "100", DebtDirection.I_OWE_THEM, key="bot:1:5")
    async with sf() as s:
        ov = await debt_overview(s)
    assert ov.people[0].i_owe_them == D("100.00")


async def test_concurrent_repayments_never_exceed_outstanding(sf, owner_id):
    d = await add(sf, "Kamol", "100000", DebtDirection.I_OWE_THEM)

    async def attempt(i):
        try:
            await repay(sf, d.person_id, "30000", DebtDirection.I_OWE_THEM, f"c{i}")
            return True
        except OverpaymentError:
            return False

    results = await asyncio.gather(*(attempt(i) for i in range(6)))
    assert results.count(True) == 3  # 3 x 30 000 fit into 100 000, the rest are rejected
    assert (await balance_of(sf, d.person_id)).i_owe_them == D("10000.00")


async def test_cancel_and_correct_with_audit(sf, owner_id):
    d = await add(sf, "Sardor", "50000", DebtDirection.THEY_OWE_ME)
    payments = await repay(sf, d.person_id, "50000", DebtDirection.THEY_OWE_ME, "p")
    async with sf.begin() as s:
        await cancel_payment(s, payments[0].id, reason="typo", actor="test")
    assert (await balance_of(sf, d.person_id)).they_owe_me == D("50000.00")
    async with sf.begin() as s:
        await correct_debt(s, d.id, amount=D("45000"), actor="test")
    assert (await balance_of(sf, d.person_id)).they_owe_me == D("45000.00")
    async with sf.begin() as s:
        await cancel_debt(s, d.id, reason="never happened", actor="test")
    async with sf() as s:
        assert all(p.person_id != d.person_id for p in (await debt_overview(s, include_settled=True)).people)
    async with sf() as s:
        audits = (await s.scalars(select(AuditLog).order_by(AuditLog.id))).all()
        payments_left = await s.scalar(select(func.count()).select_from(DebtPayment))
    assert [a.action for a in audits] == ["cancel", "update", "cancel"]
    assert payments_left == 1  # nothing is deleted, only cancelled
