"""Free text and voice -> the shared assistant pipeline. Registered last so menu buttons win."""
from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from app.assistant.service import AssistantReply
from app.bot.context import BotContext
from app.bot.keyboards.main import MENU_BUTTONS, inline
from app.models.enums import TxSource
from app.bot.services.questions import sync_question_messages
from app.transactions.questions import find_question, mark_answered
from app.utils.text import escape_html

log = logging.getLogger(__name__)
router = Router(name="assistant")

MAX_VOICE_BYTES = 10 * 1024 * 1024
MAX_VOICE_SECONDS = 180


def _markup(reply: AssistantReply):
    rows = []
    if reply.kind == "confirm" and reply.pending_id:
        rows.append([("✅ Confirm", f"as:ok:{reply.pending_id}"), ("✖️ Cancel", f"as:no:{reply.pending_id}")])
    elif reply.kind == "clarify" and reply.pending_id and reply.options:
        rows.append([(label, f"as:dir:{reply.pending_id}:{value}") for label, value in reply.options])
        rows.append([("✖️ Cancel", f"as:no:{reply.pending_id}")])
    elif reply.kind == "saved" and reply.undo:
        rows.append([("↩️ Undo", f"undo:{reply.undo[0]}:{reply.undo[1]}")])
    return inline(rows) if rows else None


# ---------------------------------------------------------------- "what was this for?" answers
async def _download_voice(message: Message, bot: Bot) -> bytes | None:
    v = message.voice
    if (v.file_size or 0) > MAX_VOICE_BYTES or (v.duration or 0) > MAX_VOICE_SECONDS:
        return None
    file = await bot.get_file(v.file_id)
    return (await bot.download_file(file.file_path)).read()


@router.message(F.reply_to_message)
async def reply(message: Message, bot: Bot, ctx: BotContext) -> None:
    """A reply to a question explains that transaction; any other reply is handled like a normal message."""
    q = await find_question(ctx.sf, message.chat.id, message.reply_to_message.message_id)
    if q is None:
        if message.voice:
            await voice(message, bot, ctx)
        elif message.text and not message.text.startswith("/") and message.text not in MENU_BUTTONS:
            await text(message, ctx)
        else:
            await fallback(message)
        return
    key = f"bot:{message.chat.id}:{message.message_id}"
    tz = await ctx.tz()
    if message.voice:
        status = await message.answer("🎧 Listening…")
        audio = await _download_voice(message, bot)
        if audio is None:
            await status.edit_text("⚠️ Voice message is too long. Please keep it under 3 minutes.")
            return
        heard, result = await ctx.assistant.explain_transaction(
            q.transaction_id, audio=(audio, message.voice.mime_type or "audio/ogg"), source=TxSource.BOT_VOICE,
            idempotency_key=key, tz=tz)
    elif message.text:
        status = await message.answer("⏳ Understanding…")
        heard, result = await ctx.assistant.explain_transaction(
            q.transaction_id, text=message.text, source=TxSource.BOT_TEXT, idempotency_key=key, tz=tz)
        heard = None  # the owner can see what they typed
    else:
        await message.answer("Please reply with text or a voice message.")
        return
    if result.kind == "saved":
        await mark_answered(ctx.sf, q.transaction_id)
        await sync_question_messages(bot, ctx.sf, q.transaction_id)
    prefix = f"🗣 <i>“{escape_html(heard)}”</i>\n\n" if heard else ""
    await status.edit_text(prefix + result.text, reply_markup=_markup(result))


