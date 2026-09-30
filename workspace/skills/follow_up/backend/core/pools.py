"""Follow Up 2.0 — два исполнителя: CPU с приоритетами и блокирующие вызовы LLM.

Процессор один. Эмбеддер (70 мс на запрос, 3.2 с на первом encode), кросс-энкодер
(13 пар/с), FAISS, BM25, гидратация — всё это конкурирует за него, и сегодня
конкурирует беспорядочно: `run_in_executor` без своего пула кладёт задачи в
общий, где их столько же, сколько потоков по умолчанию.

Отсюда два свойства этого модуля.

**POOL_CPU — один воркер с приоритетной очередью.** Один, потому что параллелить
CPU-работу не на чем: два реранка на одном ядре идут вдвое дольше каждый, а не
быстрее вместе. Приоритет решает, чья задача пойдёт следующей: вопрос аудитора
обгоняет фоновую гидратацию, а не встаёт за ней в хвост.

**Срезы и отмена.** Длинная CPU-операция режется на куски `cpu_slice_max_sec`, и
между кусками проверяется токен отмены. Без этого «Стоп» освобождает слот к
модели, а реранк 24 пар продолжает жечь единственное ядро ещё десяток секунд —
то есть отмена превращается в отказ обслуживания для следующего аудитора.

**POOL_LLM** — обычный пул потоков: вызовы к GigaChat блокирующие (клиент
синхронный, плюс sleep рейт-лимитера), и держать их на event loop нельзя.
Очередь и справедливость — не здесь, а в `llm_gateway`.
"""
from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional

logger = logging.getLogger(__name__)

# Меньше число — выше приоритет
PRIO_INTERACTIVE = 10     # вопрос аудитора, ждёт человек
PRIO_SKILL = 20           # блок карточки: человек ждёт, но карточка уже показана
PRIO_OFFLINE = 60         # разовые пересчёты по требованию админа
PRIO_HYDRATION = 90       # фон, уступает всем


class Cancelled(Exception):
    """Работа снята: аудитор нажал «Стоп» или ход вышел за дедлайн."""


@dataclass
class CancelToken:
    """Отмена, доходящая до воркера.

    `asyncio.Task.cancel()` не долетает до кода, который уже крутится в потоке:
    поток о нём не знает. Токен — это то, что можно проверить между срезами.
    """
    reason: str = ""
    _flag: threading.Event = field(default_factory=threading.Event)

    def cancel(self, reason: str = "остановлено пользователем") -> None:
        self.reason = reason
        self._flag.set()

    @property
    def cancelled(self) -> bool:
        return self._flag.is_set()

    def raise_if_cancelled(self) -> None:
        if self._flag.is_set():
            raise Cancelled(self.reason or "отменено")


_NEVER = CancelToken()


