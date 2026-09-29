"""«Настройка использования команд», «Чистка чата», «Настройка чата», «Сетка чатов»,
«Статистическая информация»."""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from mellow.chatadmin import stats as chat_stats
from mellow.chatadmin.cleanup import (CleanupPlan, delete_messages, kick_members, plan_member_cleanup,
                                      plan_message_cleanup, purge_inactive_punishments)
from mellow.chatadmin.config import COMMANDS, command_key, command_min_level, set_command_access
from mellow.chatadmin.context import TABLE, ChatContext, command, extract_target, resolve_user_id
from mellow.chatadmin.grid import grid_of_chat, grid_rows, remove_from_grid, set_grid
from mellow.chatadmin.triggers import (EVENTS, MAX_ACTIONS, delete_trigger, list_triggers, parse_actions,
                                       render_trigger, resolve_event, set_trigger)
from mellow.models import ChatMemberActivity, utcnow
from mellow.moderation import parse_period
from mellow.services import audit
from mellow.stats import parse_days

PENDING_TTL_SECONDS = 180


@dataclass
class PendingCleanup:
    token: str
    actor_id: int
    plan: CleanupPlan
    created_at: float = field(default_factory=time.monotonic)


PENDING_CLEANUPS: dict[str, PendingCleanup] = {}


def stage_cleanup(plan: CleanupPlan, actor_id: int) -> PendingCleanup:
    """Register a destructive plan until the moderator confirms it."""
    now = time.monotonic()
    for token in [token for token, item in PENDING_CLEANUPS.items()
                  if now - item.created_at > PENDING_TTL_SECONDS]:
        PENDING_CLEANUPS.pop(token, None)
    pending = PendingCleanup(token=secrets.token_urlsafe(6), actor_id=actor_id, plan=plan)
    PENDING_CLEANUPS[pending.token] = pending
    return pending


def take_cleanup(token: str, actor_id: int) -> PendingCleanup | None:
    pending = PENDING_CLEANUPS.pop(token, None)
    if pending is None or pending.actor_id != actor_id:
        return None
    if time.monotonic() - pending.created_at > PENDING_TTL_SECONDS:
        return None
    return pending


def confirmation_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"cleanup:go:{token}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"cleanup:no:{token}"),
    ]])


# --------------------------------------------------------------------------------------
# Фильтры и настройки чата
# --------------------------------------------------------------------------------------

@command("+ссылки", key_group="настройки")
@command("-ссылки", key_group="настройки")
async def cmd_links(ctx: ChatContext):
    denied = ctx.command.startswith("-")
    if denied:
        await ctx.store.update(ctx.chat_id, links_denied=True, denied_link_types=["чаты", "сайты", "теги"])
    else:
        await ctx.store.update(ctx.chat_id, links_denied=False)
    await ctx.reply("Ссылки запрещены." if denied else "Ссылки разрешены.")


def _link_type_command(kind: str):
    async def handler(ctx: ChatContext):
        denied = ctx.command.startswith("-")
        config = await ctx.store.get(ctx.chat_id)
        types = set(config.denied_link_types or [])
        types.add(kind) if denied else types.discard(kind)
        await ctx.store.update(ctx.chat_id, denied_link_types=sorted(types), links_denied=bool(types))
        title = {"чаты": "ссылки на частные чаты", "сайты": "ссылки на сайты", "теги": "ссылки на Telegram"}[kind]
        await ctx.reply(f"{title}: " + ("запрещены." if denied else "разрешены."))
    return handler


TABLE.add("-чаты", _link_type_command("чаты"), key_group="настройки")
TABLE.add("+чаты", _link_type_command("чаты"), key_group="настройки")
TABLE.add("-сайты", _link_type_command("сайты"), key_group="настройки")
TABLE.add("+сайты", _link_type_command("сайты"), key_group="настройки")
TABLE.add("-теги", _link_type_command("теги"), key_group="настройки")
TABLE.add("+теги", _link_type_command("теги"), key_group="настройки")


