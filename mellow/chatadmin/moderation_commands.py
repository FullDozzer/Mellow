"""«Команды модерации» and «Система банов и предупреждений»."""

from __future__ import annotations

import html

from sqlalchemy import select

from mellow.chatadmin.context import (ChatContext, command, extract_target, parse_leading_period,
                                      resolve_user_id)
from mellow.models import AuditLog, Punishment, Staff, User, utcnow
from mellow.moderation import (active_warnings, apply_punishment, cap_duration, describe_period,
                               parse_period, perform_telegram_action, punishment_active,
                               punishment_summary, warning_is_active)
from mellow.services import audit, hierarchy_allows, staff_level

RANKS = {0: "участник", 1: "младший модератор", 2: "модератор", 3: "старший модератор",
         4: "администратор", 5: "владелец"}
LEVEL_MIN, LEVEL_MAX = 1, 5


def rank_title(level: int, settings) -> str:
    entry = settings.levels.get(level)
    return entry.name if entry else RANKS.get(level, f"{level} ранг")


async def target_from(ctx: ChatContext) -> int | None:
    reference, _ = extract_target(ctx.args)
    return await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)


async def target_or_reply(ctx: ChatContext, *, missing: str = "Укажи пользователя: @ник, ID или ответь на сообщение."):
    target_id = await target_from(ctx)
    if target_id is None:
        await ctx.reply(missing)
    return target_id


def period_and_text(ctx: ChatContext) -> tuple[int | None, str | None, str | None, list[str]]:
    """Split arguments into ``(seconds, period_text, target_raw, leftover_words)``."""
    rest = list(ctx.args)
    reference, rest = extract_target(rest)
    seconds, token, consumed = parse_leading_period(rest)
    return seconds, token, reference, rest[consumed:]


def actor_tag(ctx: ChatContext, config) -> str:
    return f"\n<i>Модератор: {ctx.actor_id}</i>" if config.show_mod_tags else ""


# --------------------------------------------------------------------------------------
# 1. Команды модерации
# --------------------------------------------------------------------------------------

async def set_level(ctx: ChatContext, target_id: int, level: int | None) -> None:
    """Assign or remove a rank with the same hierarchy rules as «назначитьадмина»."""
    async with ctx.session_factory() as session, session.begin():
        target_level = await staff_level(session, target_id)
        if target_id == ctx.actor_id:
            await ctx.reply("Нельзя менять собственный ранг.")
            return
        if not hierarchy_allows(ctx.actor_level, target_level):
            await ctx.reply("Нельзя изменять ранг администратора своего или более высокого уровня.")
            return
        user = await session.scalar(select(User).where(User.telegram_id == target_id))
        if user is None:
            user = User(telegram_id=target_id)
            session.add(user)
            await session.flush()
        staff = await session.get(Staff, user.id)
        if level is None:
            if staff is None or not staff.active:
                await ctx.reply("Пользователь не является администратором.")
                return
            staff.active, staff.updated_at = False, utcnow()
            await audit(session, "staff_removed", ctx.actor_id, f"telegram:{target_id}",
                        {"old_level": target_level, "chat_id": ctx.chat_id})
        else:
            if staff is None:
                session.add(Staff(user_id=user.id, level=level, active=True))
            else:
                staff.level, staff.active, staff.updated_at = level, True, utcnow()
            await audit(session, "staff_level_set", ctx.actor_id, f"telegram:{target_id}",
                        {"old_level": target_level, "new_level": level, "chat_id": ctx.chat_id})
    if level is None:
        await ctx.reply(f"Ранг снят: <code>{target_id}</code>.")
    else:
        await ctx.reply(f"<code>{target_id}</code> — {html.escape(rank_title(level, ctx.settings))} ({level} ранг).")


@command("+модер", key_group="модер")
@command("модер", key_group="модер")
@command("+админ", key_group="модер")
@command("админ", key_group="модер")
async def cmd_assign_moderator(ctx: ChatContext):
    level = LEVEL_MIN
    args = list(ctx.args)
    if args and args[0].isdigit() and LEVEL_MIN <= int(args[0]) <= LEVEL_MAX:
        level = int(args.pop(0))
        ctx.args = args
    target_id = await target_or_reply(ctx, missing="Формат: +модер 2 @ник или ответ на сообщение.")
    if target_id is None:
        return
    await set_level(ctx, target_id, level)


