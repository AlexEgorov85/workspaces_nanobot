"""DbLoggingService — структурированное логирование событий агента в PostgreSQL.

Импортируется БЕЗ nanobot (psycopg2 импортируется лениво — модуль годен
для тестов с мок-подключением и для сред без psycopg2).

Архитектура:
  * единственный worker-поток (``self._thread``) дренит очередь батчами;
  * сам сервис НЕ держит psycopg2-соединение: вставки идут через общий
    пул ``utils.db`` (``run(lambda conn: …)``) — воркер пула владеет
    соединением, сервис не плодит лишних подключений;
  * неблокирующие ``log_*`` методы ставят события в ``queue.Queue``;
  * worker батчем вставляет записи по ``flush_interval_sec`` или ``batch_size``;
  * если подключение к БД недоступно или вставка падает — события НЕ пишутся
    в JSONL-файл: они выбрасываются, а ошибка фиксируется в ``stats``
    (``failed`` / ``last_error``). Скрытой записи в файл нет;
  * ``stop(timeout_sec=15)`` отправляет SHUTDOWN-сентинел и дожидается
    опустошения очереди.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


def try_log_event(
    svc: Any | None,
    log_event: LogEvent,
    *,
    producer: str,
    event_type: str,
) -> bool:
    """Defensive helper для producer'ов: попробовать записать событие.

    Используется из sync-путей (``PgDuckDbSyncService._log_sync_event``,
    ``DuckDbCacheStore`` publish-events, ``PreloadService._emit_health_event``,
    ``ContextCompactionService._record_event_log`` после `_notify`-разделения
    concerns) и других мест, где прямой вызов ``svc.log_event`` мог бы
    упасть с ``AttributeError`` при ``svc is None`` или ``AttributeError``
    при ``not svc.is_running()``.

    Контракт:

      * ``svc is None`` → ``logger.warning(...)`` на уровне **WARNING**
        (НЕ DEBUG, НЕ INFO; единый для всех producer'ов) и возврат ``False``.
      * ``not svc.is_running()`` → ``logger.warning(...)`` (тот же уровень)
        и возврат ``False``.
      * Иначе — ``svc.log_event(log_event)`` и возврат его bool-результата
        (``True`` — событие в очереди, ``False`` — переполнение).

    Семантика: ``False`` для бизнеса — **no-op** (не raise, не retry,
    не fallback INSERT). Producer продолжает работу; observability просто
    теряет одно событие. WARNING-уровень даёт операционную видимость
    сбоя конвейера без шума в обычной работе (в отличие от DEBUG, который
    при тихом запуске без logging ничего не покажет, и в отличие от INFO,
    который был бы избыточным).

    Args:
        svc: ``DbLoggingService`` или ``None``.
        log_event: готовое ``LogEvent`` для постановки в очередь.
        producer: имя producer'а для WARNING-сообщения (например,
            ``"PgDuckDbSyncService"``).
        event_type: имя event_type для WARNING-сообщения (используется
            ``log_event.event_type`` если не передан).

    Returns:
        ``True`` если событие поставлено в очередь, ``False`` иначе
        (сервис недоступен / очередь переполнена).
    """
    if svc is None:
        logger.warning(
            "%s: drop event_type=%s (DbLoggingService is None)",
            producer, event_type,
        )
        return False
    try:
        running = bool(svc.is_running())
    except Exception:
        running = False
    if not running:
        logger.warning(
            "%s: drop event_type=%s (DbLoggingService not running)",
            producer, event_type,
        )
        return False
    try:
        return bool(svc.log_event(log_event))
    except Exception as exc:
        logger.warning(
            "%s: log_event failed for event_type=%s: %s",
            producer, event_type, exc,
        )
        return False


def _json_safe(value: Any) -> Any:
    """Рекурсивно привести значение к JSON-серизуемому виду.

    Промпт/ответ могут содержать несеризуемые объекты (dataclass, Path,
    bytes и т.п.). Рекурсивно обходим структуры; неподдерживаемые скаляры
    сводим к ``str(value)``, чтобы ``psycopg2.extras.Json`` не уронил весь
    батч событий.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        try:
            return str(value)
        except Exception:
            return None


