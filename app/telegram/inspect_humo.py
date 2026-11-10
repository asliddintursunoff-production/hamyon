"""Show the buttons the HUMO Card bot actually offers (read-only; nothing is clicked or sent).

    python -m app.telegram.inspect_humo

Use the output to confirm which button returns card balances, and if automatic detection does not
find it, put its exact text in HUMO_BALANCE_BUTTON_TEXT.
"""
import asyncio
import re

from app.config import get_settings
from app.telegram.buttons import list_buttons
from app.telegram.client import build_client
from app.transactions.parser import detect_kind


async def main() -> None:
    settings = get_settings()
    client = build_client(settings)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Not logged in. Run `python -m app.telegram.login` first.")
    entity = await client.get_input_entity(settings.humo_bot_username)
    pattern = re.compile(settings.humo_balance_button_pattern)

    print(f"Recent buttons in @{settings.humo_bot_username} (newest first):")
    seen = set()
    for _, info in await list_buttons(client, entity, limit=50):
        key = (info.text, info.kind)
        if key in seen:
            continue
        seen.add(key)
        mark = "  <- matches balance pattern" if info.safe and pattern.search(info.text) else ""
        print(f"  [{info.kind}{'' if info.safe else ', NOT clickable by app'}] {info.text!r} (message {info.message_id}){mark}")
    if not seen:
        print("  (no buttons found in the last 50 messages - open the HUMO bot and send /start once)")

    print("\nRecent message kinds:")
    async for msg in client.iter_messages(entity, limit=15):
        if not msg.out:
            print(f"  #{msg.id} {msg.date:%Y-%m-%d %H:%M} {detect_kind(msg.raw_text or '')}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
