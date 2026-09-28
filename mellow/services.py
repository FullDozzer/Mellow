from __future__ import annotations

from datetime import datetime, timedelta, timezone
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from mellow.config import Settings
from mellow.models import AuditLog, MessageStat, OutboxEvent, ProcessedUpdate, Staff, User, utcnow


async def ensure_user(session: AsyncSession, telegram_user) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_user.id))
    if user is None:
        user = User(telegram_id=telegram_user.id, username=telegram_user.username)
        session.add(user)
        await session.flush()
    else:
        user.username = telegram_user.username
        user.updated_at = utcnow()
    return user


async def staff_level(session: AsyncSession, telegram_id: int) -> int:
    stmt = select(Staff.level).join(User, Staff.user_id == User.id).where(User.telegram_id == telegram_id, Staff.active.is_(True)).with_for_update()
    return int(await session.scalar(stmt) or 0)


def has_permission(settings: Settings, level: int, permission: str) -> bool:
    config = settings.levels.get(level)
    return bool(config and ("*" in config.permissions or permission in config.permissions))


async def permitted(session: AsyncSession, settings: Settings, telegram_id: int, permission: str) -> tuple[bool, int]:
    level = await staff_level(session, telegram_id)
    return has_permission(settings, level, permission), level


async def audit(session: AsyncSession, action: str, actor_id: int | None, target_ref: str | None = None, details: dict | None = None) -> None:
    # Callers must pass actor_id=None for actions whose actor is anonymous.
    session.add(AuditLog(actor_id=actor_id, action=action, target_ref=target_ref, details=details or {}))


async def apply_staff_configuration(session_factory, settings: Settings) -> None:
    """Load configured staff IDs as the authoritative starting roster."""
    async with session_factory() as session, session.begin():
        # Environment staff entries are bootstrap grants, not a source that
        # overwrites persistent role changes on every restart.
        for telegram_id, level in settings.staff.items():
            user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
            if user is None:
                user = User(telegram_id=telegram_id)
                session.add(user)
                await session.flush()
            staff = await session.get(Staff, user.id)
            if staff is None:
                session.add(Staff(user_id=user.id, level=level))


async def claim_update(session_factory, update_id: int) -> bool:
    """Persist an update ID without any user/chat association, for idempotency."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    dialect = session_factory.kw.get("bind").dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    async with session_factory() as session, session.begin():
        stmt = insert(ProcessedUpdate).values(update_id=update_id)
        stmt = stmt.on_conflict_do_nothing(index_elements=["update_id"])
        result = await session.execute(stmt)
        return result.rowcount == 1


async def increment_message_count(session_factory, settings: Settings, telegram_user_id: int,
                                  username: str | None, update_id: int) -> tuple[int | None, bool, bool]:
    """Atomically claim a Telegram update and increment an attributable user once."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    dialect = session_factory.kw.get("bind").dialect.name
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    async with session_factory() as session, session.begin():
        claim = insert(ProcessedUpdate).values(update_id=update_id).on_conflict_do_nothing(index_elements=["update_id"])
        result = await session.execute(claim)
        if result.rowcount == 0:
            return None, False, True
        if not settings.message_requirement_enabled:
            return None, False, False

        now = utcnow()
        user_stmt = insert(User).values(telegram_id=telegram_user_id, username=username, created_at=now, updated_at=now)
        user_stmt = user_stmt.on_conflict_do_update(index_elements=["telegram_id"],
            set_={"username": username, "updated_at": now}).returning(User.id)
        user_id = (await session.execute(user_stmt)).scalar_one()
        active_staff = await session.scalar(select(Staff.user_id).where(
            Staff.user_id == user_id, Staff.active.is_(True)).with_for_update())
        if active_staff is not None:
            return None, False, False

        stats_stmt = insert(MessageStat).values(user_id=user_id, message_count=1, first_message_at=now,
            last_message_at=now, threshold_reached=False)
        stats_stmt = stats_stmt.on_conflict_do_update(index_elements=["user_id"],
            set_={"message_count": MessageStat.message_count + 1, "last_message_at": now})
        count = (await session.execute(stats_stmt.returning(MessageStat.message_count))).scalar_one()
        threshold_update = await session.execute(update(MessageStat).where(
            MessageStat.user_id == user_id, MessageStat.message_count >= settings.message_threshold,
            MessageStat.threshold_reached.is_(False)).values(threshold_reached=True).returning(MessageStat.user_id))
        threshold_now = threshold_update.scalar_one_or_none() is not None
        if threshold_now:
            session.add(OutboxEvent(event_key=f"message-threshold-{user_id}", event_type="message_threshold",
                                    payload={"user_id": user_id}, status="pending"))
        return count, threshold_now, False

async def target_staff_level(session: AsyncSession, telegram_id: int) -> int:
    return await staff_level(session, telegram_id)


def hierarchy_allows(actor_level: int, target_level: int) -> bool:
    """Level 5 is fully privileged; others can act only strictly below them."""
    if actor_level < 1:
        return False
    if actor_level == 5:
        return True
    return target_level < actor_level


def parse_duration(value: str) -> int | None:
    if not value:
        return None
    value = value.strip().lower()
    multipliers = {"с": 1, "м": 60, "ч": 3600, "д": 86400, "w": 604800}
    if len(value) < 2 or value[-1] not in multipliers or not value[:-1].isdigit():
        raise ValueError("Укажи срок, например 30м, 2ч или 7д.")
    amount = int(value[:-1])
    if amount <= 0:
        raise ValueError("Срок должен быть больше нуля.")
    return amount * multipliers[value[-1]]


def expires_at(duration: int | None) -> datetime | None:
    return datetime.now(timezone.utc) + timedelta(seconds=duration) if duration else None
