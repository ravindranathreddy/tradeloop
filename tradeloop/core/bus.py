"""In-memory publish/subscribe between modules (design rule 6).

Synchronous and in-order, so a replay of the same events always gives the same
result. One failing subscriber is logged and skipped; it never stops the others.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable
from enum import Enum
from typing import Any

log = logging.getLogger("bus")


class Topic(str, Enum):
    TICK = "tick"
    ORDER_REQUEST = "order_request"     # engine -> risk layer
    ORDER_UPDATE = "order_update"       # broker -> router -> ledger, engine
    FILL = "fill"
    ALERT = "alert"                     # watchdog, risk rejections -> notifications


Handler = Callable[[Any], None]


class Bus:
    def __init__(self):
        self._subs: dict[Topic, list[Handler]] = defaultdict(list)
        self.errors = 0

    def subscribe(self, topic: Topic, handler: Handler) -> None:
        self._subs[topic].append(handler)

    def publish(self, topic: Topic, event: Any) -> None:
        for handler in list(self._subs[topic]):
            try:
                handler(event)
            except Exception:
                self.errors += 1
                log.exception("subscriber %s failed on %s", getattr(handler, "__qualname__", handler), topic.value)
