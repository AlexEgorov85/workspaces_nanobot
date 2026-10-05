"""Capability ``vectors`` — владелец векторных индексов.

Сервис держит **один** :class:`~libs.vectors.owner.VectorIndexOwner` на процесс
и отдаёт его операциям. Это то, что делает «одна сборка индекса на процесс»
свойством, а не пожеланием: если бы владелец создавался в каждом файле
операции (так устроен ``create_tool(container)`` — он вызывается один раз на
файл), индекс собрался бы трижды.

**Файл снимка capability не открывает и пути к нему не знает.** Строки
приходят от capability ``data`` (``container.get("data")``), которая владеет
снимком. Если сервис ``data`` не зарегистрирован или снимок в него не
подключён — это ``InfrastructureError``, а не «индексов нет»: молчаливое
«ничего не нашлось» неотличимо от «данных нет вообще».

**Сборка индекса ленивая.** Ни одна операция этого модуля не поднимает FAISS,
кроме ``vector_search``; ``list_indexes`` и ``index_stats`` отвечают по данным
снимка. Состояние индекса (``missing``/``building``/``ready``/``error``)
возвращается в ответе, поэтому «индекс не поднят» наблюдаемо, а не спрятано
за пустой выдачей.
"""

from __future__ import annotations

import logging
from typing import Any

from libs.enterprise_common.container import ToolContainer
from libs.enterprise_common.errors import InfrastructureError
from libs.vectors.config import (
    read_embedding_config,
    read_embedding_defaults,
    read_vector_index_config,
)
from libs.vectors.embedding import Embedder
from libs.vectors.owner import VectorIndexOwner
from libs.vectors.signature import compute_index_signature

logger = logging.getLogger(__name__)

#: Имя сервиса capability ``data`` в контейнере — источник строк снимка.
DATA_SERVICE = "data"

#: Имя сервиса capability ``llm`` в контейнере — владелец HTTP к провайдеру.
LLM_SERVICE = "llm"

#: Индекс по умолчанию, если вызывающая сторона не указала имя.
DEFAULT_INDEX = "default_index"


