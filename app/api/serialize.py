from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from fastapi.responses import JSONResponse


def plain(obj):
    """JSON-safe conversion that keeps money exact (Decimal -> string, never float)."""
    if isinstance(obj, Decimal):
        return format(obj, "f")
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [plain(v) for v in obj]
    return obj


def json_response(obj, status_code: int = 200) -> JSONResponse:
    return JSONResponse(plain(obj), status_code=status_code)
