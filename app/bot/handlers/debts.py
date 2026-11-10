from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bot.context import BotContext
from app.bot.keyboards.main import BTN_DEBTS, chunk, inline
from app.debts.service import DebtError, cancel_debt, cancel_payment, debt_overview, person_history
from app.models.enums import DebtDirection, DebtStatus
from app.utils.money import format_money
from app.utils.text import escape_html

router = Router(name="debts")


async def _overview_text(ctx: BotContext) -> tuple[str, list]:
    cur = ctx.settings.currency
    async with ctx.sf() as s:
        ov = await debt_overview(s)
    owe_me = [p for p in ov.people if p.they_owe_me > 0]
    i_owe = [p for p in ov.people if p.i_owe_them > 0]
    lines = ["🤝 <b>Debts</b>", ""]
    lines.append("🟢 <b>People who owe me</b>")
    lines += [f"   {escape_html(p.name)}: {format_money(p.they_owe_me, p.currency)}" for p in owe_me] or ["   —"]
    lines += ["", "🔴 <b>People I owe</b>"]
    lines += [f"   {escape_html(p.name)}: {format_money(p.i_owe_them, p.currency)}" for p in i_owe] or ["   —"]
    owed_to_me, owe, net = ov.totals(cur)
    lines += [
        "",
        f"Total owed to me: <b>{format_money(owed_to_me, cur)}</b>",
        f"Total I owe: <b>{format_money(owe, cur)}</b>",
        f"Net position: <b>{format_money(net, cur, signed=True)}</b>",
        "",
        "<i>Add or repay debts by writing or sending a voice message, e.g. “19 000 Orzubekdan qarz bo'ldim”. Tap a name for history.</i>",
    ]
    seen, buttons = set(), []
    for p in ov.people:
        if p.person_id not in seen:
            seen.add(p.person_id)
            buttons.append((p.name, f"debt:p:{p.person_id}"))
    return "\n".join(lines), chunk(buttons, 3)


@router.message(F.text == BTN_DEBTS)
async def debts(message: Message, ctx: BotContext) -> None:
    text, buttons = await _overview_text(ctx)
    await message.answer(text, reply_markup=inline(buttons) if buttons else None)


@router.callback_query(F.data.regexp(r"^debt:p:(\d+)$").as_("m"))
async def person(cb: CallbackQuery, ctx: BotContext, m) -> None:
    tz = await ctx.tz()
    async with ctx.sf() as s:
        try:
            h = await person_history(s, int(m.group(1)))
        except DebtError:
            await cb.answer("Not found", show_alert=True)
            return
    p = h["person"]
    lines = [f"👤 <b>{escape_html(p.name)}</b> — history", ""]
    buttons = []
    for e in sorted(h["events"], key=lambda e: e["date"], reverse=True):  # newest first
        when = e["date"].astimezone(tz).strftime("%d.%m.%Y")
        amount = format_money(e["amount"], e["currency"])
        why = e.get("purpose") or e.get("description")
        why_text = f" — {escape_html(why)}" if why else ""
        if e["type"] == "debt":
            who = "I borrowed" if e["direction"] == DebtDirection.I_OWE_THEM else "They borrowed"
            status = {"open": f"left {format_money(e['outstanding'], e['currency'])}", "settled": "paid off ✅",
                      "cancelled": "cancelled ✖️"}[e["status"]]
            strike = ("<s>", "</s>") if e["status"] == DebtStatus.CANCELLED else ("", "")
            lines.append(f"{strike[0]}📌 <b>{when}</b> · {who} <b>{amount}</b>{why_text}\n      {status}{strike[1]}  /debtdel_{e['id']}")
        else:
            who = "I paid back" if e["direction"] == DebtDirection.I_OWE_THEM else "They paid back"
            covered = f" → {escape_html(e['purpose'])}" if e.get("purpose") else ""
            strike = ("<s>", "</s>") if e["status"] == "cancelled" else ("", "")
            lines.append(f"{strike[0]}   ↩️ {when} · {who} {amount}{covered}{strike[1]}  /paydel_{e['id']}")
    lines += ["", "<i>Use /debtdel_ID or /paydel_ID to cancel a wrong entry (kept in audit history).</i>"]
    await cb.message.answer("\n".join(lines), reply_markup=inline(buttons) if buttons else None)
    await cb.answer()


@router.message(F.text.regexp(r"^/(debtdel|paydel)_(\d+)$").as_("m"))
async def ask_cancel(message: Message, m) -> None:
    kind, ref = m.group(1), m.group(2)
    label = "debt" if kind == "debtdel" else "repayment"
    await message.answer(
        f"Cancel this {label} (#{ref})? It stays in the audit history.",
        reply_markup=inline([[("🗑 Yes, cancel it", f"dc:{kind}:{ref}"), ("Keep", "noop")]]),
    )


@router.callback_query(F.data.regexp(r"^dc:(debtdel|paydel):(\d+)$").as_("m"))
async def do_cancel(cb: CallbackQuery, ctx: BotContext, m) -> None:
    kind, ref = m.group(1), int(m.group(2))
    try:
        async with ctx.sf.begin() as s:
            if kind == "debtdel":
                await cancel_debt(s, ref, reason="cancelled from bot", actor="owner:bot")
            else:
                await cancel_payment(s, ref, reason="cancelled from bot", actor="owner:bot")
    except DebtError as exc:
        await cb.answer(str(exc), show_alert=True)
        return
    await cb.message.edit_text("🗑 Cancelled (kept in the audit history).")
    await cb.answer()
