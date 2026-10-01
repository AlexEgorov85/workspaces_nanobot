"""Проверка качества результата операции.

Зачем слой, если операция и так что-то вернула. Затем, что «что-то» бывает
разным: пустой результат, обрезанный на стороне источника, несериализуемый
объект, ответ не той формы. Часть таких исходов — отказ, а часть — честный
ответ, и различать их в момент разбора уже поздно: агент к тому времени видит
только JSON.

Проверки делятся на два класса, и это деление — суть модуля:

* **технические** — результат непригоден как есть (не сериализуется, не та
  форма). Провал технической проверки — это отказ ``internal``;
* **семантические** — результат пригоден, но подозрителен (пусто, обрезано).
  Провал семантической — это признак в ответе, а **не** отказ: ноль строк в
  `run_script` — законный ответ, и подмена его на ошибку научила бы модель
  переписывать вопрос вместо того, чтобы сказать «ничего не нашлось».

Политика выбирается операцией (`ToolDefinition.quality_policy`) и приходит
из реестра операций, а не из кода проверок: набор проверок — свойство формы
результата, и форма меняется вместе с capability.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import Failure

#: Класс проверки.
TECHNICAL = "technical"
SEMANTIC = "semantic"


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """Итог одной проверки."""

    name: str
    kind: str
    ok: bool
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Исход проверок по одной политике."""

    policy: str
    checks: tuple[CheckOutcome, ...] = field(default_factory=tuple)

    @property
    def flags(self) -> tuple[str, ...]:
        """Имена проверок, которые не прошли."""
        return tuple(check.name for check in self.checks if not check.ok)

    @property
    def technical_failure(self) -> CheckOutcome | None:
        for check in self.checks:
            if check.kind == TECHNICAL and not check.ok:
                return check
        return None

    @property
    def ok(self) -> bool:
        return self.technical_failure is None

    def to_json(self) -> dict[str, Any]:
        return {
            "policy": self.policy,
            "ok": self.ok,
            "flags": list(self.flags),
            "checks": [check.to_json() for check in self.checks],
        }


#: Проверка: значение → текст замечания (пустая строка = замечаний нет).
CheckFn = Callable[[Any], str]


def _check_serializable(value: Any) -> str:
    """Результат должен уходить в JSON: иначе он не попадёт ни в ответ, ни в артефакт."""
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        return f"результат не сериализуется в JSON: {exc}"
    return ""


def _check_shape(value: Any) -> str:
    """Форма ответа: объект, список или текст. Всё прочее — дефект операции."""
    if isinstance(value, (dict, list, str, int, float, bool)) or value is None:
        return ""
    return f"неподдерживаемая форма результата: {type(value).__name__}"


def _check_truncation(value: Any) -> str:
    """Источник сам признался, что отрезал ответ.

    Отдельно от пустого результата: усечённый ответ выглядит как полный, и без
    этого признака агент продолжит опираться на данные, которых нет.
    """
    if not isinstance(value, dict):
        return ""
    if value.get("truncated") or value.get("is_truncated"):
        limit = value.get("limit") or value.get("returned")
        return f"источник усек ответ{f' (лимит {limit})' if limit else ''}"
    return ""


def _check_truncation(value: Any) -> str:
    """Источник сам признался, что отрезал ответ.

    Отдельно от пустого результата: усечённый ответ выглядит как полный, и без
    этого признака агент продолжит опираться на данные, которых нет.
    """
    if not isinstance(value, dict):
        return ""
    if value.get("truncated") or value.get("is_truncated"):
        limit = value.get("limit") or value.get("returned")
        return f"источник усек ответ{f' (лимит {limit})' if limit else ''}"
    return ""


def _first_list(value: Any, *keys: str) -> list[Any] | None:
    """Достать коллекцию из ответа по одному из известных ключей."""
    if not isinstance(value, dict):
        return None
    for key in keys:
        found = value.get(key)
        if isinstance(found, list):
            return found
    return None


def _check_empty_result(*keys: str) -> CheckFn:
    """Проверка «ответ есть, но совпадений нет».

    Ключи перечислены явно, а не «первый список в ответе»: подсказка «пусто» —
    законный вывод для ``rows`` и ``hits`` и неверный вывод для ``columns``,
    который пустым быть может.
    """

    def _check(value: Any) -> str:
        items = _first_list(value, *keys)
        if items is None:
            return f"в ответе нет списка {keys[0]!r}"
        if not items:
            return "пустой результат"
        return ""

    return _check


def _check_sql_rows(value: Any) -> str:
    """Строки результата SQL-запроса.

    Отдельная проверка, а не ``_check_empty_result``: у ответа аудита есть
    ``no_match`` — признак, который библиотека выставляет сама и который
    означает «данных нет, и это не сбой». Трактовка ноля строк как «пустой
    результат» верна, но теряет более точное объяснение, которое источник уже
    дал.
    """
    rows = _first_list(value, "rows", "results")
    if rows is None:
        return "в ответе нет списка 'rows'"
    if isinstance(value, dict) and value.get("no_match"):
        return "источник сообщил об отсутствии данных (no_match)"
    if not rows:
        return "пустой результат"
    return ""


