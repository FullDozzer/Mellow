"""«Настройка использования команд», «Чистка чата», «Настройка чата», «Сетка чатов»,
«Статистическая информация»."""

from __future__ import annotations

import html
import logging
import secrets
import time
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, select

from mellow.chatadmin import stats as chat_stats
from mellow.chatadmin.cleanup import (TELEGRAM_DELETE_LIMIT, CleanupPlan, delete_messages, kick_members,
                                      plan_member_cleanup, plan_message_cleanup,
                                      purge_inactive_punishments)
from mellow.chatadmin.config import (COMMANDS, DISABLED_LEVEL, PUBLIC_LEVEL, command_key,
                                     command_min_level, set_command_access, set_personal_access)
from mellow.chatadmin.context import TABLE, ChatContext, command, extract_target, resolve_user_id
from mellow.chatadmin.grid import grid_of_chat, grid_rows, remove_from_grid, set_grid
from mellow.chatadmin.triggers import (EVENTS, MAX_ACTIONS, delete_trigger, list_triggers, parse_actions,
                                       render_trigger, resolve_event, set_trigger)
from mellow.models import (AuditLog, ChatMemberActivity, CommandAccess, User, UserCommandAccess,
                           utcnow)
from mellow.moderation import parse_period
from mellow.services import audit
from mellow.statistics import format_moment
from mellow.stats import parse_days

log = logging.getLogger("mellow.chatadmin")

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


@command("+приветствие", key_group="настройки")
@command("установить приветствие", key_group="настройки")
async def cmd_set_welcome(ctx: ChatContext):
    await _set_text_setting(ctx, "welcome_text", "приветствие")


@command("+правила", key_group="настройки")
@command("установить правила", key_group="настройки")
async def cmd_set_rules(ctx: ChatContext):
    await _set_text_setting(ctx, "rules_text", "правила")


@command("приветствие", key_group="настройки", public=True)
async def cmd_show_welcome(ctx: ChatContext):
    """Без текста команда показывает приветствие, с текстом — задаёт его (как «+приветствие»)."""
    if ctx.tail or ctx.args:
        await _set_text_setting(ctx, "welcome_text", "приветствие")
        return
    await _show_text_setting(ctx, "welcome_text", "Приветствие")


@command("правила", key_group="настройки", public=True)
async def cmd_show_rules(ctx: ChatContext):
    if ctx.tail or ctx.args:
        await _set_text_setting(ctx, "rules_text", "правила")
        return
    await _show_text_setting(ctx, "rules_text", "Правила")


@command("-приветствие", key_group="настройки")
async def cmd_clear_welcome(ctx: ChatContext):
    await _clear_text_setting(ctx, "welcome_text", "приветствие")


@command("-правила", key_group="настройки")
async def cmd_clear_rules(ctx: ChatContext):
    await _clear_text_setting(ctx, "rules_text", "правила")


async def _show_text_setting(ctx: ChatContext, field_name: str, title: str) -> None:
    config = await ctx.store.get(ctx.chat_id)
    value = getattr(config, field_name)
    if not value:
        await ctx.reply(f"{title} не заданы. Установить: <code>+{title.lower()}</code> и текст "
                        "на следующей строке.")
        return
    await ctx.reply(f"<b>{title}</b>\n{html.escape(value)}")


async def _clear_text_setting(ctx: ChatContext, field_name: str, title: str) -> None:
    await ctx.store.update(ctx.chat_id, **{field_name: None})
    await ctx.reply(f"{title.capitalize()} очищено.")


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
        "Сообщения от каналов: " + ("запрещены" if config.channels_denied else "разрешены"),
        "Уведомления о входах: " + ("включены" if config.notify_joins else "выключены")
        + ", о выходах: " + ("включены" if config.notify_leaves else "выключены"),
        "Минимальная регистрация: " + (f"{config.minreg_days} дн." if config.minreg_days else "выключена"),
        "Автозаявки: " + ("включены" if config.auto_join_requests else "выключены"),
        "Автокик: " + (f"{config.autokick_count} выход(ов) за "
                       f"{(config.autokick_window_seconds or 0) // 86400} дн. → {config.autokick_action}"
                       if config.autokick_count else "выключен"),
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
    lowered = [arg.lower() for arg in args]
    if not args or lowered[0] in {"список", "list"}:
        await _render_access(ctx)
        return
    if lowered[0] == "установить" and len(args) > 1:
        await _install_grid(ctx, " ".join(args[1:]).strip())
        return
    if lowered[0] in {"мдк", "мой доступ команд"}:
        await _set_access_for_key(ctx, "мой дк", args[1:])
        return
    if lowered[0] == "вызов" and len(args) > 1 and lowered[1] == "дк":
        await _set_access_for_key(ctx, "дк", args[2:])
        return
    if lowered[0] == "сброс":
        if len(args) == 1:
            await _reset_access(ctx, None)
            return
        key = command_key(" ".join(args[1:]))
        if key is None:
            await ctx.reply("Неизвестная команда. Список: <code>дк список</code>.")
            return
        await _reset_access(ctx, key)
        return
    if len(args) >= 2 and args[-1].isdigit():
        key = command_key(" ".join(args[:-1]))
        if key is None:
            await ctx.reply("Неизвестная команда. Список: <code>дк список</code>.")
            return
        await _set_access(ctx, key, args[-1])
        return
    await ctx.reply("Формат: <code>дк триггеры 4</code>, <code>дк бан 6</code> (выключить), "
                    "<code>дк варн 0</code> (для всех), <code>дк мдк 1</code>, "
                    "<code>дк вызов дк 4</code>, <code>дк сброс триггеры</code>, "
                    "<code>дк установить сетку Название</code>.")


