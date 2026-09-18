"""PreloadService — прогрев FAISS-индексов при старте агента.

Тонкий сервис: только runtime-метод ``preload_vector_indexes(store)``,
который gateway вызывает после initial sync, чтобы FAISS-индексы
были готовы к первому запросу.

Дополнительно: после прогона ``store.preload_indexes()`` сервис
считает health-summary (declared vs loaded vs missing vs orphan vs
stale) и:

  * печатает multi-line резюме в **stderr** — оператор gateway видит
    состояние vector-индексов сразу в терминале;
  * пишет одно событие в ``public.agent_gateway_logs`` через
    ``DbLoggingService.try_log_event`` — для последующего
    grep / SQL / CI-алёртов.

Legacy-методы ``preload_audit_cache`` / ``background_audit_cache_refresh``
/ ``start_audit_cache_tasks`` / ``stop_tasks`` / ``get_audit_cache_config``
/ ``_audit_settings`` удалены в рефакторинге
``refactor/core-extract-duckdb-faiss``: единственный писатель
``audit_cache.duckdb`` теперь — ``DuckDbCacheStore.publish()`` через
gateway (PgDuckDbSyncService → in-memory mirror → snapshot file). CLI-агент
остаётся чистым читателем.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def _emit_health_event(
    summary: str,
    payload: dict[str, Any],
    *,
    level: str,
    service: Any | None,
) -> None:
    """Один-в-one emit в ``agent_gateway_logs`` через ``DbLoggingService``.

    Единственный writer — ``DbLoggingService`` (через
    :func:`lib.services.db_logging_service.try_log_event`).
    """
    from lib.services.db_logging_service import LogEvent, try_log_event

    log_event = LogEvent(
        event_type="vector_index_preload_health",
        level=level,
        session_id="gateway:sync",
        channel=None,
        actor="sync",
        name="vector_index_preload_health",
        summary=summary,
        payload=payload,
    )
    try_log_event(
        service,
        log_event,
        producer="PreloadService",
        event_type="vector_index_preload_health",
    )


def _format_lines(
    declared_names: list[str],
    loaded_items: list[dict[str, Any]],
    missing: list[str],
    orphan: list[str],
    stale: list[str],
) -> list[str]:
    """Human-readable multi-line для терминала.

    Цвет/жирность не навешиваем (нет ANSI на Windows-cmd). Только текст.
    """
    def fmt_loaded() -> str:
        if not loaded_items:
            return "—"
        return ", ".join(
            f"{it['index_name']}({it.get('vectors', '?')})"
            for it in sorted(
                loaded_items,
                key=lambda x: x.get("index_name") or "",
            )
        )

    return [
        "[vector] preload health summary:",
        f"  declared ({len(declared_names)}): "
        f"{', '.join(declared_names) or '—'}",
        f"  loaded   ({len(loaded_items)}): {fmt_loaded()}",
        f"  missing  ({len(missing)}): {', '.join(missing) or '—'}",
        f"  orphan   ({len(orphan)}): {', '.join(orphan) or '—'}",
        f"  stale    ({len(stale)}): {', '.join(stale) or '—'}",
    ]


def compute_index_health(
    declared: dict[str, Any],
    loaded: list[dict[str, Any]] | None,
    runtime_rows: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Pure-функция: посчитать declared/loaded/missing/orphan/stale.

    Args:
        declared: результат ``read_vector_index_config({})``.
        loaded: то, что вернул ``store.preload_indexes()`` (или ``None`` /
            пустой list при ошибке). После change
            ``remove-vector-index-store`` может содержать
            ``signature_status`` (CURRENT/STALE/INVALID) на каждой
            записи — это inline-вычисленный статус.
        runtime_rows: то, что вернул ``list_runtime_vector_indexes()``
            (теперь читает из DuckDB-снапшота ``<storage_table>``,
            DISTINCT source). Может быть ``None`` при недоступности.

    Returns:
        dict с ``declared_names``, ``loaded_items``, ``missing``,
        ``orphan``, ``stale`` (все отсортированы), ``divergence`` —
        bool, ``level`` — ``"INFO"`` или ``"WARN"``.
    """
    declared_names = sorted(declared.keys())

    loaded_items: list[dict[str, Any]] = (
        sorted(loaded, key=lambda x: x.get("index_name") or "")
        if loaded
        else []
    )
    loaded_names = {it.get("index_name") for it in loaded_items if it.get("index_name")}

    missing = sorted(n for n in declared_names if n not in loaded_names)

    if runtime_rows:
        runtime_names = sorted(
            r.get("source") for r in runtime_rows if r.get("source")
        )
        orphan = sorted(n for n in runtime_names if n not in declared)
        stale: list[str] = []
        for it in loaded_items:
            name = it.get("index_name")
            if not name or name not in declared:
                continue
            status = it.get("signature_status")
            if status in ("STALE", "INVALID"):
                stale.append(f"{name}:{status}")
    else:
        orphan = []
        stale = []

    divergence = bool(missing) or bool(orphan) or bool(stale)
    return {
        "declared_names": declared_names,
        "loaded_items": loaded_items,
        "missing": missing,
        "orphan": orphan,
        "stale": stale,
        "divergence": divergence,
        "level": "WARN" if divergence else "INFO",
    }


