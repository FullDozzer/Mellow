from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64))
    minecraft_username: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Staff(Base):
    __tablename__ = "staff"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    level: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # «+Мой онлайн»: a moderator decides whether the staff list comments on his activity.
    show_online: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Application(Base):
    __tablename__ = "applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(24), default="creating", index=True)
    application_data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    reviewed_by: Mapped[int | None] = mapped_column(Integer)
    chat_id: Mapped[int | None] = mapped_column(Integer)
    thread_id: Mapped[int | None] = mapped_column(Integer)


Index("uq_active_application_user", Application.user_id, unique=True,
      sqlite_where=text("status IN ('creating','pending','info_requested')"),
      postgresql_where=text("status IN ('creating','pending','info_requested')"))


class ApplicationDraft(Base):
    __tablename__ = "application_drafts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    question_index: Mapped[int] = mapped_column(Integer, default=0)
    editing_index: Mapped[int | None] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class MessageStat(Base):
    __tablename__ = "message_stats"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    first_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    threshold_reached: Mapped[bool] = mapped_column(Boolean, default=False)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(24))
    subject: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_to: Mapped[int | None] = mapped_column(Integer)
    chat_id: Mapped[int | None] = mapped_column(Integer)
    thread_id: Mapped[int | None] = mapped_column(Integer)


class Suggestion(Base):
    __tablename__ = "suggestions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="new", index=True)
    decision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    chat_id: Mapped[int | None] = mapped_column(Integer)
    thread_id: Mapped[int | None] = mapped_column(Integer)


class Punishment(Base):
    __tablename__ = "punishments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int | None] = mapped_column(Integer, index=True)
    target_user_id: Mapped[int] = mapped_column(Integer, index=True)
    moderator_id: Mapped[int | None] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text)
    duration: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WhitelistOperation(Base):
    __tablename__ = "whitelist_operations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation_key: Mapped[str] = mapped_column(String(64), unique=True)
    username: Mapped[str] = mapped_column(String(16))
    user_id: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProcessedUpdate(Base):
    __tablename__ = "processed_updates"
    update_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserPrompt(Base):
    __tablename__ = "user_prompts"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_ref: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    last_error: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatSettings(Base):
    """Per-chat administration settings («Настройка чата»)."""

    __tablename__ = "chat_settings"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(128))
    welcome_text: Mapped[str | None] = mapped_column(Text)
    rules_text: Mapped[str | None] = mapped_column(Text)
    warning_limit: Mapped[int] = mapped_column(Integer, default=3)
    warning_ban_seconds: Mapped[int] = mapped_column(Integer, default=604800)
    warning_period_seconds: Mapped[int | None] = mapped_column(Integer)
    mute_default_seconds: Mapped[int] = mapped_column(Integer, default=604800)
    ban_default_seconds: Mapped[int | None] = mapped_column(Integer)
    links_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    denied_link_types: Mapped[list] = mapped_column(JSON, default=list)
    allowed_links: Mapped[list] = mapped_column(JSON, default=list)
    caps_percent: Mapped[int | None] = mapped_column(Integer)
    caps_min_length: Mapped[int] = mapped_column(Integer, default=5)
    sticker_limit: Mapped[int | None] = mapped_column(Integer)
    voice_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    guest_bots_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    profanity_filter: Mapped[bool] = mapped_column(Boolean, default=False)
    show_charts: Mapped[bool] = mapped_column(Boolean, default=True)
    show_mod_tags: Mapped[bool] = mapped_column(Boolean, default=False)
    # «-Команды»: whether the bot explains that a command needs a higher rank.
    notify_command_access: Mapped[bool] = mapped_column(Boolean, default=True)
    # «+Каналы» / «-Каналы»: messages sent on behalf of a channel.
    channels_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    # «+Входы» / «+Выходы»: the bot repeats the service messages Telegram hides in big chats.
    notify_joins: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_leaves: Mapped[bool] = mapped_column(Boolean, default=False)
    leave_notify_min_messages: Mapped[int] = mapped_column(Integer, default=0)
    # «+Минрег {дней}»: kick members whose first interaction with the bot is younger than this.
    minreg_days: Mapped[int | None] = mapped_column(Integer)
    # «+Чат»: permissions that were in place before the chat was closed, to restore them later.
    closed_permissions: Mapped[dict | None] = mapped_column(JSON)
    # «+Автокик {число} {время} {кик|бан}»: punish repeated exits.
    autokick_count: Mapped[int | None] = mapped_column(Integer)
    autokick_window_seconds: Mapped[int | None] = mapped_column(Integer)
    autokick_action: Mapped[str | None] = mapped_column(String(10))
    # «+Чат ссылка»: links the bot created for this chat, so that «сброс ссылок» can revoke them.
    invite_links: Mapped[list] = mapped_column(JSON, default=list)
    # «+Боты» / «-Боты»: whether bots may be invited into the chat.
    bots_denied: Mapped[bool] = mapped_column(Boolean, default=False)
    # «+Инлайны» / «-Инлайны»: whether the bot comments on inline button presses.
    inline_notices: Mapped[bool] = mapped_column(Boolean, default=False)
    # «+Автозаявки»: approve join requests automatically.
    auto_join_requests: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Trigger(Base):
    """Automatic reaction to an event («Триггеры и автоматические наказания»).

    ``actions`` is a list of ``{"command": ..., "duration": ..., "reason": ...}`` rows
    parsed from the moderator's message; nothing from user messages is ever stored.
    """

    __tablename__ = "triggers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    event: Mapped[str] = mapped_column(String(40), index=True)
    min_level: Mapped[int] = mapped_column(Integer, default=0)
    actions: Mapped[list] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("chat_id", "event", name="uq_trigger_chat_event"),)


