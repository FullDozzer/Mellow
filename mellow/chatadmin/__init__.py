"""Chat administration layer: settings, triggers, cleanup, grid and chat statistics.

Everything here is optional and per chat: a chat that never runs an administration
command behaves exactly as before. Message text is inspected only to answer "does this
message violate the filters" and is never stored.
"""

from __future__ import annotations

from mellow.chatadmin.config import (COMMANDS, ChatConfig, ChatSettingsStore, chat_config, command_min_level,
                                     may_use, set_command_access)
from mellow.chatadmin.guard import ChatGuard, RecentMessages
from mellow.chatadmin.triggers import (DEFAULT_ACTIONS, EVENTS, parse_actions, render_trigger, trigger_actions)

__all__ = [
    "COMMANDS", "ChatConfig", "ChatSettingsStore", "chat_config", "command_min_level", "may_use",
    "set_command_access", "ChatGuard", "RecentMessages", "DEFAULT_ACTIONS", "EVENTS", "parse_actions",
    "render_trigger", "trigger_actions",
]
