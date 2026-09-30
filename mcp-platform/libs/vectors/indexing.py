"""Построение FAISS-индекса из строк векторного хранилища.

Портировано из агента: ``lib/utils/duckdb_query.py`` (``build_faiss_index``,
``_as_vector``). Удаление агентской копии — фазы 4/5/9.

Здесь и только здесь платформа импортирует ``faiss``: индекс — разделяемый
тяжёлый ресурс, и владелец у него один. Хранилище снимка
(``libs/enterprise_data/snapshot``) FAISS не импортирует — оно отдаёт строки,
индекс собирает этот модуль.
"""

from __future__ import annotations

from typing import Any


def as_vector(raw: Any) -> list[float] | None:
    """Привести значение колонки ``embedding`` к списку чисел.

    В снимке колонка ``embedding`` хранится как ``VARCHAR``: тип ``REAL[]`` из
    PostgreSQL не переносится в DuckDB напрямую, и значение приходит текстом
    вида ``"[-0.04, 0.11, ...]"``. Раньше ``build_faiss_index`` ждал только
    ``list``/``tuple`` и на строке молча возвращал ``(None, None)`` — из-за чего
    vector search отвечал «не найдено» при полностью готовом индексе и доступном
    эмбеддере.

    Возвращает ``None``, если это не вектор: пустой список, мусор, незакрытая
    скобка. Пустой вектор — тоже не вектор: из него получился бы индекс
    размерности 0, который FAISS собрать не сможет, а отказ пришёл бы позже и
    из другого места. Незакрытая скобка означает обрезанное значение, и принять
    его значит построить индекс по молча укороченным векторам.
    """
    import numpy as np

    if raw is None:
        return None
    if isinstance(raw, (list, tuple, np.ndarray)):
        vector = [float(x) for x in raw]
        return vector or None
    if isinstance(raw, (bytes, bytearray)):
        try:
            raw = raw.decode("utf-8")
        except Exception:  # noqa: BLE001 - мусорные байты → «не вектор»
            return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.startswith("["):
            if not text.endswith("]"):
                return None
            text = text[1:-1].strip()
        if not text:
            return None
        try:
            vector = [float(part) for part in text.split(",")]
        except ValueError:
            return None
        return vector or None
    return None


def vector_dimension(raw: Any) -> int | None:
    """Размерность вектора из значения колонки ``embedding`` (``None`` — не вектор).

    Нужна там, где индекс строить **нельзя и не нужно**: ``list_indexes`` и
    ``index_stats`` обязаны отвечать, не поднимая FAISS, но обязаны показать
    размерность. Разбор живёт здесь, чтобы он был один.
    """
    vector = as_vector(raw)
    return len(vector) if vector else None


def build_faiss_index(
    records: list[dict[str, Any]],
    metric: str | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    """Построить FAISS ``IndexFlatIP`` + metadata из списка записей.

    ``records`` — список словарей с ключами: ``source``, ``table``,
    ``pk_value``, ``chunk_index``, ``chunk_count``, ``embedding`` (остальные
    колонки payload'а владельцу индекса не нужны: тяжёлые поля он читает из
    снимка отдельно, при гидрации результата).

    ``metric`` — ``"cosine"`` (нормализация L2 перед IP) или
    ``None``/``"inner_product"`` (без нормализации, raw IP — обратно совместимо
    с индексами до P0-2).

    Возвращает ``(index, metadata)`` или ``(None, None)`` (пусто/несоответствие
    размерности). Единая точка сборки FAISS платформы.
    """
    import faiss
    import numpy as np

    if not records:
        return None, None

    first = as_vector(records[0]["embedding"])
    if first is None:
        return None, None
    dimension = len(first)
    vectors = np.zeros((len(records), dimension), dtype=np.float32)
    metadata: dict[str, Any] = {"metric": metric}

    for i, rec in enumerate(records):
        emb = as_vector(rec["embedding"])
        if emb is None or len(emb) != dimension:
            return None, None
        vectors[i] = np.array(emb, dtype=np.float32)

    if metric == "cosine":
        faiss.normalize_L2(vectors)
    index = faiss.IndexFlatIP(dimension)
    index.add(vectors)
    return index, metadata
