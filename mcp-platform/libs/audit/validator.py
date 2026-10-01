"""Проверка пользовательских параметров скрипта.

Портировано из агента
(``workspace/skills/audit_analyzer/scripts/predefined/validator.py``)
без изменения логики и сообщений: merge → обязательные → приведение типов,
плюс ISO-дата в строгом виде ``YYYY-MM-DD``.

Единственное изменение — ошибка. В агенте был свой ``ValidationError`` с
двумя полями, а вызывающий получал ещё и строку вместо исключения. Здесь
всё поднимает :class:`~libs.audit.errors.AuditValidationError` (пункт
4.14), а tuple-форма ``validate`` сохранена как совместимый помощник.

Инварианты (из агента, сохраняются):

* валидатор не ходит в хранилище, к LLM и ни к какой таблице;
* неизвестный параметр — ошибка, а не предупреждение: молча выкинутое
  значение означало бы, что запрос отработал не по тем данным, о которых
  думал вызывающий.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from libs.audit.errors import AuditValidationError
from libs.audit.models import ScriptDefinition

__all__ = ["ParameterValidator", "is_valid_iso_date"]

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_iso_date(value: Any) -> bool:
    """Строгая ISO-дата ``YYYY-MM-DD`` с валидным месяцем и днём.

    Регулярка отсекает не-даты, ``date.fromisoformat`` — ``2026-13-01``
    и ``2026-02-30``, которые регулярка пропускает.
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class ParameterValidator:
    """Валидирует и нормализует параметры скрипта."""

    @staticmethod
    def merge(
        script: ScriptDefinition,
        params: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], list[str]]:
        """Оставить только объявленные параметры.

        ``None`` и пустые строки отбрасываются (как ``resolve_params`` в
        агенте) — так «параметр не передан» и «параметр передан пустым»
        остаются одним и тем же.

        Returns:
            ``(merged, unknown)``.
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
        merged: Mapping[str, Any],
    ) -> str | None:
        """Сообщение об отсутствующем обязательном параметре либо ``None``."""
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
        merged: Mapping[str, Any],
    ) -> str | None:
        """Проверить типы значений по ``ParamDefinition.type``."""
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
                if not is_valid_iso_date(value):
                    return (
                        f"Параметр '{pname}' должен быть ISO-датой "
                        f"(YYYY-MM-DD), получено: {value}"
                    )
            elif pdef.type == "enum":
                choices = (pdef.validation or {}).get("choices") or []
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
        params: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], str | None]:
        """Полный цикл: merge → обязательные → типы.

        Returns:
            ``(validated_params, error_message)``. При ошибке параметры
            пустые, сообщение — для человека. Конвейер этот метод не
            использует: он зовёт :meth:`validate_or_raise`.
        """
        merged, unknown = cls.merge(script, params)

        if unknown:
            valid = ", ".join(script.parameters.keys())
            return {}, (
                f"Неизвестные параметры: {', '.join(unknown)}. "
                f"Допустимые параметры для скрипта '{script.name}': {valid}"
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
        params: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Как :meth:`validate`, но поднимает ``AuditValidationError``."""
        merged, error = cls.validate(script, params)
        if error:
            raise AuditValidationError(error)
        return merged
