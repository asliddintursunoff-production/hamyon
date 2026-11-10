"""Test setup: real PostgreSQL (TEST_DATABASE_URL), mocked Gemini, no network, no real credentials."""
from __future__ import annotations

import getpass
import json
import os

# Isolate tests from the developer's .env (env vars take precedence over the .env file).
_TEST_ENV = {
    "APP_ENV": "test",
    "TELEGRAM_BOT_TOKEN": "",
    "TELETHON_API_ID": "",
    "TELETHON_API_HASH": "",
    "TELETHON_PHONE": "",
    "GEMINI_API_KEY": "",
    "BOT_ACCESS_CODE": "open-sesame-42",
    "SESSION_SECRET": "test-secret-test-secret-test-secret",
    "PUBLIC_BASE_URL": "http://testserver",
    "TIMEZONE": "Asia/Tashkent",
    "CURRENCY": "UZS",
    "INITIAL_CARDS": "8549:AO ANOR BANK,3798:TBCBANK,5592:OCTOBANK",
    "AI_MIN_INTERVAL_SECONDS": "0",
}
os.environ.update(_TEST_ENV)
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", f"postgresql+asyncpg://{getpass.getuser()}@/expense_test?host=/var/run/postgresql"
)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

import asyncio  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.ai.gemini import AIInvalidResponse, AIUnavailable, parse_json_response  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database.seed import seed  # noqa: E402
from app.database.session import create_session_factory  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def migrated_database():
    """Build the schema with the real Alembic migrations (validates them too)."""
    from alembic import command
    from alembic.config import Config

    async def reset():
        eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(reset())
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.attributes["database_url"] = TEST_DATABASE_URL
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sf(engine):
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def owner_id(sf, settings):
    return await seed(sf, settings)


class FakeAI:
    """Deterministic Gemini stand-in. `responder(prompt, audio)` returns a dict, raw str, or raises."""

    model = "fake-gemini"

    def __init__(self, responder=None):
        self.responder = responder
        self.calls: list[str] = []

    async def generate_json(self, prompt, schema, *, system, audio=None, background=False):
        self.calls.append(prompt)
        if self.responder is None:
            raise AIUnavailable("no responder")
        out = self.responder(prompt, audio)
        if isinstance(out, Exception):
            raise out
        raw = out if isinstance(out, str) else json.dumps(out)
        return parse_json_response(raw, schema), raw


@pytest.fixture
def fake_ai():
    return FakeAI()


def tg_date(y=2026, mo=11, d=6, h=4, mi=59, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


HUMO_A = """💸 To'lov
➖ 3.000,00 UZS
📍 MCHJ NAVROZ ABDUAHAT
💳 HUMOCARD *5592
🕓 22:23 05.11.2026
💰 13.299.141,60 UZS"""

HUMO_B = """💸 To'lov
➖ 10.070,00 UZS
📍 Octo Mobile P2P H2U>
💳 HUMOCARD *5592
🕓 22:24 05.11.2026
💰 13.289.071,60 UZS"""

HUMO_C = """💸 To'lov
➖ 16.213,00 UZS
📍 OOO WB TAXI OPLATA>T
💳 HUMOCARD *5592
🕓 09:59 06.11.2026
💰 13.272.858,60 UZS"""

HUMO_D = """💸 Amaliyot
➖ 100.300,00 UZS
📍 Octo Mobile P2P H2H>
💳 HUMOCARD *5592
🕓 12:12 06.11.2026
💰 13.170.158,60 UZS"""

BALANCES = """🔹 HUMOCARD AO ANOR BANK *8549
💵 0.00 UZS

🔹 HUMOCARD TBCBANK *3798
💵 796.22 UZS

🔹 HUMOCARD OCTOBANK *5592
💵 12'359'504.85 UZS"""

AIInvalid = AIInvalidResponse
