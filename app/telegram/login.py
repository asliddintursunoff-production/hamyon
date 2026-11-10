"""One-time interactive login for the personal Telegram account.

    python -m app.telegram.login

Creates the Telethon session file at TELETHON_SESSION_PATH. Keep that file secret and on a persistent
volume. The login code and 2FA password are typed into this terminal only and are never logged.
"""
import asyncio
import getpass

from app.config import get_settings
from app.telegram.client import build_client


async def main() -> None:
    settings = get_settings()
    if not settings.telethon_configured:
        raise SystemExit("Set TELETHON_API_ID and TELETHON_API_HASH in .env first.")
    client = build_client(settings)
    phone = settings.telethon_phone or input("Phone number (international format, e.g. +998901234567): ").strip()
    await client.start(
        phone=lambda: phone,
        code_callback=lambda: input("Login code sent by Telegram: ").strip(),
        password=lambda: getpass.getpass("Two-step verification password (if enabled): "),
    )
    me = await client.get_me()
    print(f"Logged in as {me.first_name or ''} (id {me.id}). Session saved to {settings.telethon_session_path}.session")
    print(f"Tip: set OWNER_TELEGRAM_ID={me.id} in .env")
    try:
        humo = await client.get_entity(settings.humo_bot_username)
        print(f"HUMO bot found: @{getattr(humo, 'username', '?')} (id {humo.id})")
    except Exception as exc:  # noqa: BLE001
        print(f"Warning: could not find HUMO bot @{settings.humo_bot_username}: {type(exc).__name__}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
