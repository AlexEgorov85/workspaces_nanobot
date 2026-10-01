"""``build_faiss_index`` возвращает минимальный meta.

**Страж портирован из агента** (``tests/test_remove_vector_index_store_guards.py``,
класс ``TestBuildFaissIndexMinimalMeta``) в момент удаления кластера снимка из
агента, 2026-10-01.

Инвариант: в meta попадает **только** метрика. Каждая запись индекса несёт
``content``, ``search_text`` и ``row_data`` — это десятки килобайт на вектор.
Положенное в meta, оно уезжает в память процесса на всё время жизни индекса и
дублирует то, что при гидрации результата и так читается из снимка. Возвращая
из meta хотя бы одно тяжёлое поле, индекс на сотни тысяч векторов начинает
есть память, не нужную никому, и не выглядит при этом сломанным — просто
медленнее.

Тест на платформе нужен потому, что реализация переехала сюда вместе с
инвариантом, а охранял его агентский файл.
"""

from __future__ import annotations

from libs.vectors.indexing import build_faiss_index

#: Запись «как приходит из снимка»: эмбеддинг плюс тяжёлые поля payload'а.
#: Размеры подобраны так, чтобы случайная утечка meta была заметна по памяти
#: и не зависела от конкретного размера чанка.
HEAVY_RECORD = {
    "source": "s",
    "table": "t",
    "pk_value": 1,
    "embedding": [0.1] * 4,
    "content": "VERY HEAVY CONTENT " * 1000,
    "search_text": "VERY HEAVY SEARCH " * 1000,
    "row_data": {"big": "x" * 10000},
}


class TestMetaIsMinimal:
    def test_meta_contains_only_metric(self) -> None:
        index, meta = build_faiss_index([HEAVY_RECORD], metric="cosine")

        assert index is not None
        assert meta == {"metric": "cosine"}

    def test_meta_does_not_grow_with_record_count(self) -> None:
        """Meta не растёт вместе с числом записей.

        Проверка на форму, а не на конкретные поля: если завтра в meta попадёт
        ``pk_values`` или ``sources``, первый тест поймает это по составу, а
        этот — по тому, что список вообще перестал быть списком метрик.
        """
        records = [
            {**HEAVY_RECORD, "pk_value": i, "embedding": [0.1 * (i + 1)] * 4}
            for i in range(5)
        ]

        _, meta = build_faiss_index(records, metric="cosine")

        assert meta is not None
        assert len(meta) == 1
        assert all(isinstance(v, str) for v in meta.values()), (
            f"в meta ожидались только строковые значения, получено {meta}"
        )

    def test_metric_none_is_still_recorded(self) -> None:
        """``metric=None`` — не «метрики нет», а явное inner_product."""
        _, meta = build_faiss_index([HEAVY_RECORD], metric=None)

        assert meta == {"metric": None}
