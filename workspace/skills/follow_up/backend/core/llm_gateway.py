"""Follow Up 2.0 — честная очередь к GigaChat.

Ключевой прод-факт, вокруг которого построен модуль: `cfg.gigachat_delay = 9.0`
это НЕ латентность вызова, а **межвызовный интервал на весь процесс**
(`llm/client.py`: глобальная отметка ставится ДО invoke, sleep блокирующий).
Отсюда потолок: 60 / 9 ≈ 6.67 вызовов LLM в минуту **на всех аудиторов сразу**.
Это самый дефицитный ресурс системы, и относиться к нему надо как к ресурсу:
с очередью, справедливостью и честным ожидаемым временем.

Что делает модуль:

- **один слот.** Второй вызов не «ждёт 9 секунд» — он ждёт своей очереди, и
  сколько именно, система знает и может сказать;
- **справедливость по `client_id`.** Round-robin между аудиторами: три хода
  одного не должны отодвинуть единственный ход другого;
- **`eta_sec` по дедлайнам, а не `depth × 9`.** Блок карточки держит слот
  десятки секунд, и «ещё 9 секунд» в такой момент — ложь;
- **admission control.** Если ожидание не влезает в остаток дедлайна хода,
  честнее отказать сразу с названной ценой, чем занять слот и не успеть;
- **типизированный исход вместо исключения.** `Outcome.kind` различает
  `ok / transient_fail / rate_limited / auth_fail / timeout / cancelled /
  rejected`, потому что «503 на третьей попытке» и «неверный токен» требуют
  разного поведения, а `except Exception` делает их одинаковыми.

Подставляется ВНУТРЬ `llm.client.generate_async`, поэтому карточка и гипотезы
попадают в очередь без единой правки в скиллах. Контекст (профиль, ключ
справедливости, токен отмены, колбэк ожидания) передаётся через `contextvars`.
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Профили вызова
# ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Profile:
    name: str
    max_tokens: int
    temperature: float
    timeout_sec: float
    prio: int          # меньше — раньше


PROFILES: Dict[str, Profile] = {
    # Понимание запроса: короткий выход, ждёт человек
    "interpret": Profile("interpret", 800, 0.0, 60.0, 10),
    # Финальный текст ответа
    "frame": Profile("frame", 1200, 0.2, 90.0, 15),
    # Блоки карточки и гипотезы: длинный выход, но карточка уже на экране
    "skill": Profile("skill", 3500, 0.1, 180.0, 20),
    # Разовые офлайн-задачи (извлечение отклонений при индексации)
    "offline": Profile("offline", 2000, 0.0, 180.0, 60),
}
DEFAULT_PROFILE = "skill"


def profile(name: Optional[str]) -> Profile:
    return PROFILES.get(name or DEFAULT_PROFILE, PROFILES[DEFAULT_PROFILE])


# ──────────────────────────────────────────────────────────────────
# Контекст вызова (передаётся через contextvars, чтобы не править скиллы)
# ──────────────────────────────────────────────────────────────────

@dataclass
class CallContext:
    fairness_key: str = "anon"
    profile: str = DEFAULT_PROFILE
    deadline_at: Optional[float] = None     # monotonic
    cancel: object = None                   # pools.CancelToken
    on_wait: Optional[Callable[[float, int], Awaitable[None]]] = None
    stage: str = ""


_ctx: contextvars.ContextVar[Optional[CallContext]] = contextvars.ContextVar(
    "fu_llm_ctx", default=None)


def set_context(ctx: Optional[CallContext]):
    return _ctx.set(ctx)


def reset_context(token) -> None:
    try:
        _ctx.reset(token)
    except Exception:
        pass


def current_context() -> CallContext:
    return _ctx.get() or CallContext()


# ──────────────────────────────────────────────────────────────────
# Исход вызова
# ──────────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    kind: str                 # ok | transient_fail | rate_limited | auth_fail
                              # | timeout | cancelled | rejected
    text: Optional[str] = None
    error: Optional[str] = None
    waited_sec: float = 0.0
    ran_sec: float = 0.0
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def human(self) -> str:
        return {
            "transient_fail": "Модель временно недоступна — отвечаю по тому, "
                              "что уже собрано.",
            "rate_limited": "Очередь к модели переполнена — отвечаю без неё.",
            "auth_fail": "Не принят токен доступа к модели. Нужен новый.",
            "timeout": "Модель не ответила за отведённое время.",
            "cancelled": "Остановлено.",
            "rejected": "Ожидание в очереди не укладывается в лимит ответа.",
        }.get(self.kind, self.error or "Ошибка вызова модели")


_AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "token",
                 "authentication", "api_key")
_RATE_MARKERS = ("429", "rate limit", "too many requests")


def classify_error(exc: BaseException) -> str:
    msg = str(exc).lower()
    if any(m in msg for m in _AUTH_MARKERS):
        return "auth_fail"
    if any(m in msg for m in _RATE_MARKERS):
        return "rate_limited"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "timeout"
    return "transient_fail"


# ──────────────────────────────────────────────────────────────────
# Очередь
# ──────────────────────────────────────────────────────────────────

@dataclass
class _Waiter:
    id: str
    key: str
    prio: int
    enqueued_at: float
    est_sec: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False


class _Gate:
    """Один слот, round-robin по ключам справедливости.

    Без `asyncio.Lock`/`Condition` намеренно: они привязываются к первому event
    loop, который их коснулся, и любой второй цикл (тест, перезапуск сервера,
    отдельный скрипт) получает «bound to a different event loop». Состояние
    держит обычный `threading.Lock`, ожидание — короткий `asyncio.sleep`.
    Точность опроса в 50 мс на фоне девятисекундного интервала роли не играет.
    """

    _POLL_SEC = 0.05

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: List[_Waiter] = []
        self._busy: Optional[_Waiter] = None
        self._busy_until: float = 0.0
        self._last_keys: List[str] = []

    def _interval(self) -> float:
        """Межвызовный интервал очереди — по активному провайдеру.

        Раньше читался `gigachat_delay` безусловно, при любом режиме. Это
        верно ровно для GigaChat: там лимит провайдера на пользователя, и
        девять секунд — он. У локальной модели (vLLM на своих картах, как у
        нанобота) лимита нет — очередь держит сам сервер. Безусловные девять
        секунд дали бы 27 секунд ожидания на ход при потолке в три вызова,
        причём выглядело бы это как «медленный поиск», а не как настройка.
        """
        try:
            from backend.config import get_settings
            cfg = get_settings()
            explicit = getattr(cfg, "llm_min_interval_sec", None)
            if explicit is not None:
                return max(0.0, float(explicit))
            return (float(cfg.gigachat_delay)
                    if cfg.llm_mode == "gigachat" else 0.0)
        except Exception:
            return 9.0

    def depth(self) -> int:
        with self._lock:
            return len(self._queue) + (1 if self._busy else 0)

    def eta_sec(self, est_own: float = 0.0) -> float:
        """Сколько ждать НОВОМУ вызову.

        По дедлайнам занятого слота и оценкам стоящих в очереди, а не
        `depth × 9`: блок карточки держит слот десятками секунд, и «ещё 9» в
        такой момент — ложь, ради которой и вводился admission control.
        """
        now = time.monotonic()
        with self._lock:
            ahead = max(0.0, self._busy_until - now) if self._busy else 0.0
            for w in self._queue:
                ahead += max(w.est_sec, self._interval())
        return round(ahead + est_own, 1)

    def _try_grant_locked(self, w: _Waiter) -> bool:
        """Взять слот, если сейчас очередь этого ключа. Зовётся под локом."""
        if self._busy is not None or not self._queue:
            return False
        keys: List[str] = []
        for x in self._queue:
            if x.key not in keys:
                keys.append(x.key)
        fresh = [k for k in keys if k not in self._last_keys]
        if fresh:
            key_now = fresh[0]
        else:
            order = {k: i for i, k in enumerate(self._last_keys)}
            key_now = max(keys, key=lambda k: order.get(k, len(self._last_keys)))
        cand = next((x for x in self._queue if x.key == key_now), None)
        if cand is not w:
            return False
        self._queue.remove(w)
        self._busy = w
        self._busy_until = time.monotonic() + max(w.est_sec, self._interval())
        self._last_keys = ([w.key] +
                           [k for k in self._last_keys if k != w.key])[:8]
        w.granted = True
        return True

    async def acquire(self, key: str, prio: int, est_sec: float,
                      deadline_at: Optional[float],
                      on_wait: Optional[Callable[[float, int], Awaitable[None]]],
                      cancel) -> Optional[Outcome]:
        """Занять слот. None — заняли; Outcome — отказ (rejected/cancelled)."""
        w = _Waiter(uuid.uuid4().hex[:8], key, prio, time.monotonic(), est_sec)
        if deadline_at is not None:
            eta = self.eta_sec(est_sec)
            left = deadline_at - time.monotonic()
            if eta > left:
                # Admission control: занять слот и не успеть — хуже, чем сразу
                # назвать цену. Аудитор получит ответ по уже собранному.
                return Outcome("rejected",
                               error=f"ожидание {eta:.0f} с не влезает "
                                     f"в остаток {max(0, left):.0f} с")
        with self._lock:
            self._queue.append(w)
            self._queue.sort(key=lambda x: (x.prio, x.enqueued_at))

        t0 = time.monotonic()
        last_notice = 0.0
        while True:
            if cancel is not None and getattr(cancel, "cancelled", False):
                with self._lock:
                    if w in self._queue:
                        self._queue.remove(w)
                return Outcome("cancelled", waited_sec=time.monotonic() - t0)
            with self._lock:
                if self._try_grant_locked(w):
                    return None
            await asyncio.sleep(self._POLL_SEC)
            waited = time.monotonic() - t0
            if on_wait is not None and waited - last_notice >= 2.0:
                last_notice = waited
                try:
                    await on_wait(self.eta_sec(), self.depth())
                except Exception:
                    pass

    async def release(self) -> None:
        with self._lock:
            self._busy = None
            self._busy_until = 0.0

    def busy(self) -> bool:
        with self._lock:
            return self._busy is not None


_gate = _Gate()


def queue_depth() -> int:
    return _gate.depth()


def eta_sec() -> float:
    return _gate.eta_sec()


def stats() -> dict:
    return {"depth": _gate.depth(), "eta_sec": _gate.eta_sec(),
            "busy": _gate.busy()}


# ──────────────────────────────────────────────────────────────────
# Публичный вызов
# ──────────────────────────────────────────────────────────────────

_EST_BY_PROFILE = {"interpret": 12.0, "frame": 15.0, "skill": 20.0,
                   "offline": 25.0}


def _record(profile_name: str, outcome: "Outcome") -> None:
    """Исход вызова — в общий журнал GP, мимо критического пути.

    Инструмент запускается у каждого аудитора отдельно, рейт-лимитер живёт в
    процессе, и ни один процесс не видит остальных. Общая таблица — способ
    узнать фактическую частоту по всем сразу, а не гадать про квоту API.
    """
    def _do() -> None:
        try:
            import os
            import platform
            from backend.storage import gp
            if gp.gp_enabled():
                gp.LlmCallRepo.add(profile_name, outcome.kind,
                                   outcome.waited_sec, outcome.ran_sec,
                                   host=platform.node(), pid=os.getpid())
        except Exception:
            pass

    try:
        from backend.core.pools import POOL_TELEMETRY
        POOL_TELEMETRY.submit(_do)
    except Exception:
        pass


async def call(fn: Callable[[], str], *, ctx: Optional[CallContext] = None) -> Outcome:
    """Выполнить блокирующий вызов модели через очередь.

    `fn` — синхронная функция без аргументов, которая делает сам вызов (её
    отдаёт `llm.client`). Здесь только очередь, таймаут, исход и метрики.
    """
    from backend.core.pools import POOL_LLM

    ctx = ctx or current_context()
    prof = profile(ctx.profile)
    est = _EST_BY_PROFILE.get(prof.name, 20.0)

    t_wait = time.monotonic()
    rejected = await _gate.acquire(ctx.fairness_key, prof.prio, est,
                                   ctx.deadline_at, ctx.on_wait, ctx.cancel)
    waited = time.monotonic() - t_wait
    if rejected is not None:
        rejected.waited_sec = round(waited, 1)
        logger.info(f"[gate] {ctx.stage or prof.name}: {rejected.kind} "
                    f"({rejected.error})")
        _record(prof.name, rejected)
        return rejected

    t_run = time.monotonic()
    outcome: Optional[Outcome] = None
    try:
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(POOL_LLM, fn)
        text = await asyncio.wait_for(fut, timeout=prof.timeout_sec)
        outcome = Outcome("ok", text=text, waited_sec=round(waited, 1),
                          ran_sec=round(time.monotonic() - t_run, 1), attempts=1)
        return outcome
    except asyncio.CancelledError:
        outcome = Outcome("cancelled", waited_sec=round(waited, 1),
                          ran_sec=round(time.monotonic() - t_run, 1))
        return outcome
    except asyncio.TimeoutError:
        # Таймаут ПРОФИЛЯ, а не клиента: llm_request_timeout=180 плюс ретраи
        # давали до девяти минут на один зависший вызов
        logger.warning(f"[gate] {ctx.stage or prof.name}: таймаут "
                       f"{prof.timeout_sec:.0f} с")
        outcome = Outcome("timeout", waited_sec=round(waited, 1),
                          ran_sec=round(time.monotonic() - t_run, 1),
                          error=f"нет ответа за {prof.timeout_sec:.0f} с")
        return outcome
    except BaseException as e:                            # noqa: BLE001
        kind = classify_error(e)
        logger.warning(f"[gate] {ctx.stage or prof.name}: {kind}: {e}")
        outcome = Outcome(kind, waited_sec=round(waited, 1),
                          ran_sec=round(time.monotonic() - t_run, 1), error=str(e))
        return outcome
    finally:
        await _gate.release()
        if outcome is not None:
            _record(prof.name, outcome)
