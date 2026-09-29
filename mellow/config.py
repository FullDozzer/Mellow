from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any
import yaml
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Question:
    key: str
    label: str
    max_length: int = 1000


@dataclass(frozen=True)
class Level:
    name: str
    permissions: frozenset[str]
    max_punishment_seconds: int


@dataclass
class Settings:
    bot_token: str
    database_url: str
    applications_chat_id: int
    support_chat_id: int | None
    suggestions_chat_id: int | None
    administration_chat_id: int
    message_threshold: int = 100
    message_requirement_enabled: bool = True
    staff: dict[int, int] = field(default_factory=dict)
    levels: dict[int, Level] = field(default_factory=dict)
    questions: list[Question] = field(default_factory=list)
    minecraft_api_url: str | None = None
    minecraft_api_token: str | None = None
    minecraft_api_timeout: float = 8.0
    stats_top_limit: int = 20
    log_level: str = "INFO"

    @property
    def support_destination(self) -> int:
        return self.support_chat_id or self.administration_chat_id

    @property
    def suggestions_destination(self) -> int:
        return self.suggestions_chat_id or self.administration_chat_id


def _load_yaml(path: str, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
        return value or fallback
    except FileNotFoundError:
        return fallback


def load_settings() -> Settings:
    required = ["BOT_TOKEN", "APPLICATIONS_CHAT_ID", "ADMIN_CHAT_ID"]
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
    raw_staff: dict[int, int] = {}
    for entry in os.getenv("STAFF", "").split(","):
        if entry.strip():
            user_id, level = entry.strip().split(":", 1)
            raw_staff[int(user_id)] = int(level)

    levels_data = _load_yaml("config/levels.yml", {"levels": {5: {"name": "Владелец", "permissions": ["*"], "max_punishment_seconds": 0}}})["levels"]
    levels: dict[int, Level] = {}
    for raw_level, item in levels_data.items():
        level_num = int(raw_level)
        if not 1 <= level_num <= 5:
            raise RuntimeError("Staff levels must be between 1 and 5")
        levels[level_num] = Level(str(item["name"]), frozenset(item.get("permissions", [])), int(item.get("max_punishment_seconds", 0)))
    for level in raw_staff.values():
        if level not in levels:
            raise RuntimeError(f"STAFF refers to missing level {level} in config/levels.yml")

    questions_data = _load_yaml(os.getenv("FORM_QUESTIONS_FILE", "config/questions.yml"), {"questions": []}).get("questions", [])
    questions = [Question(str(item["key"]), str(item["label"]), int(item.get("max_length", 1000))) for item in questions_data]
    if not questions:
        raise RuntimeError("At least one application question must be configured")
    if len({q.key for q in questions}) != len(questions):
        raise RuntimeError("Application question keys must be unique")

    return Settings(
        bot_token=os.environ["BOT_TOKEN"],
        database_url=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./mellow.db"),
        applications_chat_id=int(os.environ["APPLICATIONS_CHAT_ID"]),
        support_chat_id=int(os.environ["SUPPORT_CHAT_ID"]) if os.getenv("SUPPORT_CHAT_ID") else None,
        suggestions_chat_id=int(os.environ["SUGGESTIONS_CHAT_ID"]) if os.getenv("SUGGESTIONS_CHAT_ID") else None,
        administration_chat_id=int(os.environ["ADMIN_CHAT_ID"]),
        message_threshold=max(1, int(os.getenv("MESSAGE_THRESHOLD", "100"))),
        message_requirement_enabled=os.getenv("MESSAGE_REQUIREMENT_ENABLED", "true").lower() in {"1", "true", "yes", "on"},
        staff=raw_staff,
        levels=levels,
        questions=questions,
        minecraft_api_url=os.getenv("MINECRAFT_API_URL"),
        minecraft_api_token=os.getenv("MINECRAFT_API_TOKEN"),
        minecraft_api_timeout=float(os.getenv("MINECRAFT_API_TIMEOUT", "8")),
        stats_top_limit=max(1, int(os.getenv("STATS_TOP_LIMIT", "20"))),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )
