"""Single deployable service: FastAPI dashboard + Telegram bot + Telethon client + background workers.

Run with ONE worker only (a Telethon session must not be shared by several processes):
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.ai.gemini import build_ai_client
from app.api.auth import LoginRateLimiter
from app.bot.services.auth import BotAuthService
from app.api.routes import api, pages
from app.assistant.service import Assistant
from app.config import get_settings
from app.database.seed import get_owner, seed, set_owner_telegram_id
from app.database.session import create_engine, create_session_factory
from app.singleton import InstanceLock
from app.telegram.balance_sync import BalanceSyncCoordinator
from app.telegram.client import TelethonService
from app.telegram.listener import HumoMessageHandler
from app.transactions.maintenance import upgrade_existing_data
from app.transactions.pipeline import MessageProcessor, WorkerPool

log = logging.getLogger("app")


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Libraries that might log request URLs/headers are kept quiet so secrets never reach logs.
    for noisy in ("httpx", "httpcore", "aiohttp", "telethon", "google_genai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = app.state.settings
    engine = create_engine(settings.database_url)
    sf = create_session_factory(engine)
    owner_id = await seed(sf, settings)

    ai = build_ai_client(settings)
    processor = MessageProcessor(sf, ai, settings, owner_id)
    pool = WorkerPool(processor, settings.worker_count, settings.queue_maxsize)
    balance = BalanceSyncCoordinator(sf, settings)
    handler = HumoMessageHandler(processor, pool, balance, settings)
    telethon = TelethonService(settings, sf, handler, balance)
    owner_tg = settings.owner_telegram_id
    lock = InstanceLock(engine)
    started: dict[str, object] = {}

    async def run_background_side() -> None:
        await lock.acquire()  # waits while an older deployment is still running
        await processor.reset_stale_leases()
        try:
            await upgrade_existing_data(sf)
        except Exception:
            log.exception("data upkeep failed (continuing)")
        pool.start()
        started["pool"] = pool
        try:
            await telethon.start()
        except Exception:
            log.exception("Telethon failed to start; HUMO ingestion disabled until restart")
        started["telethon"] = telethon

        async with sf() as s:
            stored_tg = (await get_owner(s)).telegram_id
        if owner_tg != stored_tg:
            await set_owner_telegram_id(sf, owner_id, owner_tg)

        if settings.telegram_bot_token:
            from app.bot.app import BotService
            from app.bot.context import BotContext

            ctx = BotContext(
                settings=settings, sf=sf, auth=BotAuthService(sf, settings), assistant=Assistant(sf, ai, settings),
                processor=processor, balance=balance, owner_db_id=owner_id, owner_telegram_id=owner_tg,
            )
            bot_service = BotService(settings.telegram_bot_token.get_secret_value(), ctx)
            processor.notifier = bot_service  # asks "what was this for?" about unclear fresh payments
            await bot_service.start()
            started["bot"] = bot_service
            await processor.notify_recent()  # questions not sent because of a restart
        else:
            log.warning("TELEGRAM_BOT_TOKEN not set; bot disabled")
        log.info("telegram side started")

    app.state.sf = sf
    app.state.bot_auth = BotAuthService(sf, settings)
    app.state.processor = processor
    app.state.balance = balance
    app.state.telethon = telethon
    background = asyncio.create_task(run_background_side(), name="telegram-side")
    background.add_done_callback(
        lambda t: t.cancelled() or t.exception() is None or log.error("telegram side failed", exc_info=t.exception())
    )
    log.info("application started")
    try:
        yield
    finally:
        background.cancel()
        try:
            await background
        except (asyncio.CancelledError, Exception):
            pass  # already logged by the done callback
        if "bot" in started:
            await started["bot"].stop()
        if "telethon" in started:
            await telethon.stop()
        if "pool" in started:
            await pool.stop()
        await lock.release()
        await engine.dispose()
        log.info("application stopped")


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    app = FastAPI(title="Personal Finance", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.login_limiter = LoginRateLimiter()

    secret = settings.derived_session_secret()
    app.add_middleware(
        SessionMiddleware, secret_key=secret, session_cookie="finance_session", max_age=7 * 24 * 3600,
        same_site="lax", https_only=settings.is_production,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        # The Mini App may be embedded by Telegram Web; everything else must never be framed.
        frame = "https://web.telegram.org https://*.telegram.org" if request.url.path == "/app" else "'none'"
        if frame == "'none'":
            response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com https://telegram.org; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
            f"frame-ancestors {frame}; base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        return response

    static_dir = Path(__file__).resolve().parents[1] / "frontend" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    app.include_router(pages.router)
    app.include_router(api.router)
    return app


app = create_app()
