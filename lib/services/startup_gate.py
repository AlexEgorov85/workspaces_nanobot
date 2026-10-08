"""StartupGate — явный порядок «данные → векторы → каналы».

Зачем
=====

Gateway поднимает три тяжёлые вещи: DuckDB-кэш (in-memory mirror PG),
FAISS-индексы (векторы) и каналы (Postgres/Redis/WebSocket). Порядок
раньше был «как получится»: ``PgDuckDbSyncService`` тянул initial_load в
фоновом треде, ``gateway`` создавал preload векторов фоновой задачей и
тут же стартовал ``channels.start_all()``. Первые вопросы из очереди
уезжали агенту **до** сборки FAISS-индексов — он отвечал без эмбеддингов
заметно хуже обычного, и это выглядело как «модель сегодня дурит», а не
как «данные ещё не приехали».

Этот сервис делает порядок явным и наблюдаемым — тремя шагами, каждый из
которых имеет собственный сигнал готовности:

    1. :meth:`StartupGate.wait_for_cache` — ждёт **сигнал**
       ``PgDuckDbSyncService`` «данные в памяти + снапшот опубликован»;
    2. :meth:`StartupGate.load_vectors` — дожидается **завершения** сборки
       FAISS-индексов из DuckDB-кэша;
    3. ``channels.start_all()`` — вызывает gateway, и только после
       успешных шагов 1-2.

Почему без таймаутов
====================

Таймаут на этапе готовности — это не «безопасный дефолт», а способ
**незаметно** стартовать сломанным: по истечении N секунд процесс
продолжает работу, отвечает на вопросы без векторов и выглядит живым.
Тут нам нужен явный факт «данные/векторы есть» — он и передаётся
сигналом, а не истечением времени. Поэтому в модуле нет ни одного
таймаута, ни ``asyncio.sleep``, ни ретраев: пока ``wait_for_cache`` не
вернулся, gateway **не принимает вопросы вообще**.

Что делать, если данных не будет
--------------------------------

Сигнал может не прийти, а сборка индексов — упасть. Это ошибка, а не
повод ждать дальше, и политика ``on_unavailable`` решает, что делать:

  * ``"warn"`` (дефолт) — оператор сознательно предпочитает доступность
    векторам: печатаем громкое предупреждение в терминал, пишем
    событие в ``agent_gateway_logs`` и стартуем каналы degraded;
  * ``"fail"`` — векторы обязательны: ``load_vectors`` поднимает
    ``StartupGateError``, gateway не стартует вообще (exit 2).

Третьей политики нет сознательно: жёсткий отказ на уровне startup уже
есть — ``SchemaValidationService``
(``openspec/specs/runtime/startup-schema-validation``), и «убить
процесс» здесь означало бы разорвать контракт ``GatewayRunner``.

Наблюдаемость ожидания
======================

Пока шаг 1 не завершён, ``phase == "pending"``, и readiness-проверка
``vector_search`` отдаёт ``DOWN`` с текстом фазы. То есть процесс,
который ещё грузится, виден как «не готов принимать вопросы», а не как
«всё UP при пустом FAISS-кэше».
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from lib.services.db_logging_service import LogEvent

logger = logging.getLogger(__name__)

# Фазы startup-гейта. Публичные значения — ``StartupGate.phase``.
GatePhase = Literal[
    "pending",       # ждём сигнал загрузки данных
    "skipped",       # прогрев не применяется (выключен / нет кэша)
    "cache_ready",   # данные в DuckDB есть, векторы ещё собираются
    "vectors_ready", # FAISS-индексы собраны — можно принимать вопросы
    "unavailable",   # сигнала не было или сборка упала — см. on_unavailable
]

# Что делать, если векторы не удалось подготовить.
UnavailablePolicy = Literal["warn", "fail"]

# Имя события в ``agent_gateway_logs``: оператор и ``history_search``
# видят, стартовал ли gateway с векторами или degraded.
STARTUP_PRELOAD_EVENT = "startup_vector_preload"


class StartupGateError(RuntimeError):
    """Векторы обязательны, но не готовы — при ``on_unavailable="fail"``.

    Поднимается из :meth:`StartupGate.load_vectors`, чтобы вызывающий
    (gateway startup-boundary) мог завершиться с ``exit 2``, не поднимая
    ни каналов, ни агента.

    Attributes:
        report: ``StartupGateReport`` с фазой и причиной.
    """

    def __init__(self, report: "StartupGateReport") -> None:
        super().__init__(
            "vector indexes are not ready and "
            f"gateway.startup.vector_preload.on_unavailable=fail "
            f"({report.detail})"
        )
        self.report = report


class ThreadSafeSignal:
    """Событие готовности, выставляемое из ЧУЖОГО потока.

    Гейт ждёт сигнал без таймаута, поэтому «проспать» его нельзя.
    Наивный ``asyncio.Event.set()`` из worker-треда
    ``PgDuckDbSyncService`` для этого не годится: у loop нет собственного
    таймаута, на котором он проснулся бы проверить флаг, поэтому
    ожидание могло бы длиться бесконечно уже **после** загрузки данных.

    Поэтому signal будит loop явно через ``call_soon_threadsafe``, а
    loop привязывается в том же ожидании, которое его и ждёт — тогда
    «кто ждёт, того и разбудим» верно по построению.

    Использование::

        signal = ThreadSafeSignal()
        # worker-тред синхронизации:
        signal.set()
        # startup-фаза (в её собственном loop):
        await gate.wait_for_cache(signal)
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def wait(self) -> None:
        """Дождаться сигнала; loop запоминается для потокобезопасного set."""
        self._loop = asyncio.get_running_loop()
        await self._event.wait()

    def set(self) -> None:
        """Выставить сигнал. Безопасно вызывать из любого потока."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._event.set)
                return
            except RuntimeError:
                # loop уже закрыт — падаем на прямую установку флага.
                pass
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()


class VectorPreloadConfig:
    """Типизированная проекция ``gateway.startup.vector_preload.*``.

    Читается из ``SETTINGS`` функцией :func:`read_vector_preload_config`.
    Значения по умолчанию совпадают с дефолтами в коде, то есть
    отсутствие секции в ``project.json`` даёт ровно то же поведение.

    Attributes:
        enabled: master-switch. ``False`` — векторы на старте не
            готовятся вовсе (осознанный отказ оператора).
        await_ready: ``False`` — не ждать готовности перед стартом
            каналов, ``load_vectors`` уходит в фоновую задачу (старое
            поведение: вопросы принимаются сразу, индексы догружаются).
            ``True`` (дефолт) — ждать.
        on_unavailable: ``"warn"`` (дефолт) | ``"fail"`` — см. docstring
            модуля.
    """

    __slots__ = ("enabled", "await_ready", "on_unavailable")

    def __init__(
        self,
        *,
        enabled: bool = True,
        await_ready: bool = True,
        on_unavailable: UnavailablePolicy = "warn",
    ) -> None:
        self.enabled = bool(enabled)
        self.await_ready = bool(await_ready)
        self.on_unavailable: UnavailablePolicy = (
            "fail" if on_unavailable == "fail" else "warn"
        )

    def __repr__(self) -> str:  # pragma: no cover - диагностика
        return (
            "VectorPreloadConfig("
            f"enabled={self.enabled}, await_ready={self.await_ready}, "
            f"on_unavailable={self.on_unavailable!r})"
        )


def read_vector_preload_config(settings: Any) -> VectorPreloadConfig:
    """Спроецировать ``SETTINGS['gateway']['startup']['vector_preload']``.

    Отсутствующие или мусорные значения не поднимают исключение: гейт —
    не место для падения из-за конфига, а дефолты безопасны (ждём
    векторы, при ошибке стартуем degraded). Типы и диапазоны
    валидируются на старте процесса в ``lib.core.project_settings``
    (``StartupVectorPreloadSettings``) — fail-fast там, где он уместен.
    """
    section: Any = None
    try:
        gateway = (settings or {}).get("gateway") or {}
        startup = gateway.get("startup") or {}
        section = startup.get("vector_preload")
    except Exception as exc:  # noqa: BLE001 - settings может быть чем угодно
        logger.warning("vector_preload config unreadable: %s", exc)
        return VectorPreloadConfig()
    if not isinstance(section, dict):
        return VectorPreloadConfig()

    on_unavailable = section.get("on_unavailable")
    if on_unavailable not in ("warn", "fail"):
        if on_unavailable is not None:
            logger.warning(
                "gateway.startup.vector_preload.on_unavailable=%r — "
                "допустимо 'warn' | 'fail', берём дефолт 'warn'",
                on_unavailable,
            )
        on_unavailable = "warn"

    return VectorPreloadConfig(
        enabled=bool(section.get("enabled", True)),
        await_ready=bool(section.get("await_ready", True)),
        on_unavailable=on_unavailable,
    )


@dataclass(frozen=True)
class StartupGateReport:
    """Итог попытки подготовить векторы.

    Attributes:
        phase: см. ``GatePhase``.
        ok: ``True`` только при ``phase == "vectors_ready"``.
        loaded: индексы, собранные ``preload_indexes()`` (пусто при skip
            или ошибке).
        errors: ошибки построения индексов (``store.preload_errors()``).
        detail: человекочитаемая причина: ``"disabled"``,
            ``"no cache_store"``, ``"cache_store not ready"``,
            ``"vector index build failed: ..."`` и т.п.
        policy: применённая политика ``on_unavailable``.
        duration_sec: длительность шага сборки векторов.
    """

    phase: GatePhase
    ok: bool
    loaded: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    policy: UnavailablePolicy = "warn"
    duration_sec: float = 0.0

    def summary(self) -> str:
        """Компактная строка для логов и ``agent_gateway_logs``."""
        return (
            f"phase={self.phase} loaded={len(self.loaded)} "
            f"errors={len(self.errors)} policy={self.policy} "
            f"elapsed={self.duration_sec:.1f}s"
            + (f" detail={self.detail}" if self.detail else "")
        )


class StartupGate:
    """Гейт готовности: данные → векторы → можно принимать вопросы.

    Не владеет ни кэшем, ни индексами, ни каналами: он умеет дождаться
    сигнала загрузки данных (:meth:`wait_for_cache`), дождаться сборки
    векторов (:meth:`load_vectors`) и сообщить о фазе (``phase`` /
    :attr:`report` / :meth:`is_awaiting`). Тяжёлое делают
    ``PgDuckDbSyncService`` (данные) и ``PreloadService`` (векторы).

    Attributes:
        config: ``VectorPreloadConfig`` — решение «ждать или нет».
        phase: текущая фаза; читается readiness-проверкой
            ``vector_search``.
    """

    def __init__(
        self,
        config: VectorPreloadConfig | None = None,
        *,
        preload_service: Any = None,
        cache_store: Any = None,
        db_logging_service: Any | None = None,
        on_report: Callable[[StartupGateReport], None] | None = None,
    ) -> None:
        self.config = config or VectorPreloadConfig()
        self._preload_service = preload_service
        self._cache_store = cache_store
        self._db_logging_service = db_logging_service
        self._on_report = on_report
        self._phase: GatePhase = "pending"
        self._report: StartupGateReport | None = None

    # ------------------------------------------------------------------
    # Состояние
    # ------------------------------------------------------------------

    @property
    def phase(self) -> GatePhase:
        """Текущая фаза гейта (``pending`` до первого шага)."""
        return self._phase

    @property
    def report(self) -> StartupGateReport | None:
        """Отчёт последней попытки (``None`` — ещё не готовили)."""
        return self._report

    def is_awaiting(self) -> bool:
        """True, пока вопросы принимать рано.

        Используется readiness-проверкой: «процесс жив, но векторы ещё
        едут» — это ``DEGRADED``, а не ``READY``.
        """
        return self._phase in ("pending", "cache_ready")

    def detail(self) -> str:
        """Короткое описание состояния для логов/health."""
        if self._report is not None and self._report.detail:
            return f"{self._phase}: {self._report.detail}"
        return self._phase

    # ------------------------------------------------------------------
    # Шаг 1: данные
    # ------------------------------------------------------------------

    async def wait_for_cache(self, cache_ready_event: Any = None) -> bool:
        """Дождаться сигнала «данные в DuckDB + снапшот опубликован».

        Ждёт **сигнал**, а не время: возвращается ровно тогда, когда
        ``PgDuckDbSyncService`` отработал первый цикл (initial_load +
        publish). Пока возврата нет, каналы не стартуют.

        Args:
            cache_ready_event: объект с ``wait()`` — ожидание сигнала
                готовности. Обычно ``ThreadSafeSignal`` (выставляется из
                worker-треда синхронизации). ``None`` — сервиса
                синхронизации нет (аудит выключен), ждать нечего.

        Returns:
            ``True`` — сигнал получен (или ожидание не требовалось).

        Raises:
            StartupGateError: ``on_unavailable="fail"`` и прогрев
                выключен не должен быть — см. :meth:`load_vectors`;
                само ожидание исключений не поднимает.
        """
        if not self.config.enabled:
            self._finish(
                StartupGateReport(
                    phase="skipped",
                    ok=True,
                    detail="disabled (gateway.startup.vector_preload.enabled=false)",
                    policy=self.config.on_unavailable,
                )
            )
            return False
        if cache_ready_event is None:
            # Нет сервиса синхронизации — нечего ждать; векторы, если
            # они есть, строятся из уже заполненного кэша.
            return True

        logger.info(
            "startup gate: waiting for PG→DuckDB initial load "
            "(channels stay closed until it completes)"
        )
        await cache_ready_event.wait()
        self._phase = "cache_ready"
        logger.info("startup gate: cache ready, building vector indexes")
        return True

    # ------------------------------------------------------------------
    # Шаг 2: векторы
    # ------------------------------------------------------------------

    async def load_vectors(self) -> StartupGateReport:
        """Дождаться сборки FAISS-индексов из DuckDB-кэша.

        Не поднимает исключений от ``preload_vector_indexes`` — отказ
        превращается в отчёт, чтобы политика ``on_unavailable`` решала
        дальше.

        Returns:
            ``StartupGateReport``; ``ok=True`` означает «векторы готовы,
            можно принимать вопросы».

        Raises:
            StartupGateError: при ``on_unavailable="fail"`` и ``ok=False``.
        """
        if not self.config.enabled:
            return self._report or self._finish(
                StartupGateReport(
                    phase="skipped",
                    ok=True,
                    detail="disabled (gateway.startup.vector_preload.enabled=false)",
                    policy=self.config.on_unavailable,
                )
            )
        if self._cache_store is None:
            return self._finish(
                StartupGateReport(
                    phase="skipped",
                    ok=True,
                    detail="no cache_store (sync/audit disabled)",
                    policy=self.config.on_unavailable,
                )
            )
        if self._preload_service is None:
            return self._finish(
                StartupGateReport(
                    phase="skipped",
                    ok=True,
                    detail="no preload_service",
                    policy=self.config.on_unavailable,
                )
            )

        started = time.monotonic()
        try:
            loaded = await self._preload_service.preload_vector_indexes(
                self._cache_store
            )
        except Exception as exc:  # noqa: BLE001
            return self._raise_or_report(
                self._unavailable(
                    detail=(
                        f"vector index build failed: {type(exc).__name__}: {exc}"
                    ),
                    started=started,
                )
            )

        errors = self._preload_errors()
        if loaded is None:
            # ``preload_vector_indexes`` возвращает None, когда store не
            # готов — это отказ, а не «просто нет данных».
            return self._raise_or_report(
                self._unavailable(
                    detail="cache_store not ready (preload skipped)",
                    started=started,
                    errors=errors,
                )
            )

        return self._finish(
            StartupGateReport(
                phase="vectors_ready",
                ok=True,
                loaded=list(loaded),
                errors=errors,
                policy=self.config.on_unavailable,
                duration_sec=time.monotonic() - started,
            )
        )

    # ------------------------------------------------------------------
    # Композиция шагов
    # ------------------------------------------------------------------

    async def prepare(self, cache_ready_event: Any = None) -> StartupGateReport:
        """Шаги 1 и 2 подряд — для тестов и не-gateway точек входа.

        Raises:
            StartupGateError: при ``on_unavailable="fail"`` и ``ok=False``.
        """
        await self.wait_for_cache(cache_ready_event)
        return await self.load_vectors()

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _unavailable(
        self,
        *,
        detail: str,
        started: float,
        errors: list[dict[str, Any]] | None = None,
    ) -> StartupGateReport:
        # ``fail`` — векторы обязательны, это ошибка уровня ERROR;
        # ``warn`` — сознательный degraded-старт.
        log = (
            logger.error if self.config.on_unavailable == "fail"
            else logger.warning
        )
        log(
            "startup gate: vectors unavailable (%s, policy=%s)",
            detail, self.config.on_unavailable,
        )
        return StartupGateReport(
            phase="unavailable",
            ok=False,
            loaded=[],
            errors=list(errors or self._preload_errors()),
            detail=detail,
            policy=self.config.on_unavailable,
            duration_sec=time.monotonic() - started,
        )

    def _raise_or_report(
        self, report: StartupGateReport
    ) -> StartupGateReport:
        """Зафиксировать отчёт и, при политике ``fail``, поднять ошибку."""
        self._finish(report)
        if report.policy == "fail":
            raise StartupGateError(report)
        return self._report or report

    def _preload_errors(self) -> list[dict[str, Any]]:
        getter = getattr(self._cache_store, "preload_errors", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception as exc:  # noqa: BLE001
            logger.warning("preload_errors() failed: %s", exc)
            return []

    def _finish(self, report: StartupGateReport) -> StartupGateReport:
        """Зафиксировать отчёт: фаза, лог, событие в БД, callback."""
        self._phase = report.phase
        self._report = report
        logger.info("startup gate: %s", report.summary())
        self._log_event(report)
        if self._on_report is not None:
            try:
                self._on_report(report)
            except Exception as exc:  # noqa: BLE001
                logger.warning("startup gate on_report failed: %s", exc)
        return report

    def _log_event(self, report: StartupGateReport) -> None:
        """Записать ``startup_vector_preload`` в ``agent_gateway_logs``.

        Долговечный след для ``history_search``: видно, стартовал ли
        gateway с векторами или degraded. Отсутствие
        ``DbLoggingService`` (CLI, тесты) — не ошибка.
        """
        try:
            import lib.services.db_logging_service as _svc

            _svc.try_log_event(
                self._db_logging_service,
                LogEvent(
                    event_type=STARTUP_PRELOAD_EVENT,
                    level="INFO" if report.ok else "WARN",
                    session_id="gateway:startup",
                    channel=None,
                    actor="gateway",
                    name=STARTUP_PRELOAD_EVENT,
                    summary=report.summary(),
                    payload={
                        "phase": report.phase,
                        "ok": report.ok,
                        "loaded": [
                            {
                                "index_name": it.get("index_name"),
                                "vectors": it.get("vectors"),
                            }
                            for it in report.loaded
                        ],
                        "errors": [
                            {
                                "index_name": err.get("index_name"),
                                "error_type": err.get("error_type"),
                            }
                            for err in report.errors
                        ],
                        "detail": report.detail,
                        "policy": report.policy,
                        "duration_sec": round(report.duration_sec, 3),
                    },
                ),
                producer="StartupGate",
                event_type=STARTUP_PRELOAD_EVENT,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("startup gate event not logged: %s", exc)


__all__ = [
    "GatePhase",
    "STARTUP_PRELOAD_EVENT",
    "StartupGate",
    "StartupGateError",
    "StartupGateReport",
    "ThreadSafeSignal",
    "UnavailablePolicy",
    "VectorPreloadConfig",
    "read_vector_preload_config",
]