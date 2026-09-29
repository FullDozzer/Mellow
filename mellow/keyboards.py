from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton


APPLICATION_BUTTON = "🎮 Подать заявку"
MY_APPLICATION_BUTTON = "📋 Моя заявка"
SUPPORT_BUTTON = "🛠 Техническая поддержка"
SUGGESTION_BUTTON = "💡 Предложить идею"
ADMINISTRATION_BUTTON = "👤 Обратиться к администрации"
TICKETS_BUTTON = "💬 Мои обращения"
INFO_BUTTON = "ℹ️ Информация"
STATISTICS_BUTTON = "📊 Статистика"
RIGHTS_BUTTON = "🛡 Мои права"

# Every button caption a user can send as plain text, with and without the emoji.
# A draft must not swallow these messages as answers to a form question.
MENU_TEXTS = frozenset({
    APPLICATION_BUTTON, MY_APPLICATION_BUTTON, SUPPORT_BUTTON, SUGGESTION_BUTTON,
    ADMINISTRATION_BUTTON, TICKETS_BUTTON, INFO_BUTTON, STATISTICS_BUTTON, RIGHTS_BUTTON,
    "Подать заявку", "Моя заявка", "Техническая поддержка", "Предложить идею",
    "Обратиться к администрации", "Мои обращения", "Информация", "Статистика", "Мои права",
})


def main_menu(is_staff: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=APPLICATION_BUTTON), KeyboardButton(text=MY_APPLICATION_BUTTON)],
        [KeyboardButton(text=SUPPORT_BUTTON), KeyboardButton(text=SUGGESTION_BUTTON)],
        [KeyboardButton(text=ADMINISTRATION_BUTTON), KeyboardButton(text=TICKETS_BUTTON)],
        [KeyboardButton(text=INFO_BUTTON), KeyboardButton(text=STATISTICS_BUTTON)],
    ]
    if is_staff:
        rows.append([KeyboardButton(text=RIGHTS_BUTTON)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выбери раздел Mellow")


def application_review(application_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Принять", callback_data=f"app:accept:{application_id}"),
         InlineKeyboardButton(text="Отклонить", callback_data=f"app:reject:{application_id}")],
        [InlineKeyboardButton(text="Запросить информацию", callback_data=f"app:info:{application_id}"),
         InlineKeyboardButton(text="Закрыть заявку", callback_data=f"app:close:{application_id}")],
    ])


def application_submit(count: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отправить анкету", callback_data="app:submit")],
        [InlineKeyboardButton(text="Изменить поле", callback_data="app:editmenu")],
        [InlineKeyboardButton(text="Отменить", callback_data="app:cancel")],
    ])


def application_edit_fields(labels: list[str]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label[:50], callback_data=f"app:edit:{i}")] for i, label in enumerate(labels)]
    rows.append([InlineKeyboardButton(text="Назад", callback_data="app:review")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def threshold_actions(telegram_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить в whitelist", callback_data=f"threshold:add:{telegram_id}")],
        [InlineKeyboardButton(text="Не добавлять", callback_data=f"threshold:no:{telegram_id}"),
         InlineKeyboardButton(text="Позже", callback_data=f"threshold:later:{telegram_id}")],
    ])


def ticket_actions(item_type: str, item_id: int, status: str = "open") -> InlineKeyboardMarkup | None:
    if item_type == "suggestion":
        if status not in {"new", "review"}:
            return None
        rows = []
        if status == "new":
            rows.append([InlineKeyboardButton(text="Рассматривается", callback_data=f"suggestion:review:{item_id}")])
        rows.append([InlineKeyboardButton(text="Принять", callback_data=f"suggestion:accept:{item_id}"),
                     InlineKeyboardButton(text="Отклонить", callback_data=f"suggestion:reject:{item_id}")])
        if status == "review":
            rows.append([InlineKeyboardButton(text="Реализовано", callback_data=f"suggestion:implemented:{item_id}")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    if status not in {"open", "review"}:
        return None
    rows = []
    if status == "open":
        rows.append([InlineKeyboardButton(text="Взять в работу", callback_data=f"ticket:review:{item_id}")])
    rows.append([InlineKeyboardButton(text="Закрыть обращение", callback_data=f"ticket:close:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
