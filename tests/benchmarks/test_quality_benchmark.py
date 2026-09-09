"""Quality benchmark с golden required_facts dataset.

Структурная проверка golden dataset — все 4 теста ниже проверяют только
**согласованность данных в самом тесте** (уникальные имена, наличие
required_facts, содержание фактов в тексте, извлекаемость FACT_NNN).
Они не требуют запуска pipeline.

Тесты quality runner (honest mock / bad mock / summary report) были
удалены (Этап G remediation, 2026-09-09) — они проверяли несуществующую
функциональность (``import legal_summarizer.application.service as
summarizer``; этот namespace package не существует в текущей структуре
legal_summarizer — правильный путь
``workspace.skills.legal_summarizer.scripts.application.service``).
Аналогичные проверки качества остаются в ``tools/extract_quality.py``
(скрипт) и ``tests/test_information_preservation.py`` (pipeline test).
"""

from __future__ import annotations

import re
from typing import Any

# Golden dataset (внутри test file для простоты).
GOLDEN_DOCUMENTS: list[dict[str, Any]] = [
    {
        "name": "short_contract",
        "text": (
            "Вступление.\n\n"
            "FACT_001: Договор аренды заключается в письменной форме.\n"
            "FACT_002: Срок аренды — 12 месяцев.\n"
            "FACT_003: Арендная плата 50000 рублей в месяц.\n\n"
            "Заключение."
        ),
        "required_facts": [
            "Договор аренды",
            "12 месяцев",
            "50000 рублей",
        ],
    },
    {
        "name": "rental_terms",
        "text": (
            "Условия аренды помещения.\n\n"
            "FACT_001: Арендодатель — ООО Ромашка.\n"
            "FACT_002: Арендатор — ИП Иванов Иван Иванович.\n"
            "FACT_003: Помещение расположено по адресу: г. Москва, ул. Ленина, 1.\n"
            "FACT_004: Дата начала аренды — 1 января 2026 года.\n"
            "FACT_005: Дата окончания — 31 декабря 2026 года.\n\n"
            "Подписи сторон."
        ),
        "required_facts": [
            "ООО Ромашка",
            "ИП Иванов",
            "г. Москва",
            "1 января 2026",
            "31 декабря 2026",
        ],
    },
    {
        "name": "service_agreement",
        "text": (
            "Договор оказания услуг.\n\n"
            "FACT_001: Заказчик — ООО ТехноСервис.\n"
            "FACT_002: Исполнитель — ИП Петров П.П.\n"
            "FACT_003: Стоимость услуг — 100000 рублей.\n"
            "FACT_004: Срок выполнения — 30 рабочих дней.\n\n"
            "Реквизиты."
        ),
        "required_facts": [
            "ООО ТехноСервис",
            "ИП Петров",
            "100000 рублей",
            "30 рабочих дней",
        ],
    },
]


def _extract_required_facts(text: str) -> list[str]:
    """Извлечь «факты» из текста (формат ``FACT_NNN: ...``)."""
    pattern = re.compile(r"FACT_(\d+):\s*(.+?)(?=\n|$)", re.MULTILINE)
    return [m.group(2).strip() for m in pattern.finditer(text)]


# ---------------------------------------------------------------------------
# Golden dataset: structural validation
# ---------------------------------------------------------------------------


def test_golden_dataset_has_unique_names():
    """Все golden documents имеют уникальные имена."""
    names = [d["name"] for d in GOLDEN_DOCUMENTS]
    assert len(names) == len(set(names)), (
        f"Дублирующиеся имена в golden dataset: {names}"
    )


def test_golden_dataset_all_have_required_facts():
    """Каждый golden document имеет required_facts."""
    for d in GOLDEN_DOCUMENTS:
        assert "required_facts" in d, f"{d['name']}: нет required_facts"
        assert isinstance(d["required_facts"], list)
        assert len(d["required_facts"]) > 0, (
            f"{d['name']}: required_facts пуст"
        )


def test_golden_dataset_texts_contain_required_facts():
    """Каждый required_fact присутствует в тексте документа."""
    for d in GOLDEN_DOCUMENTS:
        for f in d["required_facts"]:
            assert f in d["text"], (
                f"{d['name']}: required_fact {f!r} отсутствует в тексте"
            )


def test_golden_dataset_extracted_facts_match():
    """Extracted FACT_NNN из текста совпадают с required_facts."""
    for d in GOLDEN_DOCUMENTS:
        extracted = _extract_required_facts(d["text"])
        # Каждый extracted fact содержит хотя бы один required fact как substring.
        # (Это гарантирует что required_facts извлекаемы из текста.)
        for required in d["required_facts"]:
            found = any(required in ext for ext in extracted)
            assert found, (
                f"{d['name']}: required {required!r} не извлекается из текста"
            )
