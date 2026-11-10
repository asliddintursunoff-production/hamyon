from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.ai.gemini import AIClient
from app.ai.prompts import (
    ASSISTANT_SYSTEM,
    ASSISTANT_VOICE_KEYS,
    SPLIT_SYSTEM,
    VOICE_NOTE,
    split_prompt,
    TRANSCRIBE_PROMPT,
    TRANSCRIBE_SYSTEM,
    assistant_prompt,
)
from app.ai.schemas import AssistantIntent, SplitAnswer, Transcription


async def parse_intent(ai: AIClient, text: str, now: datetime, tz: ZoneInfo) -> tuple[AssistantIntent, str]:
    prompt = assistant_prompt(text, now.astimezone(tz).isoformat(timespec="minutes"), str(tz))
    return await ai.generate_json(prompt, AssistantIntent, system=ASSISTANT_SYSTEM)


async def transcribe(ai: AIClient, audio: bytes, mime_type: str) -> tuple[Transcription, str]:
    return await ai.generate_json(TRANSCRIBE_PROMPT, Transcription, system=TRANSCRIBE_SYSTEM, audio=(audio, mime_type))


async def parse_intent_voice(ai: AIClient, audio: bytes, mime_type: str, now: datetime, tz: ZoneInfo) -> tuple[AssistantIntent, str]:
    """One request: transcribe the voice note AND extract the intent (faster, half the quota)."""
    prompt = (
        f"Current local date/time: {now.astimezone(tz).isoformat(timespec='minutes')} ({tz})\n"
        "The owner's message is the attached voice note."
    )
    return await ai.generate_json(
        prompt, AssistantIntent, system=ASSISTANT_SYSTEM + VOICE_NOTE + ASSISTANT_VOICE_KEYS, audio=(audio, mime_type)
    )


async def parse_split(
    ai: AIClient, *, tx_facts: str, categories: str, people: str,
    text: str | None = None, audio: tuple[bytes, str] | None = None,
) -> tuple[SplitAnswer, str]:
    prompt = split_prompt(tx_facts, categories, people, text if audio is None else None)
    return await ai.generate_json(prompt, SplitAnswer, system=SPLIT_SYSTEM, audio=audio)
