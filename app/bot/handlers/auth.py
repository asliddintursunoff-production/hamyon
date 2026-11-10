"""/start -> ask for the secret access code -> unlock the bot."""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, ReplyKeyboardRemove

from app.bot.context import BotContext
from app.bot.keyboards.main import main_menu

log = logging.getLogger(__name__)
router = Router(name="auth")


class AuthStates(StatesGroup):
    waiting_code = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, ctx: BotContext) -> None:
    if not ctx.auth.code_configured:
        await message.answer(
            "⚠️ The access code is not configured on the server.\n"
            "Set <code>BOT_ACCESS_CODE</code> in the .env file and restart the application."
        )
        return
    if await ctx.auth.is_authenticated(message.from_user.id):
        await state.clear()
        await message.answer("👋 Welcome back! Choose an option below.", reply_markup=main_menu())
        return
    locked = await ctx.auth.locked_until(message.from_user.id)
    if locked:
        tz = await ctx.tz()
        await message.answer(f"⛔ Too many wrong attempts. Try again after {locked.astimezone(tz):%H:%M}.")
        return
    await state.set_state(AuthStates.waiting_code)
    await message.answer(
        "🔐 <b>Enter your access code</b>\n<i>The message with the code will be deleted right away.</i>",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(StateFilter(AuthStates.waiting_code), F.text)
async def code_entered(message: Message, state: FSMContext, ctx: BotContext) -> None:
    code = message.text or ""
    try:
        await message.delete()  # don't leave the secret in the chat history
    except Exception:
        pass
    if code.startswith("/") and code.strip() != "/start":
        await message.answer("🔐 Please enter the access code (or /start to begin again).")
        return
    result = await ctx.auth.verify(message.from_user.id, code)
    if result.ok:
        await state.clear()
        log.info("bot unlocked by owner")
        await message.answer(
            "🔓 Unlocked.\n\n"
            "• Write or send a voice message to record debts, repayments, expenses or income\n"
            "• 💳 Balance — card balances (refreshed on tap)\n"
            "• 🤝 Debts — who owes whom\n"
            "• Menu button → 📊 Analytics",
            reply_markup=main_menu(),
        )
        return
    if result.not_configured:
        await state.clear()
        await message.answer("⚠️ The access code is not configured on the server.")
        return
    if result.locked_until:
        await state.clear()
        log.warning("bot access locked after repeated wrong codes")
        tz = await ctx.tz()
        await message.answer(f"⛔ Too many wrong attempts. The bot is locked until {result.locked_until.astimezone(tz):%H:%M}.")
        return
    await message.answer(f"❌ Wrong code. Attempts left: {result.attempts_left}.")


@router.message(StateFilter(AuthStates.waiting_code))
async def code_expected(message: Message) -> None:
    await message.answer("🔐 Please type your access code.")


@router.message(Command("lock"))
async def lock(message: Message, state: FSMContext, ctx: BotContext) -> None:
    await ctx.auth.lock(message.from_user.id)
    await state.clear()
    await message.answer("🔒 Locked. Send /start to unlock.", reply_markup=ReplyKeyboardRemove())
