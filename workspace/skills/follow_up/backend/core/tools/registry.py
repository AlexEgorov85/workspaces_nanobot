"""Follow Up 2.0 — возможности системы как данные, а не как ветки кода.

Сегодня «что умеет инструмент» описано в трёх местах сразу: регексы интентов в
`query_understanding`, мапа интент→агент в `router.py` и словарь названий в
`chat.py`. Добавление одной возможности — правка в трёх файлах, и любая из них
может отстать от остальных молча.

Здесь возможность — запись. Из неё генерируется и манифест для промпта, и
список для интерфейса, и валидатор аргументов. Разъехаться им негде.

Поля, которые несут смысл дальше по конвейеру:

- `corpus_wide` — отличает охватный инструмент от точечного. Вход для расчёта
  разрешённого набора проверок: безномерной вопрос через точечный инструмент
  обязан деградировать до пассажей, а не выдавать «просмотрен весь корпус»;
- `role` — владелец тела ответа. В плане ровно один шаг с `role='body'`, и по
  нему замораживается набор проверок;
- `manifest_hidden` — инструмент есть, но модель его не выбирает: его
  вставляет код. Нужен, чтобы служебные шаги не удорожали вход планировщика;
- `cost_class` и `latency_hint_ms` попадают В ПРОМПТ, а не только в интерфейс:
  модель должна знать, что охватный поиск дороже точечного.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Literal, Optional, Tuple

logger = logging.getLogger(__name__)

from backend.core.evidence import ARTIFACT, DOC, ENTITY, PASSAGE, ROW  # noqa: E402


@dataclass(frozen=True)
class ToolSpec:
    name: str
    human_label: str                     # для интерфейса и строк прогресса
    description: str                     # для промпта планировщика
    args_schema: Dict                    # {имя: {"type", "required", "desc"}}
    produces: str                        # PASSAGE | DOC | ENTITY | ROW | ARTIFACT
    corpus_wide: bool                    # обещает ли полноту по корпусу
    role: Literal["body", "scope", "enrich", "prepare"] = "body"
    cost_class: Literal["free", "cpu", "llm"] = "cpu"
    cost_llm_calls: int = 0
    latency_hint_ms: int = 500
    manifest_hidden: bool = False
    when_not: str = ""                   # когда НЕ звать — в промпт
    examples: Tuple[str, ...] = ()


_TOOLS: Dict[str, Tuple[ToolSpec, Callable]] = {}


def register(spec: ToolSpec, fn: Callable) -> None:
    if spec.name in _TOOLS:
        logger.warning(f"[registry] Инструмент {spec.name} переопределён")
    _TOOLS[spec.name] = (spec, fn)


def get(name: str) -> Tuple[ToolSpec, Callable]:
    if name not in _TOOLS:
        raise KeyError(f"инструмент «{name}» не зарегистрирован")
    return _TOOLS[name]


def names(include_hidden: bool = False) -> List[str]:
    return [n for n, (s, _) in sorted(_TOOLS.items())
            if include_hidden or not s.manifest_hidden]


def specs(include_hidden: bool = False) -> List[ToolSpec]:
    return [s for _, (s, _) in sorted(_TOOLS.items())
            if include_hidden or not s.manifest_hidden]


def manifest() -> str:
    """Описание возможностей для промпта планировщика.

    Стоимость входит в текст намеренно: без неё модель выбирает охватный поиск
    там, где хватило бы точечного, и ход дорожает на ровном месте.
    """
    lines: List[str] = []
    for s in specs():
        args = ", ".join(
            f"{k}{'' if v.get('required') else '?'}: {v.get('type', 'str')}"
            for k, v in s.args_schema.items())
        cost = {"free": "мгновенно", "cpu": f"~{s.latency_hint_ms} мс",
                "llm": f"{s.cost_llm_calls} вызов(а) модели"}[s.cost_class]
        line = f"- {s.name}({args}) — {s.description} [{cost}"
        if s.corpus_wide:
            line += ", даёт охват по корпусу"
        line += "]"
        if s.when_not:
            line += f"\n  НЕ звать: {s.when_not}"
        if s.examples:
            line += "\n  примеры: " + "; ".join(f"«{e}»" for e in s.examples)
        lines.append(line)
    return "\n".join(lines)


def public() -> List[Dict]:
    """Для интерфейса: что инструмент умеет, человеческим языком."""
    return [{"name": s.name, "label": s.human_label,
             "description": s.description, "corpus_wide": s.corpus_wide,
             "cost": s.cost_class} for s in specs()]


def validate(name: str, args: Dict) -> Dict:
    """Проверка аргументов по схеме. Лишние отбрасываются, обязательные — строго.

    Возвращает очищенный словарь, а не бросает на первом же лишнем ключе:
    модель регулярно добавляет поля от себя, и ронять из-за этого весь ход
    дороже, чем их выбросить.
    """
    spec, _ = get(name)
    out: Dict = {}
    for key, rule in spec.args_schema.items():
        if key in args and args[key] is not None:
            out[key] = args[key]
        elif rule.get("required"):
            raise ValueError(f"{name}: не указан обязательный аргумент «{key}»")
    extra = set(args) - set(spec.args_schema)
    if extra:
        logger.debug(f"[registry] {name}: лишние аргументы отброшены: {extra}")
    return out


def clear() -> None:
    """Только для тестов."""
    _TOOLS.clear()