class PreloadService:
    """Runtime-сервис: прогрев FAISS-индексов в память.

    Этот сервис НЕ знает о цикле запуска — он предоставляет async-метод,
    который точка входа (``gateway.py``) запускает после ``PgDuckDbSyncService``
    initial_load. Имя и API сохранены для back-compat (см.
    TARGET_ARCHITECTURE.md §34 — KEEP).
    """

    def __init__(
        self,
        settings: Any = None,
        db_logging_service: Any | None = None,
    ) -> None:
        self._settings = settings
        self._db_logging_service = db_logging_service

    async def preload_vector_indexes(self, store: Any) -> list | None:
        """Прогреть FAISS-индексы из DuckDB-кэша в память (gateway).

        ``store.preload_indexes()`` — тяжёлая синхронная операция
        (читает из DuckDB, строит FAISS-индексы для каждого источника
        в ``vector_db_table``). Запускаем в ``asyncio.to_thread``, чтобы
        не блокировать event loop.

        Вызывающий (обычно ``gateway.py``) ОБЯЗАН дождаться
        ``PgDuckDbSyncService`` initial_load ПЕРЕД этим методом (иначе
        DuckDB пуст и preload вернёт ``[]``). В gateway это решается
        через ``asyncio.Event`` + таймаут 30с.

        После успешного (или неудачного) preload печатает health
        summary в **stderr** и пишет событие в ``agent_gateway_logs``.
        Эти шаги НЕ зависят от успеха preload — даже если ``loaded is None``
        (PG недоступна, embed отсутствуют и т.п.), summary всё равно
        считается по declared + runtime state и пишется. Это даёт
        оператору полную картину divergence на старте.

        Returns:
            Список построенных индексов вида
            ``[{"index_name": ..., "vectors": N}, ...]`` или ``None``,
            если ``store is None`` / ``not is_ready()`` / исключение.
        """
        if store is None or not store.is_ready():
            return None
        try:
            loaded = await asyncio.to_thread(store.preload_indexes)
        except Exception as exc:
            logger.warning(
                "PreloadService.preload_vector_indexes failed: %s", exc,
            )
            loaded = None

        # Health summary — всё равно считаем, даже при ``loaded is None``
        # (например, store недоступен → divergence всё равно видно через
        # declared vs runtime).
        try:
            self._emit_health_summary(loaded)
        except Exception as exc:  # noqa: BLE001
            logger.warning("vector index health summary failed: %s", exc)

        return loaded

    def _emit_health_summary(self, loaded: list | None) -> None:
        """Печать в stderr + запись в ``agent_gateway_logs``.

        ``declared`` берём из JSON (read_vector_index_config).
        ``runtime`` — из PG store (list_runtime_vector_indexes).

        Любые ошибки PG/config глотаем — health summary **никогда**
        не должна валить gateway startup. Лучше без summary, чем без
        gateway.
        """
        # Сбор declared
        try:
            from lib.services.cache_provider_impl import (
                read_vector_index_config,
                list_runtime_vector_indexes,
            )
            declared = read_vector_index_config({}) or {}
        except Exception:  # noqa: BLE001
            declared = {}

        # Сбор runtime
        try:
            runtime_rows = list_runtime_vector_indexes()
        except Exception:  # noqa: BLE001
            runtime_rows = None

        health = compute_index_health(
            declared=declared,
            loaded=loaded,
            runtime_rows=runtime_rows,
        )

        # --- terminal ---
        lines = _format_lines(
            declared_names=health["declared_names"],
            loaded_items=health["loaded_items"],
            missing=health["missing"],
            orphan=health["orphan"],
            stale=health["stale"],
        )
        try:
            sys.stderr.write("\n".join(lines) + "\n")
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass

        # --- DB log ---
        summary = (
            f"declared={len(health['declared_names'])} "
            f"loaded={len(health['loaded_items'])} "
            f"missing={len(health['missing'])} "
            f"orphan={len(health['orphan'])} "
            f"stale={len(health['stale'])}"
        )
        payload: dict[str, Any] = {
            "declared": health["declared_names"],
            "loaded": [
                {"index_name": it.get("index_name"), "vectors": it.get("vectors")}
                for it in health["loaded_items"]
            ],
            "missing": health["missing"],
            "orphan": health["orphan"],
            "stale": health["stale"],
        }
        _emit_health_event(
            summary=summary,
            payload=payload,
            level=health["level"],
            service=self._db_logging_service,
        )
