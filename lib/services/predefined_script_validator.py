"""Валидация параметров предопределённых SQL-скриптов.

Принимает ``parameters`` из ``PredefinedScript.parameters`` (JSONB из
``public.agent_predefined_scripts.parameters``) и пользовательский
``values: dict[str, Any]``. Возвращает либо ``(ok, cleaned_values)``, либо
``ParameterValidationError`` с описанием всех нарушений.

Контракт схемы параметра (из DDL-комментария в
``sql/audit_analyzer/create_public_agent_predefined_scripts.sql``)::

    {
      "param_name": {
        "type": "string" | "integer" | "number" | "boolean" |
                "date" | "datetime",
        "required": true | false,
        "default": <literal>,
        "description": <text>,
        "validation": {
          "min": <number>, "max": <number>,
          "min_length": <int>, "max_length": <int>,
          "pattern": "<regex>",
          "choices": [<literal>, ...]
        }
      }
    }

Правила:
  * Неизвестные параметры в ``values`` — ошибка (явное перечисление).
  * ``required=true`` без значения → ошибка.
  * ``null`` для ``required=true`` → ошибка.
  * ``default`` подставляется, если параметр отсутствует.
  * Type coercion: ``integer`` → ``int(...)``, ``number`` → ``float(...)``,
    ``boolean`` → ``bool(...)``, ``string`` → ``str(...)``.
  * ``validation`` — диапазоны/длины/regex/choices.
  * Несколько ошибок собираются и возвращаются одним исключением.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


__all__ = ["ParameterValidator", "ParameterValidationError"]


_SUPPORTED_TYPES = frozenset({"string", "integer", "number", "boolean", "date", "datetime"})


@dataclass
class ParameterValidationError(Exception):
    """Агрегированная ошибка валидации параметров скрипта.

    Attributes:
        errors: список человеко-читаемых описаний (по одной на нарушение).
        script_name: имя скрипта (если известно) — для контекста.
    """

    errors: tuple[str, ...]
    script_name: str | None = None

    def __str__(self) -> str:
        prefix = f"[{self.script_name}] " if self.script_name else ""
        return f"{prefix}Ошибки параметров: {'; '.join(self.errors)}"


class ParameterValidator:
    """Валидатор параметров для одного скрипта.

    Args:
        parameter_defs: ``parameters`` из ``PredefinedScript`` — словарь
            ``{name: ParamDefinition}``.
        script_name: имя скрипта (для сообщений об ошибках).
    """

    def __init__(
        self,
        *,
        parameter_defs: dict[str, dict[str, Any]],
        script_name: str | None = None,
    ) -> None:
        self._defs = dict(parameter_defs)
        self._script_name = script_name

    @property
    def declared_names(self) -> tuple[str, ...]:
        return tuple(self._defs.keys())

    def validate(self, values: dict[str, Any] | None) -> dict[str, Any]:
        """Валидировать и привести ``values`` к канону.

        Returns:
        ``{name: coerced_value}`` со всеми ``default``-значениями.

        Raises:
        ParameterValidationError: одна или несколько ошибок.
        """
        values = dict(values or {})
        errors: list[str] = []
        out: dict[str, Any] = {}

        declared = set(self._defs.keys())
        provided = set(values.keys())
        unknown = provided - declared
        for name in sorted(unknown):
            errors.append(f"неизвестный параметр {name!r}")

        for name, spec in self._defs.items():
            param_type = spec.get("type")
            if param_type not in _SUPPORTED_TYPES:
                errors.append(
                    f"параметр {name!r}: неподдерживаемый тип {param_type!r}"
                )
                continue
            required = bool(spec.get("required"))
            has_value = name in values and values[name] is not None
            if not has_value:
                if required:
                    errors.append(f"параметр {name!r} обязателен")
                    continue
                if "default" in spec:
                    out[name] = spec["default"]
                continue
            raw = values[name]
            try:
                coerced = self._coerce(raw, param_type)
            except (TypeError, ValueError) as exc:
                errors.append(
                    f"параметр {name!r}: не удалось привести к {param_type}: {exc}"
                )
                continue
            v_err = self._check_validation(name, coerced, spec.get("validation") or {})
            if v_err:
                errors.append(v_err)
                continue
            out[name] = coerced

        if errors:
            raise ParameterValidationError(
                errors=tuple(errors), script_name=self._script_name
            )
        return out

    @staticmethod
    def _coerce(raw: Any, param_type: str) -> Any:
        if param_type == "string":
            if isinstance(raw, str):
                return raw
            return str(raw)
        if param_type == "integer":
            if isinstance(raw, bool):
                raise TypeError("bool не является integer")
            if isinstance(raw, int):
                return raw
            if isinstance(raw, str):
                return int(raw.strip())
            raise TypeError(f"неожиданный тип {type(raw).__name__}")
        if param_type == "number":
            if isinstance(raw, bool):
                raise TypeError("bool не является number")
            if isinstance(raw, (int, float)):
                return float(raw)
            if isinstance(raw, str):
                return float(raw.strip())
            raise TypeError(f"неожиданный тип {type(raw).__name__}")
        if param_type == "boolean":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                lowered = raw.strip().lower()
                if lowered in {"true", "1", "yes"}:
                    return True
                if lowered in {"false", "0", "no"}:
                    return False
                raise TypeError(f"не удалось разобрать boolean из {raw!r}")
            if isinstance(raw, int) and raw in (0, 1):
                return bool(raw)
            raise TypeError(f"неожиданный тип {type(raw).__name__}")
        if param_type in {"date", "datetime"}:
            if isinstance(raw, str):
                return raw
            raise TypeError(f"ожидается ISO-страна для {param_type}")
        raise TypeError(f"unsupported type {param_type}")

    @staticmethod
    def _check_validation(name: str, value: Any, rules: dict[str, Any]) -> str | None:
        if "choices" in rules and value not in rules["choices"]:
            choices = ", ".join(repr(c) for c in rules["choices"])
            return f"параметр {name!r}: значение вне допустимых ({choices})"
        if value is None:
            return None
        if "min" in rules and isinstance(value, (int, float)):
            if value < rules["min"]:
                return f"параметр {name!r}: {value} < min={rules['min']}"
        if "max" in rules and isinstance(value, (int, float)):
            if value > rules["max"]:
                return f"параметр {name!r}: {value} > max={rules['max']}"
        if "min_length" in rules and isinstance(value, str):
            if len(value) < rules["min_length"]:
                return (
                    f"параметр {name!r}: длина {len(value)} < "
                    f"min_length={rules['min_length']}"
                )
        if "max_length" in rules and isinstance(value, str):
            if len(value) > rules["max_length"]:
                return (
                    f"параметр {name!r}: длина {len(value)} > "
                    f"max_length={rules['max_length']}"
                )
        if "pattern" in rules and isinstance(value, str):
            try:
                if not re.search(rules["pattern"], value):
                    return (
                        f"параметр {name!r}: значение не соответствует "
                        f"pattern {rules['pattern']!r}"
                    )
            except re.error:
                return (
                    f"параметр {name!r}: некорректный pattern в конфиге"
                )
        return None