@dataclass
class LogEvent:
    """Одно событие для записи в БД (стройная таблица agent_gateway_logs).

    Контекст вопроса (user_id/agent_id/is_subagent/parent_*) живёт в
    отдельной таблице agent_question_runs (см. upsert_question_run) и здесь
    не дублируется — только request_id для связи.

    Поле ``user_id`` хранится в ``agent_gateway_logs`` явно как security
    boundary для ``history_search(session_scope="all")``. Это намеренное
    исключение из правила "identity только в ``agent_question_runs``":
    без него ``scope="all"`` требует JOIN на каждый поиск, а сам факт
    JOIN'а по чужой сессии открывает окно для утечки. Колонка
    заполняется через ``DbLoggingService`` явно (от producer'а или через
    request_id matching в ``_enqueue``) и через backfill-миграцию V004.
    """

    event_type: str
    level: str = "INFO"
    session_id: str | None = None
    channel: str | None = None
    actor: str | None = None
    summary: str | None = None
    payload: dict | None = None
    metadata: dict | None = None
    request_id: str | None = None
    user_id: str | None = None
    name: str | None = None
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    queued_at: float | None = None


@dataclass
class _FlushSentinel:
    pass


@dataclass
class _QuestionRunRecord:
    """Контекст вопроса для upsert в agent_question_runs (не в agent_gateway_logs)."""

    request_id: str
    session_id: str | None = None
    user_id: str | None = None
    chat_id: str | None = None
    channel: str | None = None
    parent_request_id: str | None = None
    agent_id: str | None = None
    parent_agent_id: str | None = None
    is_subagent: bool = False
    status: str | None = None
    summary: str | None = None
    question: str | None = None
    response: str | None = None
    media: list | None = None
    update_only: bool = False


