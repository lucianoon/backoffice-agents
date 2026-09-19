"""Limitador de requisições por minuto, compartilhado entre threads.

O Jev publica 1.200 requisições por minuto por conta; com vários workers e concorrência dentro
de cada um, o limitador evita rajadas de 429 e o backoff que elas provocam.
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic, sleep=time.sleep) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._stamps: deque[float] = deque()

    def acquire(self) -> float:
        """Bloqueia até haver vaga na janela de 60 s. Devolve quanto esperou, em segundos."""
        if self.per_minute <= 0:
            return 0.0
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                if len(self._stamps) < self.per_minute:
                    self._stamps.append(now)
                    return waited
                delay = 60.0 - (now - self._stamps[0])
            self._sleep(delay)
            waited += delay
