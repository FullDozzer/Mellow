from types import SimpleNamespace

from mellow.privacy import is_anonymous_or_group_authored


def test_sender_chat_is_excluded_without_reading_any_sender_metadata():
    class SenderChat:
        @property
        def id(self):
            raise AssertionError("sender_chat.id must never be inspected")

        @property
        def title(self):
            raise AssertionError("sender_chat.title must never be inspected")

    class GroupMessage:
        sender_chat = SenderChat()

        @property
        def from_user(self):
            raise AssertionError("do not inspect a synthetic from_user for group-authored posts")

    assert is_anonymous_or_group_authored(GroupMessage()) is True


def test_message_without_user_fails_closed():
    assert is_anonymous_or_group_authored(SimpleNamespace(sender_chat=None, from_user=None))


def test_normal_user_message_is_attributable():
    assert not is_anonymous_or_group_authored(SimpleNamespace(sender_chat=None, from_user=object()))
