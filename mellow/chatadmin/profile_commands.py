"""«Анкета пользователя»: карточка участника внутри чата.

Ирис хранит у профиля ник, звание, девиз, описание, пол, город, дату рождения и
гражданство. Здесь те же поля живут в таблице ``user_profiles`` с ключом
``(chat_id, telegram_id)``: ник и звание — «в конкретном чате», как и в документации.
Команды вида 🎩 («назначить ник», «удалить описание») относятся к разделу «профиль»
и по умолчанию доступны только владельцу.
"""

from __future__ import annotations

import html

from sqlalchemy import select

from mellow.chatadmin.context import ChatContext, command, extract_target, resolve_user_id
from mellow.models import User, UserProfile, utcnow

NICK_LIMIT = 30
TITLE_LIMIT = 30
MOTTO_LIMIT = 100
ABOUT_LIMIT = 3800
CITY_LIMIT = 60
DEFAULT_LEVEL_PROFILE = 0

GENDERS = {"м": "м", "муж": "м", "мужской": "м", "ж": "ж", "жен": "ж", "женский": "ж",
           "др": "др", "другое": "др", "другой": "др"}
BIRTHDAY_VISIBILITY = {"всё": "всё", "все": "всё", "месяц": "месяц", "год": "год"}


async def get_profile(session, chat_id: int, telegram_id: int) -> UserProfile | None:
    return await session.get(UserProfile, (chat_id, telegram_id))


async def ensure_profile(session, chat_id: int, telegram_id: int) -> UserProfile:
    row = await get_profile(session, chat_id, telegram_id)
    if row is None:
        row = UserProfile(chat_id=chat_id, telegram_id=telegram_id)
        session.add(row)
        await session.flush()
    return row


async def form_text(session, chat_id: int, telegram_id: int, *, viewer_id: int | None = None,
                    with_header: bool = True) -> str:
    """Карточка анкеты. Скрытую анкету видит только её владелец."""
    row = await get_profile(session, chat_id, telegram_id)
    lines = []
    if row is not None and not row.form_visible and viewer_id != telegram_id:
        return "<b>Анкета</b>\nУчастник скрыл свою анкету."
    lines.append(f"<b>Анкета</b> <code>{telegram_id}</code>" if with_header else "<b>Анкета</b>")
    fields = [
        ("Ник", row.nickname if row else None),
        ("Звание", row.title if row else None),
        ("Девиз", row.motto if row else None),
        ("Пол", row.gender if row else None),
        ("Город", row.city if row else None),
        ("Дата рождения", _birthday(row) if row else None),
    ]
    for name, value in fields:
        if value:
            lines.append(f"{name}: {html.escape(str(value))}")
    if row is not None and row.about:
        lines.append(f"О себе: {html.escape(row.about)}")
    if row is not None and row.citizenship:
        lines.append("Гражданство: 🏡 житель этого чата")
    if len(lines) == 1:
        lines.append("Пока пусто. Заполнить: <code>+ник</code>, <code>+звание</code>, "
                     "<code>мой город</code>, <code>о себе</code>.")
    return "\n".join(lines)


def _birthday(row) -> str | None:
    if not row or not row.birthday:
        return None
    visibility = row.birthday_visibility or "всё"
    return f"{row.birthday} (видно: {visibility})"


# --------------------------------------------------------------------------------------
# Анкета: заполнение и видимость
# --------------------------------------------------------------------------------------

@command("анкета", key_group="анкета", public=True)
async def cmd_form(ctx: ChatContext):
    reference, _ = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target) or ctx.actor_id
    async with ctx.session_factory() as session:
        await ctx.reply(await form_text(session, ctx.chat_id, target_id, viewer_id=ctx.actor_id))


@command("моя анкета", key_group="анкета", public=True)
async def cmd_my_form(ctx: ChatContext):
    async with ctx.session_factory() as session:
        await ctx.reply(await form_text(session, ctx.chat_id, ctx.actor_id, viewer_id=ctx.actor_id))


