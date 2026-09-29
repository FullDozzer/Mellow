"""«Настройка сетки чатов»: команды, которые срабатывают во всех чатах сетки.

Сетка — это набор связанных чатов (таблица ``grid_chats``). Глобальные роли, баны и
доступ команд из документации Ириса здесь работают так же: действие применяется к
каждому чату сетки, а чаты, где бот не администратор, просто пропускаются и попадают
в счётчик «не удалось».
"""

from __future__ import annotations

import html

from sqlalchemy import select

from mellow.chatadmin.context import ChatContext, command, extract_target, resolve_user_id
from mellow.chatadmin.grid import grid_chat_ids, grid_of_chat
from mellow.chatadmin.config import command_key, set_command_access
from mellow.models import Punishment, Staff, User, utcnow
from mellow.moderation import apply_punishment, cap_duration, punishment_active
from mellow.services import audit, staff_level

# «+Глмодер»/«+Гладмин»: глобальные роли сетки в едином ростере Mellow.
GLOBAL_MODERATOR_LEVEL = 1
GLOBAL_ADMIN_LEVEL = 4
GRID_CREATOR_LEVEL = 5


async def require_grid(ctx: ChatContext) -> str | None:
    async with ctx.session_factory() as session:
        name = await grid_of_chat(session, ctx.chat_id)
    if name is None:
        await ctx.reply("Этот чат не входит в сетку. Установить: <code>дк установить сетку "
                        "Название</code>.")
        return None
    return name


async def _chats(ctx: ChatContext, name: str) -> list[int]:
    async with ctx.session_factory() as session:
        return await grid_chat_ids(session, name)


async def _resolve(ctx: ChatContext) -> int | None:
    reference, _ = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply(f"Формат: <code>{ctx.command} @ник</code>.")
    return target_id


# --------------------------------------------------------------------------------------
# Глобальные роли
# --------------------------------------------------------------------------------------

