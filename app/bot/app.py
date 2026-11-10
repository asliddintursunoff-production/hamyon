from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands, MenuButtonWebApp, WebAppInfo

from app.bot.context import BotContext
from app.bot.handlers import assistant, auth, debts, menu
from app.bot.keyboards.main import inline
from app.bot.middleware import PrivateChatOnlyMiddleware, RequireUnlockedMiddleware
from app.models.enums import TxDirection, TxType
from app.bot.services.questions import question_markup, question_text
from app.transactions.questions import load_for_question, remember_question
from app.utils.tasks import cancel_tasks, supervised
from app.webapp_url import resolve_webapp_base

log = logging.getLogger(__name__)


def build_dispatcher(ctx: BotContext) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp.update.outer_middleware(PrivateChatOnlyMiddleware())
    for r in (menu.router, debts.router, assistant.router):
        r.message.middleware(RequireUnlockedMiddleware())
        r.callback_query.middleware(RequireUnlockedMiddleware())
    # auth first (no unlock required), assistant last (catch-all for free text/voice)
    dp.include_routers(auth.router, menu.router, debts.router, assistant.router)
    return dp


QUESTION_DELAY_FOR_TRANSFERS = 20  # seconds: the other half of an own-card transfer arrives within seconds


class BotService:
    """Runs aiogram polling inside the FastAPI process; asks the owner about unclear payments."""

    def __init__(self, token: str, ctx: BotContext):
        self.ctx = ctx
        self.bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = build_dispatcher(ctx)
        self._task: asyncio.Task | None = None
        self._tasks: list[asyncio.Task] = []
        self._menu_url: str | None = "unset"  # forces the first update, even to "no URL"
        self._pending_questions: set[asyncio.Task] = set()

    async def start(self) -> None:
        try:
            await self.bot.set_my_commands([BotCommand(command="start", description="Start / unlock")])
        except Exception:
            log.warning("could not set bot commands (network?)")
        self._task = asyncio.create_task(
            self.dp.start_polling(self.bot, handle_signals=False, allowed_updates=self.dp.resolve_used_update_types()),
            name="telegram-bot-polling",
        )
        self._task.add_done_callback(self._on_done)
        self._tasks.append(supervised("webapp-menu-button", self._keep_menu_button))

    async def _keep_menu_button(self) -> None:
        """Point the bot's Menu button at the analytics Mini App (needs a public HTTPS URL)."""
        while True:
            base = await resolve_webapp_base(self.ctx.settings)
            url = f"{base}/app" if base else None
            if url != self._menu_url:
                if url:
                    await self.bot.set_chat_menu_button(
                        menu_button=MenuButtonWebApp(text="📊 Analytics", web_app=WebAppInfo(url=url))
                    )
                    log.info("Menu button opens the analytics Mini App at %s", url)
                else:
                    await self.bot.set_chat_menu_button(menu_button=MenuButtonCommands())
                    log.warning("No public HTTPS URL yet: set PUBLIC_BASE_URL or start the tunnel to enable the Mini App")
                self._menu_url = url
            await asyncio.sleep(60)

    # ------------------------------------------------------------------ pipeline notifier
    async def transaction_processed(self, tx_id: int) -> None:
        """Called for fresh bank transactions. Ask "what was this for?" when the purpose is unclear."""
        tx = await load_for_question(self.ctx.sf, tx_id)
        if tx is None:
            return
        delay = QUESTION_DELAY_FOR_TRANSFERS if tx.transaction_type == TxType.TRANSFER else 0
        task = asyncio.create_task(self._ask_later(tx_id, delay), name=f"ask-{tx_id}")
        self._pending_questions.add(task)
        task.add_done_callback(self._pending_questions.discard)

    async def _ask_later(self, tx_id: int, delay: float) -> None:
        try:
            if delay:
                await asyncio.sleep(delay)
            tx = await load_for_question(self.ctx.sf, tx_id)  # re-check: may now be an own-card transfer
            if tx is None:
                return
            recipients = await self.ctx.auth.unlocked_user_ids()
            if not recipients:
                return
            text = question_text(tx)
            for chat_id in recipients:
                try:
                    msg = await self.bot.send_message(chat_id, text, reply_markup=question_markup(tx.id, tx.direction == TxDirection.IN))
                    await remember_question(self.ctx.sf, tx.id, chat_id, msg.message_id)
                except Exception:
                    log.warning("could not send question to %s", chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("asking about transaction %s failed", tx_id)

    @staticmethod
    def _on_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("bot polling stopped: %r", task.exception())

    async def stop(self) -> None:
        await cancel_tasks(self._tasks + list(self._pending_questions))
        try:
            await self.dp.stop_polling()
        except Exception:
            pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        await self.bot.session.close()
