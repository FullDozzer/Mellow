"""The «-маты» filter has to catch disguised words without eating ordinary speech."""

from __future__ import annotations

import pytest

from mellow.chatadmin.guard import contains_profanity, normalize_text

DISGUISED = [
    "ты бл*ть совсем",
    "бл**ь",
    "бля-ть",
    "б л я т ь",
    "БЛЯТЬ!",
    "ты бляяять",
    "с*ка, ты че",
    "х*й тебе",
    "п-и-з-д-е-ц",
    "ты долбоёб",
    "ёбанный рот",
    "заебал уже",
    "да пошёл ты нах*й",
    "fuck you",
    "what a bitch",
    "ты debil",
]

INNOCENT = [
    "привет, как дела",
    "купил хлеб и молоко",
    "сегодня отличная погода",
    "объявление: собрание в 18:00",
    "яблоки по 120 рублей за килограмм",
    "он требует ответа до пятницы",
    "требовал объяснений на собрании",
    "написал статью про блогеров",
    "сборка проекта прошла без ошибок",
    "конкурс талантов в субботу",
    "котлета по-киевски и компот",
    "заблокировали доступ к сайту",
    "уберите спам из чата, пожалуйста",
    "булка с маком и чай",
    "стрелка компаса показывает на север",
    "выборы в студсовет пройдут в мае",
    "победа в турнире по шахматам",
    "министерство культуры объявило программу",
    "суккулент на окне расцвёл",
    "статистика по каналу за неделю выросла",
]


@pytest.mark.parametrize("text", DISGUISED)
def test_disguised_profanity_is_caught(text: str) -> None:
    assert contains_profanity(text), text


@pytest.mark.parametrize("text", INNOCENT)
def test_ordinary_messages_are_not_touched(text: str) -> None:
    assert not contains_profanity(text), text


def test_normalize_removes_separators_and_leet() -> None:
    assert normalize_text("Б л я ть!") in {"блять"}
    assert normalize_text("о6ляд") == "обляд"