class DbLoggingService:
    """Фоновый writer событий агента в PostgreSQL (без fallback-JSONL)."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        table_name: str,
        question_runs_table: str,
        schema: str = "public",
        dialect: str = "postgres",
        flush_interval_sec: float = 5.0,
        batch_size: int = 100,
        queue_maxsize: int = 10000,
        min_level: str = "INFO",
        connect_backoff_sec: float = 1.0,
        connect_backoff_max_sec: float = 60.0,
        summary_max_chars: int = 200,
        retention_days: int = 0,
        purge_interval_sec: float = 3600.0,
    ) -> None:
        self._dsn = dsn or ""
        self._table_name = table_name
        self._question_runs_table = question_runs_table
        self._schema = schema
        self._dialect = (dialect or "postgres").lower()
        self._flush_interval = float(flush_interval_sec)
        self._batch_size = int(batch_size)
        self._min_level = min_level
        self._connect_backoff_sec = float(connect_backoff_sec)
        self._connect_backoff_max_sec = float(connect_backoff_max_sec)
        self._summary_max_chars = int(summary_max_chars)
        self._retention_days = int(retention_days)
        self._purge_interval_sec = float(purge_interval_sec)
        self._last_purge = 0.0

        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._state_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False

        self._stats: dict[str, Any] = {
            "started_at": None,
            "written": 0,
            "queued": 0,
            "failed": 0,
            "batch_count": 0,
            "queue_full": 0,
            "connected": False,
            "last_error": None,
            "question_runs": 0,
            "last_purge_at": None,
            "last_purged_events": 0,
            "last_purged_runs": 0,
            "written_by_type": {},
        }

        # Индекс «текущий вопрос»: session_key -> контекст вопроса.
        # Парная запись {request_id, user_id} — обе поля обновляются
        # атомарно под _request_index_lock в register_request. Позволяет
        # пронести request_id/user_id/chat_id/parent_request_id
        # на все события вопроса (tool_call/run_finished/outbound),
        # даже если сами события не несут этих полей.
        # В рамках сессии прогоны последовательны, разные сессии имеют
        # разные ключи — коллизий нет.
        # ``user_id`` денормализован для security boundary в
        # ``history_search(session_scope="all")``. ``get_request_user_id``
        # НЕ вводится публично — индекс читается только внутри ``_enqueue``
        # (через request_id matching), чтобы ни один компонент не получил
        # бы способ резолвить чужой identity по session_key.
        self._request_index: dict[str, dict[str, str | None]] = {}
        self._request_index_lock = threading.Lock()
        self._schema_ok = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Запустить worker-поток."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._running = True
        with self._state_lock:
            self._stats["started_at"] = time.time()
        self._thread = threading.Thread(
            target=self._worker, name="db-logging", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 15.0) -> None:
        """Остановить worker, дождавшись опустошения очереди."""
        self._running = False
        self._stop_event.set()
        try:
            self._queue.put_nowait(_FlushSentinel())
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout_sec)
            self._thread = None

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------
    # Публичный API (неблокирующий)
    # ------------------------------------------------------------------

    def log_event(self, event: LogEvent) -> bool:
        if not self._should_log(event.level):
            return False
        return self._enqueue(event)

    # ------------------------------------------------------------------
    # Контекст вопроса (таблица agent_question_runs) + индекс session_key→request_id
    # ------------------------------------------------------------------
    #
    # Контекст вопроса (user_id/agent_id/is_subagent/parent_*) пишется ОДИН
    # раз в отдельную таблицу agent_question_runs (upsert), а не дублируется на
    # каждое событие. В agent_gateway_logs хранится только request_id для связи.
    #
    # Индекс session_key → request_id позволяет tool/run/outbound-событиям
    # узнать request_id текущего вопроса. При параллельной обработке вопросов
    # разных пользователей session_key (= channel:chat_id) уникален для
    # каждого чата → коллизий нет.

    def register_request(
        self,
        session_key: str | None,
        request_id: str | None,
        *,
        user_id: str | None = None,
        chat_id: str | None = None,
        channel: str | None = None,
        parent_request_id: str | None = None,
        agent_id: str | None = None,
        parent_agent_id: str | None = None,
        is_subagent: bool = False,
        status: str | None = "running",
        summary: str | None = None,
        question: str | None = None,
        media: list | None = None,
    ) -> bool:
        """Зарегистрировать контекст вопроса (upsert в agent_question_runs).

        Также сохраняет session_key → {request_id, user_id} в индексе,
        чтобы последующие tool/run/outbound-события знали request_id
        текущего вопроса и могли получить ``user_id`` через request_id
        matching в ``_enqueue`` (для security boundary
        ``history_search(session_scope="all")``).

        Пара ``{request_id, user_id}`` обновляется атомарно под
        ``_request_index_lock`` — параллельный reader видит либо
        полностью старое состояние, либо полностью новое.
        """
        if not request_id:
            return False
        if session_key:
            with self._request_index_lock:
                self._request_index[session_key] = {
                    "request_id": request_id,
                    "user_id": user_id,
                }
        return self._enqueue(_QuestionRunRecord(
            request_id=request_id,
            session_id=session_key,
            user_id=user_id,
            chat_id=chat_id,
            channel=channel,
            parent_request_id=parent_request_id,
            agent_id=agent_id,
            parent_agent_id=parent_agent_id,
            is_subagent=is_subagent,
            status=status,
            summary=summary,
            question=question,
            media=media,
        ))

    def get_request_id(self, session_key: str | None) -> str | None:
        """Получить request_id текущего вопроса для сессии."""
        if not session_key:
            return None
        with self._request_index_lock:
            entry = self._request_index.get(session_key)
            if not isinstance(entry, dict):
                return None
            value = entry.get("request_id")
            return value if isinstance(value, str) else None

    def clear_request(self, session_key: str | None) -> None:
        """Снять привязку вопроса по завершении прогона."""
        if not session_key:
            return
        with self._request_index_lock:
            self._request_index.pop(session_key, None)

    def finish_request(
        self,
        request_id: str | None,
        *,
        status: str = "finished",
        summary: str | None = None,
        response: str | None = None,
        media: list | None = None,
    ) -> bool:
        """Обновить статус/summary/response вопроса (upsert в agent_question_runs)."""
        if not request_id:
            return False
        return self._enqueue(_QuestionRunRecord(
            request_id=request_id,
            status=status,
            summary=summary,
            response=response,
            media=media,
            update_only=True,
        ))

    def log_inbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        message_id: str | None = None,
        sender_id: str | None = None,
        chat_id: str | None = None,
        actor: str | None = None,
        request_id: str | None = None,
        level: str = "INFO",
        media: list | None = None,
    ) -> bool:
        actor_val = actor or sender_id or "user"
        payload: dict[str, Any] = {"content": content, "message_id": message_id}
        if sender_id:
            payload["sender_id"] = sender_id
        if chat_id:
            payload["chat_id"] = chat_id
        if media:
            payload["media"] = list(media)
        return self.log_event(LogEvent(
            event_type="inbound",
            level=level,
            session_id=session_id,
            channel=channel,
            actor=actor_val,
            name=actor_val,
            summary=content[: self._summary_max_chars] if content else "",
            payload=payload,
            request_id=request_id or message_id,
        ))

    def log_outbound(
        self,
        session_id: str,
        channel: str,
        content: str,
        *,
        latency_ms: float | None = None,
        tokens_used: int | None = None,
        kind: str = "outbound_final",
        request_id: str | None = None,
        level: str = "INFO",
        media: list | None = None,
    ) -> bool:
        payload: dict[str, Any] = {"content": content}
        if media:
            payload["media"] = list(media)
        return self.log_event(LogEvent(
            event_type=kind,
            level=level,
            session_id=session_id,
            channel=channel,
            actor="agent",
            name="assistant",
            summary=content[: self._summary_max_chars] if content else "",
            payload=payload,
            metadata={"latency_ms": latency_ms, "tokens_used": tokens_used},
            request_id=request_id,
        ))

    def log_tool_call(
        self,
        session_id: str,
        tool_name: str,
        args: dict | None = None,
        *,
        tool_call_id: str | None = None,
        request_id: str | None = None,
        level: str = "INFO",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="tool_call",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=tool_name,
            payload={"tool": tool_name, "args": args or {}, "tool_call_id": tool_call_id},
            request_id=request_id,
            name=tool_name,
        ))

    def log_tool_result(
        self,
        session_id: str,
        tool_name: str,
        result: Any,
        latency_ms: float,
        *,
        tool_call_id: str | None = None,
        status: str = "ok",
        error: str | None = None,
        request_id: str | None = None,
        level: str = "INFO",
    ) -> bool:
        # Для ошибочных результатов: уровень ERROR (для фильтрации) и текст
        # ошибки в summary — причина («что не так») видна сразу, без
        # раскрытия payload.
        if status == "error":
            level = "ERROR"
        summary = tool_name
        if status == "error" and error:
            summary = str(error)[: self._summary_max_chars]
        return self.log_event(LogEvent(
            event_type="tool_result",
            level=level,
            session_id=session_id,
            actor="agent",
            summary=summary,
            payload={"tool": tool_name, "status": status, "result": result, "error": error},
            metadata={"latency_ms": latency_ms, "tool_call_id": tool_call_id},
            request_id=request_id,
            name=tool_name,
        ))

    def log_llm_call(
        self,
        session_id: str,
        prompt: Any,
        response: Any,
        *,
        iteration: int | None = None,
        model: str | None = None,
        finish_reason: str | None = None,
        usage: dict | None = None,
        request_id: str | None = None,
        level: str = "INFO",
    ) -> bool:
        """Записать полный запрос и ответ LLM за одну итерацию.

        Полный ``messages`` (промпт) передаётся в ``payload["prompt"]``,
        ответ модели (``LLMResponse``/asdict) — в ``payload["response"]``.
        Оба значения рекурсивно приводятся к JSON-серизуемому виду
        (``_json_safe``), поэтому писать можно сразу на оборот агента.
        """
        return self.log_event(LogEvent(
            event_type="llm_call",
            level=level,
            session_id=session_id,
            actor="agent",
            name=model or "llm",
            summary=finish_reason or "llm_call",
            payload={
                "prompt": _json_safe(prompt),
                "response": _json_safe(response),
            },
            metadata={
                "iteration": iteration,
                "model": model,
                "finish_reason": finish_reason,
                "usage": usage or {},
            },
            request_id=request_id,
        ))

    def log_error(
        self,
        error: str,
        *,
        session_id: str | None = None,
        context: dict | None = None,
        request_id: str | None = None,
        level: str = "ERROR",
    ) -> bool:
        return self.log_event(LogEvent(
            event_type="error",
            level=level,
            session_id=session_id,
            name="error",
            summary=error[:200],
            payload={"error": error, "context": context or {}},
            request_id=request_id,
        ))

    def log_sync_event(
        self,
        event_type: str,
        summary: str,
        payload: dict | None = None,
        *,
        name: str | None = None,
        level: str = "INFO",
    ) -> bool:
        """Записать событие из PG→DuckDB sync-пути.

        Используется из worker-потока ``PgDuckDbSyncService`` (и аналогичных).
        При отсутствии сервиса caller должен использовать
        :func:`DbLoggingService.try_log_event` (defensive helper), который
        даёт no-op for business + operational WARNING, без fallback INSERT
        в ``agent_gateway_logs``. Старый sync-fallback (helper в модуле
        ``event_log`` утилит workspace, удалён change'ом
        ``unify-agent-event-logging-pipeline``).
        """
        return self.log_event(LogEvent(
            event_type=event_type,
            level=level,
            session_id="gateway:sync",
            channel=None,
            actor="sync",
            name=name or event_type,
            summary=summary[: self._summary_max_chars] if summary else "",
            payload=payload or {},
        ))

    def get_stats(self) -> dict[str, Any]:
        with self._state_lock:
            s = dict(self._stats)
        s.update({
            "running": self.is_running(),
            "queue_size": self._queue.qsize(),
            "oldest_queued_age_sec": self._compute_oldest_queued_age_sec(),
        })
        return s

    def _compute_oldest_queued_age_sec(self) -> float | None:
        """Возраст самого старого ``LogEvent`` в очереди (секунды).

        Учитываются ТОЛЬКО объекты ``LogEvent`` с непустым ``queued_at``
        (выставленным в ``_enqueue``). ``_QuestionRunRecord`` и
        ``_FlushSentinel`` исключаются: они не идут в
        ``agent_gateway_logs`` и не должны влиять на метрику задержки
        записи событий. Если очередь пуста или содержит только
        служебные объекты — возвращается ``None``.

        Возвращает ``max(time.time() - queued_at)`` (самый старый = самый
        большой возраст). Семантика — «как давно самое старое событие
        ждёт записи», а не «возраст первого по FIFO».
        """
        now = time.time()
        ages = [
            now - event.queued_at
            for event in self._queue.queue
            if isinstance(event, LogEvent) and event.queued_at is not None
        ]
        if not ages:
            return None
        return max(ages)

    # ------------------------------------------------------------------
    # Внутренние
    # ------------------------------------------------------------------

    def _should_log(self, level: str) -> bool:
        """Проверить, что ``level`` не ниже ``self._min_level``.

        Сравнение по числовой шкале (``DEBUG=0``, ``INFO=1``, ``WARN=2``,
        ``ERROR=3``). Неизвестные уровни считаются как ``INFO``.
        """
        order = {"DEBUG": 0, "INFO": 1, "WARN": 2, "ERROR": 3}
        return order.get(level, 1) >= order.get(self._min_level, 1)

    def _enqueue(self, event: LogEvent) -> bool:
        """Неблокирующе положить событие в очередь.

        Перед постановкой в очередь резолвит ``event.user_id`` по трём
        ветвям (security boundary для ``history_search(scope="all")``):

          1. Explicit value wins. Если producer явно задал ``user_id`` —
             используется оно, индекс не читается. Это закрывает кейс
             subagent'а, который должен прокинуть identity родителя
             вне обычного request-index resolution path.
          2. Match by request_id. Если ``event.user_id is None`` AND
             ``event.request_id is not None`` AND индекс для
             ``event.session_id`` содержит запись с тем же
             ``request_id`` — подставляется ``user_id`` из индекса.
          3. No inference. Иначе (``request_id is None``, request_id не
             совпадает, или session_key отсутствует в индексе) —
             ``event.user_id`` остаётся ``None``. Событие записывается
             с ``user_id IS NULL`` и НЕ участвует в ``scope="all"``.

        Matching ОБЯЗАН идти по ``event.request_id == entry["request_id"]``
        (а не по ``session_id`` alone): между созданием события и его
        enqueue может произойти ``register_request`` для следующего
        request в той же ``session_key``, и без сверки по ``request_id``
        отложенное событие получило бы чужой ``user_id``.

        Returns:
            ``True`` — событие в очереди, ``False`` — очередь переполнена
            (``queue_full++`` в статистике). Никогда не блокирует.
        """
        self._resolve_event_user_id(event)
        event.queued_at = time.time()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._state_lock:
                self._stats["queue_full"] += 1
            return False
        with self._state_lock:
            self._stats["queued"] += 1
        return True

    def _resolve_event_user_id(self, event: LogEvent) -> None:
        """Резолв ``event.user_id`` из индекса (security boundary path).

        Вызывается из :py:meth:`_enqueue` перед постановкой события в
        очередь. Не делает ничего, если ``user_id`` уже задан producer'ом;
        иначе пытается сопоставить ``event.request_id`` с текущим
        request в индексе для ``event.session_id``. Никогда не выводит
        ``user_id`` только по ``session_id`` — это закрывает класс атак
        «событие-сирота получает текущего пользователя сессии».
        """
        if event.user_id is not None:
            return
        if event.request_id is None or event.session_id is None:
            return
        with self._request_index_lock:
            entry = self._request_index.get(event.session_id)
        if not isinstance(entry, dict):
            return
        if entry.get("request_id") != event.request_id:
            return
        resolved = entry.get("user_id")
        if isinstance(resolved, str):
            event.user_id = resolved

    def _worker(self) -> None:
        """Главный цикл worker-потока: drain очереди → батч → flush.

        Алгоритм:
          1. Получить из очереди элемент с таймаутом до ``flush_interval``
             (так цикл «просыпается» хотя бы раз в ``flush_interval``
             для флаша мелких батчей);
          2. ``_FlushSentinel`` → финальный флаш и выход (если ``_running == False``);
          3. Обычный ``LogEvent`` → в буфер;
          4. Если буфер заполнился (``>= batch_size``) ИЛИ
             ``time.time() >= deadline`` — выполнить flush.

        При flush:
          * запись идёт через общий пул ``utils.db`` (``run(lambda conn: …)``) —
            сервис своего psycopg2-соединения не держит;
          * нет DSN → события выбрасываются (счётчик ``failed``),
            JSONL-файл не пишется;
          * при ошибке batch — события выбрасываются (``failed``),
            ``connected = False``.

        В блоке ``finally`` — финальный flush (иначе при штатной остановке
        теряем события из буфера).
        """
        buffer: list[LogEvent] = []
        deadline = time.time() + self._flush_interval

        try:
            while True:
                timeout = max(0.0, deadline - time.time())
                try:
                    item = self._queue.get(timeout=timeout)
                except queue.Empty:
                    item = None

                if isinstance(item, _FlushSentinel):
                    self._flush_batch(buffer)
                    buffer.clear()
                    if not self._running:
                        return
                    continue

                # Контекст вопроса — отдельный upsert в agent_question_runs,
                # не смешивается с батчем событий agent_gateway_logs.
                if isinstance(item, _QuestionRunRecord):
                    self._handle_question_run(item)
                    continue

                if item is not None:
                    buffer.append(item)

                if len(buffer) >= self._batch_size:
                    self._flush_batch(buffer)
                    buffer.clear()
                    deadline = time.time() + self._flush_interval
                elif time.time() >= deadline:
                    if buffer:
                        self._flush_batch(buffer)
                        buffer.clear()
                    deadline = time.time() + self._flush_interval

                # Периодическая очистка: пустой outbound-мусор (stream-чанки)
                # чистим всегда; старые события/question_runs — только при
                # retention_days > 0. Защита от неограниченного роста таблицы.
                if self._purge_interval_sec > 0 and (
                    time.time() - self._last_purge >= self._purge_interval_sec
                ):
                    self._last_purge = time.time()
                    self._purge_old()
        finally:
            # Финальный флаш
            if buffer:
                self._flush_batch(buffer)

    # ------------------------------------------------------------------
    # Запись через общий пул utils.db
    # ------------------------------------------------------------------

    def _db_run(self, fn):
        """Выполнить ``fn(conn)`` на свободном соединении общего пула ``utils.db``."""
        from utils.db import configure, run

        if self._dsn:
            configure(self._dsn)
        return run(fn)

    def _ensure_schema(self, conn: Any) -> None:
        """Проверить существование таблиц логов/контекста вопросов.

        Сервис НЕ провижинит схему: таблицы ``agent_question_runs`` /
        ``agent_gateway_logs`` должны быть созданы заранее
        (``sql/logs/create_public_agent_question_runs.sql`` /
        ``sql/logs/create_public_agent_gateway_logs.sql``).
        Если таблица логов отсутствует — поднимается исключение; вызывающий
        ``_flush_batch`` логирует его (``last_error`` + ``logger.error``),
        а события выбрасываются (счётчик ``failed``).

        Имя таблицы берётся из конструктора (``table_name``, параметр), а не
        хардкодится. Инлайн-DDL в коде нет и не выполняется.
        """
        check_tables = [self._table_name]
        if self._question_runs_table:
            check_tables.append(self._question_runs_table)
        cur = conn.cursor()
        try:
            for tbl in check_tables:
                cur.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    [self._schema, tbl],
                )
                if cur.fetchone() is None:
                    raise RuntimeError(
                        f"DbLoggingService: таблица не найдена: "
                        f"{self._schema}.{tbl} — создайте её миграцией, "
                        "сервис DDL не выполняет"
                    )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _flush_batch(self, batch: list[LogEvent]) -> None:
        """Вставить батч через общий пул ``utils.db`` (без своего соединения).

        Использует ``psycopg2.extras.execute_batch`` с
        ``page_size=self._batch_size`` для chunked-вставки — ``execute_batch``
        сам режет список на страницы и выполняет несколько ``INSERT`` с одним
        statement. На каждой строке — ``id`` (UUID), ``level``/``event_type``/
        ``summary`` (простые VARCHAR/TEXT), и ``payload``/``metadata`` как
        ``psycopg2.extras.Json`` (→ JSONB).

        При исключении (битый JSONB, отвалившееся соединение, deadlock):
          * батч целиком выбрасывается (``failed += len(batch)``);
          * ``connected = False``.

        No-op при пустом батче.
        """
        if not batch:
            return
        if not self._dsn:
            self._drop_batch(batch)
            return
        try:

            def _work(conn: Any) -> None:
                if not self._schema_ok:
                    self._ensure_schema(conn)
                    self._schema_ok = True
                self._insert_batch(conn, batch)

            self._db_run(_work)
            with self._state_lock:
                self._stats["written"] += len(batch)
                self._stats["batch_count"] += 1
                self._stats["connected"] = True
                for etype, count in _count_by_type(batch).items():
                    self._stats["written_by_type"][etype] = (
                        self._stats["written_by_type"].get(etype, 0) + count
                    )
        except Exception as exc:
            self._schema_ok = False
            with self._state_lock:
                self._stats["failed"] += len(batch)
                self._stats["last_error"] = f"flush: {exc}"
                self._stats["connected"] = False

    def _insert_batch(self, conn: Any, batch: list[LogEvent]) -> None:
        """Выполнить ``execute_batch`` INSERT на данном соединении."""
        import psycopg2.extras

        cur = conn.cursor()
        try:
            psycopg2.extras.execute_batch(
                cur,
                f'INSERT INTO "{self._schema}"."{self._table_name}" '
                '(id, level, event_type, user_id, session_id, channel, actor, summary, payload, '
                'metadata, request_id, name) '
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(
                    e.id, e.level, e.event_type, e.user_id, e.session_id, e.channel, e.actor,
                    e.summary,
                    psycopg2.extras.Json(e.payload or {}),
                    psycopg2.extras.Json(e.metadata or {}),
                    e.request_id, e.name,
                ) for e in batch],
                page_size=self._batch_size,
            )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _handle_question_run(self, rec: _QuestionRunRecord) -> None:
        """Обработать контекст вопроса: upsert в agent_question_runs.

        Через общий пул ``utils.db``. При неудаче запись выбрасывается
        (``failed++``), JSONL-файл не пишется. При ошибке upsert —
        ``connected = False``.
        """
        if not self._dsn:
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = "question_run: нет соединения с БД"
            return
        try:

            def _work(conn: Any) -> None:
                if not self._schema_ok:
                    self._ensure_schema(conn)
                    self._schema_ok = True
                self._upsert_question_run(conn, rec)

            self._db_run(_work)
            with self._state_lock:
                self._stats["question_runs"] += 1
                self._stats["connected"] = True
        except Exception as exc:
            self._schema_ok = False
            with self._state_lock:
                self._stats["failed"] += 1
                self._stats["last_error"] = f"question_run upsert: {exc}"
                self._stats["connected"] = False

    def _upsert_question_run(self, conn: Any, rec: _QuestionRunRecord) -> None:
        """Upsert контекста вопроса в agent_question_runs (без ON CONFLICT).

        Greenplum 6.x (база PostgreSQL 9.4) НЕ поддерживает
        ``INSERT ... ON CONFLICT (…) DO UPDATE`` — он появился только в
        Greenplum 7. Поэтому используем переносимый двухшаговый паттерн,
        работающий и на PostgreSQL 13, и на Greenplum 6.5:

          1. ``UPDATE ... WHERE request_id = …`` — обновить существующую строку;
          2. ``INSERT ... SELECT … WHERE NOT EXISTS (…)`` — вставить, если
             строки ещё нет (закрывает гонку "нет строки после UPDATE").

        ``update_only`` (вызов finish_request) обновляет только
        ``updated_at``/``status``/``summary``, не затирая контекст вопроса.
        Обычная регистрация upsert-ит все поля (новый вопрос — вставка,
        повторная регистрация — перезапись контекста).
        """
        cur = conn.cursor()
        try:
            # media хранится как JSON-строка в TEXT-колонке
            media_json = json.dumps(rec.media, ensure_ascii=False) if rec.media else None
            if rec.update_only:
                cur.execute(
                    f'UPDATE "{self._schema}"."{self._question_runs_table}" '
                    "SET updated_at = now(), status = %s, summary = %s, "
                    "response = COALESCE(%s, response), media = COALESCE(%s, media) "
                    "WHERE request_id = %s",
                    (rec.status, rec.summary, rec.response, media_json, rec.request_id),
                )
                cur.execute(
                    f'INSERT INTO "{self._schema}"."{self._question_runs_table}" '
                    "(request_id, status, summary, response, media) "
                    "SELECT %s, %s, %s, %s, %s "
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{self._schema}"."{self._question_runs_table}" '
                    "WHERE request_id = %s)",
                    (rec.request_id, rec.status, rec.summary, rec.response, media_json, rec.request_id),
                )
            else:
                cur.execute(
                    f'UPDATE "{self._schema}"."{self._question_runs_table}" '
                    "SET session_id = %s, user_id = %s, chat_id = %s, "
                    "channel = %s, parent_request_id = %s, agent_id = %s, "
                    "parent_agent_id = %s, is_subagent = %s, status = %s, "
                    "summary = %s, question = %s, media = %s, updated_at = now() "
                    "WHERE request_id = %s",
                    (
                        rec.session_id, rec.user_id, rec.chat_id, rec.channel,
                        rec.parent_request_id, rec.agent_id,
                        rec.parent_agent_id, rec.is_subagent, rec.status,
                        rec.summary, rec.question, media_json, rec.request_id,
                    ),
                )
                cur.execute(
                    f'INSERT INTO "{self._schema}"."{self._question_runs_table}" '
                    "(request_id, session_id, user_id, chat_id, channel, "
                    "parent_request_id, agent_id, parent_agent_id, is_subagent, "
                    "status, summary, question, media) "
                    "SELECT %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
                    f'WHERE NOT EXISTS (SELECT 1 FROM "{self._schema}"."{self._question_runs_table}" '
                    "WHERE request_id = %s)",
                    (
                        rec.request_id, rec.session_id, rec.user_id, rec.chat_id,
                        rec.channel, rec.parent_request_id, rec.agent_id,
                        rec.parent_agent_id, rec.is_subagent, rec.status,
                        rec.summary, rec.question, media_json, rec.request_id,
                    ),
                )
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _drop_batch(self, batch: list[LogEvent]) -> None:
        """Выбросить батч, когда БД недоступна (без записи в JSONL-файл).

        Увеличивает ``stats["failed"]`` и фиксирует ``last_error`` — скрытая
        запись в файл не производится, событие считается потерянным.
        """
        with self._state_lock:
            self._stats["failed"] += len(batch)
            self._stats["last_error"] = "flush: БД недоступна, батч выброшен"

    # ------------------------------------------------------------------
    # Очистка (retention + удаление пустого мусора)
    # ------------------------------------------------------------------

    def purge_empty_outbound(self) -> int:
        """Удалить пустые outbound-события (stream-чанки / синтетические финалы).

        Удаляются ``outbound_final``/``outbound_delta`` с пустым/whitespace
        ``content`` И без ``media`` (реальные доставки файлов с пустым
        текстом сохраняются — у них есть media). Возвращает число удалённых
        строк. Работает через общий пул ``utils.db`` (своего соединения нет).
        """
        if not self._dsn:
            return 0
        try:
            def _work(conn: Any) -> int:
                cur = conn.cursor()
                try:
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."{self._table_name}" '
                        "WHERE event_type IN ('outbound_final', 'outbound_delta') "
                        "AND coalesce(btrim(payload->>'content'), '') = '' "
                        "AND (payload->'media') IS NULL"
                    )
                    return int(cur.rowcount)
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass

            removed = self._db_run(_work) or 0
            with self._state_lock:
                self._stats["last_purged_events"] += removed
                self._stats["last_purge_at"] = time.time()
            return removed
        except Exception as exc:
            with self._state_lock:
                self._stats["last_error"] = f"purge_empty: {exc}"
            return 0

    def purge_old(self, retention_days: int | None = None) -> tuple[int, int]:
        """Удалить события и question_runs старее ``retention_days``.

        Возвращает ``(удалено_событий, удалено_question_runs)``. Если
        ``retention_days <= 0`` или нет DSN — ничего не делает. Интервал
        считается через ``NOW() - (%s || ' days')::interval`` (совместимо
        с Greenplum 6.5, без ``make_interval``).
        """
        days = int(retention_days if retention_days is not None else self._retention_days)
        if days <= 0 or not self._dsn:
            return (0, 0)
        try:
            def _work(conn: Any) -> tuple[int, int]:
                cur = conn.cursor()
                try:
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."{self._table_name}" '
                        "WHERE \"timestamp\" < NOW() - (%s || ' days')::interval",
                        (str(days),),
                    )
                    ev = int(cur.rowcount)
                    cur.execute(
                        f'DELETE FROM "{self._schema}"."{self._question_runs_table}" '
                        "WHERE updated_at < NOW() - (%s || ' days')::interval",
                        (str(days),),
                    )
                    return (ev, int(cur.rowcount))
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass

            res = self._db_run(_work) or (0, 0)
            with self._state_lock:
                self._stats["last_purged_events"] += res[0]
                self._stats["last_purged_runs"] += res[1]
                self._stats["last_purge_at"] = time.time()
            return res
        except Exception as exc:
            with self._state_lock:
                self._stats["last_error"] = f"purge_old: {exc}"
            return (0, 0)

    def _purge_old(self) -> None:
        """Один шаг периодической очистки из worker-цикла.

        Пустой outbound-мусор чистим всегда (независимо от retention);
        старые данные — только если задан ``retention_days > 0``.
        """
        self.purge_empty_outbound()
        if self._retention_days > 0:
            self.purge_old(self._retention_days)


def _count_by_type(batch: list[LogEvent]) -> dict[str, int]:
    """Подсчитать число событий каждого event_type в батче.

    Возвращает dict с ключами = event_type (только непустые значения).
    Используется для инкремента ``written_by_type`` только после успешного
    INSERT'а в БД.
    """
    counter: dict[str, int] = {}
    for e in batch:
        et = e.event_type
        if not et:
            continue
        counter[et] = counter.get(et, 0) + 1
    return counter
