"""Follow Up 2.0 — типизированный леджер доказательств.

Сегодня инструмент возвращает список словарей-чанков, и по нему невозможно
ответить на три вопроса, без которых ответ аудитору бесполезен:

- **чья это проверка** — каждая КМ отдельная, и артефакт чужой в ответе
  недопустим. У доказательства есть `check_id` и провенанс, а не только текст;
- **какова полнота** — «нашёл в трёх актах» звучит как «в трёх и есть», хотя
  просмотрено пять фрагментов. `Coverage` несёт `scanned/matched/returned`, и
  строка полноты собирается из чисел, а не из ощущения;
- **что отказало** — если реранкер не загрузился, а BM25 пуст, ответ всё равно
  соберётся, просто он будет хуже. `degraded_sources` не даёт выдать такой
  ответ за полноценный.

Coverage НЕ суммируется между инструментами: два поиска по одному корпусу
просмотрели один и тот же корпус, а не два. Складывать их знаменатели —
это завысить охват вдвое, поэтому `Ledger.coverage()` берёт максимум по
`scanned` и объединение по документам.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Literal, Optional, Set

PASSAGE = "PASSAGE"
DOC = "DOC"
ENTITY = "ENTITY"
ROW = "ROW"
ARTIFACT = "ARTIFACT"


@dataclass(frozen=True)
class Provenance:
    """Откуда доказательство и можно ли приписать его этой проверке."""
    how: Literal["exact", "family", "filename", "vitrina", "semantic"]
    is_own_check: bool = True
    note: Optional[str] = None


@dataclass
class Evidence:
    kind: Literal["passage", "doc_fragment", "deviation", "poruch",
                  "entity_mention", "fact_row"]
    check_id: str
    quote: str                       # дословно, без переформулировки
    header_path: str = ""            # «Раздел > Подраздел» — навигация аудитора
    chunk_uid: Optional[str] = None
    fields: Dict = field(default_factory=dict)
    score: float = 0.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance("exact", True))
    where: Optional[Literal["case_text", "requisites"]] = None
    sim_query: Optional[float] = None

    @property
    def uid(self) -> str:
        return self.chunk_uid or f"{self.check_id}:{hash(self.quote) & 0xffffff}"


@dataclass
class Coverage:
    """Честная полнота одного инструмента.

    scanned  — сколько единиц реально просмотрено (документов или строк);
    matched  — сколько подошло;
    returned — сколько отдано наверх (может быть меньше matched из-за потолка);
    truncated — отдали не всё, и это надо сказать вслух.
    """
    unit: Literal["docs", "chunks", "rows"] = "chunks"
    scanned: int = 0
    matched: int = 0
    returned: int = 0
    truncated: bool = False
    field_fill: Dict[str, float] = field(default_factory=dict)

    def line(self) -> str:
        unit_ru = {"docs": "актов", "chunks": "фрагментов", "rows": "строк"}[self.unit]
        s = f"просмотрено {self.scanned} {unit_ru}, совпало {self.matched}"
        if self.returned and self.returned != self.matched:
            s += f", показано {self.returned}"
        if self.truncated:
            s += " (показано не всё)"
        return s


@dataclass
class ToolResult:
    evidence: List[Evidence] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    status: Literal["ok", "empty", "degraded", "failed"] = "ok"
    degraded_sources: List[str] = field(default_factory=list)
    tool: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status in ("ok", "degraded")


class Ledger:
    """Накопитель доказательств хода."""

    def __init__(self) -> None:
        self._items: Dict[str, Evidence] = {}
        self._results: List[ToolResult] = []

    def add(self, result: ToolResult) -> "Ledger":
        self._results.append(result)
        for ev in result.evidence:
            prev = self._items.get(ev.uid)
            if prev is None or ev.score > prev.score:
                self._items[ev.uid] = ev
        return self

    def evidence(self) -> List[Evidence]:
        return sorted(self._items.values(), key=lambda e: -e.score)

    def checks(self) -> Set[str]:
        return {e.check_id for e in self._items.values() if e.check_id}

    def quotes(self) -> List[Evidence]:
        return [e for e in self._items.values() if e.quote]

    def by_check(self) -> Dict[str, List[Evidence]]:
        out: Dict[str, List[Evidence]] = {}
        for e in self.evidence():
            out.setdefault(e.check_id, []).append(e)
        return out

    def coverage(self) -> Coverage:
        """Общая полнота. НЕ сумма: два поиска по одному корпусу просмотрели
        один корпус, а не два — иначе знаменатель удваивается на ровном месте."""
        if not self._results:
            return Coverage()
        unit = self._results[0].coverage.unit
        same_unit = [r.coverage for r in self._results
                     if r.coverage.unit == unit]
        # field_fill ОБЯЗАН доезжать до общей полноты: это то самое число,
        # которое отличает «нарушений дороже 10 млн — три» от ловушки. Берётся
        # ХУДШЕЕ по каждому полю: если один инструмент видел поле у 90 % строк,
        # а другой у 7 %, ответ опирается на 7 %.
        fill: Dict[str, float] = {}
        for c in same_unit:
            for k, v in (c.field_fill or {}).items():
                fill[k] = min(fill.get(k, 1.0), v)
        return Coverage(
            unit=unit,
            scanned=max((c.scanned for c in same_unit), default=0),
            matched=len(self.checks()) if unit == "docs" else len(self._items),
            returned=len(self._items),
            truncated=any(c.truncated for c in same_unit),
            field_fill=fill,
        )

    def degraded_sources(self) -> List[str]:
        out: List[str] = []
        for r in self._results:
            for s in r.degraded_sources:
                if s not in out:
                    out.append(s)
        return out

    def to_context(self, budget_chars: int = 20000) -> str:
        """Контекст для модели: цитаты с их проверками, без склейки чужого."""
        parts, total = [], 0
        for e in self.evidence():
            head = f"[{e.check_id}"
            if e.header_path:
                head += f" | {e.header_path}"
            head += "]"
            block = f"{head}\n{e.quote}"
            if total + len(block) > budget_chars:
                break
            parts.append(block)
            total += len(block)
        return "\n\n---\n\n".join(parts) or "Ничего не найдено."

    def __len__(self) -> int:
        return len(self._items)
