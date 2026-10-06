"""Регрессии на два F821, найденные при снятии линтового долга платформы.

Обе правки закрывали не стиль, а настоящий баг: обращение к имени, которого
нет в области видимости. F821 такие места не показывал громче остальных, а
тестов на них не было — именно поэтому они и дожили до линта. Тесты нужны
именно поэтому, а не ради самого факта «линт зелёный»: без них следующая
перестановка импортов снова сделает путь тихо падающим.

* ``MappedOutlineCandidate.to_heading_candidate`` конструировал
  ``HeadingCandidate`` — имя, не импортированное в этом модуле. Вызов
  заканчивался ``NameError``, то есть ровно на success-ветке. Соседняя
  функция ``mapped_to_heading_candidates`` тот же объект строила через
  отложенный импорт, поэтому чинить пришлось тем же способом.
* ``chat`` в LLM-клиенте ссылался на ``_LLM_TRACE_ENABLED`` — имени,
  не существующего нигде в репозитории. Ветка «модель вернула пустой ответ»
  (единственный сигнал о потере данных в map-reduce) падала с
  ``NameError`` вместо предупреждения в stderr. Флаг трассировки в модуле
  называется ``_trace_enabled()``.
"""

from __future__ import annotations

from libs.legal_summarizer.document.pdf_outline import (
    MappedOutlineCandidate,
    StructureAnchor,
)
from libs.legal_summarizer.llm import client as llm_client


def _mapped(**overrides) -> MappedOutlineCandidate:
    fields = {
        "block_index": 3,
        "text": "Глава 1",
        "level": 1,
        "score": 0.95,
        "anchor": StructureAnchor(block_ordinal=3, page_index=1),
    }
    fields.update(overrides)
    return MappedOutlineCandidate(**fields)


def test_to_heading_candidate_builds_the_candidate() -> None:
    """Успешно mapped кандидат превращается в ``HeadingCandidate``."""
    candidate = _mapped().to_heading_candidate()

    assert candidate is not None
    assert candidate.block_index == 3
    assert candidate.text == "Глава 1"
    assert candidate.level == 1
    assert candidate.source == "pdf_outline"


def test_to_heading_candidate_without_anchor_is_none() -> None:
    """Провалившийся mapping даёт ``None``, а не исключение."""
    assert _mapped(block_index=-1, anchor=None).to_heading_candidate() is None


def test_chat_references_a_trace_gate_that_exists() -> None:
    """``chat`` не ссылается на глобал, которого в модуле нет.

    Проверяется байт-код, а не исходник: ``co_names`` — это имена, которые
    функция грузит из глобального пространства, то есть ровно те, чьё
    отсутствие даёт ``NameError`` в рантайме. Переписывание строки с
    ``f``-префиксом или перенос выражения тест не задевает.

    Раньше здесь стояло ещё ``assert "_trace_enabled" in referenced``: пустой
    ответ писался в stderr под этим флагом, и падение того вывода выглядело
    бы как «диагностика молча пропала». Теперь такой вывод не нужен — ответ
    без текста поднимается как отказ вызова с названной причиной, а размер
    ответа и без того виден в ``[llm-trace] done`` — и требование «функция
    обязана звать флаг» стало бы требованием звать его ради зова. Остаётся то,
    ради чего проба писалась: ссылок на несуществующие глобалы нет.
    """
    referenced = set(llm_client.chat.__code__.co_names)

    dangling = {name for name in referenced if name.startswith("_LLM_TRACE")}
    assert not dangling, f"chat ссылается на несуществующие глобалы: {dangling}"
    assert callable(llm_client._trace_enabled)