@command("повысить", key_group="модер")
async def cmd_promote(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    async with ctx.session_factory() as session:
        current = await staff_level(session, target_id)
    await set_level(ctx, target_id, min(LEVEL_MAX, current + 1))


@command("понизить", key_group="модер")
async def cmd_demote(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    async with ctx.session_factory() as session:
        current = await staff_level(session, target_id)
    if current <= 0:
        await ctx.reply("У пользователя нет ранга.")
        return
    await set_level(ctx, target_id, current - 1 or None)


@command("снять", key_group="модер")
@command("разжаловать", key_group="модер")
async def cmd_dismiss(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    await set_level(ctx, target_id, None)


@command("снять вышедших", key_group="модер")
async def cmd_dismiss_left(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = (await session.execute(select(User.telegram_id, Staff.level).join(Staff, Staff.user_id == User.id)
                                      .where(Staff.active.is_(True)))).all()
    removed = 0
    for telegram_id, level in rows:
        if telegram_id == ctx.actor_id or not hierarchy_allows(ctx.actor_level, level):
            continue
        try:
            member = await ctx.bot.get_chat_member(ctx.chat_id, telegram_id)
        except Exception:
            continue
        if member.status in {"left", "kicked"}:
            await set_level(ctx, telegram_id, None)
            removed += 1
    await ctx.reply(f"Снят ранг с вышедших: {removed}.")


@command("снять всех", key_group="модер")
async def cmd_dismiss_all(ctx: ChatContext):
    if ctx.actor_level < LEVEL_MAX:
        await ctx.reply("Снять всех может только владелец (5 ранг).")
        return
    async with ctx.session_factory() as session, session.begin():
        rows = list((await session.scalars(select(Staff).where(Staff.active.is_(True)))).all())
        for row in rows:
            if row.level >= LEVEL_MAX and row.user_id != ctx.actor_id:
                continue
            row.active, row.updated_at = False, utcnow()
        await audit(session, "staff_removed_all", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"removed": len(rows)})
    await ctx.reply(f"Ранг снят со всех модераторов ({len(rows)}), кроме владельца.")


@command("снимаю полномочия", key_group="модер")
async def cmd_resign(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == ctx.actor_id))
        staff = await session.get(Staff, user.id) if user else None
        if staff is None or not staff.active:
            await ctx.reply("У тебя нет ранга.")
            return
        staff.active, staff.updated_at = False, utcnow()
        await audit(session, "staff_resigned", ctx.actor_id, f"telegram:{ctx.actor_id}",
                    {"old_level": staff.level, "chat_id": ctx.chat_id})
    await ctx.reply("Полномочия сняты. Спасибо за работу.")


@command("восстановить создателя", key_group="модер")
async def cmd_restore_creator(ctx: ChatContext):
    try:
        member = await ctx.bot.get_chat_member(ctx.chat_id, ctx.actor_id)
    except Exception:
        await ctx.reply("Не удалось проверить права в чате.")
        return
    if member.status != "creator":
        await ctx.reply("Восстановить права создателя может только создатель чата в Telegram.")
        return
    async with ctx.session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == ctx.actor_id))
        if user is None:
            user = User(telegram_id=ctx.actor_id)
            session.add(user)
            await session.flush()
        staff = await session.get(Staff, user.id)
        if staff is None:
            session.add(Staff(user_id=user.id, level=LEVEL_MAX, active=True))
        else:
            staff.level, staff.active, staff.updated_at = LEVEL_MAX, True, utcnow()
        await audit(session, "staff_level_set", ctx.actor_id, f"telegram:{ctx.actor_id}",
                    {"new_level": LEVEL_MAX, "reason": "creator_restore", "chat_id": ctx.chat_id})
    await ctx.reply(f"Права создателя восстановлены: {html.escape(rank_title(LEVEL_MAX, ctx.settings))}.")