@command("разрешить ссылку", key_group="настройки")
async def cmd_allow_link(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: разрешить ссылку t.me/mellow")
        return
    value = ctx.args[0].lower().rstrip("/")
    config = await ctx.store.get(ctx.chat_id)
    allowed = sorted({*(config.allowed_links or []), value})
    await ctx.store.update(ctx.chat_id, allowed_links=allowed)
    await ctx.reply(f"Ссылка добавлена в исключения: <code>{html.escape(value)}</code>")


@command("удалить ссылку", key_group="настройки")
async def cmd_deny_link(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: удалить ссылку t.me/mellow")
        return
    value = ctx.args[0].lower().rstrip("/")
    config = await ctx.store.get(ctx.chat_id)
    allowed = [item for item in (config.allowed_links or []) if str(item).lower().rstrip("/") != value]
    await ctx.store.update(ctx.chat_id, allowed_links=allowed)
    await ctx.reply("Ссылка убрана из исключений.")


@command("+стикеры", key_group="настройки")
@command("-стикеры", key_group="настройки")
async def cmd_stickers(ctx: ChatContext):
    if ctx.command.startswith("+"):
        await ctx.store.update(ctx.chat_id, sticker_limit=None)
        await ctx.reply("Ограничение на стикеры снято.")
        return
    limit = 1
    if ctx.args and ctx.args[0].isdigit():
        limit = max(1, min(50, int(ctx.args[0])))
    await ctx.store.update(ctx.chat_id, sticker_limit=limit)
    await ctx.reply(f"Разрешено стикеров подряд: {limit}. При превышении — наказание по правилам чата.")


@command("+капс", key_group="настройки")
@command("-капс", key_group="настройки")
async def cmd_caps(ctx: ChatContext):
    if ctx.command.startswith("+"):
        await ctx.store.update(ctx.chat_id, caps_percent=None)
        await ctx.reply("Ограничение КАПСА снято.")
        return
    percent, length = 80, 5
    numbers = [int(token) for token in ctx.args if token.isdigit()]
    if numbers:
        percent = max(10, min(100, numbers[0]))
    if len(numbers) > 1:
        length = max(1, numbers[1])
    await ctx.store.update(ctx.chat_id, caps_percent=percent, caps_min_length=length)
    await ctx.reply(f"КАПС запрещён: от {percent}% заглавных в сообщении длиной от {length} букв.")


@command("-гс", key_group="настройки")
@command("+гс", key_group="настройки")
async def cmd_voice(ctx: ChatContext):
    denied = ctx.command.startswith("-")
    await ctx.store.update(ctx.chat_id, voice_denied=denied)
    await ctx.reply("Голосовые сообщения и кружки запрещены." if denied
                    else "Голосовые сообщения и кружки разрешены.")


@command("-гостевые боты", key_group="настройки")
@command("+гостевые боты", key_group="настройки")
async def cmd_guest_bots(ctx: ChatContext):
    denied = ctx.command.startswith("-")
    await ctx.store.update(ctx.chat_id, guest_bots_denied=denied)
    await ctx.reply("Гостевые боты запрещены." if denied else "Гостевые боты разрешены.")


@command("-маты", key_group="настройки")
@command("+маты", key_group="настройки")
async def cmd_profanity(ctx: ChatContext):
    enabled = ctx.command.startswith("-")
    await ctx.store.update(ctx.chat_id, profanity_filter=enabled)
    await ctx.reply("Фильтр сквернословия включён." if enabled else "Фильтр сквернословия выключен.")


@command("+график", key_group="настройки")
@command("-график", key_group="настройки")
async def cmd_charts(ctx: ChatContext):
    enabled = ctx.command.startswith("+")
    await ctx.store.update(ctx.chat_id, show_charts=enabled)
    await ctx.reply("Графики в статистике " + ("включены." if enabled else "выключены."))


@command("установить приветствие", key_group="настройки")
@command("приветствие", key_group="настройки")
async def cmd_set_welcome(ctx: ChatContext):
    await _set_text_setting(ctx, "welcome_text", "приветствие")


@command("установить правила", key_group="настройки")
@command("правила", key_group="настройки")
async def cmd_set_rules(ctx: ChatContext):
    await _set_text_setting(ctx, "rules_text", "правила")


async def _set_text_setting(ctx: ChatContext, field_name: str, title: str) -> None:
    body = ctx.tail or " ".join(ctx.args)
    if body.lower() in {"сброс", "удалить", "-"}:
        await ctx.store.update(ctx.chat_id, **{field_name: None})
        await ctx.reply(f"Текст «{title}» удалён.")
        return
    if not body:
        await ctx.reply(f"Отправь команду так:\nустановить {title}\nПервая строка — коротко, "
                        "дальше — текст (или «Правила сброс», чтобы удалить).")
        return
    await ctx.store.update(ctx.chat_id, **{field_name: body[:3500]})
    await ctx.reply(f"Текст «{title}» сохранён.")


@command("настройки чата", key_group="настройки")
async def cmd_chat_settings(ctx: ChatContext):
    config = await ctx.store.get(ctx.chat_id)
    lines = [
        "<b>Настройки чата</b>",
        f"Лимит предупреждений: {config.warning_limit}",
        f"Наказание при лимите: {(config.warning_ban_seconds or 0) // 86400} дн.",
        "Срок хранения предупреждений: "
        + (f"{(config.warning_period_seconds or 0) // 86400} дн." if config.warning_period_seconds else "не ограничен"),
        f"Мут по умолчанию: {config.mute_default_seconds // 3600} ч.",
        "Бан по умолчанию: " + (f"{config.ban_default_seconds // 3600} ч." if config.ban_default_seconds else "навсегда"),
        "Ссылки: " + ("запрещены (" + ", ".join(config.denied_link_types or []) + ")" if config.links_denied else "разрешены"),
        f"Исключения для ссылок: {len(config.allowed_links or [])}",
        "Стикеры: " + (str(config.sticker_limit) if config.sticker_limit is not None else "без ограничений"),
        "КАПС: " + (f"от {config.caps_percent}%" if config.caps_percent else "разрешён"),
        "Голосовые и кружки: " + ("запрещены" if config.voice_denied else "разрешены"),
        "Гостевые боты: " + ("запрещены" if config.guest_bots_denied else "разрешены"),
        "Фильтр сквернословия: " + ("включён" if config.profanity_filter else "выключен"),
        "Графики: " + ("включены" if config.show_charts else "выключены"),
        "Приветствие: " + ("настроено" if config.welcome_text else "не задано"),
        "Правила: " + ("настроены" if config.rules_text else "не заданы"),
        "",
        "<i>Изменить: -ссылки, -капс 70 5, -стикеры 3, -гс, -маты, варны лимит 3, мут период 1 день.</i>",
    ]
    await ctx.reply("\n".join(lines))


# --------------------------------------------------------------------------------------
# Триггеры
# --------------------------------------------------------------------------------------

TRIGGER_HELP = """<b>Триггеры</b>

Установка:
<code>+триггер событие [ранг]
Варн 30 минут / Причина
Мут 1 час / Причина</code>

Доступные события: {events}
Команды: варн, мут, бан, кик, удалить (до {max_actions} в одном триггере).

Просмотр: <code>триггеры</code> или <code>триггер ссылки</code>
Удаление: <code>-триггер ссылки</code>
Ограничение: <code>дк унб 4</code>

Если триггер не настроен, действует стандартное наказание (обычно предупреждение)."""


@command("триггер помощь", key_group="триггеры", public=True)
async def cmd_trigger_help(ctx: ChatContext):
    await ctx.reply(TRIGGER_HELP.format(events=", ".join(EVENTS), max_actions=MAX_ACTIONS))


@command("+триггер", key_group="триггеры")
async def cmd_set_trigger(ctx: ChatContext):
    args = list(ctx.args)
    event_key = None
    consumed = 0
    for size in (3, 2, 1):
        if len(args) >= size:
            candidate = resolve_event(" ".join(args[:size]))
            if candidate:
                event_key, consumed = candidate, size
                break
    if event_key is None:
        await ctx.reply(f"Неизвестное событие. Доступны: {', '.join(EVENTS)}.")
        return
    rest = args[consumed:]
    min_level = 0
    if rest and rest[0].isdigit():
        min_level = max(0, min(5, int(rest.pop(0))))
    lines = [line for line in ctx.message.text.split("\n")[1:] if line.strip()]
    if not lines:
        await ctx.reply("Нужна хотя бы одна строка с командой, например:\n<code>+триггер ссылки 1\n"
                        "Варн / Ссылки в чате</code>")
        return
    try:
        actions = parse_actions(lines)
    except ValueError as exc:
        await ctx.reply(html.escape(str(exc)))
        return
    async with ctx.session_factory() as session, session.begin():
        await set_trigger(session, ctx.chat_id, event_key, min_level, actions, created_by=ctx.actor_id)
        await audit(session, "trigger_set", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"event": event_key, "level": min_level, "actions": [a.get("command") for a in actions]})
    label = EVENTS[event_key]
    await ctx.reply(f"Триггер установлен: <b>{html.escape(event_key)}</b> ({html.escape(label)}), "
                    f"ранг наказания {min_level}.")


@command("-триггер", key_group="триггеры")
async def cmd_delete_trigger(ctx: ChatContext):
    event_key = resolve_event(" ".join(ctx.args))
    if event_key is None:
        await ctx.reply(f"Укажи событие: {', '.join(EVENTS)}.")
        return
    async with ctx.session_factory() as session, session.begin():
        removed = await delete_trigger(session, ctx.chat_id, event_key)
        if removed:
            await audit(session, "trigger_delete", ctx.actor_id, f"chat:{ctx.chat_id}", {"event": event_key})
    await ctx.reply("Триггер удалён: " + html.escape(event_key) if removed else "Такого триггера нет.")


@command("триггеры", key_group="триггеры")
@command("триги", key_group="триггеры")
async def cmd_list_triggers(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = await list_triggers(session, ctx.chat_id)
    if not rows:
        await ctx.reply("Триггеры не настроены. Справка: <code>триггер помощь</code>.")
        return
    lines = ["<b>Триггеры чата</b>", ""]
    lines.extend(render_trigger(row) for row in rows)
    lines.append("\nСправка: <code>триггер помощь</code>")
    await ctx.reply("\n".join(lines))


@command("триггер", key_group="триггеры")
async def cmd_show_trigger(ctx: ChatContext):
    event_key = resolve_event(" ".join(ctx.args))
    if event_key is None:
        await cmd_list_triggers(ctx)
        return
    async with ctx.session_factory() as session:
        rows = [row for row in await list_triggers(session, ctx.chat_id) if row.event == event_key]
    if not rows:
        await ctx.reply(f"Триггер «{html.escape(event_key)}» не настроен. Действует стандартное наказание.")
        return
    await ctx.reply(render_trigger(rows[0]))


# --------------------------------------------------------------------------------------
# Доступ команд
# --------------------------------------------------------------------------------------

@command("дк", key_group="доступ")
async def cmd_command_access(ctx: ChatContext):
    args = list(ctx.args)
    if not args or args[0].lower() in {"список", "list"}:
        async with ctx.session_factory() as session:
            lines = ["<b>Доступ команд</b>", ""]
            for key, (default, title) in COMMANDS.items():
                required = await command_min_level(session, ctx.chat_id, key)
                marker = "" if required == default else " ← изменено"
                lines.append(f"<code>{key}</code> — от {required} уровня: {title}{marker}")
        lines.append("\nИзменить: <code>дк триггеры 4</code>, сбросить: <code>дк сброс триггеры</code>")
        await ctx.reply("\n".join(lines))
        return
    if args[0].lower() == "установить" and len(args) > 1:
        await _install_grid(ctx, " ".join(args[1:]).strip())
        return
    if args[0].lower() == "сетка" and len(args) > 1 and args[1].isdigit():
        await _set_access(ctx, "сетка", args[1])
        return
    if args[0].lower() == "сброс" and len(args) > 1:
        key = command_key(" ".join(args[1:]))
        if key is None:
            await ctx.reply("Неизвестная команда. Список: <code>дк список</code>.")
            return
        async with ctx.session_factory() as session, session.begin():
            await set_command_access(session, ctx.chat_id, key, None)
            await audit(session, "command_access_reset", ctx.actor_id, f"chat:{ctx.chat_id}", {"command": key})
        await ctx.reply(f"Доступ команды «{html.escape(key)}» сброшен к значению по умолчанию.")
        return
    if len(args) >= 2 and args[-1].isdigit():
        key = command_key(" ".join(args[:-1]))
        if key is None:
            await ctx.reply("Неизвестная команда. Список: <code>дк список</code>.")
            return
        await _set_access(ctx, key, args[-1])
        return
    await ctx.reply("Формат: <code>дк триггеры 4</code>, <code>дк список</code>, "
                    "<code>дк установить сетку Название</code>.")


async def _set_access(ctx: ChatContext, key: str, raw_level: str) -> None:
    level = max(0, min(5, int(raw_level)))
    async with ctx.session_factory() as session, session.begin():
        await set_command_access(session, ctx.chat_id, key, level)
        await audit(session, "command_access_set", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"command": key, "level": level})
    await ctx.reply(f"Команда «{html.escape(key)}» доступна с {level} уровня.")


# --------------------------------------------------------------------------------------
# Чистка чата
# --------------------------------------------------------------------------------------

CLEANUP_HELP = """<b>Чистка чата</b>

• <code>удалить 20</code> — удалить последние сообщения (или <code>чистка смс 50</code>)
• <code>кик неактив 30 дней</code> — исключить тех, кто не писал
• <code>кик актив 7 дней</code> — исключить тех, кто писал за период
• <code>кик новичков 1 день</code> — исключить недавно вошедших
• <code>кик удалённых</code> — убрать вышедших из статистики и списков

Telegram удаляет не более 100 сообщений за раз и не старше 48 часов,
поэтому старые сообщения могут не удалиться — бот сообщит об этом."""


@command("чистка", key_group="чистка")
@command("чистка смс", key_group="чистка")
@command("чистка чата", key_group="чистка", public=False)
async def cmd_cleanup_help(ctx: ChatContext):
    if ctx.command == "чистка смс" and ctx.args:
        await _delete_recent(ctx, ctx.args[0])
        return
    await ctx.reply(CLEANUP_HELP)


@command("удалить", key_group="чистка")
async def cmd_delete(ctx: ChatContext):
    count = ctx.args[0] if ctx.args else "20"
    await _delete_recent(ctx, count)


async def _delete_recent(ctx: ChatContext, raw_count: str) -> None:
    if not raw_count.isdigit():
        await ctx.reply("Укажи число сообщений: <code>удалить 20</code>.")
        return
    count = max(1, min(100, int(raw_count)))
    message_ids = list(ctx.recent.last(ctx.chat_id, count))
    if ctx.reply_target is not None:
        message_ids.append(ctx.reply_target.message_id)
    if not message_ids:
        await ctx.reply("Нечего удалять: я запоминаю только сообщения, отправленные после запуска бота.")
        return
    plan = await plan_message_cleanup(ctx.chat_id, message_ids)
    pending = stage_cleanup(plan, ctx.actor_id)
    await ctx.reply(f"Удалить {len(plan.message_ids)} сообщений? " +
                    "Старые (более 48 часов) Telegram может не удалить.",
                    reply_markup=confirmation_keyboard(pending.token))


async def _remember_members(session, chat_id: int, telegram_ids: list[int], joined: bool) -> None:
    for telegram_id in telegram_ids:
        row = await session.get(ChatMemberActivity, (chat_id, telegram_id))
        if row is None:
            session.add(ChatMemberActivity(chat_id=chat_id, telegram_id=telegram_id,
                                           joined_at=utcnow() if joined else None,
                                           last_message_at=None, is_member=joined))
        else:
            row.is_member = joined
            if joined:
                row.joined_at = utcnow()
            row.updated_at = utcnow()


@command("кик неактив", key_group="чистка")
@command("кик актив", key_group="чистка")
@command("кик новичков", key_group="чистка")
@command("кик удалённых", key_group="чистка")
async def cmd_kick(ctx: ChatContext):
    days = None
    if ctx.args:
        if ctx.args[0].isdigit():
            days = max(1, int(ctx.args[0]))
        else:
            seconds = parse_period(" ".join(ctx.args))
            days = max(1, seconds // 86400) if seconds else None
    async with ctx.session_factory() as session, session.begin():
        closed = await purge_inactive_punishments(session)
        plan = await plan_member_cleanup(session, ctx.chat_id, ctx.command, days)
    if closed:
        await ctx.reply(f"Закрыто истёкших наказаний: {closed}.")
    if not plan.targets:
        await ctx.reply("Под условие никто не подходит.")
        return
    preview = ", ".join(f"<code>{target}</code>" for target in plan.targets[:20])
    more = f" и ещё {len(plan.targets) - 20}" if len(plan.targets) > 20 else ""
    pending = stage_cleanup(plan, ctx.actor_id)
    await ctx.reply(f"{html.escape(plan.summary)}.\n{preview}{more}\n\nПодтвердить?",
                    reply_markup=confirmation_keyboard(pending.token))


async def run_cleanup(bot, pending: PendingCleanup, session_factory) -> str:
    """Execute a confirmed plan and return a human summary."""
    plan = pending.plan
    if plan.kind == "удалить":
        deleted, failed = await delete_messages(bot, plan.chat_id, plan.message_ids)
        text = f"Удалено сообщений: {deleted}."
        if failed:
            text += f" Не удалось удалить: {failed} (слишком старые или уже удалены)."
        return text
    if plan.kind == "кик удалённых":
        checked = left = 0
        async with session_factory() as session, session.begin():
            for target_id in plan.targets:
                checked += 1
                try:
                    member = await bot.get_chat_member(plan.chat_id, target_id)
                    status = member.status
                except Exception:
                    status = "unknown"
                if status in {"left", "kicked"}:
                    left += 1
                    row = await session.get(ChatMemberActivity, (plan.chat_id, target_id))
                    if row is not None:
                        row.is_member = False
                        row.updated_at = utcnow()
        return f"Проверено участников: {checked}. Убрано вышедших: {left}."
    kicked, failed = await kick_members(bot, plan.chat_id, plan.targets)
    async with session_factory() as session, session.begin():
        rows = await session.scalars(select(ChatMemberActivity)
                                     .where(ChatMemberActivity.chat_id == plan.chat_id,
                                            ChatMemberActivity.telegram_id.in_(plan.targets)))
        for row in rows.all():
            row.is_member = False
            row.updated_at = utcnow()
        await audit(session, "cleanup_kick", None, f"chat:{plan.chat_id}",
                    {"kind": plan.kind, "kicked": kicked, "failed": failed})
    text = f"Исключено участников: {kicked}."
    if failed:
        text += f" Не удалось: {failed} (проверь права бота)."
    return text


# --------------------------------------------------------------------------------------
# Сетка чатов
# --------------------------------------------------------------------------------------

@command("чаты", key_group="чаты")
@command("сетка чатов", key_group="чаты")
async def cmd_grid_list(ctx: ChatContext):
    async with ctx.session_factory() as session:
        name = await grid_of_chat(session, ctx.chat_id)
        if name is None:
            await ctx.reply("Этот чат не входит в сетку. Установить: <code>дк установить сетку Название</code>.")
            return
        rows = await grid_rows(session, name)
    lines = [f"<b>Сетка «{html.escape(name)}»</b> — чатов: {len(rows)}", ""]
    for row in rows:
        title = html.escape(row.title or str(row.chat_id))
        link = f" — @{row.username}" if row.username else ""
        lines.append(f"• {title} (<code>{row.chat_id}</code>){link}")
    await ctx.reply("\n".join(lines))


async def _install_grid(ctx: ChatContext, name: str) -> None:
    if not name:
        await ctx.reply("Формат: <code>дк установить сетку Название</code>.")
        return
    async with ctx.session_factory() as session, session.begin():
        await set_grid(session, ctx.chat_id, name[:64])
        await audit(session, "grid_set", ctx.actor_id, f"chat:{ctx.chat_id}", {"grid": name[:64]})
    await ctx.reply(f"Чат добавлен в сетку «{html.escape(name[:64])}».")


@command("сетка", key_group="сетка")
async def cmd_grid(ctx: ChatContext):
    args = list(ctx.args)
    async with ctx.session_factory() as session:
        name = await grid_of_chat(session, ctx.chat_id)
    if name is None:
        if args and args[0].lower() in {"установить", "создать"}:
            await _install_grid(ctx, " ".join(args[1:]))
            return
        await ctx.reply("Этот чат не входит в сетку. Установить: <code>дк установить сетку Название</code>.")
        return
    if not args:
        await cmd_grid_list(ctx)
        return
    action = args[0].lower()
    if action in {"выйти", "покинуть", "-"}:
        async with ctx.session_factory() as session, session.begin():
            removed = await remove_from_grid(session, ctx.chat_id)
        await ctx.reply("Чат убран из сетки." if removed else "Чат не был в сетке.")
        return
    if action.startswith("!") or action in {"модер", "админ"}:
        level = args[0].count("!")
        if level == 0:
            level = 1
        reference, _ = extract_target(args[1:])
        target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
        if target_id is None:
            await ctx.reply("Формат: <code>сетка !!модер @ник</code>.")
            return
        await _grid_set_level(ctx, name, target_id, min(5, max(1, level)))
        return
    if action in {"разжаловать", "снять"}:
        reference, _ = extract_target(args[1:])
        target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
        if target_id is None:
            await ctx.reply("Формат: <code>сетка разжаловать @ник</code>.")
            return
        await _grid_set_level(ctx, name, target_id, None)
        return
    await ctx.reply("Команды сетки: <code>сетка</code>, <code>сетка !!модер @ник</code>, "
                    "<code>сетка разжаловать @ник</code>, <code>сетка выйти</code>.")


async def _grid_set_level(ctx: ChatContext, grid_name: str, target_id: int, level: int | None) -> None:
    """A grid rank is applied through the bot's global roster (Mellow keeps one roster)."""
    from mellow.chatadmin.moderation_commands import rank_title, set_level
    if target_id == ctx.actor_id:
        await ctx.reply("Нельзя менять собственный ранг.")
        return
    if level is not None and ctx.actor_level <= level and ctx.actor_level < 5:
        await ctx.reply("Нельзя назначить ранг не ниже своего.")
        return
    await set_level(ctx, target_id, level)
    async with ctx.session_factory() as session, session.begin():
        await audit(session, "grid_staff_changed", ctx.actor_id, f"telegram:{target_id}",
                    {"grid": grid_name, "level": level})
    title = rank_title(level, ctx.settings) if level else "без ранга"
    await ctx.reply(f"Сетка «{html.escape(grid_name)}»: <code>{target_id}</code> — {html.escape(title)}.")


# --------------------------------------------------------------------------------------
# Статистическая информация
# --------------------------------------------------------------------------------------

# «Чат стата {число дней}»: как в документации Ириса — от 30 дней до 5000, без числа - год.
CHAT_STATS_DEFAULT_DAYS = 365
CHAT_STATS_MIN_DAYS = 30
CHAT_STATS_MAX_DAYS = 5000

@command("чат инфо", key_group="статистика")
async def cmd_chat_info(ctx: ChatContext):
    async with ctx.session_factory() as session:
        config = await ctx.store.get(ctx.chat_id)
        name = await grid_of_chat(session, ctx.chat_id)
        await ctx.reply(await chat_stats.chat_overview(session, ctx.chat_id, config, name))


@command("чат стата", key_group="статистика")
@command("статистика чата", key_group="статистика")
async def cmd_chat_stats(ctx: ChatContext):
    days = parse_days(ctx.args[0] if ctx.args else None, default=CHAT_STATS_DEFAULT_DAYS,
                      minimum=CHAT_STATS_MIN_DAYS, maximum=CHAT_STATS_MAX_DAYS)
    if days is None:
        await ctx.reply(f"Период: от {CHAT_STATS_MIN_DAYS} до {CHAT_STATS_MAX_DAYS} дней. "
                        f"Например: <code>чат стата 90</code>. Без числа — {CHAT_STATS_DEFAULT_DAYS} дней.")
        return
    async with ctx.session_factory() as session:
        config = await ctx.store.get(ctx.chat_id)
        text, chart = await chat_stats.chat_statistics(session, ctx.chat_id, days, config)
    if chart:
        await ctx.reply(text + "\n\n<pre>" + html.escape(chart) + "</pre>")
    else:
        await ctx.reply(text + "\n\n<i>График выключен: +график</i>")


@command("статистика вложений", key_group="статистика")
@command("стата вложений", key_group="статистика")
async def cmd_attachment_stats(ctx: ChatContext):
    async with ctx.session_factory() as session:
        messages, attachments = await chat_stats.chat_totals(session, ctx.chat_id)
    share = round(attachments * 100 / messages, 1) if messages else 0
    await ctx.reply(f"<b>Статистика вложений</b>\nСообщений: {messages}\nВложений: {attachments} "
                    f"({share}% сообщений)")


@command("стата", key_group="статистика")
@command("статистика сообщений", key_group="статистика")
async def cmd_message_stats(ctx: ChatContext):
    async with ctx.session_factory() as session:
        await ctx.reply(await chat_stats.message_statistics(session, ctx.settings))


@command("моя стата", key_group="статистика", public=True)
async def cmd_my_stats(ctx: ChatContext):
    async with ctx.session_factory() as session:
        await ctx.reply(await chat_stats.user_statistics(session, ctx.settings, ctx.actor_id, ctx.chat_id))


@command("профиль", key_group="статистика", public=True)
async def cmd_profile(ctx: ChatContext):
    reference, _ = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target) or ctx.actor_id
    async with ctx.session_factory() as session:
        await ctx.reply(await chat_stats.user_statistics(session, ctx.settings, target_id, ctx.chat_id))
