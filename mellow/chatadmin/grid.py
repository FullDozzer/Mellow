"""«Настройка сетки чатов»: several chats managed as one group."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete as sql_delete
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mellow.models import ChatSettings, GridChat, utcnow


@dataclass(frozen=True)
class GridRow:
    chat_id: int
    title: str | None
    username: str | None


async def grid_of_chat(session: AsyncSession, chat_id: int) -> str | None:
    """The grid this chat belongs to; ``None`` when the chat is not linked."""
    return await session.scalar(select(GridChat.grid_name).where(GridChat.chat_id == chat_id))


async def grid_rows(session: AsyncSession, name: str) -> list[GridRow]:
    rows = (await session.execute(select(GridChat.chat_id, ChatSettings.title)
                                  .outerjoin(ChatSettings, ChatSettings.chat_id == GridChat.chat_id)
                                  .where(GridChat.grid_name == name)
                                  .order_by(GridChat.chat_id))).all()
    return [GridRow(chat_id=chat_id, title=title, username=None) for chat_id, title in rows]


async def grid_chat_ids(session: AsyncSession, name: str) -> list[int]:
    rows = await session.scalars(select(GridChat.chat_id).where(GridChat.grid_name == name))
    return list(rows.all())


async def set_grid(session: AsyncSession, chat_id: int, name: str) -> None:
    row = await session.get(GridChat, chat_id)
    if row is None:
        session.add(GridChat(chat_id=chat_id, grid_name=name))
    else:
        row.grid_name = name
    await session.flush()


async def remove_from_grid(session: AsyncSession, chat_id: int) -> bool:
    result = await session.execute(sql_delete(GridChat).where(GridChat.chat_id == chat_id))
    return bool(result.rowcount)


async def remember_chat_title(session: AsyncSession, chat_id: int, title: str | None) -> None:
    if not title:
        return
    row = await session.get(ChatSettings, chat_id)
    if row is None:
        session.add(ChatSettings(chat_id=chat_id, title=title))
    elif row.title != title:
        row.title = title
        row.updated_at = utcnow()
