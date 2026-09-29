"""Trigger catalogue: events, action parsing and storage («Триггеры»)."""

from __future__ import annotations

import html

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.models import Trigger, utcnow
from mellow.moderation import PUNISHMENT_TYPES, parse_period

# Events the guard can react to. The keys are what a moderator types after «+триггер».
EVENTS: dict[str, str] = {
    "ссылки": "отправка ссылок",
    "капс": "сообщение капсом",
    "стикеры": "превышение лимита стикеров",
    "голосовые": "голосовые сообщения и видеокружки",
    "гостевые боты": "вызов гостевого бота",
    "маты": "сквернословие",
    "варн лимит": "достижение лимита предупреждений",
    "новый участник": "вход нового участника",
}

EVENT_ALIASES = {
    "ссылка": "ссылки", "сайты": "ссылки", "чаты": "ссылки", "теги": "ссылки", "линки": "ссылки",
    "капс": "капс", "caps": "капс",
    "стикер": "стикеры", "стикеры": "стикеры", "гиф": "стикеры", "гифки": "стикеры",
    "гс": "голосовые", "голосовые": "голосовые", "кружки": "голосовые", "голосовухи": "голосовые",
    "гостевые боты": "гостевые боты", "гостьбот": "гостевые боты", "гостевой бот": "гостевые боты",
    "мат": "маты", "маты": "маты", "матюки": "маты", "сквернословие": "маты",
    "варн лимит": "варн лимит", "лимит варнов": "варн лимит",
    "новый участник": "новый участник", "вход": "новый участник", "новичок": "новый участник",
}

# What happens when an event fires and no trigger is configured for it.
DEFAULT_ACTIONS: dict[str, list[dict]] = {
    "ссылки": [{"command": "варн", "duration": None, "reason": "Ссылки в чате"}],
    "капс": [{"command": "варн", "duration": None, "reason": "КАПС в сообщении"}],
    "стикеры": [{"command": "варн", "duration": None, "reason": "Слишком много стикеров"}],
    "голосовые": [{"command": "варн", "duration": None, "reason": "Голосовые сообщения запрещены"}],
    "гостевые боты": [{"command": "кик", "duration": None, "reason": "Гостевой бот в чате"}],
    "маты": [{"command": "варн", "duration": None, "reason": "Сквернословие"}],
}

ACTION_ALIASES = {
    "варн": "варн", "пред": "варн", "предупреждение": "варн", "warn": "варн",
    "мут": "мут", "mute": "мут", "заткнуть": "мут",
    "бан": "бан", "чс": "бан", "ban": "бан", "перм": "бан",
    "кик": "кик", "kick": "кик",
    "удалить": "удалить", "удаление": "удалить", "снести": "удалить",
}

MAX_ACTIONS = 3


def resolve_event(raw: str) -> str | None:
    value = " ".join((raw or "").lower().split())
    if value in EVENTS:
        return value
    return EVENT_ALIASES.get(value)


def parse_action(line: str) -> dict:
    """Parse one trigger line: ``Варн / Флуд`` or ``Мут 10 минут / Спам``."""
    raw = line.strip()
    if not raw:
        raise ValueError("Пустая строка команды триггера.")
    body, _, reason = raw.partition("/")
    reason = reason.strip() or None
    tokens = body.split()
    if not tokens:
        raise ValueError("Укажи команду: варн, мут, бан, кик или удалить.")
    command = ACTION_ALIASES.get(tokens[0].lower().strip("!"))
    if command is None:
        raise ValueError(f"Неизвестная команда «{tokens[0]}». Доступны: варн, мут, бан, кик, удалить.")
    duration = None
    if len(tokens) > 1:
        duration = parse_period(" ".join(tokens[1:]))
    elif command in PUNISHMENT_TYPES and command != "варн":
        duration = None
    return {"command": command, "duration": duration, "reason": reason}


def parse_actions(lines: list[str]) -> list[dict]:
    actions = [parse_action(line) for line in lines if line.strip()]
    if not actions:
        raise ValueError("Нужна хотя бы одна команда на следующей строке, например: Варн / Флуд.")
    if len(actions) > MAX_ACTIONS:
        raise ValueError(f"В одном триггере можно указать не больше {MAX_ACTIONS} команд.")
    return actions


def render_action(action: dict) -> str:
    from mellow.moderation import describe_period

    command = action.get("command", "?")
    duration = action.get("duration")
    suffix = f" {describe_period(duration)}" if command in {"мут", "бан"} and duration else ""
    reason = f" — {html.escape(action['reason'])}" if action.get("reason") else ""
    return f"<code>{command}{suffix}</code>{reason}"


def render_trigger(trigger: Trigger) -> str:
    title = EVENTS.get(trigger.event, trigger.event)
    lines = [f"🔹 <b>{html.escape(trigger.event)}</b> — {html.escape(title)}",
             f"   ранг наказания: {trigger.min_level}",
             "   команды:"]
    lines.extend(f"   • {render_action(action)}" for action in (trigger.actions or []))
    return "\n".join(lines)


async def trigger_actions(session: AsyncSession, chat_id: int, event: str) -> list[dict] | None:
    """Configured actions for an event, or ``None`` when the chat has no trigger."""
    row = await session.scalar(select(Trigger).where(Trigger.chat_id == chat_id, Trigger.event == event,
                                                    Trigger.enabled.is_(True)))
    if row is None:
        return None
    return [dict(action) for action in (row.actions or [])]


async def trigger_level(session: AsyncSession, chat_id: int, event: str) -> int:
    row = await session.scalar(select(Trigger.min_level).where(Trigger.chat_id == chat_id,
                                                              Trigger.event == event,
                                                              Trigger.enabled.is_(True)))
    return int(row or 0)


async def list_triggers(session: AsyncSession, chat_id: int) -> list[Trigger]:
    rows = await session.scalars(select(Trigger).where(Trigger.chat_id == chat_id).order_by(Trigger.event))
    return list(rows.all())


async def set_trigger(session: AsyncSession, chat_id: int, event: str, min_level: int,
                      actions: list[dict], created_by: int | None = None) -> Trigger:
    row = await session.scalar(select(Trigger).where(Trigger.chat_id == chat_id, Trigger.event == event))
    if row is None:
        row = Trigger(chat_id=chat_id, event=event, min_level=min_level, actions=actions,
                      created_by=created_by)
        session.add(row)
    else:
        row.min_level, row.actions, row.enabled, row.updated_at = min_level, actions, True, utcnow()
    await session.flush()
    return row


async def delete_trigger(session: AsyncSession, chat_id: int, event: str) -> bool:
    result = await session.execute(sql_delete(Trigger).where(Trigger.chat_id == chat_id,
                                                          Trigger.event == event))
    return bool(result.rowcount)


async def toggle_trigger(session: AsyncSession, chat_id: int, event: str, enabled: bool) -> bool:
    row = await session.scalar(select(Trigger).where(Trigger.chat_id == chat_id, Trigger.event == event))
    if row is None:
        return False
    row.enabled, row.updated_at = enabled, utcnow()
    return True