@router.callback_query(F.data.regexp(r"^q:(\d+):(food|groceries|taxi|shopping|bills|transfer|income|keep|edit)$").as_("m"))
async def quick_answer(cb: CallbackQuery, bot: Bot, ctx: BotContext, m) -> None:
    tx_id, choice = int(m.group(1)), m.group(2)
    if choice == "keep":
        await cb.message.edit_reply_markup(reply_markup=None)
        await cb.answer("OK. You can still reply to it later.")
        return
    if choice == "edit":  # re-open the choices of an answered question
        await sync_question_messages(bot, ctx.sf, tx_id, editing=True)
        await cb.answer("Choose again, or reply with text/voice")
        return
    result = await ctx.assistant.quick_explain(tx_id, choice)
    if result.kind == "saved":
        await mark_answered(ctx.sf, tx_id)
        await sync_question_messages(bot, ctx.sf, tx_id)
        await cb.answer("✅")
    else:
        await cb.answer(re.sub("<[^>]+>", "", result.text)[:190], show_alert=True)


@router.message(F.voice)
async def voice(message: Message, bot: Bot, ctx: BotContext) -> None:
    v = message.voice
    if (v.file_size or 0) > MAX_VOICE_BYTES or (v.duration or 0) > MAX_VOICE_SECONDS:
        await message.answer("⚠️ Voice message is too long. Please keep it under 3 minutes.")
        return
    status = await message.answer("🎧 Listening…")
    file = await bot.get_file(v.file_id)
    buf = await bot.download_file(file.file_path)
    audio = buf.read()
    transcript, reply = await ctx.assistant.handle_voice(
        audio, v.mime_type or "audio/ogg", idempotency_key=f"bot:{message.chat.id}:{message.message_id}", tz=await ctx.tz()
    )
    heard = f"🗣 <i>“{escape_html(transcript)}”</i>\n\n" if transcript else ""
    await status.edit_text(heard + reply.text, reply_markup=_markup(reply))


@router.message(F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS))
async def text(message: Message, ctx: BotContext) -> None:
    status = await message.answer("⏳ Understanding…")
    reply = await ctx.assistant.handle_text(
        message.text, source=TxSource.BOT_TEXT, idempotency_key=f"bot:{message.chat.id}:{message.message_id}", tz=await ctx.tz()
    )
    await status.edit_text(reply.text, reply_markup=_markup(reply))


@router.callback_query(F.data.regexp(r"^as:ok:(\d+)$").as_("m"))
async def confirm(cb: CallbackQuery, bot: Bot, ctx: BotContext, m) -> None:
    reply = await ctx.assistant.confirm(int(m.group(1)), tz=await ctx.tz())
    await cb.message.edit_text(reply.text, reply_markup=_markup(reply))
    if reply.kind == "saved" and reply.transaction_id:
        await mark_answered(ctx.sf, reply.transaction_id)
        await sync_question_messages(bot, ctx.sf, reply.transaction_id)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^as:dir:(\d+):(i_owe_them|they_owe_me)$").as_("m"))
async def choose_direction(cb: CallbackQuery, ctx: BotContext, m) -> None:
    reply = await ctx.assistant.confirm(int(m.group(1)), tz=await ctx.tz(), direction=m.group(2))
    await cb.message.edit_text(reply.text, reply_markup=_markup(reply))
    await cb.answer()


@router.callback_query(F.data.regexp(r"^as:no:(\d+)$").as_("m"))
async def cancel(cb: CallbackQuery, ctx: BotContext, m) -> None:
    await ctx.assistant.cancel(int(m.group(1)))
    await cb.message.edit_text("✖️ Cancelled. Nothing was saved.")
    await cb.answer()


@router.callback_query(F.data.regexp(r"^undo:(debt|tx|repayment|explain):(.+)$").as_("m"))
async def undo(cb: CallbackQuery, bot: Bot, ctx: BotContext, m) -> None:
    result = await ctx.assistant.undo(m.group(1), m.group(2))
    await cb.message.edit_text(f"{cb.message.html_text}\n\n{result}", reply_markup=None)
    if m.group(1) == "explain":
        await sync_question_messages(bot, ctx.sf, int(m.group(2)))
    await cb.answer()


@router.message()
async def fallback(message: Message) -> None:
    await message.answer("Write or send a voice message, or use 💳 Balance / 🤝 Debts below. /menu")
