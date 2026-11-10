from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_APOSTROPHES = "ʻʼ‘’`´"


def unify_apostrophes(s: str) -> str:
    for ch in _APOSTROPHES:
        s = s.replace(ch, "'")
    return s


def normalize_name(s: str) -> str:
    """Normalization used for categories and people: case/space/apostrophe insensitive."""
    s = unicodedata.normalize("NFKC", s or "")
    s = unify_apostrophes(s).lower().strip()
    s = re.sub(r"[\s_]+", " ", s)
    s = s.strip(" .,;:!?-")
    return s


def normalize_merchant(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    s = unify_apostrophes(s).upper().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def singular(s: str) -> str:
    if len(s) > 4 and s.endswith("ies"):
        return s[:-3] + "y"
    if len(s) > 3 and s.endswith("s") and not s.endswith("ss"):
        return s[:-1]
    return s


# Uzbek case suffixes that may stick to a name ("Orzubekdan", "Aliga", "Alining").
_UZ_SUFFIXES = ("ning", "dan", "ga", "ka", "qa", "ni", "da", "ga", "lar")


def strip_uz_suffix(name: str) -> str:
    for suf in _UZ_SUFFIXES:
        if name.endswith(suf) and len(name) - len(suf) >= 3:
            return name[: -len(suf)]
    return name


def escape_html(s: str | None) -> str:
    if s is None:
        return ""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