@command("+анкета", key_group="анкета", public=True)
@command("-анкета", key_group="анкета", public=True)
async def cmd_form_visibility(ctx: ChatContext):
    visible = ctx.command.startswith("+")
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.form_visible, row.updated_at = visible, utcnow()
    await ctx.reply("Анкета открыта для других участников." if visible
                    else "Анкета скрыта: её видишь только ты.")


@command("мой пол", key_group="анкета", public=True)
async def cmd_gender(ctx: ChatContext):
    value = (ctx.args[0].lower() if ctx.args else "")
    gender = GENDERS.get(value)
    if gender is None:
        await ctx.reply("Формат: <code>мой пол ж</code> — м, ж или др.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.gender, row.updated_at = gender, utcnow()
    await ctx.reply(f"Пол в анкете: {gender}.")


@command("-мой пол", key_group="анкета", public=True)
async def cmd_gender_clear(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.gender, row.updated_at = None, utcnow()
    await ctx.reply("Пол удалён из анкеты.")


@command("мой город", key_group="анкета", public=True)
async def cmd_city(ctx: ChatContext):
    city = " ".join(ctx.args).strip()[:CITY_LIMIT]
    if not city:
        await ctx.reply("Формат: <code>мой город Казань</code>.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.city, row.updated_at = city, utcnow()
    await ctx.reply(f"Город в анкете: {html.escape(city)}.")


@command("-мой город", key_group="анкета", public=True)
async def cmd_city_clear(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.city, row.updated_at = None, utcnow()
    await ctx.reply("Город удалён из анкеты.")


@command("мой др", key_group="анкета", public=True)
async def cmd_birthday(ctx: ChatContext):
    value = ctx.args[0] if ctx.args else ""
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts) or len(parts[0]) != 2 \
            or len(parts[1]) != 2 or len(parts[2]) != 4:
        await ctx.reply("Формат: <code>мой др 01.01.2000 [всё|месяц|год]</code>.")
        return
    day, month, year = (int(part) for part in parts)
    if not (1 <= day <= 31 and 1 <= month <= 12):
        await ctx.reply("Проверь день и месяц: например, <code>мой др 01.01.2000</code>.")
        return
    visibility = "всё"
    if len(ctx.args) > 1:
        chosen = BIRTHDAY_VISIBILITY.get(ctx.args[1].lower())
        if chosen is None:
            await ctx.reply("Видимость указывается словом: <code>всё</code>, <code>месяц</code> "
                            "или <code>год</code>.")
            return
        visibility = chosen
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.birthday, row.birthday_visibility, row.updated_at = value, visibility, utcnow()
    await ctx.reply(f"Дата рождения сохранена: {value} (видно: {visibility}).")


@command("-мой др", key_group="анкета", public=True)
async def cmd_birthday_clear(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.birthday, row.birthday_visibility, row.updated_at = None, None, utcnow()
    await ctx.reply("Дата рождения удалена из анкеты.")


# --------------------------------------------------------------------------------------
# Описание («О себе»)
# --------------------------------------------------------------------------------------

@command("о себе", key_group="анкета", public=True)
@command("описание", key_group="анкета", public=True)
async def cmd_about(ctx: ChatContext):
    """«О себе [enter]»: текст берётся со следующей строки, как в документации."""
    author_id = ctx.actor_id
    reference, _ = extract_target(ctx.args)
    view_only = ctx.command == "описание" and reference is not None
    if view_only:
        target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
        if target_id is None:
            await ctx.reply("Не удалось определить пользователя.")
            return
        async with ctx.session_factory() as session:
            row = await get_profile(session, ctx.chat_id, target_id)
            if row is None or not row.about:
                await ctx.reply("Описание не заполнено.")
                return
            await ctx.reply(f"<b>О себе</b>\n{html.escape(row.about)}")
        return
    text = (ctx.tail or " ".join(ctx.args)).strip()[:ABOUT_LIMIT]
    if not text:
        await ctx.reply("Напиши описание на следующей строке:\n<code>о себе</code>\n"
                        "<code>текст в несколько строк</code>.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, author_id)
        row.about, row.updated_at = text, utcnow()
    await ctx.reply(f"Описание сохранено ({len(text)} символов).")


@command("-о себе", key_group="анкета", public=True)
async def cmd_about_clear(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.about, row.updated_at = None, utcnow()
    await ctx.reply("Описание удалено.")


@command("назначить описание", key_group="профиль")
@command("удалить описание", key_group="профиль")
async def cmd_about_staff(ctx: ChatContext):
    target_id = await resolve_user_id(ctx.session_factory, *extract_target(ctx.args)[:1],
                                      ctx.reply_target)
    if target_id is None:
        await ctx.reply("Формат: <code>назначить описание @ник</code> и текст на следующей строке.")
        return
    if ctx.command.startswith("удалить"):
        async with ctx.session_factory() as session, session.begin():
            row = await ensure_profile(session, ctx.chat_id, target_id)
            row.about, row.updated_at = None, utcnow()
        await ctx.reply("Описание удалено.")
        return
    text = ctx.tail.strip()[:ABOUT_LIMIT]
    if not text:
        await ctx.reply("Текст описания указывается на следующей строке.")
        return
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, target_id)
        row.about, row.updated_at = text, utcnow()
    await ctx.reply(f"Описание участника <code>{target_id}</code> обновлено.")


# --------------------------------------------------------------------------------------
# Ник, звание и девиз
# --------------------------------------------------------------------------------------

async def _set_field(ctx: ChatContext, field_name: str, value: str | None, limit: int,
                     title: str, *, target_id: int | None = None) -> None:
    target = target_id or ctx.actor_id
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, target)
        setattr(row, field_name, value[:limit] if value else None)
        row.updated_at = utcnow()
    if value:
        await ctx.reply(f"{title}: {html.escape(value[:limit])}"
                        + (f" — для <code>{target}</code>." if target_id else "."))
    else:
        await ctx.reply(f"{title} удалён" + (f" у <code>{target}</code>." if target_id else "."))


@command("+ник", key_group="анкета", public=True)
async def cmd_nick(ctx: ChatContext):
    await _set_field(ctx, "nickname", " ".join(ctx.args).strip(), NICK_LIMIT, "Ник", )


@command("ник", key_group="анкета", public=True)
@command("звание", key_group="анкета", public=True)
@command("девиз", key_group="анкета", public=True)
async def cmd_show_field(ctx: ChatContext):
    field_name, title = {"ник": ("nickname", "Ник"), "звание": ("title", "Звание"),
                         "девиз": ("motto", "Девиз")}[ctx.command]
    reference, _ = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target) or ctx.actor_id
    async with ctx.session_factory() as session:
        row = await get_profile(session, ctx.chat_id, target_id)
    value = getattr(row, field_name, None) if row else None
    await ctx.reply(f"{title}: {html.escape(value)}" if value else f"{title} не задан.")


@command("ник удалить", key_group="анкета", public=True)
@command("-ник", key_group="анкета", public=True)
async def cmd_nick_clear(ctx: ChatContext):
    await _set_field(ctx, "nickname", None, NICK_LIMIT, "Ник")


@command("+звание", key_group="анкета", public=True)
async def cmd_title_set(ctx: ChatContext):
    await _set_field(ctx, "title", " ".join(ctx.args).strip(), TITLE_LIMIT, "Звание")


@command("звание удалить", key_group="анкета", public=True)
@command("-звание", key_group="анкета", public=True)
async def cmd_title_clear(ctx: ChatContext):
    await _set_field(ctx, "title", None, TITLE_LIMIT, "Звание")


@command("+девиз", key_group="анкета", public=True)
async def cmd_motto_set(ctx: ChatContext):
    await _set_field(ctx, "motto", " ".join(ctx.args).strip(), MOTTO_LIMIT, "Девиз")


@command("-девиз", key_group="анкета", public=True)
async def cmd_motto_clear(ctx: ChatContext):
    await _set_field(ctx, "motto", None, MOTTO_LIMIT, "Девиз")


@command("назначить ник", key_group="профиль")
@command("назначить звание", key_group="профиль")
async def cmd_set_field_for_member(ctx: ChatContext):
    field_name, title, limit = (("nickname", "Ник", NICK_LIMIT)
                                if ctx.command.endswith("ник") else ("title", "Звание", TITLE_LIMIT))
    reference, rest = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None or not rest:
        await ctx.reply(f"Формат: <code>{ctx.command} {'текст' if limit == NICK_LIMIT else 'текст'} "
                        f"@ник</code>.")
        return
    await _set_field(ctx, field_name, " ".join(rest).strip(), limit, title, target_id=target_id)


@command("удалить ник", key_group="профиль")
@command("удалить звание", key_group="профиль")
async def cmd_clear_field_for_member(ctx: ChatContext):
    field_name, title, limit = (("nickname", "Ник", NICK_LIMIT)
                                if ctx.command.endswith("ник") else ("title", "Звание", TITLE_LIMIT))
    reference, _ = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target)
    if target_id is None:
        await ctx.reply(f"Формат: <code>{ctx.command} @ник</code>.")
        return
    await _set_field(ctx, field_name, None, limit, title, target_id=target_id)


# --------------------------------------------------------------------------------------
# Гражданство и карточка
# --------------------------------------------------------------------------------------

@command("+гражданство", key_group="анкета", public=True)
async def cmd_citizenship(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.citizenship, row.updated_at = True, utcnow()
    await ctx.reply("Гражданство этого чата установлено: 🏡")


@command("-гражданство", key_group="анкета", public=True)
async def cmd_citizenship_clear(ctx: ChatContext):
    async with ctx.session_factory() as session, session.begin():
        row = await ensure_profile(session, ctx.chat_id, ctx.actor_id)
        row.citizenship, row.updated_at = False, utcnow()
    await ctx.reply("Гражданство снято.")


@command("все граждане", key_group="анкета", public=True)
@command("кто гражданин", key_group="анкета", public=True)
@command("кто граждане", key_group="анкета", public=True)
async def cmd_citizens(ctx: ChatContext):
    async with ctx.session_factory() as session:
        rows = (await session.scalars(select(UserProfile)
                                      .where(UserProfile.chat_id == ctx.chat_id,
                                             UserProfile.citizenship.is_(True))
                                      .order_by(UserProfile.updated_at))).all()
        users = {user.telegram_id: user for user in
                 (await session.scalars(select(User).where(
                     User.telegram_id.in_([row.telegram_id for row in rows])))).all()} if rows else {}
    if not rows:
        await ctx.reply("Жителей этого чата пока нет. Стать жителем: <code>+гражданство</code>.")
        return
    lines = ["<b>Граждане чата</b> 🏡"]
    for row in rows:
        user = users.get(row.telegram_id)
        name = (f"@{user.username}" if user and user.username else str(row.telegram_id))
        lines.append(f"• {html.escape(row.nickname or '')} {('· ' + html.escape(row.title)) if row.title else ''} "
                     f"— {html.escape(name)}".replace("  ", " ").strip())
    await ctx.reply("\n".join(lines))


@command("кто я", key_group="анкета", public=True)
@command("хто я", key_group="анкета", public=True)
async def cmd_who_am_i(ctx: ChatContext):
    ctx.args = []
    await cmd_profile_like(ctx)


@command("кто ты", key_group="анкета", public=True)
async def cmd_who_are_you(ctx: ChatContext):
    await cmd_profile_like(ctx)


async def cmd_profile_like(ctx: ChatContext) -> None:
    from mellow.chatadmin import stats as chat_stats
    reference, _ = extract_target(ctx.args)
    target_id = await resolve_user_id(ctx.session_factory, reference, ctx.reply_target) or ctx.actor_id
    async with ctx.session_factory() as session:
        card = await chat_stats.user_statistics(session, ctx.settings, target_id, ctx.chat_id)
        form = await form_text(session, ctx.chat_id, target_id, viewer_id=ctx.actor_id,
                               with_header=False)
    await ctx.reply(card + "\n\n" + form)
