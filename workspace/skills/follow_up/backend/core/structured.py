"""Follow Up 2.0 — разбор структурированного ответа модели.

Один разборщик вместо четырёх копий `re.search(r"\\{.*\\}", raw, re.DOTALL)`
(`execution_control.py`, `hypothesis_agent.py`, `query_understanding.py`,
`bitbucket.py`). Копии не просто дублировались — они одинаково ошибались:

- жадный `\\{.*\\}` на ответе с ```json-обёрткой и пояснением после закрывающей
  скобки хватает от первой `{` до ПОСЛЕДНЕЙ `}` в тексте. Если модель написала
  «…} Надеюсь, это то, что нужно. {смайлик}», разбор падал целиком;
- скобка внутри строкового значения («ставка 5 %) годовых») ломала счёт;
- при неудаче все четыре возвращали None, не сказав почему, — а промпту нужен
  текст ошибки, чтобы ретрай имел смысл (приём отработан в арбитре резолвера,
  `poruch_resolver.py:354-371`).

GigaChat не поддерживает tools/function calling, поэтому JSON в тексте — не
временное решение, а единственный доступный протокол. Значит разбирать его надо
один раз и хорошо.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ```json … ``` или просто ``` … ```
_FENCE = re.compile(r"```(?:json)?\s*(.+?)\s*```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ParseResult:
    value: Optional[Any]
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.value is not None


def _scan_balanced(text: str, opener: str, closer: str) -> Optional[str]:
    """Первый сбалансированный блок, с учётом строк и экранирования.

    Жадный регекс здесь не годится: он не знает ни про кавычки, ни про то, что
    после валидного объекта модель часто дописывает прозу с ещё одной скобкой.
    """
    start = text.find(opener)
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse(raw: Optional[str], *, want: str = "object") -> ParseResult:
    """Текст ответа модели → dict/list. `want`: 'object' | 'array' | 'any'.

    Порядок попыток: содержимое ```-блока → сбалансированный блок из текста →
    весь текст целиком. Ошибка возвращается текстом, а не проглатывается: её
    подмешивают в ретрай.
    """
    if not raw or not str(raw).strip():
        return ParseResult(None, "пустой ответ модели")

    text = str(raw)
    candidates: list[str] = []

    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1))

    pairs = ([("{", "}")] if want == "object"
             else [("[", "]")] if want == "array"
             else [("{", "}"), ("[", "]")])
    for opener, closer in pairs:
        for source in ([m.group(1)] if m else []) + [text]:
            block = _scan_balanced(source, opener, closer)
            if block:
                candidates.append(block)

    candidates.append(text.strip())

    last_err = "JSON в ответе не найден"
    seen: set[str] = set()
    for cand in candidates:
        cand = cand.strip()
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            value = json.loads(cand)
        except (ValueError, TypeError) as e:
            last_err = str(e)
            continue
        if want == "object" and not isinstance(value, dict):
            last_err = f"ожидался объект, пришёл {type(value).__name__}"
            continue
        if want == "array" and not isinstance(value, list):
            last_err = f"ожидался массив, пришёл {type(value).__name__}"
            continue
        return ParseResult(value)

    return ParseResult(None, last_err)


def parse_object(raw: Optional[str]) -> Optional[dict]:
    """Совместимая с прежними `_parse_llm_json` форма: dict или None."""
    return parse(raw, want="object").value


def parse_array(raw: Optional[str]) -> Optional[list]:
    return parse(raw, want="array").value