async def _set_access_for_key(ctx: ChatContext, key: str, args: list[str]) -> None:
    """«Дк мдк {ранг}» и «Дк вызов дк {ранг}» — ограничение для этих двух команд."""
    if not args or not args[0].isdigit():
        await ctx.reply(f"Формат: <code>дк {'мдк' if key == 'мой дк' else 'вызов дк'} "
                        f"{{ранг}}</code>.")
        return
    await _set_access(ctx, key, args[0])


@command("мой дк", key_group="доступ")
@command("мой доступ команд", key_group="доступ")
@command("мдк", key_group="доступ")
async def cmd_my_access(ctx: ChatContext):
    """«Мой доступ команд»: что доступно лично тебе."""
    async with ctx.session_factory() as session:
        lines = ["<b>Мой доступ команд</b>", f"Твой ранг: {ctx.actor_level}"]
        for key, (default, title) in COMMANDS.items():
            required = await command_min_level(session, ctx.chat_id, key)
            if required > 5:
                mark = "❌"
            elif ctx.actor_level >= required and (ctx.actor_level > 0 or required <= 0):
                mark = "✅"
            else:
                mark = "⛔"
            lines.append(f"{mark} <code>{key}</code> — {html.escape(title)}")
    lines.append("\n✅ доступно · ⛔ нужен ранг выше · ❌ выключено")
    await ctx.reply("\n".join(lines))


@command("+дк", key_group="доступ")
@command("-дк", key_group="доступ")
async def cmd_access_toggle(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: <code>+дк варн</code> (открыть всем) или <code>-дк варн</code> "
                        "(выключить).")
        return
    key = command_key(" ".join(ctx.args))
    if key is None:
        await ctx.reply("Неизвестная команда. Список: <code>дк список</code>.")
        return
    level = PUBLIC_LEVEL if ctx.command.startswith("+") else DISABLED_LEVEL
    await _set_access(ctx, key, str(level))


async def _render_access(ctx: ChatContext) -> None:
    async with ctx.session_factory() as session:
        lines = ["<b>Доступ команд</b>", ""]
        for key, (default, title) in COMMANDS.items():
            required = await command_min_level(session, ctx.chat_id, key)
            if required > 5:
                state = "❌ выключено"
            elif required <= 0:
                state = "✅ для всех"
            else:
                state = f"от {required} уровня"
            marker = "" if required == default else " ← изменено"
            lines.append(f"<code>{key}</code> — {state}: {title}{marker}")
        exceptions = await session.scalar(select(func.count()).select_from(UserCommandAccess)
                                          .where(UserCommandAccess.chat_id == ctx.chat_id))
    lines.append("")
    lines.append("Изменить: <code>дк триггеры 4</code> · выключить: <code>-дк бан</code> · "
                 "для всех: <code>+дк варн</code> · сбросить: <code>сброс команд</code>")
    lines.append(f"Личные исключения: {int(exceptions or 0)} — смотреть: <code>все лдк</code>")
    await ctx.reply("\n".join(lines))


async def _set_access(ctx: ChatContext, key: str, raw_level: str) -> None:
    level = max(PUBLIC_LEVEL, min(DISABLED_LEVEL, int(raw_level)))
    async with ctx.session_factory() as session, session.begin():
        await set_command_access(session, ctx.chat_id, key, level)
        await audit(session, "command_access_set", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"command": key, "level": level})
    if level > 5:
        summary = "выключена"
    elif level <= 0:
        summary = "доступна всем"
    else:
        summary = f"доступна с {level} уровня"
    await ctx.reply(f"Команда «{html.escape(key)}» {summary}.")


async def _reset_access(ctx: ChatContext, key: str | None) -> None:
    async with ctx.session_factory() as session, session.begin():
        if key is None:
            await session.execute(sql_delete(CommandAccess).where(CommandAccess.chat_id == ctx.chat_id))
        else:
            await set_command_access(session, ctx.chat_id, key, None)
        await audit(session, "command_access_reset", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"command": key or "все"})
    await ctx.reply("Все настройки доступа сброшены." if key is None
                    else f"Доступ команды «{html.escape(key)}» сброшен к значению по умолчанию.")


@command("сброс команд", key_group="доступ")
async def cmd_reset_access(ctx: ChatContext):
    await _reset_access(ctx, None)


@command("импорт команд из", key_group="доступ")
async def cmd_import_access(ctx: ChatContext):
    if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
        await ctx.reply("Формат: <code>импорт команд из -1001234567890</code>.")
        return
    source = int(ctx.args[0])
    if source == ctx.chat_id:
        await ctx.reply("Это тот же чат.")
        return
    async with ctx.session_factory() as session, session.begin():
        rows = (await session.scalars(select(CommandAccess)
                                      .where(CommandAccess.chat_id == source))).all()
        if not rows:
            await ctx.reply("В указанном чате нет изменённых доступов.")
            return
        await session.execute(sql_delete(CommandAccess).where(CommandAccess.chat_id == ctx.chat_id))
        for row in rows:
            session.add(CommandAccess(chat_id=ctx.chat_id, command=row.command, min_level=row.min_level))
        await audit(session, "command_access_imported", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"source": source, "count": len(rows)})
    await ctx.reply(f"Импортировано настроек доступа: {len(rows)} из <code>{source}</code>.")


