"""Helpers to inspect the HUMO bot's real buttons. We never invent callback data or button names."""
from __future__ import annotations

from dataclasses import dataclass

# Only these are safe to press: plain reply-keyboard text buttons and inline callback buttons.
# Phone/location/URL/payment buttons are never pressed.
_SAFE_TYPES = {"ButtonTypeDefault", "InlineButtonTypeCallback", "KeyboardButton", "KeyboardButtonCallback"}


def button_kind(b) -> str:
    raw = getattr(b, "button", None)
    t = getattr(raw, "type", None)
    if t is not None:
        return type(t).__name__
    return type(raw).__name__


def is_safe_button(b) -> bool:
    return button_kind(b) in _SAFE_TYPES and not getattr(b, "url", None)


@dataclass
class ButtonInfo:
    message_id: int
    text: str
    kind: str
    safe: bool


async def list_buttons(client, entity, limit: int = 50) -> list[tuple[object, ButtonInfo]]:
    """Newest first: (MessageButton, info) for every button in recent HUMO bot messages."""
    out = []
    async for msg in client.iter_messages(entity, limit=limit):
        if msg.out or not msg.reply_markup:
            continue
        rows = await msg.get_buttons()
        for row in rows or []:
            for b in row:
                out.append((b, ButtonInfo(msg.id, (b.text or "").strip(), button_kind(b), is_safe_button(b))))
    return out
