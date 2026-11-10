"""Balance messages and the Refresh Balances flow (Telethon replaced by fakes)."""
import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import BalanceSnapshot, BalanceSyncRequest, Card, Transaction
from app.telegram.balance_sync import BalanceRefreshError, BalanceSyncCoordinator
from app.telegram.listener import HumoMessageHandler
from app.transactions.pipeline import MessageProcessor, WorkerPool
from app.utils.timeutil import utcnow
from tests.conftest import BALANCES, FakeAI

HUMO = 4242


class FakeButton:
    def __init__(self, text, on_click, kind="ButtonTypeDefault"):
        self.text = text
        self.url = None
        self.button = SimpleNamespace(type=type(kind, (), {})())
        self._on_click = on_click
        self.clicks = 0

    async def click(self):
        self.clicks += 1
        await self._on_click()


class FakeMsg:
    def __init__(self, id, text="", buttons=None, out=False, date=None, edit_date=None):
        self.id, self.raw_text, self.out = id, text, out
        self.sender_id = HUMO
        self.date = date or utcnow()
        self.edit_date = edit_date
        self.reply_markup = object() if buttons else None
        self._buttons = buttons

    async def get_buttons(self):
        return self._buttons


class FakeClient:
    def __init__(self, history):
        self.history = history  # newest first

    def is_connected(self):
        return True

    async def iter_messages_list(self, limit):
        return self.history[:limit]

    def iter_messages(self, entity, limit=50, **kw):
        items = self.history[:limit]

        async def gen():
            for m in items:
                yield m
        return gen()

    async def get_messages(self, entity, limit=1):
        return self.history[:limit]


def setup(sf, settings, owner_id, *, answer_text=BALANCES, delay=0.01, answer=True, old_id=None):
    proc = MessageProcessor(sf, FakeAI(), settings, owner_id)
    pool = WorkerPool(proc)
    coord = BalanceSyncCoordinator(sf, settings)
    handler = HumoMessageHandler(proc, pool, coord, settings)
    handler.humo_chat_id = HUMO
    history = []
    next_id = {"v": 100}

    async def on_click():
        if not answer:
            return
        async def reply():
            await asyncio.sleep(delay)
            if old_id is not None:  # an OLD balance message shows up (e.g. from history sync) - must be ignored
                await handler.handle(FakeMsg(old_id, BALANCES.replace("0.00 UZS", "5.00 UZS"), date=utcnow() - timedelta(days=1)))
                await asyncio.sleep(delay)
            next_id["v"] += 1
            await handler.handle(FakeMsg(next_id["v"], answer_text))
        asyncio.create_task(reply())

    button = FakeButton("💰 Balans", on_click)
    history.append(FakeMsg(100, "Menu", buttons=[[FakeButton("📞 Contact", on_click, "ButtonTypeRequestPhone"), button]]))
    coord.attach(FakeClient(history), entity=object())
    return proc, coord, handler, button


async def test_balance_message_updates_cards_without_transactions(sf, settings, owner_id):
    proc, coord, handler, _ = setup(sf, settings, owner_id)
    await handler.handle(FakeMsg(50, BALANCES))
    async with sf() as s:
        cards = {c.card_last_four: c for c in (await s.scalars(select(Card))).all()}
        snaps = await s.scalar(select(func.count()).select_from(BalanceSnapshot))
        txs = await s.scalar(select(func.count()).select_from(Transaction))
    assert cards["5592"].current_balance == Decimal("12359504.85")
    assert cards["3798"].current_balance == Decimal("796.22")
    assert cards["8549"].current_balance == Decimal("0.00")
    assert snaps == 3
    assert txs == 0


async def test_refresh_clicks_real_button_and_updates_balances(sf, settings, owner_id):
    proc, coord, handler, button = setup(sf, settings, owner_id)
    result = await coord.refresh("bot")
    assert button.clicks == 1
    assert {b.card_last_four for b in result.balances} == {"8549", "3798", "5592"}
    async with sf() as s:
        req = await s.get(BalanceSyncRequest, result.request_id)
        snaps = (await s.scalars(select(BalanceSnapshot))).all()
        txs = await s.scalar(select(func.count()).select_from(Transaction))
    assert req.status == "completed" and req.response_message_id == 101
    assert all(sn.source == "balance_refresh" and sn.sync_request_id == req.id for sn in snaps)
    assert txs == 0  # refreshing balances never creates income/expense


async def test_old_balance_message_cannot_satisfy_new_request(sf, settings, owner_id):
    proc, coord, handler, _ = setup(sf, settings, owner_id, old_id=90)
    result = await coord.refresh("bot")
    async with sf() as s:
        req = await s.get(BalanceSyncRequest, result.request_id)
        card = (await s.scalars(select(Card).where(Card.card_last_four == "8549"))).one()
    assert req.response_message_id == 101  # not the old message 90
    assert card.current_balance == Decimal("0.00")  # older snapshot (5.00) did not overwrite the fresh one