@command("лог дк", key_group="доступ")
async def cmd_access_log(ctx: ChatContext):
    reference, _ = extract_target(list(ctx.args))
    actor_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    async with ctx.session_factory() as session:
        query = select(AuditLog).where(AuditLog.action.in_(("command_access_set", "command_access_reset",
                                                            "command_access_imported",
                                                            "user_command_access_set")))
        if actor_id is not None:
            query = query.where(AuditLog.actor_id == actor_id)
        rows = (await session.scalars(query.order_by(AuditLog.id.desc()).limit(15))).all()
    if not rows:
        await ctx.reply("Изменений доступа команд не найдено.")
        return
    lines = ["<b>Лог доступа команд</b>"]
    for row in rows:
        payload = row.details or {}
        command = payload.get("command", "?")
        level = payload.get("level", payload.get("allowed"))
        lines.append(f"• {format_moment(row.created_at)} — <code>{html.escape(str(row.actor_id))}</code>: "
                     f"<code>{html.escape(str(command))}</code> → {level}")
    await ctx.reply("\n".join(lines))


@command("+команды", key_group="доступ")
@command("-команды", key_group="доступ")
async def cmd_access_notice(ctx: ChatContext):
    enabled = ctx.command.startswith("+")
    await ctx.store.update(ctx.chat_id, notify_command_access=enabled)
    await ctx.reply("Оповещение о доступности команд включено." if enabled
                    else "Оповещение о доступности команд выключено.")


# --------------------------------------------------------------------------------------
# Личный доступ команд («Лдк»)
# --------------------------------------------------------------------------------------

@command("+лдк", key_group="лдк")
@command("-лдк", key_group="лдк")
async def cmd_personal_access(ctx: ChatContext):
    if len(ctx.args) < 2:
        await ctx.reply("Формат: <code>+лдк варн @ник</code> или <code>-лдк варн @ник</code>.")
        return
    key = command_key(" ".join(ctx.args[:-1]))
    if key is None:
        await ctx.reply("Неизвестная команда. Список: <code>дк список</code>.")
        return
    reference = ctx.args[-1]
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Не удалось определить пользователя.")
        return
    allowed = ctx.command.startswith("+")
    async with ctx.session_factory() as session, session.begin():
        await set_personal_access(session, ctx.chat_id, target_id, key, allowed)
        await audit(session, "user_command_access_set", ctx.actor_id, f"telegram:{target_id}",
                    {"chat_id": ctx.chat_id, "command": key, "allowed": allowed})
    await ctx.reply(f"Личный доступ к «{html.escape(key)}» для <code>{target_id}</code>: "
                    + ("открыт." if allowed else "закрыт."))


@command("лдк", key_group="лдк")
async def cmd_show_personal_access(ctx: ChatContext):
    if not ctx.args:
        await ctx.reply("Формат: <code>лдк @ник</code> или <code>лдк варн</code>.")
        return
    key = command_key(" ".join(ctx.args))
    async with ctx.session_factory() as session:
        if key is not None:
            rows = (await session.scalars(select(UserCommandAccess)
                                          .where(UserCommandAccess.chat_id == ctx.chat_id,
                                                 UserCommandAccess.command == key))).all()
            if not rows:
                await ctx.reply(f"Личных исключений для «{html.escape(key)}» нет.")
                return
            lines = [f"<b>Лдк: {html.escape(key)}</b>"] + [
                f"• <code>{row.telegram_id}</code> — " + ("открыт" if row.allowed else "закрыт")
                for row in rows]
            await ctx.reply("\n".join(lines))
            return
        target_id = await resolve_user_id(ctx.session_factory, " ".join(ctx.args), ctx.reply_target)
        if target_id is None:
            await ctx.reply("Не удалось определить пользователя.")
            return
        rows = (await session.scalars(select(UserCommandAccess)
                                      .where(UserCommandAccess.chat_id == ctx.chat_id,
                                             UserCommandAccess.telegram_id == target_id))).all()
    if not rows:
        await ctx.reply(f"У <code>{target_id}</code> нет личных исключений: права по рангу.")
        return
    lines = [f"<b>Лдк {html.escape(str(target_id))}</b>"] + [
        f"• <code>{html.escape(row.command)}</code> — " + ("открыт" if row.allowed else "закрыт")
        for row in rows]
    await ctx.reply("\n".join(lines))


