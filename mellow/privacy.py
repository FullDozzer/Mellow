"""Privacy boundary for Telegram group events.

Never inspect sender_chat.id/title/username, never infer a human behind it, and
never turn an anonymous/group-authored post into a user record or message stat.
"""


def is_anonymous_or_group_authored(message) -> bool:
    # Fail closed: a message without a normal Telegram user is not attributable.
    # Do not access or log any sender_chat properties.
    return getattr(message, "sender_chat", None) is not None or getattr(message, "from_user", None) is None