class CommandAccess(Base):
    """Per-chat override of the minimum rank required for a command («Доступ команд»)."""

    __tablename__ = "command_access"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(String(40), primary_key=True)
    min_level: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class GridChat(Base):
    """A chat that belongs to a moderator grid («Настройка сетки чатов»)."""

    __tablename__ = "grid_chats"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grid_name: Mapped[str] = mapped_column(String(64), index=True)
    mod_level: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ChatMemberActivity(Base):
    """Join/leave tracking used by «Кик новичков», «Кик неактив» and «Кик удалённых»."""

    __tablename__ = "chat_members"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_member: Mapped[bool] = mapped_column(Boolean, default=True)
    # «+Тг тег текст {ссылка}»: a personal note shown next to the name in bot messages.
    tag: Mapped[str | None] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DailyMessageStat(Base):
    """Messages and attachments per chat per day, for «Чат стата {число дней}»."""

    __tablename__ = "daily_message_stats"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    attachment_count: Mapped[int] = mapped_column(Integer, default=0)


class CreatorWill(Base):
    """«Завещание»: the person who may take over the rank if the creator loses access."""

    __tablename__ = "creator_wills"
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    heir_telegram_id: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserProfile(Base):
    """«Анкета пользователя»: карточка участника внутри чата."""

    __tablename__ = "user_profiles"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # «+Ник», «+Звание»: в документации Ириса они свои для каждого чата.
    nickname: Mapped[str | None] = mapped_column(String(30))
    title: Mapped[str | None] = mapped_column(String(30))
    motto: Mapped[str | None] = mapped_column(String(100))
    about: Mapped[str | None] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(60))
    gender: Mapped[str | None] = mapped_column(String(10))
    birthday: Mapped[str | None] = mapped_column(String(10))
    birthday_visibility: Mapped[str | None] = mapped_column(String(10))
    citizenship: Mapped[bool] = mapped_column(Boolean, default=False)
    form_visible: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class UserCommandAccess(Base):
    """«Личный доступ команд» («+лдк»): an exception for one person in one chat."""

    __tablename__ = "user_command_access"
    chat_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    command: Mapped[str] = mapped_column(String(40), primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ChatLeave(Base):
    """Every exit is remembered so «+Автокик {число} {время}» can count them."""

    __tablename__ = "chat_leaves"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(Integer, index=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    left_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
