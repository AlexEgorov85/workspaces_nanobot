"""Долгие операции навыка: запустить и опрашивать.

Карточка контроля исполнения строится минутами. У внешнего агента на это
один воркер (`max_concurrent = 1` в канале nanobot): пока он ждёт нас,
очередь всего подразделения стоит. Поэтому долгая работа возвращает
управление сразу, а результат забирается отдельным вызовом.

Реестр держится в памяти процесса намеренно. Задача живёт минуты и не
переживает перезапуск осмысленно: после рестарта агент всё равно не помнит,
чего ждал, а недостроенная карточка бесполезна — её надо строить заново, а
не восстанавливать.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Сколько держать завершённую задачу, чтобы агент успел забрать результат.
# Меньше — агент опоздает с опросом и увидит «нет такой задачи»; больше —
# память процесса копит то, что уже никому не нужно.
_TTL_DONE_SEC = 900.0
# Потолок одновременных задач: каждая держит слот к модели и процессор.
_MAX_RUNNING = 4
# Сколько вызов `wait` держит ответ, пока задача не готова.
#
# Это не украшение, а единственный способ задать темп опроса. У агента
# нанобота между вызовами инструментов нет паузы: получив `done: false`, он
# зовёт снова немедленно. Карточка строится минутами — значит агент сжёг бы
# `maxToolIterations` (200 у коллег) за секунды, а каждая итерация это ещё и
# вызов модели из общей квоты, то есть главный риск ёмкости из раздела 6.3
# плана. Ждём мы, а не он: три минуты карточки превращаются в девять
# вызовов вместо нескольких сотен.
#
# 20 секунд — с большим запасом до таймаута инструментов агента (240 с).
_POLL_WINDOW_SEC = 20.0
# Шаг внутреннего опроса. Не событие: `asyncio.Event` пришлось бы создавать
# в том же цикле, где его ждут, а тут два входа — MCP-сервер и тесты.
_POLL_STEP_SEC = 0.25


@dataclass
class Job:
    id: str
    label: str
    stage: str = "принято"
    done: bool = False
    ok: bool = False
    result: Optional[Dict] = None
    error: str = ""
    started_at: float = field(default_factory=time.monotonic)
    finished_at: Optional[float] = None
    _task: Optional[asyncio.Task] = None

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "job_id": self.id,
            "label": self.label,
            "stage": self.stage,
            "done": self.done,
            "elapsed_sec": round((self.finished_at or time.monotonic())
                                 - self.started_at, 1),
        }
        if self.done:
            out["ok"] = self.ok
            if self.ok:
                out["result"] = self.result
            else:
                out["error"] = self.error
        return out


_JOBS: Dict[str, Job] = {}
_LOCK = asyncio.Lock()


def _reap() -> None:
    """Убрать завершённые задачи, которые уже никто не заберёт."""
    now = time.monotonic()
    for jid in [j.id for j in _JOBS.values()
                if j.done and j.finished_at is not None
                and now - j.finished_at > _TTL_DONE_SEC]:
        _JOBS.pop(jid, None)


def running_count() -> int:
    return sum(1 for j in _JOBS.values() if not j.done)


async def start(
    work: Callable[[Callable[[str], None]], Awaitable[Dict]],
    label: str,
) -> Dict[str, Any]:
    """Запустить долгую работу и сразу вернуть её идентификатор.

    `work` получает функцию `progress(stage)` — ею задача рассказывает, где
    она сейчас. Агент показывает это аудитору вместо тишины на пять минут.
    """
    async with _LOCK:
        _reap()
        if running_count() >= _MAX_RUNNING:
            return {"ok": False,
                    "reason": f"Уже выполняется {running_count()} длинных "
                              f"задач — дождитесь завершения"}
        job = Job(id=uuid.uuid4().hex[:12], label=label)
        _JOBS[job.id] = job

    def progress(stage: str) -> None:
        job.stage = stage

    async def _run() -> None:
        try:
            job.result = await work(progress)
            job.ok = True
            job.stage = "готово"
        except asyncio.CancelledError:
            job.error = "задача отменена"
            job.stage = "отменено"
            raise
        except Exception as e:                              # noqa: BLE001
            logger.exception(f"[skill] Задача {job.label} упала")
            job.error = f"{type(e).__name__}: {e}"
            job.stage = "ошибка"
        finally:
            job.done = True
            job.finished_at = time.monotonic()

    job._task = asyncio.ensure_future(_run())
    return {"ok": True, "job_id": job.id, "stage": job.stage,
            "hint": "зовите card_status с этим job_id; вызов сам подождёт "
                    "готовности до 20 секунд и вернёт стадию — повторяйте, "
                    "пока не придёт done: true"}


def status(job_id: str) -> Dict[str, Any]:
    _reap()
    job = _JOBS.get(job_id)
    if job is None:
        return {"ok": False,
                "reason": f"Задача {job_id} не найдена: либо неверный "
                          f"идентификатор, либо результат уже забрали"}
    return {"ok": True, **job.as_dict()}


async def wait(job_id: str,
               timeout_sec: Optional[float] = None) -> Dict[str, Any]:
    """Статус задачи, но с ожиданием готовности.

    Готовая задача отвечает сразу. Неготовая — держит вызов до
    `timeout_sec` (по умолчанию `_POLL_WINDOW_SEC`) и отвечает текущей
    стадией. Смысл — в темпе: пауза между опросами есть у нас и нет у
    вызывающего.
    """
    window = _POLL_WINDOW_SEC if timeout_sec is None else max(0.0, timeout_sec)
    deadline = time.monotonic() + window
    while True:
        st = status(job_id)
        # Нет такой задачи или уже готова — ждать нечего
        if not st.get("ok") or st.get("done"):
            return st
        if time.monotonic() >= deadline:
            return st
        await asyncio.sleep(min(_POLL_STEP_SEC, max(0.0, deadline - time.monotonic())))


def cancel(job_id: str) -> Dict[str, Any]:
    job = _JOBS.get(job_id)
    if job is None or job.done:
        return {"ok": False, "reason": "задача не найдена или уже завершена"}
    if job._task is not None:
        job._task.cancel()
    return {"ok": True, "job_id": job_id, "stage": "отменяется"}


def reset() -> None:
    """Только для тестов: очистить реестр."""
    _JOBS.clear()
