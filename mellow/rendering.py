"""Text rendering shared by user dialogs and the delivery worker.

Keeping these helpers in one module avoids a circular import between
``mellow.handlers`` (which renders previews for the author) and
``mellow.delivery`` (which renders the message that staff receives).
"""

from __future__ import annotations

import html

from mellow.config import Settings

APPLICATION_STATUS_LABELS = {
    "creating": "готовится к отправке",
    "pending": "на рассмотрении",
    "info_requested": "нужна дополнительная информация",
    "accepted": "принята",
    "rejected": "отклонена",
    "closed": "закрыта",
}

SERVICE_ITEM_LABELS = {"ticket": "Тикет", "suggestion": "Предложение"}


def display_user(username: str | None, tg_id: int) -> str:
    """Name a participant without ever leaking a nameless account's identity."""
    return f"@{html.escape(username)}" if username else f"<code>{tg_id}</code>"


def display_name(username: str | None, tg_id: int) -> str:
    """Plain-text participant label (escaped by the caller when rendered)."""
    return f"@{username}" if username else f"ID {tg_id}"


def render_application(data: dict, settings: Settings, *, heading: str = "Проверь анкету") -> str:
    lines = [f"<b>{html.escape(heading)}</b>"]
    for question in settings.questions:
        lines.append(f"\n<b>{html.escape(question.label)}</b>\n{html.escape(str(data.get(question.key, '—')))}")
    return "\n".join(lines)


def application_staff_text(application_id: int, data: dict, settings: Settings,
                           username: str | None, telegram_id: int) -> str:
    return (f"<b>Новая заявка</b>\nНомер: <code>#{application_id}</code>\n"
            f"Пользователь: {display_user(username, telegram_id)}\n\n"
            f"{render_application(data, settings)}")


def service_item_staff_text(kind: str, item_id: int, subject: str, body: str,
                            username: str | None, telegram_id: int) -> str:
    heading = SERVICE_ITEM_LABELS.get(kind, "Обращение")
    lines = [f"<b>{heading} #{item_id}</b>", f"Автор: {display_user(username, telegram_id)}"]
    if kind != "suggestion":
        lines.append(f"Тема: {html.escape(subject)}")
    lines.append("")
    lines.append(html.escape(body))
    return "\n".join(lines)


def application_topic_name(application_id: int, data: dict) -> str:
    return f"Заявка #{application_id} — {data.get('minecraft_username') or 'Участник'}"[:120]


def service_item_topic_name(subject: str, item_id: int) -> str:
    return f"{subject[:75]} #{item_id}"[:120]
