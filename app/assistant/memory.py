"""Remember what each assistant reply was about, so a Telegram reply to it can complete or correct it."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.database.session import SessionFactory
from app.models import AssistantMessage


async def remember_reply(sf: SessionFactory, chat_id: int, message_id: int, *, user_text: str | None, kind: str,
                         undo: tuple[str, str] | None = None, transaction_id: int | None = None) -> None:
    values = dict(chat_id=chat_id, message_id=message_id, user_text=(user_text or None) and user_text[:4000],
                  kind=kind, undo_kind=undo[0] if undo else None, undo_ref=undo[1] if undo else None,
                  transaction_id=transaction_id)
    stmt = insert(AssistantMessage).values(**values)
    async with sf.begin() as s:
        await s.execute(stmt.on_conflict_do_update(
            constraint="uq_assistant_messages_chat_message",
            set_={
                "kind": stmt.excluded.kind,
                "undo_kind": stmt.excluded.undo_kind,
                "undo_ref": stmt.excluded.undo_ref,
                # a confirmation button edits the message: keep what the owner originally said
                "user_text": func.coalesce(stmt.excluded.user_text, AssistantMessage.user_text),
                "transaction_id": func.coalesce(stmt.excluded.transaction_id, AssistantMessage.transaction_id),
            },
        ))


async def find_reply(sf: SessionFactory, chat_id: int, message_id: int) -> AssistantMessage | None:
    async with sf() as s:
        return (await s.scalars(select(AssistantMessage).where(
            AssistantMessage.chat_id == chat_id, AssistantMessage.message_id == message_id))).first()


def combine(earlier: str | None, new: str | None) -> str | None:
    """Conversation text kept for the next correction."""
    parts = [p.strip() for p in (earlier, new) if p and p.strip()]
    return "\n".join(parts)[-3000:] or None