@command("кто админ", key_group="модер", public=True)
async def cmd_staff_list(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = (await session.execute(select(User.telegram_id, User.username, Staff.level)
                                      .join(Staff, Staff.user_id == User.id)
                                      .where(Staff.active.is_(True))
                                      .order_by(Staff.level.desc()))).all()
    if not rows:
        await ctx.reply("Администрация не назначена.")
        return
    lines = ["<b>Администрация</b>"]
    for telegram_id, username, level in rows:
        try:
            member = await ctx.bot.get_chat_member(ctx.chat_id, telegram_id)
            mark = "➖" if member.status in {"left", "kicked"} else "🟢"
        except Exception:
            mark = "⚪️"
        lines.append(f"{mark} {level} · {html.escape(rank_title(level, ctx.settings))} — "
                     f"{html.escape(username and '@' + username or str(telegram_id))}")
    lines.append("\n🟢 в чате · ⚪️ неизвестно · ➖ вышел из чата")
    await ctx.reply("\n".join(lines))


@command("мой ранг", key_group="модер", public=True)
async def cmd_my_rank(ctx: ChatContext):
    if ctx.actor_level <= 0:
        await ctx.reply("У тебя нет ранга модератора.")
        return
    await ctx.reply(f"Твой ранг: {ctx.actor_level} — {html.escape(rank_title(ctx.actor_level, ctx.settings))}.")


@command("модер лог", key_group="модер")
async def cmd_rank_log(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = list((await session.scalars(select(AuditLog)
                                           .where(AuditLog.action.in_(("staff_level_set", "staff_removed",
                                                                       "staff_resigned", "staff_removed_all")))
                                           .order_by(AuditLog.id.desc()).limit(15))).all())
    if not rows:
        await ctx.reply("Изменений рангов пока не было.")
        return
    lines = ["<b>Последние изменения рангов</b>"]
    for row in rows:
        actor = f"<code>{row.actor_id}</code>" if row.actor_id else "автоматически"
        lines.append(f"#{row.id} {row.action} · {html.escape(str(row.target_ref))} · {actor}")
    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------------------
# 2. Предупреждения
# --------------------------------------------------------------------------------------

@command("варн", key_group="варны")
@command("пред", key_group="варны")
async def cmd_warn(ctx: ChatContext):
    duration, token, reference, rest = period_and_text(ctx)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: варн [срок] @ник или ответ на сообщение.")
        return
    config = await ctx.store.get(ctx.chat_id)
    async with ctx.session_factory() as session:
        level = await staff_level(session, target_id)
    if not hierarchy_allows(ctx.actor_level, level):
        await ctx.reply("Нельзя предупреждать администратора своего или более высокого уровня.")
        return
    duration = duration or config.warning_period_seconds
    reason = ctx.tail or " ".join(rest).strip() or None
    result = await apply_punishment(ctx.bot, ctx.session_factory, chat_id=ctx.chat_id, target_id=target_id,
                                    action="варн", duration=duration, reason=reason, actor_id=ctx.actor_id)
    if not result.applied:
        await ctx.reply("Telegram не подтвердил действие. Проверь права бота.")
        return
    async with ctx.session_factory() as session:
        count = len(await active_warnings(session, target_id))
    text = (f"Предупреждение выдано <code>{target_id}</code> "
            f"(всего активных: {count} из {config.warning_limit}).")
    if result.banned_by_limit:
        text += "\nДостигнут лимит предупреждений — применено наказание по правилам чата."
    await ctx.reply(text + actor_tag(ctx, config))


@command("варны", key_group="варны")
async def cmd_warnings_list(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    config = await ctx.store.get(ctx.chat_id)
    async with ctx.session_factory() as session:
        warnings = await active_warnings(session, target_id)
    if not warnings:
        await ctx.reply(f"Активных предупреждений у <code>{target_id}</code> нет.")
        return
    lines = [f"<b>Предупреждения</b> <code>{target_id}</code> — {len(warnings)} из {config.warning_limit}"]
    lines.extend(punishment_summary(row) for row in warnings)
    await ctx.reply("\n".join(lines))


@command("мои варны", key_group="варны", public=True)
@command("мои преды", key_group="варны", public=True)
async def cmd_my_warnings(ctx: ChatContext):
    config = await ctx.store.get(ctx.chat_id)
    async with ctx.session_factory() as session:
        warnings = await active_warnings(session, ctx.actor_id)
    if not warnings:
        await ctx.reply("У тебя нет предупреждений.")
        return
    lines = [f"<b>Твои предупреждения</b> — {len(warnings)} из {config.warning_limit}"]
    lines.extend(punishment_summary(row) for row in warnings)
    await ctx.reply("\n".join(lines))


@command("варнлист", key_group="варны")
async def cmd_warn_list(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = list((await session.scalars(select(Punishment)
                                           .where(Punishment.chat_id == ctx.chat_id, Punishment.type == "warn")
                                           .order_by(Punishment.id.desc()).limit(15))).all())
    if not rows:
        await ctx.reply("Предупреждений в этом чате ещё не было.")
        return
    lines = ["<b>Последние предупреждения</b>"]
    lines.extend(f"<code>{row.target_user_id}</code> · {punishment_summary(row)}"
                 + ("" if warning_is_active(row) else " <i>(истекло)</i>") for row in rows)
    await ctx.reply("\n".join(lines))


async def remove_warnings(ctx: ChatContext, target_id: int, count: int | None, punishment_id: int | None = None) -> int:
    async with ctx.session_factory() as session, session.begin():
        if punishment_id is not None:
            warnings = [row for row in (await session.scalars(select(Punishment)
                                                              .where(Punishment.id == punishment_id,
                                                                     Punishment.type == "warn"))).all()
                        if warning_is_active(row)]
        else:
            warnings = await active_warnings(session, target_id)
            if count is not None:
                warnings = warnings[-count:]
        for row in warnings:
            row.active = False
        if warnings:
            await audit(session, "warn_remove", ctx.actor_id, f"telegram:{target_id}",
                        {"ids": [row.id for row in warnings], "chat_id": ctx.chat_id})
        removed = len(warnings)
    return removed


@command("-варн", key_group="варны")
async def cmd_unwarn(ctx: ChatContext):
    args = list(ctx.args)
    if args and args[0].isdigit():
        args.pop(0)
    reference, _ = extract_target(args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: -варн @ник или ответ на сообщение.")
        return
    removed = await remove_warnings(ctx, target_id, 1)
    await ctx.reply(f"Снято предупреждений: {removed}.")


@command("снять варн", key_group="варны")
async def cmd_unwarn_one(ctx: ChatContext):
    """«снять варн 12 @ник» removes that exact warning («снять варн номер 12 @ник»)."""
    args = list(ctx.args)
    punishment_id = None
    if args and args[0].lower() == "номер":
        args.pop(0)
    if args and args[0].isdigit():
        punishment_id = int(args.pop(0))
    reference, _ = extract_target(args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: снять варн 12 @ник (номер виден в списке «варны»).")
        return
    removed = await remove_warnings(ctx, target_id, 1, punishment_id=punishment_id)
    await ctx.reply(f"Снято предупреждений: {removed}." if removed else "Такое предупреждение не найдено.")


@command("снять варны", key_group="варны")
async def cmd_unwarn_many(ctx: ChatContext):
    args = list(ctx.args)
    count = None
    if args and args[0].isdigit():
        count = int(args.pop(0))
    reference, _ = extract_target(args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: снять варны 2 @ник.")
        return
    removed = await remove_warnings(ctx, target_id, count)
    await ctx.reply(f"Снято предупреждений: {removed}.")


@command("снять все варны", key_group="варны")
async def cmd_unwarn_all(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    removed = await remove_warnings(ctx, target_id, None)
    await ctx.reply(f"Снято предупреждений: {removed}.")


@command("варны лимит", key_group="настройки")
async def cmd_warning_limit(ctx: ChatContext):
    if not ctx.args or not ctx.args[0].isdigit():
        await ctx.reply("Формат: варны лимит 3 (0 отключает автоматическое наказание).")
        return
    limit = max(0, min(50, int(ctx.args[0])))
    await ctx.store.update(ctx.chat_id, warning_limit=limit)
    await ctx.reply(f"Лимит предупреждений: {limit}.")


@command("варны чс", key_group="настройки")
async def cmd_warning_ban(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: варны чс 7 дней (или «варны чс навсегда»).")
        return
    duration = parse_period(" ".join(ctx.args))
    await ctx.store.update(ctx.chat_id, warning_ban_seconds=duration or 0)
    await ctx.reply(f"Наказание при достижении лимита: {describe_period(duration)}.")


@command("варны период", key_group="настройки")
async def cmd_warning_period(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: варны период 30 дней (или «варны период навсегда»).")
        return
    duration = parse_period(" ".join(ctx.args))
    await ctx.store.update(ctx.chat_id, warning_period_seconds=duration)
    await ctx.reply(f"Срок хранения предупреждений: {describe_period(duration)}.")


# --------------------------------------------------------------------------------------
# 3. Мут и бан
# --------------------------------------------------------------------------------------

async def remove_punishment(ctx: ChatContext, target_id: int, action: str) -> int:
    ptype = {"размут": "mute", "разбан": "ban"}[action]
    async with ctx.session_factory() as session, session.begin():
        rows = list((await session.scalars(select(Punishment)
                                           .where(Punishment.target_user_id == target_id,
                                                  Punishment.type == ptype, Punishment.active.is_(True)))).all())
        for row in rows:
            row.active = False
        ids = [row.id for row in rows]
        await audit(session, f"{ptype}_remove", ctx.actor_id, f"telegram:{target_id}",
                    {"ids": ids, "chat_id": ctx.chat_id})
    try:
        await perform_telegram_action(ctx.bot, ctx.chat_id, action, target_id)
    except Exception:
        # Telegram refused: the records must not pretend the punishment is gone.
        if ids:
            async with ctx.session_factory() as session, session.begin():
                restored = await session.scalars(select(Punishment).where(Punishment.id.in_(ids)))
                for row in restored.all():
                    row.active = True
                await audit(session, "moderation_api_not_confirmed", ctx.actor_id, f"telegram:{target_id}",
                            {"command": action, "chat_id": ctx.chat_id})
        await ctx.reply("Telegram не подтвердил снятие наказания. Проверь права бота.")
        return -1
    return len(ids)


@command("мут", key_group="варны")
async def cmd_mute(ctx: ChatContext):
    duration, token, reference, rest = period_and_text(ctx)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: мут 30 минут @ник [причина].")
        return
    config = await ctx.store.get(ctx.chat_id)
    async with ctx.session_factory() as session:
        level = await staff_level(session, target_id)
    if not hierarchy_allows(ctx.actor_level, level):
        await ctx.reply("Нельзя ограничивать администратора своего или более высокого уровня.")
        return
    duration = cap_duration(ctx.settings, ctx.actor_level, duration or config.mute_default_seconds)
    reason = ctx.tail or " ".join(rest).strip() or None
    result = await apply_punishment(ctx.bot, ctx.session_factory, chat_id=ctx.chat_id, target_id=target_id,
                                    action="мут", duration=duration, reason=reason, actor_id=ctx.actor_id)
    if not result.applied:
        await ctx.reply("Telegram не подтвердил мут. Проверь права бота и статус пользователя.")
        return
    await ctx.reply(f"Мут <code>{target_id}</code> — {describe_period(duration)}."
                    + (f"\nПричина: {html.escape(reason)}" if reason else "") + actor_tag(ctx, config))


@command("-мут", key_group="варны")
@command("размут", key_group="варны")
@command("unmute", key_group="варны")
async def cmd_unmute(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    removed = await remove_punishment(ctx, target_id, "размут")
    if removed >= 0:
        await ctx.reply(f"Мут снят с <code>{target_id}</code>.")


@command("муты", key_group="муты")
async def cmd_mute_list(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = list((await session.scalars(select(Punishment)
                                           .where(Punishment.chat_id == ctx.chat_id, Punishment.type == "mute",
                                                  Punishment.active.is_(True))
                                           .order_by(Punishment.id.desc()).limit(20))).all())
    active = [row for row in rows if punishment_active(row)]
    if not active:
        await ctx.reply("В этом чате никто не в муте.")
        return
    lines = ["<b>В муте</b>"]
    lines.extend(f"<code>{row.target_user_id}</code> · {punishment_summary(row)}" for row in active)
    await ctx.reply("\n".join(lines))


@command("проверить мут", key_group="муты")
async def cmd_check_mute(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    try:
        member = await ctx.bot.get_chat_member(ctx.chat_id, target_id)
    except Exception:
        await ctx.reply("Не удалось проверить пользователя.")
        return
    restricted = getattr(member, "can_send_messages", True) is False
    await ctx.reply(f"<code>{target_id}</code>: " + ("в муте." if restricted else "может писать."))


@command("бан", key_group="варны")
@command("чс", key_group="варны")
async def cmd_ban(ctx: ChatContext):
    duration, token, reference, rest = period_and_text(ctx)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: бан [срок] @ник [причина]. Без срока — навсегда.")
        return
    config = await ctx.store.get(ctx.chat_id)
    async with ctx.session_factory() as session:
        level = await staff_level(session, target_id)
    if not hierarchy_allows(ctx.actor_level, level):
        await ctx.reply("Нельзя банить администратора своего или более высокого уровня.")
        return
    duration = duration if token else config.ban_default_seconds
    duration = cap_duration(ctx.settings, ctx.actor_level, duration)
    reason = ctx.tail or " ".join(rest).strip() or None
    result = await apply_punishment(ctx.bot, ctx.session_factory, chat_id=ctx.chat_id, target_id=target_id,
                                    action="бан", duration=duration, reason=reason, actor_id=ctx.actor_id)
    if not result.applied:
        await ctx.reply("Telegram не подтвердил бан. Проверь права бота и статус пользователя.")
        return
    await ctx.reply(f"Бан <code>{target_id}</code> — {describe_period(duration)}."
                    + (f"\nПричина: {html.escape(reason)}" if reason else "") + actor_tag(ctx, config))


@command("-бан", key_group="варны")
@command("разбан", key_group="варны")
async def cmd_unban(ctx: ChatContext):
    target_id = await target_or_reply(ctx)
    if target_id is None:
        return
    removed = await remove_punishment(ctx, target_id, "разбан")
    if removed >= 0:
        await ctx.reply(f"Бан снят с <code>{target_id}</code>.")


@command("банлист", key_group="муты")
async def cmd_ban_list(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = list((await session.scalars(select(Punishment)
                                           .where(Punishment.chat_id == ctx.chat_id, Punishment.type == "ban",
                                                  Punishment.active.is_(True))
                                           .order_by(Punishment.id.desc()).limit(20))).all())
    if not rows:
        await ctx.reply("В этом чате никто не забанен.")
        return
    lines = ["<b>Банлист</b>"]
    lines.extend(f"<code>{row.target_user_id}</code> · {punishment_summary(row)}" for row in rows)
    await ctx.reply("\n".join(lines))


@command("мут период", key_group="настройки")
async def cmd_mute_period(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: мут период 1 день.")
        return
    duration = parse_period(" ".join(ctx.args))
    if duration is None:
        await ctx.reply("Срок мута по умолчанию не может быть бессрочным.")
        return
    await ctx.store.update(ctx.chat_id, mute_default_seconds=duration)
    await ctx.reply(f"Мут по умолчанию: {describe_period(duration)}.")


@command("бан период", key_group="настройки")
async def cmd_ban_period(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: бан период 7 дней (или «бан период навсегда»).")
        return
    duration = parse_period(" ".join(ctx.args))
    await ctx.store.update(ctx.chat_id, ban_default_seconds=duration)
    await ctx.reply(f"Бан по умолчанию: {describe_period(duration)}.")


@command("-модер теги", key_group="настройки")
@command("+модер теги", key_group="настройки")
async def cmd_mod_tags(ctx: ChatContext):
    enabled = ctx.command.startswith("+")
    await ctx.store.update(ctx.chat_id, show_mod_tags=enabled)
    await ctx.reply("Теги модератора в ответах: " + ("включены." if enabled else "выключены."))


@command("кик", key_group="варны")
@command("исключить", key_group="варны")
async def cmd_kick_member(ctx: ChatContext):
    """«кик @ник [причина]» — ban+unban in one step (the member can come back)."""
    _, _, reference, rest = period_and_text(ctx)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: кик @ник [причина] или ответ на сообщение.")
        return
    async with ctx.session_factory() as session:
        level = await staff_level(session, target_id)
    if not hierarchy_allows(ctx.actor_level, level):
        await ctx.reply("Нельзя исключать администратора своего или более высокого уровня.")
        return
    reason = ctx.tail or " ".join(rest).strip() or None
    result = await apply_punishment(ctx.bot, ctx.session_factory, chat_id=ctx.chat_id, target_id=target_id,
                                    action="кик", reason=reason, actor_id=ctx.actor_id)
    if not result.applied:
        await ctx.reply("Telegram не подтвердил исключение. Проверь права бота и статус пользователя.")
        return
    await ctx.reply(f"Пользователь <code>{target_id}</code> исключён."
                    + (f"\nПричина: {html.escape(reason)}" if reason else ""))


@command("предупреждения", key_group="варны")
async def cmd_warnings_alias(ctx: ChatContext):
    await cmd_warnings_list(ctx)


@command("снятьварн", key_group="варны")
async def cmd_unwarn_alias(ctx: ChatContext):
    await cmd_unwarn(ctx)