@command("все лдк", key_group="лдк")
async def cmd_all_personal_access(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = (await session.scalars(select(UserCommandAccess)
                                      .where(UserCommandAccess.chat_id == ctx.chat_id))).all()
    if not rows:
        await ctx.reply("Личных исключений в чате нет.")
        return
    grouped: dict[int, list[str]] = {}
    for row in rows:
        grouped.setdefault(row.telegram_id, []).append(row.command + ("" if row.allowed else " (закрыт)"))
    lines = ["<b>Личный доступ команд</b>"] + [
        f"• <code>{telegram_id}</code>: {', '.join(sorted(commands))}"
        for telegram_id, commands in sorted(grouped.items())]
    await ctx.reply("\n".join(lines))


@command("сброс лдк", key_group="лдк")
async def cmd_reset_personal_access(ctx: ChatContext):
    reference, _ = extract_target(list(ctx.args))
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Укажи пользователя: <code>сброс лдк @ник</code>.")
        return
    async with ctx.session_factory() as session, session.begin():
        await set_personal_access(session, ctx.chat_id, target_id, "*", None)
        rows = (await session.scalars(select(UserCommandAccess)
                                      .where(UserCommandAccess.chat_id == ctx.chat_id,
                                             UserCommandAccess.telegram_id == target_id))).all()
        for row in rows:
            await session.delete(row)
        await audit(session, "user_command_access_reset", ctx.actor_id, f"telegram:{target_id}",
                    {"chat_id": ctx.chat_id, "count": len(rows)})
    await ctx.reply(f"Личные исключения пользователя <code>{target_id}</code> сброшены: {len(rows)}.")


@command("сброс всех лдк", key_group="лдк")
async def cmd_reset_all_personal_access(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        rows = (await session.scalars(select(UserCommandAccess)
                                      .where(UserCommandAccess.chat_id == ctx.chat_id))).all()
        for row in rows:
            await session.delete(row)
        await audit(session, "user_command_access_reset_all", ctx.actor_id, f"chat:{ctx.chat_id}",
                    {"count": len(rows)})
    await ctx.reply(f"Все личные исключения сброшены: {len(rows)}.")


# --------------------------------------------------------------------------------------
# Чистка чата
# --------------------------------------------------------------------------------------

CLEANUP_HELP = """<b>Чистка чата</b>

• <code>удалить 20</code> — удалить последние сообщения (или <code>чистка смс 50</code>)
• <code>-смс 20</code> — удалить 20 сообщений выше команды, <code>пург 50</code> — ниже
• <code>кик неактив 30 дней</code> или <code>кик неактив 10</code> — молчащие участники
• <code>кик актив 7 дней</code> — исключить тех, кто писал за период
• <code>кик новичков 1 день</code> — исключить недавно вошедших
• <code>кик молчунов 7</code> — в чате дольше срока и без сообщений
• <code>кик по смс 5 2 недели</code> — у кого меньше 5 сообщений
• <code>кик удалённых</code> / <code>кто удалён</code> — вышедшие и удалённые аккаунты

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
@command("кик молчунов", key_group="чистка")
@command("кик по смс", key_group="чистка")
@command("кик по сообщениям", key_group="чистка")
async def cmd_kick(ctx: ChatContext):
    raw = list(ctx.args)
    days = count = min_messages = None
    if ctx.command in {"кик по смс", "кик по сообщениям"} and raw and raw[0].isdigit():
        min_messages = max(1, int(raw.pop(0)))
    if raw and raw[0].isdigit() and len(raw) == 1 and ctx.command == "кик неактив":
        # «Кик неактив 10» — исключить десять самых неактивных, «кик неактив 10 дней» — период.
        count = max(1, int(raw[0]))
    elif raw:
        if raw[0].isdigit():
            days = max(1, int(raw[0]))
        else:
            seconds = parse_period(" ".join(raw))
            days = max(1, seconds // 86400) if seconds else None
    async with ctx.session_factory() as session, session.begin():
        closed = await purge_inactive_punishments(session)
        plan = await plan_member_cleanup(session, ctx.chat_id, ctx.command, days, count=count,
                                        min_messages=min_messages)
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


# --------------------------------------------------------------------------------------
# Настройка чата: закрепы, название, описание, входы и выходы, теги
# --------------------------------------------------------------------------------------

TAG_LIMIT = 16
DEFAULT_MEMBER_PERMISSIONS = {
    "can_send_messages": True, "can_send_audios": True, "can_send_documents": True,
    "can_send_photos": True, "can_send_videos": True, "can_send_video_notes": True,
    "can_send_voice_notes": True, "can_send_polls": True, "can_send_other_messages": True,
    "can_add_web_page_previews": True,
}


async def _message_id(ctx: ChatContext) -> int | None:
    if ctx.args and ctx.args[0].lstrip("-").isdigit():
        return int(ctx.args[0])
    if ctx.reply_target is not None:
        return ctx.reply_target.message_id
    return None


@command("закреп", key_group="настройки")
@command("пин", key_group="настройки")
@command("pin", key_group="настройки")
async def cmd_pin(ctx: ChatContext):
    message_id = await _message_id(ctx)
    if message_id is None:
        await ctx.reply("Ответь командой на сообщение или укажи ID: <code>закреп 12345</code>.")
        return
    try:
        await ctx.bot.pin_chat_message(ctx.chat_id, message_id)
    except Exception:
        await ctx.reply("Не удалось закрепить: проверь право бота «закреплять сообщения».")
        return
    await ctx.reply("Закреплено.")


@command("открепить", key_group="настройки")
@command("анпин", key_group="настройки")
@command("unpin", key_group="настройки")
async def cmd_unpin(ctx: ChatContext):
    message_id = await _message_id(ctx)
    try:
        await ctx.bot.unpin_chat_message(ctx.chat_id, message_id)
    except Exception:
        await ctx.reply("Не удалось открепить: проверь право бота «закреплять сообщения».")
        return
    await ctx.reply("Откреплено.")


@command("название", key_group="настройки")
async def cmd_set_title(ctx: ChatContext):
    title = (ctx.tail or " ".join(ctx.args)).strip()
    if not title:
        await ctx.reply("Формат: <code>название Mellow Server</code>.")
        return
    try:
        await ctx.bot.set_chat_title(ctx.chat_id, title[:128])
    except Exception:
        await ctx.reply("Не удалось переименовать чат: проверь право бота «изменять чат».")
        return
    await ctx.store.remember_title(ctx.chat_id, title[:128])
    await ctx.reply("Название обновлено.")


@command("+описание чата", key_group="настройки")
@command("-описание чата", key_group="настройки")
async def cmd_set_description(ctx: ChatContext):
    text = "" if ctx.command.startswith("-") else (ctx.tail or " ".join(ctx.args)).strip()
    try:
        await ctx.bot.set_chat_description(ctx.chat_id, text[:255])
    except Exception:
        await ctx.reply("Не удалось изменить описание чата: проверь права бота.")
        return
    await ctx.reply("Описание чата очищено." if not text else "Описание чата обновлено.")


@command("+чат ссылка", key_group="настройки")
@command("+чат ссылка по заявкам", key_group="настройки")
async def cmd_create_chat_link(ctx: ChatContext):
    """«+Чат ссылка» / «+Чат ссылка по заявкам»: бот создаёт и запоминает ссылку."""
    join_request = ctx.command.endswith("по заявкам")
    try:
        link = await ctx.bot.create_chat_invite_link(ctx.chat_id, creates_join_request=join_request)
    except Exception:
        await ctx.reply("Не удалось создать ссылку: проверь право бота «приглашать участников».")
        return
    config = await ctx.store.get(ctx.chat_id)
    links = list(config.invite_links or []) + [link.invite_link]
    await ctx.store.update(ctx.chat_id, invite_links=links[-20:])
    kind = "по заявкам" if join_request else "обычная"
    await ctx.reply(f"Ссылка на чат ({kind}): {html.escape(link.invite_link)}")


@command("-чат ссылка", key_group="настройки")
async def cmd_revoke_chat_link(ctx: ChatContext):
    config = await ctx.store.get(ctx.chat_id)
    links = list(config.invite_links or [])
    if not links:
        await ctx.reply("Ссылок, созданных ботом, нет.")
        return
    try:
        await ctx.bot.revoke_chat_invite_link(ctx.chat_id, links[-1])
    except Exception:
        await ctx.reply("Не удалось отозвать ссылку: проверь право бота «приглашать участников».")
        return
    await ctx.store.update(ctx.chat_id, invite_links=links[:-1])
    await ctx.reply("Ссылка отозвана.")


@command("сброс ссылок", key_group="настройки")
async def cmd_reset_chat_links(ctx: ChatContext):
    config = await ctx.store.get(ctx.chat_id)
    links = list(config.invite_links or [])
    revoked = 0
    for link in links:
        try:
            await ctx.bot.revoke_chat_invite_link(ctx.chat_id, link)
            revoked += 1
        except Exception:
            log.info("Could not revoke %s in chat %s", link, ctx.chat_id)
    await ctx.store.update(ctx.chat_id, invite_links=[])
    await ctx.reply(f"Отозвано ссылок: {revoked} из {len(links)}.")


@command("чат-ссылка", key_group="настройки", public=True)
async def cmd_show_chat_link(ctx: ChatContext):
    config = await ctx.store.get(ctx.chat_id)
    links = list(config.invite_links or [])
    if links:
        await ctx.reply(f"Ссылка на чат: {html.escape(links[-1])}")
        return
    try:
        link = await ctx.bot.export_chat_invite_link(ctx.chat_id)
    except Exception:
        await ctx.reply("Ссылки нет. Создать: <code>+чат ссылка</code> или "
                        "<code>+чат ссылка по заявкам</code>.")
        return
    await ctx.reply(f"Ссылка на чат: {html.escape(link)}")


@command("топик название", key_group="настройки")
async def cmd_rename_topic(ctx: ChatContext):
    title = (ctx.tail or " ".join(ctx.args)).strip()
    thread_id = getattr(ctx.message, "message_thread_id", None)
    if not thread_id:
        await ctx.reply("Команда работает в топике: напиши её в том топике, который нужно "
                        "переименовать.")
        return
    if not title:
        await ctx.reply("Формат: <code>топик название Новое имя</code>.")
        return
    try:
        await ctx.bot.edit_forum_topic(ctx.chat_id, thread_id, name=title[:128])
    except Exception:
        await ctx.reply("Не удалось переименовать топик: проверь право бота «управлять темами».")
        return
    await ctx.reply("Топик переименован.")


@command("тг права", key_group="настройки")
async def cmd_telegram_rights(ctx: ChatContext):
    """«Тг права {ссылка}»: что участник может в Telegram."""
    reference, _ = extract_target(list(ctx.args))
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: <code>тг права @ник</code>.")
        return
    try:
        member = await ctx.bot.get_chat_member(ctx.chat_id, target_id)
    except Exception:
        await ctx.reply("Не удалось получить права участника.")
        return
    rights = []
    for name in ("can_manage_chat", "can_delete_messages", "can_restrict_members",
                 "can_pin_messages", "can_invite_users", "can_promote_members"):
        if getattr(member, name, False):
            rights.append(name)
    status = getattr(member, "status", "unknown")
    text = f"<b>Telegram-права</b>\nСтатус: <code>{html.escape(str(status))}</code>"
    if rights:
        text += "\n" + ", ".join(f"<code>{html.escape(name)}</code>" for name in rights)
    else:
        text += "\nАдминистративных прав нет."
    await ctx.reply(text)


@command("тг разрешения чата", key_group="настройки")
async def cmd_telegram_permissions(ctx: ChatContext):
    """«Тг разрешения чата»: какие права у обычных участников."""
    try:
        chat = await ctx.bot.get_chat(ctx.chat_id)
    except Exception:
        await ctx.reply("Не удалось получить настройки чата.")
        return
    permissions = getattr(chat, "permissions", None)
    if permissions is None:
        await ctx.reply("Telegram не сообщил ограничения: у участников стандартные права.")
        return
    allowed, denied = [], []
    for name, value in permissions.model_dump(exclude_none=True).items():
        (allowed if value else denied).append(name)
    text = "<b>Разрешения чата</b>"
    text += "\nРазрешено: " + (", ".join(f"<code>{name}</code>" for name in sorted(allowed))
                               or "—")
    text += "\nЗапрещено: " + (", ".join(f"<code>{name}</code>" for name in sorted(denied)) or "—")
    await ctx.reply(text)


@command("+чат", key_group="настройки")
@command("-чат", key_group="настройки")
async def cmd_close_chat(ctx: ChatContext):
    from aiogram.types import ChatPermissions
    if ctx.command.startswith("+"):
        config = await ctx.store.get(ctx.chat_id)
        previous = config.closed_permissions or DEFAULT_MEMBER_PERMISSIONS
        try:
            await ctx.bot.set_chat_permissions(ctx.chat_id, ChatPermissions(can_send_messages=False))
        except Exception:
            await ctx.reply("Не удалось закрыть чат: проверь право бота «ограничивать участников».")
            return
        await ctx.store.update(ctx.chat_id, closed_permissions=previous)
        await ctx.reply("Чат закрыт: писать могут только администраторы.")
        return
    config = await ctx.store.get(ctx.chat_id)
    permissions = ChatPermissions(**(config.closed_permissions or DEFAULT_MEMBER_PERMISSIONS))
    try:
        await ctx.bot.set_chat_permissions(ctx.chat_id, permissions)
    except Exception:
        await ctx.reply("Не удалось открыть чат: проверь права бота.")
        return
    await ctx.store.update(ctx.chat_id, closed_permissions=None)
    await ctx.reply("Чат открыт: участники снова могут писать.")


@command("+каналы", key_group="настройки")
@command("-каналы", key_group="настройки")
async def cmd_channels(ctx: ChatContext):
    denied = ctx.command.startswith("-")
    await ctx.store.update(ctx.chat_id, channels_denied=denied)
    await ctx.reply("Сообщения от имени каналов запрещены: бот будет их удалять." if denied
                    else "Сообщения от имени каналов разрешены.")


@command("+входы", key_group="настройки")
@command("-входы", key_group="настройки")
@command("+выходы", key_group="настройки")
@command("-выходы", key_group="настройки")
@command("+входы-выходы", key_group="настройки")
@command("-входы-выходы", key_group="настройки")
async def cmd_join_leave_notices(ctx: ChatContext):
    enabled = ctx.command.startswith("+")
    command = ctx.command.lstrip("+-")
    threshold = None
    if ctx.args and ctx.args[0].isdigit():
        threshold = max(0, int(ctx.args[0]))
    fields = {}
    if command in {"входы", "входы-выходы"}:
        fields["notify_joins"] = enabled
    if command in {"выходы", "входы-выходы"}:
        fields["notify_leaves"] = enabled
    if threshold is not None and command == "выходы":
        fields["leave_notify_min_messages"] = threshold
    await ctx.store.update(ctx.chat_id, **fields)
    state = "включены" if enabled else "выключены"
    extra = f" Порог для выходов: {threshold} сообщений." if threshold is not None else ""
    await ctx.reply(f"Уведомления «{command}» {state}.{extra}")


@command("+минрег", key_group="настройки")
@command("-минрег", key_group="настройки")
@command("минрег", key_group="настройки")
async def cmd_minreg(ctx: ChatContext):
    if ctx.command == "минрег":
        config = await ctx.store.get(ctx.chat_id)
        state = f"{config.minreg_days} дн." if config.minreg_days else "выключена"
        await ctx.reply(f"Минимальная регистрация: {state}. Порог считается от первого "
                        "взаимодействия с ботом, а не от создания аккаунта в Telegram.")
        return
    if ctx.command.startswith("-"):
        await ctx.store.update(ctx.chat_id, minreg_days=None)
        await ctx.reply("Фильтр минимальной регистрации выключен.")
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await ctx.reply("Формат: <code>+минрег 3</code> — минимум дней с первого взаимодействия "
                        "с ботом.")
        return
    days = max(1, min(3650, int(ctx.args[0])))
    await ctx.store.update(ctx.chat_id, minreg_days=days)
    await ctx.reply(f"Минимальная регистрация: {days} дн. Новые участники моложе срока будут "
                    "исключаться при входе.")


@command("+автозаявки", key_group="настройки")
@command("-автозаявки", key_group="настройки")
async def cmd_auto_join_requests(ctx: ChatContext):
    enabled = ctx.command.startswith("+")
    await ctx.store.update(ctx.chat_id, auto_join_requests=enabled)
    await ctx.reply("Заявки на вступление принимаются автоматически." if enabled
                    else "Автоматическое принятие заявок выключено.")


@command("+автокик", key_group="настройки")
@command("-автокик", key_group="настройки")
async def cmd_autokick(ctx: ChatContext):
    if ctx.command.startswith("-") or not ctx.args:
        await ctx.store.update(ctx.chat_id, autokick_count=None, autokick_window_seconds=None,
                              autokick_action=None)
        await ctx.reply("Автокик на выход выключен."
                        if ctx.command.startswith("-") else
                        "Формат: <code>+автокик 3 60 бан</code> — 3 выхода за 60 минут → бан.")
        return
    args = list(ctx.args)
    count = int(args[0]) if args[0].isdigit() else None
    if count is None:
        await ctx.reply("Формат: <code>+автокик 3 60 бан</code> — 3 выхода за 60 минут → бан.")
        return
    window_minutes = int(args[1]) if len(args) > 1 and args[1].isdigit() else 60
    action = "бан" if "бан" in " ".join(args[2:]).lower() else "кик"
    await ctx.store.update(ctx.chat_id, autokick_count=count,
                          autokick_window_seconds=window_minutes * 60, autokick_action=action)
    await ctx.reply(f"Автокик включён: {count} выход(ов) за {window_minutes} мин. → "
                    f"{'бан' if action == 'бан' else 'кик'}.")


@command("+тг тег", key_group="настройки")
async def cmd_set_tag(ctx: ChatContext):
    args = list(ctx.args)
    if len(args) < 2:
        await ctx.reply("Формат: <code>+тг тег олдфаг @ник</code> (до 16 символов).")
        return
    reference = args[-1]
    tag = " ".join(args[:-1]).strip()[:TAG_LIMIT]
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Не удалось определить пользователя.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await session.get(ChatMemberActivity, (ctx.chat_id, target_id))
        if row is None:
            session.add(ChatMemberActivity(chat_id=ctx.chat_id, telegram_id=target_id, tag=tag))
        else:
            row.tag, row.updated_at = tag, utcnow()
        await audit(session, "member_tag_set", ctx.actor_id, f"telegram:{target_id}",
                    {"chat_id": ctx.chat_id, "tag": tag})
    await ctx.reply(f"Тег <code>{html.escape(tag)}</code> установлен для <code>{target_id}</code>.")


@command("-тг тег", key_group="настройки")
async def cmd_clear_tag(ctx: ChatContext):
    reference, _ = extract_target(list(ctx.args))
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Укажи пользователя: <code>-тг тег @ник</code>.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await session.get(ChatMemberActivity, (ctx.chat_id, target_id))
        if row is None or not row.tag:
            await ctx.reply("У этого пользователя нет тега.")
            return
        row.tag, row.updated_at = None, utcnow()
    await ctx.reply("Тег снят.")


@command("+тг админ", key_group="настройки")
async def cmd_promote_admin(ctx: ChatContext):
    args = list(ctx.args)
    if not args:
        await ctx.reply("Формат: <code>+тг админ Модератор @ник</code>.")
        return
    reference = args[-1]
    title = " ".join(args[:-1]).strip()[:16]
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Не удалось определить пользователя.")
        return
    try:
        await ctx.bot.promote_chat_member(ctx.chat_id, target_id, can_manage_chat=True,
                                          can_delete_messages=True, can_restrict_members=True,
                                          can_invite_users=True, can_pin_messages=True)
        if title:
            await ctx.bot.set_chat_administrator_custom_title(ctx.chat_id, target_id, title)
    except Exception:
        await ctx.reply("Не удалось назначить администратора: проверь права бота и то, что "
                        "участника не назначил другой администратор.")
        return
    await ctx.reply(f"<code>{target_id}</code> назначен Telegram-администратором"
                    + (f" с должностью <code>{html.escape(title)}</code>." if title else "."))


@command("-тг админ", key_group="настройки")
async def cmd_demote_admin(ctx: ChatContext):
    reference, _ = extract_target(list(ctx.args))
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply("Укажи пользователя: <code>-тг админ @ник</code>.")
        return
    try:
        await ctx.bot.promote_chat_member(ctx.chat_id, target_id, can_manage_chat=False,
                                          can_delete_messages=False, can_restrict_members=False,
                                          can_invite_users=False, can_pin_messages=False)
    except Exception:
        await ctx.reply("Не удалось снять администратора: права выдал другой администратор "
                        "или у бота нет права «добавлять администраторов».")
        return
    await ctx.reply(f"<code>{target_id}</code> больше не Telegram-администратор.")


@command("проверить в чате", key_group="настройки")
async def cmd_check_members(ctx: ChatContext):
    """Проверяет, что участники из статистики действительно находятся в чате."""
    async with ctx.session_factory() as session:
        rows = (await session.scalars(select(ChatMemberActivity)
                                      .where(ChatMemberActivity.chat_id == ctx.chat_id,
                                             ChatMemberActivity.is_member.is_(True))
                                      .limit(100))).all()
    checked = removed = 0
    for row in rows:
        checked += 1
        try:
            member = await ctx.bot.get_chat_member(ctx.chat_id, row.telegram_id)
            status = getattr(member, "status", None)
        except Exception:
            status = "left"
        if status in {"left", "kicked"}:
            removed += 1
            async with ctx.session_factory() as session, session.begin():
                stored = await session.get(ChatMemberActivity, (ctx.chat_id, row.telegram_id))
                if stored is not None:
                    stored.is_member = False
                    stored.updated_at = utcnow()
    await ctx.reply(f"Проверено участников: {checked}. Вышедших убрано из статистики: {removed}.")


# --------------------------------------------------------------------------------------
# Удаление сообщений без подтверждения и удалённые аккаунты
# --------------------------------------------------------------------------------------

@command("-смс", key_group="чистка")
async def cmd_delete_messages(ctx: ChatContext):
    quiet = any(arg.lower().startswith("тих") for arg in ctx.args)
    numbers = [arg for arg in ctx.args if arg.isdigit()]
    message_ids: list[int] = []
    if numbers:
        message_ids = list(ctx.recent.last(ctx.chat_id, max(1, min(TELEGRAM_DELETE_LIMIT,
                                                                  int(numbers[0])))))
    elif ctx.reply_target is not None:
        message_ids = [ctx.reply_target.message_id]
    message_ids.append(ctx.message.message_id)
    deleted, failed = await delete_messages(ctx.bot, ctx.chat_id, message_ids)
    if quiet:
        return
    if not deleted:
        await ctx.reply("Нечего удалять: я помню только сообщения, отправленные после запуска бота. "
                        "Для длинной команды ответь на сообщение.")
        return
    await ctx.reply(f"Удалено сообщений: {deleted}."
                    + (f" Не удалось: {failed} (старше 48 часов)." if failed else ""))


@command("пург", key_group="чистка")
async def cmd_purge(ctx: ChatContext):
    if ctx.reply_target is None:
        await ctx.reply("Пург работает только ответом на сообщение: <code>пург 50</code>.")
        return
    quiet = any(arg.lower().startswith("тих") for arg in ctx.args)
    numbers = [arg for arg in ctx.args if arg.isdigit()]
    limit = int(numbers[0]) if numbers else TELEGRAM_DELETE_LIMIT
    anchor = ctx.reply_target.message_id
    below = [message_id for message_id in ctx.recent.last(ctx.chat_id, TELEGRAM_DELETE_LIMIT * 2)
             if message_id >= anchor]
    message_ids = sorted(below)[:max(1, min(TELEGRAM_DELETE_LIMIT * 2, limit))]
    message_ids.append(ctx.message.message_id)
    deleted, failed = await delete_messages(ctx.bot, ctx.chat_id, message_ids)
    if quiet:
        return
    await ctx.reply(f"Удалено сообщений: {deleted}."
                    + (f" Не удалось: {failed}." if failed else ""))


@command("кто удалён", key_group="чистка")
@command("кто собака", key_group="чистка")
async def cmd_deleted_accounts(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = (await session.scalars(select(ChatMemberActivity)
                                      .where(ChatMemberActivity.chat_id == ctx.chat_id,
                                             ChatMemberActivity.is_member.is_(True))
                                      .limit(100))).all()
    found = []
    for row in rows:
        try:
            member = await ctx.bot.get_chat_member(ctx.chat_id, row.telegram_id)
        except Exception:
            continue
        user = getattr(member, "user", None)
        name = (getattr(user, "first_name", "") or "").strip().lower()
        if name == "deleted account" or getattr(user, "is_deleted", False):
            found.append(row.telegram_id)
    if not found:
        await ctx.reply("Удалённых аккаунтов в чате не найдено.")
        return
    await ctx.reply("<b>Удалённые аккаунты</b>\n"
                    + ", ".join(f"<code>{target_id}</code>" for target_id in found[:50])
                    + "\n\nУбрать: <code>кик удалённых</code>.")


# «Кик собак» — синоним «кик удалённых» по документации.
@command("кик собак", key_group="чистка")
async def cmd_kick_deleted_accounts(ctx: ChatContext):
    ctx.command = "кик удалённых"
    await cmd_kick(ctx)


# --------------------------------------------------------------------------------------
# Анкета пользователя
# --------------------------------------------------------------------------------------

@command("анкета", key_group="статистика", public=True)
@command("моя анкета", key_group="статистика", public=True)
async def cmd_profile_form(ctx: ChatContext):
    """«Анкета пользователя»: в Mellow это карточка профиля с данными анкеты и активностью."""
    await cmd_profile(ctx)


# --------------------------------------------------------------------------------------
# Сетка: отставка
# --------------------------------------------------------------------------------------

async def grid_resign(ctx: ChatContext, grid_name: str) -> None:
    """«Сетка ухожу в отставку»: снимает собственный ранг во всей сетке."""
    from mellow.models import Staff
    async with ctx.session_factory() as session, session.begin():
        user = await session.scalar(select(User).where(User.telegram_id == ctx.actor_id))
        staff = await session.get(Staff, user.id) if user is not None else None
        if staff is None or not staff.active:
            await ctx.reply("Ты не в составе модерации.")
            return
        if staff.level >= 5:
            await ctx.reply("Создатель не может уйти в отставку: сначала передай права.")
            return
        staff.active, staff.updated_at = False, utcnow()
        await audit(session, "grid_resign", ctx.actor_id, f"chat:{ctx.chat_id}", {"grid": grid_name})
    await ctx.reply(f"Полномочия сняты во всей сетке «{html.escape(grid_name)}».")
