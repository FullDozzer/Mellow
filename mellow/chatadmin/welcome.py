"""Подстановка переменных в приветствие («{имя}», «{ж|м|мн}»)."""

from __future__ import annotations

import re

import html

VARIABLE_RE = re.compile(r"\{(имя|ж\|м\|мн)\}", re.IGNORECASE)


def render_welcome(template: str, *, full_name: str, plural: bool) -> str:
    """Подставляет имя участника и выбирает форму для пола или группы.

    Пол Telegram не сообщает, поэтому для одного человека берётся мужская форма:
    «{ж|м|мн}» → «м». Для нескольких участников используется форма «мн».
    """
    safe_name = html.escape(full_name)

    def replace(match: re.Match) -> str:
        return safe_name if match.group(1).lower() == "имя" else ("мн" if plural else "м")

    return VARIABLE_RE.sub(replace, template)