def _check_llm_result(value: Any) -> str:
    """Ответ операции LLM пригоден, если в нём есть текст или вектор.

    Одна проверка на две формы потому, что политика называет слой, а не
    операцию: ``complete`` отдаёт текст, ``embed`` — вектор, и «пригодный ответ
    провайдера» одинаково означает «не пусто».
    """
    if isinstance(value, str):
        return "" if value.strip() else "провайдер вернул пустой текст"
    if not isinstance(value, dict):
        return f"неподдерживаемая форма ответа LLM: {type(value).__name__}"
    for key in ("text", "content", "answer"):
        text = value.get(key)
        if isinstance(text, str):
            return "" if text.strip() else "провайдер вернул пустой текст"
    items = _first_list(value, "vector", "embeddings", "vectors", "data")
    if items is None:
        return "в ответе нет ни текста, ни вектора"
    return "пустой вектор" if not items else ""


def _check_embedding_dimension(value: Any) -> str:
    """Длина вектора обязана совпадать с объявленной размерностью.

    Проверка техническая, а не семантическая: эмбеддинг неверной длины
    непригоден для вызывающего — поиск по нему вернёт мусор, и узнать об этом
    можно будет только по итогам работы, то есть слишком поздно.
    """
    if not isinstance(value, dict):
        return ""
    items = _first_list(value, "vector", "embeddings", "vectors", "data")
    dimension = value.get("dimension")
    if items is None or not isinstance(dimension, int) or isinstance(dimension, bool):
        return ""
    if len(items) != dimension:
        return f"длина вектора {len(items)} не совпадает с dimension={dimension}"
    return ""


#: Проверки, общие для любой операции: без них результат непригоден как есть.
_TECHNICAL: tuple[tuple[str, str, CheckFn], ...] = (
    ("serialization", TECHNICAL, _check_serializable),
    ("shape", TECHNICAL, _check_shape),
)

#: Политики качества. Ключи перечислены в § ``runtime/tool-execution``; набор
#: проверок дополняет их, а не заменяет. ``none`` означает «проверок нет»:
#: поле ``quality`` тогда отсутствует, а не пустое.
QUALITY_POLICIES: Mapping[str, tuple[tuple[str, str, CheckFn], ...]] = {
    "default": _TECHNICAL,
    "none": (),
    "sql_result": _TECHNICAL
    + (
        ("rows", SEMANTIC, _check_sql_rows),
        ("truncation", SEMANTIC, _check_truncation),
    ),
    "vector_result": _TECHNICAL
    + (
        ("matches", SEMANTIC, _check_empty_result("results", "hits")),
        ("truncation", SEMANTIC, _check_truncation),
    ),
    "llm_result": _TECHNICAL
    + (
        ("answer", SEMANTIC, _check_llm_result),
        ("dimension", TECHNICAL, _check_embedding_dimension),
    ),
}

#: Имена политик для проверки при загрузке реестра. Неизвестное имя — ошибка
#: загрузки, а не предупреждение: опечатка в политике молча отключала бы
#: проверки целиком.
POLICY_NAMES: frozenset[str] = frozenset(QUALITY_POLICIES)


class QualityChecker:
    """Проверка результата по политике операции."""

    def __init__(self, policies: Mapping[str, tuple[tuple[str, str, CheckFn], ...]] | None = None) -> None:
        self._policies = dict(policies or QUALITY_POLICIES)

    def known(self, policy: str) -> bool:
        return policy in self._policies

    def check(self, policy: str, value: Any) -> QualityReport:
        """Выполнить проверки политики.

        Политика ``none`` даёт **пустой** отчёт без единой проверки: вызывающий
        отличает «проверок не было» от «проверки прошли» и не пишет об этом
        событие.
        """
        checks = self._policies.get(policy, self._policies["default"])
        outcomes: list[CheckOutcome] = []
        for name, kind, fn in checks:
            try:
                detail = fn(value)
            except Exception as exc:  # noqa: BLE001 - дефект проверки не отменяет вызов
                detail = f"проверка не выполнена: {exc}"
                kind = SEMANTIC
            outcomes.append(CheckOutcome(name=name, kind=kind, ok=not detail, detail=detail))
        return QualityReport(policy=policy, checks=tuple(outcomes))


def technical_failure(report: QualityReport) -> Failure | None:
    """Отказ по провалу технической проверки, если он был."""
    check = report.technical_failure
    if check is None:
        return None
    return Failure(code="internal", message=f"результат негоден: {check.detail}")


def default_checker() -> QualityChecker:
    return QualityChecker()
