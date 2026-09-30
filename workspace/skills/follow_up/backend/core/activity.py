"""Follow Up 2.0 — единый счётчик активности аудитора.

До этого модуля активность знала только карточка (`_last_card_activity` в
`execution_control.py`): фоновая гидратация уступала CPU при сборке карточки и
не уступала при обычном вопросе — а вопрос идёт по тому же одному процессору.

Клиенты: карточка, любой диалоговый ход, гидратация, бэкофилл, синк витрины.
Первые два зовут `touch`, остальные читают `idle_sec` и `open_turns` и решают,
уступить ли.
"""
from __future__ import annotations

import threading
import time
from collections import Counter
from typing import Dict

# RLock, а не Lock: stats() зовёт idle_sec(), который берёт тот же
# лок — с обычным Lock это гарантированный дедлок потока-обработчика.
_lock = threading.RLock()
_last_touch: float = 0.0
_by_kind: Counter = Counter()
_open: int = 0


def touch(kind: str = "turn") -> None:
    """Аудитор что-то делает прямо сейчас."""
    global _last_touch
    with _lock:
        _last_touch = time.monotonic()
        _by_kind[kind] += 1


def idle_sec() -> float:
    """Сколько секунд назад аудитор трогал систему. Без активности — большое число."""
    with _lock:
        if not _last_touch:
            return 1e9
        return time.monotonic() - _last_touch


def open_turns() -> int:
    with _lock:
        return _open


class turn:
    """Контекст открытого хода: `with activity.turn('card'): ...`

    Фоновые писатели смотрят не только на `idle_sec`, но и на число открытых
    ходов: пауза в 9 с между вызовами LLM — это не «аудитор ушёл», это ход,
    который ещё идёт.
    """

    def __init__(self, kind: str = "turn") -> None:
        self.kind = kind

    def __enter__(self) -> "turn":
        global _open
        touch(self.kind)
        with _lock:
            _open += 1
        return self

    def __exit__(self, *exc) -> None:
        global _open
        with _lock:
            _open = max(0, _open - 1)
        touch(self.kind)
        return None


def user_is_busy(idle_threshold_sec: float) -> bool:
    """Уступать ли CPU. True, пока идёт ход или аудитор трогал систему недавно."""
    return open_turns() > 0 or idle_sec() < idle_threshold_sec


def stats() -> Dict:
    with _lock:
        return {"idle_sec": round(idle_sec(), 1), "open_turns": _open,
                "by_kind": dict(_by_kind)}
