"""``ParameterValidator`` — проверка пользовательских параметров.

Перенесено из ``workspace/skills/audit_analyzer/scripts/predefined_mode.py``
(эталон a606fe0). Логика и сообщения об ошибках сохранены.

Critical rules:
  * Validator НЕ обращается к DuckDB / PostgreSQL / LLM / Agent.
  * Validator НЕ знает про конкретные таблицы.
  * Validator получает ``ScriptDefinition`` + ``params`` и возвращает
    либо кортеж ``(merged_params, unknown_keys, None)``,
    либо ``(None, None, error_payload)``.
"""

from __future__ import annotations

import re
from typing import Any

from workspace.skills.audit_analyzer.scripts.predefined.models import (
    ParamDefinition,
    ScriptDefinition,
)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _is_valid_iso_date(value: Any) -> bool:
    """ISO-дата формата ``YYYY-MM-DD`` с валидным месяцем/днём.

    Проверяем через ``datetime.strptime`` (ISO strict), чтобы отсеять
    ``2026-1-1``, ``2026-99-99`` и прочие «не строгие» варианты. Регулярка
    нужна только для быстрого отказа на не-дата-строках (``hello``).
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    from datetime import date

    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


__all__ = ["ParameterValidator", "ValidationError"]


class ValidationError(Exception):
    """Ошибка валидации параметров (для тестов и программных вызовов).

    Attributes:
        message: Человекочитаемое описание ошибки.
        script_name: Имя скрипта, в котором произошла ошибка.
    """

    def __init__(self, message: str, *, script_name: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.script_name = script_name


class ParameterValidator:
    """Валидирует и нормализует параметры для ``ScriptDefinition``.

    Делает то же самое, что встроенная логика
    ``predefined_mode.run()`` эталона a606fe0:

    1. drop пустых (``None`` / ``""``) и неизвестных ключей;
    2. возврат ``unknown`` для предупреждения;
    3. проверка обязательных параметров;
    4. грубая type-coercion (number → ``int``, boolean → ``bool``,
       limit → ``int``).
    """

    @staticmethod
    def merge(
        script: ScriptDefinition,
        params: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Оставить только параметры, объявленные в ``script.parameters``.

        Пропускает ``None`` и пустые строки (как было в ``predefined.py:resolve_params``).

        Returns:
            Кортеж ``(merged, unknown)``.
        """
        merged: dict[str, Any] = {}
        unknown: list[str] = []
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            if key in script.parameters:
                merged[key] = value
            else:
                unknown.append(key)
        return merged, unknown

    @staticmethod
    def check_required(
        script: ScriptDefinition,
        merged: dict[str, Any],
    ) -> str | None:
        """Вернуть сообщение об ошибке, если какого-то обязательного параметра нет.

        Returns:
            ``None`` если всё ок, иначе строка с описанием.
        """
        for pname, pdef in script.parameters.items():
            if pname in merged:
                continue
            if pdef.required:
                return (
                    f"Обязательный параметр '{pname}' не указан "
                    f"для скрипта '{script.name}'"
                )
        return None

    @staticmethod
    def coerce_types(
        script: ScriptDefinition,
        merged: dict[str, Any],
    ) -> str | None:
        """Проверить/преобразовать типы значений по ``ParamDefinition.type``.

        Соответствует поведению ``predefined_mode.run()`` (number/limit/boolean).
        Ошибка → строка с описанием (``None`` если всё ок).
        """
        for pname, pdef in script.parameters.items():
            if pname not in merged:
                continue
            value = merged[pname]
            if pdef.type in ("number", "limit"):
                try:
                    int(value)
                except (ValueError, TypeError):
                    return (
                        f"Параметр '{pname}' должен быть числом, "
                        f"получено: {value}"
                    )
            elif pdef.type == "boolean":
                if not isinstance(value, bool):
                    return (
                        f"Параметр '{pname}' должен быть boolean "
                        f"(true/false), получено: {value}"
                    )
            elif pdef.type == "date":
                if not _is_valid_iso_date(value):
                    return (
                        f"Параметр '{pname}' должен быть ISO-датой "
                        f"(YYYY-MM-DD), получено: {value}"
                    )
            elif pdef.type == "enum":
                validation = pdef.validation or {}
                choices = validation.get("choices") or []
                if choices and value not in choices:
                    return (
                        f"Параметр '{pname}' должен быть одним из "
                        f"{choices}, получено: {value}"
                    )
        return None

    @classmethod
    def validate(
        cls,
        script: ScriptDefinition,
        params: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Полный цикл: merge + required + coerce.

        Returns:
            ``(validated_params, error_message)``. ``error_message`` None
            при успехе. ``validated_params`` — пустой dict при ошибке.
        """
        merged, unknown = cls.merge(script, params)

        if unknown:
            valid = ", ".join(script.parameters.keys())
            return {}, (
                f"Неизвестные параметры: {', '.join(unknown)}. "
                f"Допустимые параметры для скрипта '{script.name}': "
                f"{valid}"
            )

        if params and not merged:
            valid = ", ".join(script.parameters.keys())
            return {}, (
                f"Ни один из переданных параметров не подходит для скрипта "
                f"'{script.name}'. Допустимые параметры: {valid}"
            )

        required_error = cls.check_required(script, merged)
        if required_error:
            return {}, required_error

        type_error = cls.coerce_types(script, merged)
        if type_error:
            return {}, type_error

        return merged, None

    @classmethod
    def validate_or_raise(
        cls,
        script: ScriptDefinition,
        params: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Как ``validate``, но бросает ``ValidationError`` при ошибке."""
        merged, error = cls.validate(script, params)
        if error:
            raise ValidationError(error, script_name=script.name)
        return merged
