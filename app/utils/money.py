"""Exact money handling. Never use float for financial values."""
from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

MONEY_QUANT = Decimal("0.01")
MAX_AMOUNT = Decimal("999999999999999.99")  # fits NUMERIC(18, 2)

_GROUP_CHARS = "'’`ʼ    "
AMOUNT_TOKEN_RE = re.compile(r"[-+]?\d[\d'’`ʼ    .,]*\d|\d")


class AmountParseError(ValueError):
    pass


def quantize(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def parse_amount(token: str) -> Decimal:
    """Parse a single amount token with any common decimal/thousands convention.

    Examples: "3.000,00" -> 3000.00, "12'359'504.85" -> 12359504.85, "19 000" -> 19000,
    "0.00" -> 0.00, "1,5" -> 1.50, "13.299.141,60" -> 13299141.60.
    """
    if token is None:
        raise AmountParseError("empty amount")
    s = token.strip()
    for ch in _GROUP_CHARS:
        s = s.replace(ch, "")
    if not s:
        raise AmountParseError("empty amount")
    sign = ""
    if s[0] in "+-":
        sign, s = s[0], s[1:]
    if not s or not re.fullmatch(r"[\d.,]+", s) or not s[0].isdigit() or not s[-1].isdigit():
        raise AmountParseError(f"invalid amount: {token!r}")

    has_dot, has_comma = "." in s, "," in s
    if has_dot and has_comma:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        thou = "," if dec == "." else "."
        int_part, frac = s.rsplit(dec, 1)
        if dec in int_part:
            raise AmountParseError(f"invalid amount: {token!r}")
        groups = int_part.split(thou)
        if any(len(g) != 3 for g in groups[1:]) or not (1 <= len(groups[0]) <= 3):
            raise AmountParseError(f"invalid thousands grouping: {token!r}")
        int_part = "".join(groups)
    elif has_dot or has_comma:
        sep = "." if has_dot else ","
        parts = s.split(sep)
        if len(parts) > 2:
            if any(len(p) != 3 for p in parts[1:]) or not (1 <= len(parts[0]) <= 3):
                raise AmountParseError(f"invalid thousands grouping: {token!r}")
            int_part, frac = "".join(parts), ""
        else:
            a, b = parts
            if len(b) == 3:  # "19.000" / "1,500" -> thousands separator
                int_part, frac = a + b, ""
            else:
                int_part, frac = a, b
    else:
        int_part, frac = s, ""

    try:
        value = Decimal(f"{sign}{int_part}.{frac or '0'}")
    except InvalidOperation as exc:  # pragma: no cover - guarded by regex
        raise AmountParseError(f"invalid amount: {token!r}") from exc
    if abs(value) > MAX_AMOUNT:
        raise AmountParseError("amount too large")
    return quantize(value)


def to_decimal(value) -> Decimal:
    """Convert AI / API values (str, int, Decimal) to an exact Decimal. Rejects floats' imprecision
    by going through str()."""
    if isinstance(value, Decimal):
        return quantize(value)
    if isinstance(value, bool) or value is None:
        raise AmountParseError("invalid amount")
    if isinstance(value, int):
        return quantize(Decimal(value))
    if isinstance(value, float):
        return quantize(Decimal(str(value)))
    return parse_amount(str(value))


def format_money(value: Decimal | None, currency: str = "UZS", signed: bool = False) -> str:
    if value is None:
        return "—"
    q = quantize(Decimal(value))
    sign = ""
    if signed and q > 0:
        sign = "+"
    if q < 0:
        sign = "-"
        q = -q
    text = f"{q:,.2f}".replace(",", " ")
    if text.endswith(".00"):
        text = text[:-3]
    return f"{sign}{text} {currency}"


_MULTIPLIERS = {
    "ming": Decimal(1000), "минг": Decimal(1000), "k": Decimal(1000), "thousand": Decimal(1000),
    "тысяч": Decimal(1000), "тыс": Decimal(1000),
    "mln": Decimal(1_000_000), "million": Decimal(1_000_000), "millon": Decimal(1_000_000),
    "миллион": Decimal(1_000_000), "млн": Decimal(1_000_000),
    "mlrd": Decimal(1_000_000_000), "milliard": Decimal(1_000_000_000), "billion": Decimal(1_000_000_000),
}
_NUM_WITH_MULT_RE = re.compile(
    r"(\d[\d'’`ʼ  .,]*\d|\d)\s*(" + "|".join(sorted(map(re.escape, _MULTIPLIERS), key=len, reverse=True)) + r")?\b",
    re.IGNORECASE,
)


def candidate_amounts(text: str) -> set[Decimal]:
    """Deterministically extract possible amounts from free text ("20 ming", "1.5 mln", "19 000").

    Used to cross-check amounts extracted by AI; if the AI amount is not among these candidates the
    user must confirm the operation.
    """
    out: set[Decimal] = set()
    for m in _NUM_WITH_MULT_RE.finditer(text or ""):
        raw, mult = m.group(1).strip(), (m.group(2) or "").lower()
        try:
            value = parse_amount(raw)
        except AmountParseError:
            continue
        out.add(abs(value))
        if mult:
            out.add(abs(quantize(value * _MULTIPLIERS[mult])))
    return out