@command("+глмодер", key_group="сетка")
@command("-глмодер", key_group="сетка")
@command("+гладмин", key_group="сетка")
@command("-гладмин", key_group="сетка")
async def cmd_global_role(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    target_id = await _resolve(ctx)
    if target_id is None:
        return
    level = GLOBAL_ADMIN_LEVEL if "гладмин" in ctx.command else GLOBAL_MODERATOR_LEVEL
    promote = ctx.command.startswith("+")
    if level >= GLOBAL_ADMIN_LEVEL and ctx.actor_level < GRID_CREATOR_LEVEL:
        await ctx.reply("Глобального администратора назначает только владелец сетки (5 уровень).")
        return
    await _grid_set_rank(ctx, name, target_id, level if promote else None)


@command("сетка модеры", key_group="сетка")
async def cmd_grid_moderators(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    chat_ids = await _chats(ctx, name)
    async with ctx.session_factory() as session:
        rows = (await session.execute(select(User.telegram_id, User.username, Staff.level)
                                      .join(Staff, Staff.user_id == User.id)
                                      .where(Staff.active.is_(True))
                                      .order_by(Staff.level.desc()))).all()
        members = set()
        for chat_id in chat_ids:
            members |= set((await session.scalars(select(Punishment.target_user_id)
                                                  .where(Punishment.chat_id == chat_id))).all())
    lines = [f"<b>Модерация сетки «{html.escape(name)}»</b>"]
    for telegram_id, username, level in rows:
        if level <= 0:
            continue
        name_text = f"@{username}" if username else str(telegram_id)
        marks = []
        if level >= GRID_CREATOR_LEVEL:
            marks.append("👑 создатель")
        elif level >= GLOBAL_ADMIN_LEVEL:
            marks.append("глобальный админ")
        elif level >= GLOBAL_MODERATOR_LEVEL:
            marks.append("глобальный модер")
        lines.append(f"• {level} · {html.escape(name_text)} — {', '.join(marks) or 'модератор'}")
    if len(lines) == 1:
        await ctx.reply("В сетке нет модерации.")
        return
    await ctx.reply("\n".join(lines))


async def write_rank(ctx: ChatContext, target_id: int, level: int | None, action: str) -> None:
    """Запись ранга без проверок иерархии: нужна передаче прав создателя сетки."""
    async with ctx.session_factory() as session, session.begin():
        old_level = await staff_level(session, target_id)
        user = await session.scalar(select(User).where(User.telegram_id == target_id))
        if user is None:
            user = User(telegram_id=target_id)
            session.add(user)
            await session.flush()
        staff = await session.get(Staff, user.id)
        if level is None:
            if staff is not None:
                staff.active, staff.updated_at = False, utcnow()
            await audit(session, "staff_removed", ctx.actor_id, f"telegram:{target_id}",
                        {"old_level": old_level, "chat_id": ctx.chat_id, "reason": action})
            return
        if staff is None:
            session.add(Staff(user_id=user.id, level=level, active=True))
        else:
            staff.level, staff.active, staff.updated_at = level, True, utcnow()
        await audit(session, "staff_level_set", ctx.actor_id, f"telegram:{target_id}",
                    {"old_level": old_level, "new_level": level, "chat_id": ctx.chat_id,
                     "reason": action})


async def _grid_set_rank(ctx: ChatContext, grid_name: str, target_id: int, level: int | None,
                         *, quiet: bool = False) -> None:
    from mellow.chatadmin.moderation_commands import rank_title, set_level

    if target_id == ctx.actor_id:
        await ctx.reply("Нельзя менять собственный ранг.")
        return
    current = 0
    async with ctx.session_factory() as session:
        current = await staff_level(session, target_id)
    if level is not None and current >= level and ctx.actor_level < GRID_CREATOR_LEVEL:
        await ctx.reply("Нельзя выдать ранг не ниже уже имеющегося.")
        return
    await set_level(ctx, target_id, level)
    async with ctx.session_factory() as session, session.begin():
        await audit(session, "grid_staff_changed", ctx.actor_id, f"telegram:{target_id}",
                    {"grid": grid_name, "level": level})
    if quiet:
        return
    title = rank_title(level, ctx.settings) if level else "без ранга"
    await ctx.reply(f"Сетка «{html.escape(grid_name)}»: <code>{target_id}</code> — "
                    f"{html.escape(title)}.")


# --------------------------------------------------------------------------------------
# Глобальные блокировки
# --------------------------------------------------------------------------------------

@command("глобан", key_group="сетка")
@command("сетка бан", key_group="сетка")
async def cmd_global_ban(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    # Срок необязателен: «глобан @ник» банит навсегда, «глобан 7 дней @ник» — на неделю.
    from mellow.chatadmin.context import parse_leading_period
    reference, rest = extract_target(ctx.args)
    seconds, token, consumed = parse_leading_period(rest)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: <code>глобан [срок] @ник</code>.")
        return
    duration = cap_duration(ctx.settings, ctx.actor_level, seconds) if token else None
    reason = ctx.tail or " ".join(rest).strip() or f"Глобан сетки «{name}»"
    chat_ids = await _chats(ctx, name)
    done = failed = 0
    for chat_id in chat_ids:
        result = await apply_punishment(ctx.bot, ctx.session_factory, chat_id=chat_id,
                                        target_id=target_id, action="бан", duration=duration,
                                        reason=reason, actor_id=ctx.actor_id)
        done += 1 if result.applied else 0
        failed += 0 if result.applied else 1
    await ctx.reply(f"Глобан в сетке «{html.escape(name)}»: забанено в {done} чатах"
                    + (f", не удалось в {failed}" if failed else "") + ".")


@command("глоразбан", key_group="сетка")
@command("сетка разбан", key_group="сетка")
async def cmd_global_unban(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    target_id = await _resolve(ctx)
    if target_id is None:
        return
    chat_ids = await _chats(ctx, name)
    for chat_id in chat_ids:
        async with ctx.session_factory() as session, session.begin():
            rows = (await session.scalars(select(Punishment)
                                          .where(Punishment.chat_id == chat_id,
                                                 Punishment.target_user_id == target_id,
                                                 Punishment.type == "ban",
                                                 Punishment.active.is_(True)))).all()
            for row in rows:
                row.active = False
            if rows:
                await audit(session, "ban_remove", ctx.actor_id, f"telegram:{target_id}",
                            {"chat_id": chat_id, "grid": name})
        try:
            from mellow.moderation import perform_telegram_action
            await perform_telegram_action(ctx.bot, chat_id, "разбан", target_id)
        except Exception:
            pass
    await ctx.reply(f"Глобан снят с <code>{target_id}</code> во всей сетке «{html.escape(name)}».")


@command("сетка баны", key_group="сетка")
async def cmd_grid_bans(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    chat_ids = await _chats(ctx, name)
    async with ctx.session_factory() as session:
        rows = list((await session.scalars(select(Punishment)
                                           .where(Punishment.chat_id.in_(chat_ids),
                                                  Punishment.type == "ban",
                                                  Punishment.active.is_(True))
                                           .order_by(Punishment.id.desc()).limit(50))).all())
        active = [row for row in rows if punishment_active(row)]
        usernames = {user.telegram_id: user.username for user in
                     (await session.scalars(select(User).where(
                         User.telegram_id.in_([row.target_user_id for row in active])))).all()} if active else {}
    if not active:
        await ctx.reply("В сетке никто не забанен.")
        return
    lines = [f"<b>Баны сетки «{html.escape(name)}»</b>"]
    seen = set()
    for row in active:
        if row.target_user_id in seen:
            continue
        seen.add(row.target_user_id)
        chats = sum(1 for other in active if other.target_user_id == row.target_user_id)
        name_text = usernames.get(row.target_user_id)
        label = f"@{name_text}" if name_text else str(row.target_user_id)
        lines.append(f"• {html.escape(label)} — чатов: {chats}"
                     + (f", причина: {html.escape(row.reason)}" if row.reason else ""))
    lines.append("\nСнять: <code>глоразбан @ник</code>")
    await ctx.reply("\n".join(lines))


@command("сетка кик", key_group="сетка")
async def cmd_grid_kick(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    target_id = await _resolve(ctx)
    if target_id is None:
        return
    from mellow.moderation import perform_telegram_action
    chat_ids = await _chats(ctx, name)
    done = failed = 0
    for chat_id in chat_ids:
        try:
            await perform_telegram_action(ctx.bot, chat_id, "кик", target_id)
            done += 1
        except Exception:
            failed += 1
    await ctx.reply(f"Исключён из {done} чатов сетки «{html.escape(name)}»"
                    + (f", не удалось в {failed}" if failed else "") + ".")


# --------------------------------------------------------------------------------------
# Глобальный доступ команд и права создателя сетки
# --------------------------------------------------------------------------------------

@command("сетка дк", key_group="сетка")
async def cmd_grid_access(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    args = list(ctx.args)
    if not args:
        await ctx.reply("Формат: <code>сетка дк варны 3</code>, <code>сетка +дк варны</code>, "
                        "<code>сетка -дк варны</code>.")
        return
    enabled = True
    if args[0] in {"+дк", "-дк"}:
        enabled = args[0] == "+дк"
        raw_key = " ".join(args[1:])
        level = 0 if enabled else 6
    elif args[-1].isdigit():
        raw_key = " ".join(args[:-1])
        level = max(0, min(6, int(args[-1])))
    else:
        raw_key = " ".join(args)
        level = None
    key = command_key(raw_key)
    if key is None:
        await ctx.reply("Неизвестная команда. Список: <code>дк</code>.")
        return
    if level is None:
        await ctx.reply(f"Для «{html.escape(key)}» укажи ранг: <code>сетка дк {html.escape(key)} "
                        f"3</code>.")
        return
    chat_ids = await _chats(ctx, name)
    for chat_id in chat_ids:
        async with ctx.session_factory() as session, session.begin():
            await set_command_access(session, chat_id, key, level)
            await audit(session, "command_access_set", ctx.actor_id, f"chat:{chat_id}",
                        {"command": key, "level": level, "grid": name})
    state = "для всех" if level == 0 else ("выключено" if level > 5 else f"от {level} уровня")
    await ctx.reply(f"Сетка «{html.escape(name)}»: «{html.escape(key)}» → {state} "
                    f"во всех {len(chat_ids)} чатах.")


@command("сетка передать создателя", key_group="сетка")
async def cmd_grid_transfer_creator(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    if ctx.actor_level < GRID_CREATOR_LEVEL:
        await ctx.reply("Передать создателя сетки может только владелец (5 уровень).")
        return
    target_id = await _resolve(ctx)
    if target_id is None:
        return
    await write_rank(ctx, target_id, GRID_CREATOR_LEVEL, "grid_transfer")
    await write_rank(ctx, ctx.actor_id, GRID_CREATOR_LEVEL - 1, "grid_transfer")
    await ctx.reply(f"Права создателя сетки «{html.escape(name)}» переданы "
                    f"<code>{target_id}</code>.")


@command("сетка восстановить создателя", key_group="сетка")
async def cmd_grid_restore_creator(ctx: ChatContext):
    name = await require_grid(ctx)
    if name is None:
        return
    try:
        member = await ctx.bot.get_chat_member(ctx.chat_id, ctx.actor_id)
    except Exception:
        member = None
    if getattr(member, "status", None) != "creator" and ctx.actor_level < GRID_CREATOR_LEVEL:
        await ctx.reply("Восстановить права создателя сетки может создатель чата в Telegram.")
        return
    await write_rank(ctx, ctx.actor_id, GRID_CREATOR_LEVEL, "grid_creator_restore")
    await ctx.reply(f"Права создателя сетки «{html.escape(name)}» восстановлены.")


# --------------------------------------------------------------------------------------
# «Сетка ухожу в отставку» уже описана в community_commands (grid_resign).
# --------------------------------------------------------------------------------------

async def grid_summary(ctx: ChatContext, name: str) -> str:
    """Строка со списком чатов сетки — используется в «чаты» и «чат инфо»."""
    chat_ids = await _chats(ctx, name)
    async with ctx.session_factory() as session:
        users = {}
        staff_rows = (await session.execute(select(User.telegram_id, Staff.level)
                                            .join(Staff, Staff.user_id == User.id)
                                            .where(Staff.active.is_(True)))).all()
        for telegram_id, level in staff_rows:
            if level > 0:
                users[telegram_id] = level
    return f"{len(chat_ids)} чат(ов), модерации: {len(users)}"