def _read_embedding_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Достать объявленные параметры эмбеддинга из конфигурации платформы.

    Только то, что влияет на индекс: модель, размерность, таймаут. Адрес и
    ключ провайдера здесь не читаются — они принадлежат capability ``llm``.
    """
    gateway = (config or {}).get("gateway") or {}
    vector = gateway.get("vector") or {}
    embedding = vector.get("embedding") or {}
    return {
        "model": embedding.get("model"),
        "dimension": embedding.get("dimension"),
        "timeout_sec": embedding.get("timeout_sec"),
    }


def _index_signature_config(
    index_name: str,
    declared: dict[str, Any],
    embedding_config: dict[str, Any],
    chunk_defaults: dict[str, Any],
) -> dict[str, Any]:
    """Каноническая конфигурация индекса — вход подписи.

    Собирается и для ``VectorIndexOwner`` (проверка «не устарел ли индекс»), и
    для :meth:`VectorsService.index_signature`. Один источник, иначе подписи
    в двух местах разошлись бы и всегда читались бы как ``STALE``.
    """
    cfg: dict[str, Any] = {
        "embedding_model": embedding_config.get("model"),
        "embedding_dimension": embedding_config.get("dimension"),
        "chunk_size": chunk_defaults.get("chunk_size"),
        "chunk_overlap": chunk_defaults.get("chunk_overlap"),
    }
    declared_cfg = declared.get(index_name)
    if declared_cfg:
        cfg.update(
            {
                "src_table": declared_cfg.get("source_table")
                or declared_cfg.get("table"),
                "pk_column": declared_cfg.get("pk"),
                "content_cols": declared_cfg.get("content_columns"),
                "embedding_cols": declared_cfg.get("embedding_columns"),
                "track_column": declared_cfg.get("track_column"),
                "metric": declared_cfg.get("metric"),
            }
        )
    return cfg


class VectorsService:
    """Семантический поиск по снимку и состояние векторных индексов."""

    def __init__(
        self,
        *,
        container: ToolContainer,
        config: dict[str, Any] | None = None,
        data_service: Any | None = None,
        embed: Any | None = None,
    ) -> None:
        self._container = container
        self._config = config or {}
        self._data_service = data_service
        self._embed = embed
        self._llm: Any | None = None

        self._declared = read_vector_index_config(self._config)
        self._embedding_config = read_embedding_config(
            _read_embedding_overrides(self._config)
        )
        self._chunk_defaults = read_embedding_defaults()
        self._current_index_config = _index_signature_config(
            DEFAULT_INDEX, self._declared, self._embedding_config,
            self._chunk_defaults,
        )

        # Владелец создаётся один раз, в конструкторе. Он ничего не читает и
        # не строит: сборку запускает старт сервера
        # (``_prepare_capabilities``), чтобы первый же поиск не платил за неё.
        # Эмбеддер подключается здесь же, а не на первом поиске: отсутствие
        # сервиса llm — ошибка сборки, и ждать её на первом запросе означало бы
        # отдать агенту «индексов нет» там, где на самом деле нет эмбеддера.
        self._owner = VectorIndexOwner(
            snapshot=self._snapshot(),
            embed=self._embedder(),
            declared=self._declared,
            current_index_config=self._current_index_config,
        )

    # ------------------------------------------------------------------
    # Эмбеддер: только через capability llm
    # ------------------------------------------------------------------

    def _embedder(self) -> Embedder:
        """Колбэк эмбеддера поверх сервиса ``llm``.

        Владелец индексов не знает адреса провайдера и не держит HTTP-клиента:
        векторизация — тоже вызов провайдера, и он принадлежит capability
        ``llm``. Явный ``embed=`` (тест, другая реализация) имеет приоритет.

        Raises:
            InfrastructureError: сервис ``llm`` не зарегистрирован или не умеет
                векторизацию. Молчаливый возврат «ничего не найдено» здесь
                означал бы, что агент потратит итерации на несуществующую выдачу.
        """
        if self._embed is not None:
            return self._embed
        if self._llm is None:
            service = self._container.get(LLM_SERVICE)
            if not hasattr(service, "embed"):
                raise InfrastructureError(
                    f"сервис {LLM_SERVICE!r} не умеет векторизацию: "
                    "не найден метод embed"
                )
            self._llm = service
        llm = self._llm

        def _call_llm(text: str) -> list[float] | None:
            try:
                result = llm.embed(text=text, audience="runtime")
            except InfrastructureError:
                raise
            except Exception as exc:  # noqa: BLE001 - наружу доменная ошибка
                raise InfrastructureError(
                    f"эмбеддер не ответил: {exc}", code="embedding_unavailable"
                ) from exc
            return list(result.vector)

        return _call_llm

    # ------------------------------------------------------------------
    # Снимок: только через capability data
    # ------------------------------------------------------------------

    def _data(self) -> Any:
        if self._data_service is not None:
            return self._data_service
        return self._container.get(DATA_SERVICE)

    def _snapshot(self) -> Any:
        """Снимок из capability ``data``.

        Raises:
            InfrastructureError: capability ``data`` не зарегистрирована или
                в ней не подключён снимок.
        """
        data = self._data()
        # Наличие чтения снимка проверяется один раз, при создании владельца:
        # сервис без снимка должен упасть здесь, а не на первом же запросе.
        if not hasattr(data, "vector_source_stats"):
            raise InfrastructureError(
                f"сервис {DATA_SERVICE!r} не отдаёт снимок: не найдены методы "
                "чтения снимка (vector_source_stats и др.)"
            )
        return data

    # ------------------------------------------------------------------
    # Операции
    # ------------------------------------------------------------------

    def vector_search(
        self,
        query: str,
        index_name: str = DEFAULT_INDEX,
        top_k: int = 5,
        threshold: float | None = None,
    ) -> list[Any]:
        """Семантический поиск. Индекс для этого имени собирается лениво, при
        первом обращении к нему (владелец индексов); на старте не собирается
        ничего — см. ``libs/vectors/owner.py``.

        ``ensure_index`` внутри остаётся не как ленивая загрузка, а как
        восстановление: индекс мог быть удалён или помечен ошибкой уже после
        старта, и молча отдать «ничего не нашлось» там, где индекс есть, —
        хуже пересборки.
        """
        return self._owner.search(
            query, index_name, top_k=max(1, int(top_k)), threshold=threshold
        )

    def list_indexes(self) -> list[dict[str, Any]]:
        """Все известные индексы и их состояние. FAISS не поднимается."""
        return self._owner.list_indexes()

    def index_stats(self, index_name: str) -> dict[str, Any]:
        """Метрики индекса. FAISS не поднимается."""
        return self._owner.index_stats(index_name)

    def state(self, index_name: str) -> str:
        """Состояние индекса (``missing``/``building``/``ready``/``error``)."""
        return self._owner.state(index_name)

    def build_count(self, index_name: str) -> int:
        """Сколько раз индекс собирался (диагностика)."""
        return self._owner.build_count(index_name)

    @property
    def owner(self) -> VectorIndexOwner:
        """Владелец индексов (для диагностики и тестов)."""
        return self._owner

    def index_signature(self, index_name: str) -> str:
        """Подпись конфигурации индекса (для сопоставления снимков)."""
        return compute_index_signature(
            _index_signature_config(
                index_name,
                self._declared,
                self._embedding_config,
                self._chunk_defaults,
            )
        )