async def test_concurrent_refreshes_share_one_request(sf, settings, owner_id):
    proc, coord, handler, button = setup(sf, settings, owner_id, delay=0.05)
    r1, r2, r3 = await asyncio.gather(coord.refresh("bot"), coord.refresh("dashboard"), coord.refresh("bot"))
    assert button.clicks == 1
    assert r1.request_id == r2.request_id == r3.request_id


async def test_timeout_keeps_last_known_balances(sf, settings, owner_id):
    settings.balance_refresh_timeout_seconds = 0.2
    proc, coord, handler, _ = setup(sf, settings, owner_id)
    await handler.handle(FakeMsg(50, BALANCES))  # last known
    proc2, coord2, handler2, _ = setup(sf, settings, owner_id, answer=False)
    with pytest.raises(BalanceRefreshError):
        await coord2.refresh("bot")
    async with sf() as s:
        card = (await s.scalars(select(Card).where(Card.card_last_four == "5592"))).one()
        req = (await s.scalars(select(BalanceSyncRequest))).one()
    assert card.current_balance == Decimal("12359504.85")
    assert req.status == "timeout"


async def test_missing_button_is_an_error_not_a_guess(sf, settings, owner_id):
    settings.humo_balance_button_text = None
    proc, coord, handler, _ = setup(sf, settings, owner_id)
    coord.attach(FakeClient([FakeMsg(100, "hi", buttons=[[FakeButton("Settings", None)]])]), entity=object())
    with pytest.raises(BalanceRefreshError, match="balance button"):
        await coord.refresh("bot")


async def test_phone_request_button_is_never_pressed(sf, settings, owner_id):
    settings.humo_balance_button_text = None
    settings.humo_balance_button_pattern = r"(?i)contact"
    proc, coord, handler, _ = setup(sf, settings, owner_id)
    with pytest.raises(BalanceRefreshError):
        await coord.refresh("bot")


async def test_messages_before_start_date_are_not_collected(sf, settings, owner_id):
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    settings.history_start_date = date(2026, 10, 29)  # optional limit; default is the whole history

    proc, coord, handler, _ = setup(sf, settings, owner_id)
    old = datetime(2026, 10, 28, 23, 59, tzinfo=ZoneInfo("Asia/Tashkent"))
    new = datetime(2026, 10, 29, 0, 1, tzinfo=ZoneInfo("Asia/Tashkent"))
    await handler.handle(FakeMsg(60, BALANCES, date=old))
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(BalanceSnapshot)) == 0
    await handler.handle(FakeMsg(61, BALANCES, date=new))
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(BalanceSnapshot)) == 3


async def test_whole_history_is_collected_by_default(sf, settings, owner_id):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    assert settings.history_start_date is None
    proc, coord, handler, _ = setup(sf, settings, owner_id)
    await handler.handle(FakeMsg(5, BALANCES, date=datetime(2024, 1, 1, tzinfo=ZoneInfo("Asia/Tashkent"))))
    async with sf() as s:
        assert await s.scalar(select(func.count()).select_from(BalanceSnapshot)) == 3



async def test_inline_toggle_buttons_are_never_pressed(sf, settings, owner_id):
    """The inline 💰 under HUMO transactions toggles a setting; only the menu 'Balans' button may be used."""
    proc, coord, handler, menu_button = setup(sf, settings, owner_id)
    toggle_clicks = []

    async def toggled():
        toggle_clicks.append(1)

    inline_toggle = FakeButton("💰", toggled, "InlineButtonTypeCallback")
    inline_word = FakeButton("Balans", toggled, "InlineButtonTypeCallback")
    history = [FakeMsg(99, "tx", buttons=[[inline_toggle, inline_word]]), *coord.client.history]
    coord.attach(FakeClient(history), entity=object())
    settings.humo_balance_button_text = None
    await coord.refresh("bot")
    assert toggle_clicks == [] and menu_button.clicks == 1


async def test_configured_text_is_sent_when_keyboard_not_in_history(sf, settings, owner_id):
    proc, coord, handler, _ = setup(sf, settings, owner_id)
    sent = []

    class Client(FakeClient):
        async def send_message(self, entity, text, parse_mode=None):
            sent.append(text)
            asyncio.get_running_loop().call_later(0.01, lambda: asyncio.ensure_future(handler.handle(FakeMsg(500, BALANCES))))
            return FakeMsg(499, text, out=True)

    coord.attach(Client([FakeMsg(100, "tx", buttons=[[FakeButton("💰", None, "InlineButtonTypeCallback")]])]), entity=object())
    assert settings.humo_balance_button_text == "💰 Balans"
    result = await coord.refresh("bot")
    assert sent == ["💰 Balans"] and len(result.balances) == 3
