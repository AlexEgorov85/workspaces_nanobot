"""Владелец векторных индексов: сборка по требованию, одна на процесс.

Портировано из агента: ``lib/services/duckdb_cache_store.py``
(``_load_source_index`` / ленивая пересборка ``search_vector``) и
``lib/services/preload_service.py`` (сборка всех индексов). Удаление агентской
копии — фазы 4/5/9.

**Сборка происходит на старте сервера, а не на первом запросе.** Вызывает её
``servers/enterprise/server.py::_prepare_capabilities``: для каждого объявленного
и включённого индекса вызывается :meth:`ensure_index` до старта event loop, и к
моменту обслуживания запросов индексы уже в памяти. Здесь, во владельце, сборка
остаётся по требованию (:meth:`ensure_index`) — это позволяет и прогреву, и
восстановлению после ошибки идти одним и тем же путём, а не двумя
разными реализациями «собрать индекс».

Почему так, а не наоборот:

* первый же поиск не платит за чужую подготовку — на большом снимке это секунды
  молчания на горячем пути;
* старт становится честной проверкой: индекс, который не собрался, известен до
  первого запроса, а не посреди оборота;
* состояние наблюдаемо: ``missing`` / ``building`` / ``ready`` / ``error``
  возвращается в ответе, поэтому «индекс не поднят» видно, а не прячется за
  пустой выдачей.

Синхронизация — single-flight: первый вызов берёт локер и строит, остальные
блокируются на том же локере и после пробуждения видят готовый индекс. Сборок
за прогон ровно одна (счётчик ``builds`` это фиксирует, на нём стоит тест).
Прогрев на старте — обычные вызовы того же метода, поэтому он тоже даёт ровно
одну сборку на индекс.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from libs.enterprise_common.errors import InfrastructureError, NotFoundError
from libs.enterprise_data.snapshot.contracts import (
    IndexIntegrityError,
    SearchResult,
)
from libs.vectors.embedding import Embedder
from libs.vectors.grouping import build_raw_items, group_vector_hits
from libs.vectors.indexing import build_faiss_index, vector_dimension
from libs.vectors.signature import compute_index_signature, verify_index_signature

#: Наблюдаемые состояния индекса. Возвращаются в ответах capability.
STATE_MISSING = "missing"
STATE_BUILDING = "building"
STATE_READY = "ready"
STATE_ERROR = "error"


class SnapshotReader(Protocol):
    """Всё, что владелец индексов умеет просить у снимка.

    Структурный тип намеренно: снимок принадлежит capability ``data``, и
    владелец индексов получает её оттуда (``container.get("data")``), а не
    открывает файл сам. DuckDB здесь не импортируется.
    """

    def vector_source_stats(self) -> list[dict[str, Any]]:
        """``[{"source", "vector_count", "embedding_sample"}, ...]``."""
        ...

    def fetch_source_vectors(self, source: str) -> list[dict[str, Any]]:
        """Строки векторного хранилища одного источника."""
        ...

    def fetch_chunk_payload(
        self, source: str, pk_value: Any, chunk_index: int
    ) -> dict[str, Any]:
        """Текст и исходная строка одного чанка (``{}`` — чанка нет)."""
        ...


@dataclass
class _IndexSlot:
    """Состояние одного индекса. Читается без локера (значения атомарны)."""

    name: str
    state: str = STATE_MISSING
    index: Any = None
    meta: dict[str, Any] | None = None
    built_at: datetime | None = None
    queries: int = 0
    builds: int = 0
    error: str = ""
    error_code: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


class VectorIndexOwner:
    """Владелец FAISS-индексов одного процесса.

    Экземпляр создаётся **один раз** (в конструкторе capability) — от этого
    зависит «одна сборка на процесс». Конструктор ничего не читает и не строит.
    """

    def __init__(
        self,
        *,
        snapshot: SnapshotReader,
        embed: Embedder | None = None,
        declared: dict[str, Any] | None = None,
        current_index_config: dict[str, Any] | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._declared = dict(declared or {})
        #: Подпись конфигурации, под которой индекс считается актуальным.
        self._current_index_config = current_index_config or {}
        self._slots: dict[str, _IndexSlot] = {}
        self._build_locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        # Эмбеддер обязателен и подставляется сверху: у владельтеля индексов
        # нет ни адреса провайдера, ни HTTP-клиента — вызов эмбеддера
        # принадлежит capability ``llm``. Владелец без эмбеддера не может
        # выполнить поиск, поэтому его отсутствие — ошибка сборки, а не
        # «поиск без эмбеддинга».
        if embed is None:
            raise InfrastructureError(
                "владелец индексов требует эмбеддер: подключите сервис "
                "capability llm (VectorIndexOwner(embed=...))",
                code="embedding_unavailable",
            )
        self._embed = embed

    # ------------------------------------------------------------------
    # Наблюдение без сборки
    # ------------------------------------------------------------------

    def known_names(self) -> list[str]:
        """Объявленные индексы + источники, реально присутствующие в снимке."""
        names = set(self._declared)
        for row in self._snapshot.vector_source_stats():
            source = row.get("source")
            if source:
                names.add(source)
        return sorted(names)

    def state(self, name: str) -> str:
        """Текущее состояние индекса (без сборки)."""
        with self._guard:
            slot = self._slots.get(name)
        if slot is not None:
            return slot.state
        return STATE_MISSING if self._declared.get(name) else STATE_MISSING

    def _source_row(self, name: str) -> dict[str, Any] | None:
        for row in self._snapshot.vector_source_stats():
            if row.get("source") == name:
                return row
        return None

    def describe(self, name: str) -> dict[str, Any]:
        """Описание индекса **без** его сборки.

        Отвечает и для ещё не построенного индекса: размерность и количество
        векторов берутся из строк снимка, а не из FAISS. Это и есть требование
        «``list_indexes``/``index_stats`` отвечают, не поднимая индекс».
        """
        row = self._source_row(name)
        declared_cfg = self._declared.get(name)
        if row is None and declared_cfg is None:
            raise NotFoundError(
                f"векторный индекс {name!r} не объявлен и не найден в снимке"
            )
        with self._guard:
            slot = self._slots.get(name)
        state = slot.state if slot else STATE_MISSING
        return {
            "index_name": name,
            "state": state,
            "declared": declared_cfg is not None,
            # Отдельное поле, а не вывод из ``declared``: объявленный индекс с
            # ``enabled=false`` объявлен, но прогревать его нельзя, и вызывающая
            # сторона обязана отличать одно от другого — иначе отключённый
            # индекс молча собирается на старте.
            "enabled": (
                bool(declared_cfg.get("enabled", True))
                if declared_cfg is not None
                else False
            ),
            "vector_count": (row or {}).get("vector_count") if row else 0,
            "dimension": (
                vector_dimension((row or {}).get("embedding_sample")) if row else None
            ),
            "metric": (declared_cfg or {}).get("metric"),
            "error": slot.error if slot else "",
            "error_code": slot.error_code if slot else "",
        }

    def list_indexes(self) -> list[dict[str, Any]]:
        """Все известные индексы с состоянием. Ни одного FAISS-индекса не строит."""
        return [self.describe(name) for name in self.known_names()]

    def index_stats(self, name: str) -> dict[str, Any]:
        """Метрики индекса: векторы, размерность, время сборки, счётчик запросов.

        Тоже **без** сборки: если индекс ещё не поднят, ``vectors`` равен
        размеру источника в снимке, а ``last_built_at`` — ``None``.
        """
        described = self.describe(name)
        with self._guard:
            slot = self._slots.get(name)
        return {
            **described,
            "vectors": described["vector_count"],
            "dim": described["dimension"],
            "last_built_at": slot.built_at.isoformat() if slot and slot.built_at else None,
            "queries": slot.queries if slot else 0,
            "builds": slot.builds if slot else 0,
        }

    def build_count(self, name: str) -> int:
        """Сколько раз индекс реально собирался (счётчик для тестов)."""
        with self._guard:
            slot = self._slots.get(name)
        return slot.builds if slot else 0

    # ------------------------------------------------------------------
    # Сборка
    # ------------------------------------------------------------------

    def _build_lock(self, name: str) -> threading.Lock:
        with self._guard:
            lock = self._build_locks.get(name)
            if lock is None:
                lock = threading.Lock()
                self._build_locks[name] = lock
            return lock

    def _slot(self, name: str) -> _IndexSlot:
        with self._guard:
            slot = self._slots.get(name)
            if slot is None:
                slot = _IndexSlot(name=name)
                self._slots[name] = slot
            return slot

    def ensure_index(self, name: str) -> _IndexSlot:
        """Вернуть готовый индекс, собрав его при необходимости (single-flight).

        Параллельные вызовы блокируются на локере индекса: собирает первый,
        остальные получают результат. Итог — ровно одна сборка.
        """
        slot = self._slot(name)
        if slot.state == STATE_READY:
            return slot

        with self._build_lock(name):
            # Повторная проверка под локером: пока ждали, индекс мог собрать
            # другой поток. Без неё N параллельных запросов дали бы N сборок.
            slot = self._slot(name)
            if slot.state == STATE_READY:
                return slot
            with self._guard:
                slot.state = STATE_BUILDING
            try:
                index, meta = self._build(name)
            except Exception as exc:
                with self._guard:
                    slot.state = STATE_ERROR
                    slot.error = str(exc)
                    slot.error_code = getattr(exc, "code", "") or type(exc).__name__
                raise
            with self._guard:
                slot.index = index
                slot.meta = meta
                slot.state = STATE_READY
                slot.built_at = datetime.now(UTC)
                slot.builds += 1
                slot.error = ""
                slot.error_code = ""
            return slot

    def _build(self, name: str) -> tuple[Any, dict[str, Any] | None]:
        """Собрать индекс источника из строк снимка.

        Raises:
            NotFoundError: источник не объявлен и в снимке отсутствует.
            IndexIntegrityError: подпись индекса не прошла проверку.
            InfrastructureError: строки есть, но векторы не разобрались.
        """
        records = self._snapshot.fetch_source_vectors(name)
        if not records:
            raise NotFoundError(
                f"в снимке нет векторов для индекса {name!r}: "
                "индекс не загружен или пуст"
            )
        metric = (self._declared.get(name) or {}).get("metric")
        index, meta = build_faiss_index(records, metric=metric)
        if index is None:
            raise InfrastructureError(
                f"не удалось собрать векторный индекс {name!r}: "
                "эмбеддинги отсутствуют или имеют разную размерность",
                code="invalid_index",
            )
        if meta is None:
            meta = {}
        # Подпись фиксируется при сборке и проверяется при каждом поиске:
        # смена конфигурации индекса не должна молча менять смысл выдачи.
        meta["signature"] = compute_index_signature(self._current_index_config)
        meta.setdefault("metadata", {})
        for i, rec in enumerate(records):
            meta["metadata"][str(i)] = {
                "source": rec.get("source") or name,
                "table": rec.get("table") or "",
                "pk_value": rec.get("pk_value", i),
                "chunk_index": rec.get("chunk_index") or 0,
                "chunk_count": rec.get("chunk_count") or 1,
            }
        return index, meta

    def preload(self) -> list[dict[str, Any]]:
        """Прогреть все известные индексы, не падая на одном плохом.

        Вариант «всё или ничего» для вызывающей стороны, которой нужен отчёт,
        а не отказ: упавший индекс молча пропускается и оставляет состояние
        ``error`` в слоте. Прогрев старта сервера этим методом **не**
        пользуется — он зовёт :meth:`ensure_index` поимённо, чтобы отличать
        «в снимке нет векторов» от «сборка упала» и называть индекс в отказе.
        """
        loaded: list[dict[str, Any]] = []
        for name in self.known_names():
            try:
                slot = self.ensure_index(name)
            except Exception:  # noqa: BLE001 - сводка прогрева не должна падать
                continue
            loaded.append({
                "index_name": name,
                "vectors": int(slot.index.ntotal),
                "signature_status": verify_index_signature(
                    slot.meta, self._current_index_config
                ),
            })
        return loaded

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------

    def search(
        self,
        text: str,
        index_name: str,
        *,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> list[SearchResult]:
        """Семантический поиск; индекс собирается, если ещё не собран.

        Raises:
            NotFoundError: индекса нет ни в объявлении, ни в снимке.
            IndexIntegrityError: подпись индекса ``STALE``/``INVALID``.
            InfrastructureError: снимок недоступен либо эмбеддер не ответил.
        """
        slot = self._ensure_known(index_name)
        status = verify_index_signature(slot.meta, self._current_index_config)
        if status in ("STALE", "INVALID"):
            # Не молчаливая деградация: агент получает отдельный код
            # (``stale_index``/``invalid_index``), а не «ничего не найдено».
            raise IndexIntegrityError(index_name, status, "index signature mismatch")

        embedding = self._embed(text)
        if embedding is None:
            raise InfrastructureError(
                "эмбеддер не вернул вектор запроса: поиск невозможен "
                "(это сбой инфраструктуры, а не пустой результат)",
                code="embedding_unavailable",
            )

        import numpy as np

        query_vec = np.array([embedding], dtype=np.float32)
        meta = slot.meta or {}
        if meta.get("metric") == "cosine":
            import faiss

            faiss.normalize_L2(query_vec)
        n = slot.index.ntotal if threshold is not None else min(top_k, slot.index.ntotal)
        scores, ids = slot.index.search(query_vec, n)

        raw = build_raw_items(
            meta.get("metadata", {}),
            scores,
            ids,
            index_name,
            threshold,
            fetch_fn=self._snapshot.fetch_chunk_payload,
        )
        results = group_vector_hits(raw, top_k, threshold)

        with self._guard:
            slot.queries += 1

        return [
            SearchResult(
                content=r["content"],
                score=r["score"],
                source=r["source"],
                table=r["table"],
                pk_value=r["pk_value"],
                chunk=r.get("chunk", ""),
                matched_chunks=r.get("matched_chunks", 1),
                row=r.get("row", {}),
            )
            for r in results
        ]

    def _ensure_known(self, name: str) -> _IndexSlot:
        """Проверить, что индекс существует, и вернуть готовый слот."""
        if name not in self.known_names():
            raise NotFoundError(
                f"векторный индекс {name!r} не объявлен и не найден в снимке"
            )
        return self.ensure_index(name)
