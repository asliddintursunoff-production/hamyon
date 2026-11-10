"""History import: whole history on first start, offline recovery, and back-fill of older messages."""
from datetime import timedelta

from sqlalchemy import func, select

from app.models import TelegramMessage
from app.telegram.balance_sync import BalanceSyncCoordinator
from app.telegram.history_sync import sync_history
from app.telegram.listener import HumoMessageHandler
from app.transactions.pipeline import MessageProcessor, WorkerPool
from app.utils.timeutil import utcnow
from tests.conftest import FakeAI

HUMO = 4242


class Msg:
    def __init__(self, id):
        self.id, self.out, self.sender_id, self.edit_date = id, False, HUMO, None
        self.date = utcnow() - timedelta(days=1000 - id)
        self.raw_text = f"🔹 HUMOCARD *5592\n💵 {id}.00 UZS"


class FakeChat:
    """Minimal Telethon stand-in implementing the iter_messages arguments we use."""

    def __init__(self, ids):
        self.msgs = [Msg(i) for i in ids]  # oldest first

    def iter_messages(self, entity, reverse=False, limit=None, min_id=0, max_id=0, offset_date=None):
        items = [m for m in self.msgs if m.id > min_id and (not max_id or m.id < max_id)]
        if offset_date is not None:
            items = [m for m in items if m.date >= offset_date]
        if not reverse:
            items = list(reversed(items))
        if limit:
            items = items[:limit]

        async def gen():
            for m in items:
                yield m
        return gen()

    async def get_messages(self, entity, limit=1):
        return list(reversed(self.msgs))[:limit]


async def stored(sf):
    async with sf() as s:
        return sorted((await s.scalars(select(TelegramMessage.telegram_message_id))).all())


def make(sf, settings, owner_id):
    proc = MessageProcessor(sf, FakeAI(), settings, owner_id)
    handler = HumoMessageHandler(proc, WorkerPool(proc), BalanceSyncCoordinator(sf, settings), settings)
    handler.humo_chat_id = HUMO
    return handler


async def test_first_start_imports_whole_history(sf, settings, owner_id):
    handler = make(sf, settings, owner_id)
    await sync_history(FakeChat(range(1, 121)), object(), handler, sf, None)
    assert await stored(sf) == list(range(1, 121))


async def test_existing_database_backfills_older_and_recovers_newer(sf, settings, owner_id):
    handler = make(sf, settings, owner_id)
    await sync_history(FakeChat(range(50, 81)), object(), handler, sf, None)  # was started with a date limit
    chat = FakeChat(range(1, 101))  # full chat now: older 1..49 exist, and 81..100 arrived while offline
    await sync_history(chat, object(), handler, sf, None)
    assert await stored(sf) == list(range(1, 101))
    await sync_history(chat, object(), handler, sf, None)  # idempotent
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(TelegramMessage)) == 100
