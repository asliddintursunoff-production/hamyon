"""💳 Balance: show last known balances immediately, then refresh them from the HUMO bot."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.context import BotContext
from app.bot.keyboards.main import BTN_BALANCE, inline, main_menu
from app.bot.services.formatting import card_line
from app.cards.service import list_cards, totals_by_currency
from app.telegram.balance_sync import BalanceRefreshError
from app.utils.money import format_money
from app.utils.text import escape_html

router = Router(name="menu")

REFRESH_AGAIN = inline([[("🔄 Refresh again", "bal:refresh")]])


async def _balances_text(ctx: BotContext, header: str) -> str:
    tz = ctx.settings.tz
    async with ctx.sf() as s:
        cards = await list_cards(s)
    totals = totals_by_currency(cards)
    total = " + ".join(format_money(v, c) for c, v in totals.items()) or "unknown"
    lines = [header, "", f"💰 <b>Total: {total}</b>", ""]
    lines += [card_line(c, tz) for c in cards]
    return "\n".join(lines)


async def _show_and_refresh(message: Message, ctx: BotContext) -> None:
    status = await message.answer(await _balances_text(ctx, "⏳ <i>Refreshing from HUMO…</i>"))
    try:
        await ctx.balance.refresh("bot")
        header = "✅ <b>Balances updated</b>"
    except BalanceRefreshError as exc:
        header = f"⚠️ {escape_html(str(exc))}\n<i>Showing last known balances.</i>"
    await status.edit_text(await _balances_text(ctx, header), reply_markup=REFRESH_AGAIN)


@router.message(F.text == BTN_BALANCE)
async def balance(message: Message, ctx: BotContext) -> None:
    await _show_and_refresh(message, ctx)


@router.callback_query(F.data == "bal:refresh")
async def balance_again(cb: CallbackQuery, ctx: BotContext) -> None:
    await cb.answer("Refreshing…")
    await cb.message.edit_reply_markup(reply_markup=None)
    await _show_and_refresh(cb.message, ctx)


@router.message(F.text == "/menu")
async def menu(message: Message) -> None:
    await message.answer("Menu:", reply_markup=main_menu())
