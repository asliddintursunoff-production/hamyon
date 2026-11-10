from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

BTN_BALANCE = "💳 Balance"
BTN_DEBTS = "🤝 Debts"

MENU_BUTTONS = {BTN_BALANCE, BTN_DEBTS}


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_BALANCE), KeyboardButton(text=BTN_DEBTS)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Type or send a voice message…",
    )


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row]
    )


def chunk(items: list[tuple[str, str]], size: int = 2) -> list[list[tuple[str, str]]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