class PriorityWorker:
    """Один поток, задачи по приоритету. FIFO внутри приоритета."""

    def __init__(self, name: str = "cpu") -> None:
        self._name = name
        self._cv = threading.Condition()
        self._heap: list = []
        self._seq = itertools.count()
        self._stop = False
        self._busy_stage: Optional[str] = None
        self._thread = threading.Thread(target=self._loop, name=f"pool-{name}",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            with self._cv:
                while not self._heap and not self._stop:
                    self._cv.wait()
                if self._stop and not self._heap:
                    return
                _, _, stage, fn, loop, fut = heapq.heappop(self._heap)
                self._busy_stage = stage
            try:
                value, exc = fn(), None
            except BaseException as e:                     # noqa: BLE001
                value, exc = None, e
            finally:
                with self._cv:
                    self._busy_stage = None
            if fut is None:                # задача без ожидающего (прогрев)
                if exc is not None:
                    logger.warning(f"[pools] {stage}: {exc}")
                continue
            if exc is None:
                loop.call_soon_threadsafe(_set_result, fut, value)
            else:
                loop.call_soon_threadsafe(_set_exception, fut, exc)

    def submit(self, fn: Callable[[], Any], prio: int, stage: str,
               loop: Optional[asyncio.AbstractEventLoop] = None) -> Optional[asyncio.Future]:
        """loop=None — «поставить в очередь и забыть»: результат никому не нужен."""
        fut = loop.create_future() if loop is not None else None
        with self._cv:
            heapq.heappush(self._heap,
                           (prio, next(self._seq), stage, fn, loop, fut))
            self._cv.notify()
        return fut

    def depth(self) -> int:
        with self._cv:
            return len(self._heap)

    def busy_with(self) -> Optional[str]:
        with self._cv:
            return self._busy_stage

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()


def _set_result(fut: asyncio.Future, value: Any) -> None:
    if not fut.done():
        fut.set_result(value)


def _set_exception(fut: asyncio.Future, exc: BaseException) -> None:
    if not fut.done():
        fut.set_exception(exc)


POOL_CPU = PriorityWorker("cpu")
POOL_LLM = ThreadPoolExecutor(max_workers=4, thread_name_prefix="llm")
# Телеметрия — ОТДЕЛЬНЫЙ поток. В общем пуле запись в GP держала бы слот на всё
# время похода в базу: при недоступном Greenplum четырёх зависших задач хватает,
# чтобы вызовы модели перестали стартовать вовсе, и выглядело бы это как
# «модель не отвечает», хотя недоступна база.
POOL_TELEMETRY = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tlm")


def _slice_budget() -> float:
    try:
        from backend.config import get_settings
        return float(get_settings().cpu_slice_max_sec)
    except Exception:
        return 2.0


async def run_cpu(fn: Callable[..., Any], *args, prio: int = PRIO_INTERACTIVE,
                  cancel: Optional[CancelToken] = None,
                  stage: str = "cpu", **kwargs) -> Any:
    """CPU-работа в единственном воркере, с приоритетом и проверкой отмены.

    Отмена проверяется ДО запуска: пока задача ждала своей очереди, аудитор мог
    уже нажать «Стоп», и считать её незачем.
    """
    tok = cancel or _NEVER
    tok.raise_if_cancelled()
    loop = asyncio.get_running_loop()
    fut = POOL_CPU.submit(lambda: fn(*args, **kwargs), prio, stage, loop)
    try:
        return await fut
    except asyncio.CancelledError:
        tok.cancel("ход отменён")
        raise


async def run_cpu_sliced(items: Iterable[Any],
                         step: Callable[[list], Any],
                         *, slice_size: int,
                         prio: int = PRIO_INTERACTIVE,
                         cancel: Optional[CancelToken] = None,
                         stage: str = "cpu") -> list:
    """Длинная CPU-операция кусками, с проверкой отмены между кусками.

    `step` получает список элементов и возвращает список результатов. Размер
    среза подбирается вызывающим под свою стоимость элемента: для кросс-энкодера
    при 13 парах/с срез в 24 пары — это ~1.8 с, то есть в пределах
    `cpu_slice_max_sec`.
    """
    tok = cancel or _NEVER
    out: list = []
    batch: list = list(items)
    budget = _slice_budget()
    for i in range(0, len(batch), max(1, slice_size)):
        tok.raise_if_cancelled()
        chunk = batch[i:i + slice_size]
        t0 = time.monotonic()
        part = await run_cpu(step, chunk, prio=prio, cancel=tok,
                             stage=f"{stage}[{i}:{i + len(chunk)}]")
        dt = time.monotonic() - t0
        if dt > budget * 2:
            # Срез вдвое дороже бюджета — отмена будет доходить медленно.
            # Не падаем, но говорим об этом: калибровать slice_size придётся.
            logger.warning(
                f"[pools] Срез {stage} занял {dt:.1f} с при бюджете {budget:.1f} с "
                f"({len(chunk)} элементов) — отмена доходит за это же время")
        out.extend(part if isinstance(part, list) else [part])
    tok.raise_if_cancelled()
    return out


def warmup() -> None:
    """Прогрев тяжёлых моделей ВНУТРИ POOL_CPU.

    Прежний прогрев грузил веса в отдельном потоке и ни одного `encode` не
    делал, поэтому первый реальный запрос платил 3.2 с за первый прогон. Плюс
    он шёл мимо пула — то есть конкурировал с первым же вопросом аудитора за
    тот же процессор.
    """
    def _warm() -> str:
        done = []
        try:
            from backend.indexing.embedder import embed_texts
            embed_texts(["прогрев"], normalize=True)
            done.append("bge-m3")
        except Exception as e:
            logger.warning(f"[pools] Прогрев эмбеддера не удался: {e}")
        try:
            from backend.rag import reranker
            model = reranker._get_reranker()
            if model is not None:
                model.predict([("прогрев", "прогрев")], show_progress_bar=False)
                done.append("reranker")
        except Exception as e:
            logger.warning(f"[pools] Прогрев реранкера не удался: {e}")
        return ", ".join(done) or "ничего"

    def _run() -> None:
        t0 = time.monotonic()
        what = _warm()
        logger.info(f"[pools] Прогрет: {what} за {time.monotonic() - t0:.1f} с")

    # Прогрев — фоновая работа: он не должен обгонять первый вопрос аудитора.
    # Результата никто не ждёт, поэтому без loop и без future.
    POOL_CPU.submit(_run, PRIO_OFFLINE, "warmup")


def stats() -> dict:
    return {"cpu_queue": POOL_CPU.depth(), "cpu_busy_with": POOL_CPU.busy_with()}
