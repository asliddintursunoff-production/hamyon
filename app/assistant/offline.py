"""Rule-based understanding of common Uzbek/English finance phrases, used when Gemini is unavailable.

Covers the usual ways the owner writes debts, repayments, expenses and income. Results always carry a
low confidence, so the assistant asks for confirmation before saving anything.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.ai.schemas import AssistantIntent
from app.utils.money import AmountParseError, parse_amount, quantize
from app.utils.text import unify_apostrophes

_MULT = {"ming": 1000, "минг": 1000, "k": 1000, "mln": 1_000_000, "million": 1_000_000, "milyon": 1_000_000, "млн": 1_000_000}
AMT = r"(?P<amt>\d[\d\s.,']*\d|\d)\s*(?P<mult>ming|минг|k|mln|million|milyon|млн)?\s*(?:so'?m|sum|uzs|сум|сўм)?"
NAME = r"(?P<name>[^\W\d_][\w']*)"

# (pattern, intent, direction, category, subcategory)
RULES: list[tuple[str, str, str | None, str | None, str | None]] = [
    # --- Uzbek: I owe them
    (rf"{NAME}dan\s+{AMT}\s+qarz\s+(?:oldim|bo'ldim|qildim)", "create_debt", "i_owe_them", None, None),
    (rf"{AMT}\s+{NAME}dan\s+qarz\s+(?:oldim|bo'ldim|qildim)", "create_debt", "i_owe_them", None, None),
    (rf"{NAME}(?:ga|ka|qa)\s+{AMT}\s+qarzdorman", "create_debt", "i_owe_them", None, None),
    # --- Uzbek: they owe me
    (rf"{NAME}\s+mendan\s+{AMT}\s+qarz", "create_debt", "they_owe_me", None, None),
    (rf"{NAME}(?:ga|ka|qa)\s+{AMT}\s+qarz\s+berdim", "create_debt", "they_owe_me", None, None),
    (rf"{AMT}\s+{NAME}(?:ga|ka|qa)\s+qarz\s+berdim", "create_debt", "they_owe_me", None, None),
    (rf"{NAME}\s+menga\s+{AMT}\s+qarzdor", "create_debt", "they_owe_me", None, None),
    # --- Uzbek: repayments
    (rf"{NAME}(?:ga|ka|qa)\s+{AMT}\s+(?:qarzimni\s+)?qaytardim", "repay_debt", "i_owe_them", None, None),
    (rf"{NAME}\s+{AMT}\s+(?:qarzini\s+)?qaytardi", "repay_debt", "they_owe_me", None, None),
    (rf"{NAME}\s+(?:qarzini\s+)?{AMT}\s+qaytardi", "repay_debt", "they_owe_me", None, None),
    # --- English
    (rf"i\s+borrowed\s+{AMT}\s+from\s+{NAME}", "create_debt", "i_owe_them", None, None),
    (rf"i\s+owe\s+{NAME}\s+{AMT}", "create_debt", "i_owe_them", None, None),
    (rf"{NAME}\s+owes\s+me\s+{AMT}", "create_debt", "they_owe_me", None, None),
    (rf"i\s+lent\s+{NAME}\s+{AMT}", "create_debt", "they_owe_me", None, None),
    (rf"i\s+lent\s+{AMT}\s+to\s+{NAME}", "create_debt", "they_owe_me", None, None),
    (rf"{NAME}\s+(?:paid\s+me\s+back|returned|repaid)\s+{AMT}", "repay_debt", "they_owe_me", None, None),
    (rf"i\s+paid\s+{NAME}\s+back\s+{AMT}", "repay_debt", "i_owe_them", None, None),
    (rf"i\s+repaid\s+{NAME}\s+{AMT}", "repay_debt", "i_owe_them", None, None),
    (rf"i\s+returned\s+{AMT}\s+to\s+{NAME}", "repay_debt", "i_owe_them", None, None),
    # --- income / expenses (no person)
    (rf"{AMT}\s+(?:so'?m\s+)?(?:maosh|oylik)\s+(?:tushdi|oldim)", "add_income", None, "Income", "Salary"),
    (rf"(?:maosh|oylik)\s+{AMT}", "add_income", None, "Income", "Salary"),
    (rf"(?:salary|got\s+paid)\D*{AMT}", "add_income", None, "Income", "Salary"),
    (rf"taksi\w*\s+{AMT}", "add_expense", None, "Transport", "Taxi"),
    (rf"{AMT}\s+.*?(?:sarfladim|sarf\s+qildim|to'ladim|ishlatdim|xarajat)", "add_expense", None, None, None),
    (rf"(?:spent|paid)\s+{AMT}", "add_expense", None, None, None),
]
_COMPILED = [(re.compile(p, re.IGNORECASE | re.UNICODE), *rest) for p, *rest in RULES]

_TAXI = re.compile(r"\btaks|\btaxi", re.IGNORECASE)
_FOOD = re.compile(r"ovqat|tushlik|kafe|restoran|food|lunch|dinner", re.IGNORECASE)


def _amount(raw: str, mult: str | None) -> Decimal | None:
    try:
        value = parse_amount(raw)
    except AmountParseError:
        return None
    if mult:
        value = quantize(value * _MULT[mult.lower()])
    return value if value > 0 else None


def parse_offline(text: str) -> AssistantIntent | None:
    t = unify_apostrophes(" ".join((text or "").split()))
    for rx, intent, direction, category, subcategory in _COMPILED:
        m = rx.search(t)
        if not m:
            continue
        amount = _amount(m.group("amt"), m.groupdict().get("mult"))
        if amount is None:
            continue
        name = m.groupdict().get("name")
        if intent in ("create_debt", "repay_debt") and not name:
            continue
        if intent == "add_expense" and category is None:
            if _TAXI.search(t):
                category, subcategory = "Transport", "Taxi"
            elif _FOOD.search(t):
                category, subcategory = "Food", None
        return AssistantIntent(
            intent=intent,
            person_name=name[0].upper() + name[1:] if name else None,
            amount=amount,
            currency="UZS",
            direction=direction,
            category=category,
            subcategory=subcategory,
            description=None,
            confidence=0.6,  # always confirm
            needs_confirmation=True,
        )
    return None
