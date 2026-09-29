"""Shared test helpers."""

from __future__ import annotations


def attach_routers(dispatcher, *routers) -> None:
    """Include the bot's module-level routers into a fresh dispatcher.

    Aiogram allows a router to be attached to exactly one dispatcher. The production code
    builds its dispatcher once in ``mellow.main``; tests build one per test, so the routers
    are detached from the previous dispatcher first. This is test glue only.
    """
    for router in routers:
        parent = getattr(router, "_parent_router", None)
        if parent is not None:
            try:
                parent.sub_routers.remove(router)
            except ValueError:
                pass
            router._parent_router = None
        dispatcher.include_router(router)
