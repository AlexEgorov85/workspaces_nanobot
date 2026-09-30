"""Follow Up 2.0 — бюджет хода как объект.

Потолок диалогового хода — три слота очереди, включая ретраи. Не потому, что
три красивое число, а потому что слот стоит девять секунд межвызовного
интервала: четвёртый вызов означает минуту ожидания, и аудитор к этому моменту
уже открыл соседнюю вкладку.

Бюджет — не счётчик для отчёта, а то, что отбирает работу. Когда денег нет,
переплан не выполняется, и это говорится вслух, а не молча пропускается.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List


@dataclass
class TurnBudget:
    max_llm_calls: int = 3
    deadline_sec: float = 150.0
    spent_calls: int = 0
    t0: float = field(default_factory=time.monotonic)
    log: List[str] = field(default_factory=list)

    def can_afford(self, n: int = 1) -> bool:
        return self.spent_calls + n <= self.max_llm_calls and self.left_sec() > 5

    def charge(self, n: int = 1, what: str = "") -> None:
        self.spent_calls += n
        if what:
            self.log.append(f"{what}: {n}")

    def left_sec(self) -> float:
        return max(0.0, self.deadline_sec - (time.monotonic() - self.t0))

    def spent_sec(self) -> float:
        return time.monotonic() - self.t0

    def refuse_reason(self) -> str:
        if self.spent_calls >= self.max_llm_calls:
            return (f"бюджет хода исчерпан ({self.spent_calls} из "
                    f"{self.max_llm_calls} вызовов)")
        return f"до конца хода {self.left_sec():.0f} с — не успеть"

    def report(self) -> dict:
        return {"calls": self.spent_calls, "max_calls": self.max_llm_calls,
                "elapsed_sec": round(self.spent_sec(), 1),
                "left_sec": round(self.left_sec(), 1), "log": list(self.log)}
