"""Follow Up 2.0 — контракт ответа: чем ответ ОБЯЗАН быть подкреплён.

Контракт предлагает интерпретатор, но применяется он только через
`enforce_floor` — чистую функцию, которая умеет ужесточать и не умеет
ослаблять. Смысл: сбой понимания не должен превращаться в сбой
доказательности. Интерпретатор, вернувший «цитата не нужна», ничего не
отключает.

`scope` — это ОБЛАСТЬ ответа, а не запрет множественности. Один булев
`single_check` не мог развести две разные вещи:

- **запрещено при любой области** — смешение артефактов разных проверок
  внутри ОДНОГО утверждения: методология одной КМ подана как относящаяся
  к другой;
- **нормально при `scope='corpus'`** — перечисление многих проверок, где
  каждая строка несёт свой номер и свои цитаты. Без этого «в каких актах
  встречался Иванов» и «все акты про лимиты» не могли быть приняты в
  принципе.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, FrozenSet, List, Literal, Optional

Kind = Literal["passages", "within_document", "coverage", "entity_rollup",
               "facets", "compare", "corpus_profile", "artifact", "refusal"]


@dataclass(frozen=True)
class AnswerContract:
    kind: Kind = "passages"
    scope: Literal["focused", "corpus"] = "corpus"
    needs_quote: bool = False
    min_quotes: int = 0
    min_docs: int = 0
    needs_coverage_line: bool = False
    min_field_fill: float = 0.0
    render: Optional[str] = None
    allowed_checks: FrozenSet[str] = frozenset()
    proposed_scope: Optional[str] = None      # что предложила модель — в журнал

    def as_dict(self) -> Dict:
        return {"kind": self.kind, "scope": self.scope,
                "needs_quote": self.needs_quote, "min_quotes": self.min_quotes,
                "min_docs": self.min_docs,
                "needs_coverage_line": self.needs_coverage_line,
                "min_field_fill": self.min_field_fill,
                "render": self.render,
                "allowed_checks": sorted(self.allowed_checks),
                "proposed_scope": self.proposed_scope}


# Какие виды ответа рендерит код, а не модель. Без стриминга токенов длина
# вывода — это время: таблица на 27 актов это полторы-две тысячи токенов
# мёртвого ожидания и главный источник выдуманных чисел.
RENDER_BY_KIND: Dict[str, str] = {
    "coverage": "table_docs",
    "entity_rollup": "table_entity",
    "facets": "table_rows",
    "compare": "table_compare",
    "corpus_profile": "profile",
    # Прозаические виды тело не рендерят кодом, но цитаты собираются им же —
    # без этого ответ с needs_quote после верификатора остался бы пустым
    "passages": "quotes",
    "within_document": "quotes",
}

AGGREGATE_KINDS = ("facets", "coverage", "entity_rollup", "corpus_profile")
FOCUSED_KINDS = ("within_document", "artifact")
