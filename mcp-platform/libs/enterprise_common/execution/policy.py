"""Политика исполнения: пороги и флаги из конфигурации, а не из кода операции.

Значения приходят из секции ``execution`` файла ``platform.json`` и разрешаются в
порядке «операция → capability → платформа». Переопределение на уровне операции
существует не для красоты: порог ответа у ``history_search`` и у ``run_script``
различается на порядок, и общий потолок заставил бы либо писать огромные ответы
в JSON-RPC, либо сохранять артефактами то, что влезает в один кадр.

Литерал в коде операции сделал бы значение декоративным: сервер поднялся бы и
применил бы не тот порог, который написан в документации. Страж реестра настроек
(`tests/test_settings_registry.py::test_no_platform_setting_carries_a_value_in_code`)
такой литерал ловит — поэтому здесь значений нет, только чтение.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

#: Настройки, из которых собирается политика. Порядок объявлен один раз, чтобы
#: ключи не расходились между реестром, файлом и этим местом.
SETTING_NAMES: tuple[str, ...] = (
    "ENTERPRISE_EXEC_MAX_INLINE_BYTES",
    "ENTERPRISE_EXEC_PREVIEW_BYTES",
    "ENTERPRISE_EXEC_TIMEOUT_SEC",
    "ENTERPRISE_EXEC_PERSIST_LARGE",
    "ENTERPRISE_EXEC_QUALITY_CHECK",
    "ENTERPRISE_EXEC_LOGGING",
    "ENTERPRISE_EXEC_SESSION_ROOT",
    "ENTERPRISE_EXEC_LOG_ARG_FIELDS",
    "ENTERPRISE_EXEC_SESSION_EVENTS",
    "ENTERPRISE_EXEC_REQUIRE_CALL_META",
)


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """Пороги и флаги одного вызова операции."""

    max_inline_result_bytes: int
    preview_bytes: int
    execution_timeout_sec: float
    persist_large_results: bool
    quality_check_enabled: bool
    logging_enabled: bool
    session_root: str
    log_argument_fields: tuple[str, ...]
    persist_session_events: bool
    require_call_meta: bool

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any]) -> ExecutionPolicy:
        """Собрать платформенную политику из объединённых настроек.

        Значения по умолчанию здесь — не молчаливые, а защита от неполного
        окружения: обязательные настройки проверяет реестр на старте, и до этого
        места код не доживает без них.
        """
        return cls(
            max_inline_result_bytes=int(settings.get("ENTERPRISE_EXEC_MAX_INLINE_BYTES") or 0),
            preview_bytes=int(settings.get("ENTERPRISE_EXEC_PREVIEW_BYTES") or 0),
            execution_timeout_sec=float(settings.get("ENTERPRISE_EXEC_TIMEOUT_SEC") or 0.0),
            persist_large_results=bool(settings.get("ENTERPRISE_EXEC_PERSIST_LARGE", True)),
            quality_check_enabled=bool(settings.get("ENTERPRISE_EXEC_QUALITY_CHECK", True)),
            logging_enabled=bool(settings.get("ENTERPRISE_EXEC_LOGGING", True)),
            session_root=str(settings.get("ENTERPRISE_EXEC_SESSION_ROOT") or ""),
            log_argument_fields=_parse_field_list(
                settings.get("ENTERPRISE_EXEC_LOG_ARG_FIELDS")
            ),
            persist_session_events=bool(settings.get("ENTERPRISE_EXEC_SESSION_EVENTS", False)),
            require_call_meta=bool(settings.get("ENTERPRISE_EXEC_REQUIRE_CALL_META", False)),
        )


def _parse_field_list(raw: Any) -> tuple[str, ...]:
    """Разобрать белый список полей аргументов.

    Строка через запятую — формат файла настроек. Пустое значение допустимо и
    означает «не логировать ни одного поля структурированно»: остальное уходит
    размером и хешем, а не телом.
    """
    if raw is None:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return tuple(
        part.strip() for part in str(raw).replace("\n", ",").split(",") if part.strip()
    )


def _override(target: ExecutionPolicy, scope: str, raw: Any) -> ExecutionPolicy:
    """Применить переопределение уровня capability или операции.

    Ключи секции совпадают с полями политики: ``max_inline_result_bytes`` в
    ``platform.json`` попадает в ``max_inline_result_bytes`` политики. Ошибка
    разбора значения не молчит — неверный тип порога должен остановить старт, а
    не тихо оставить прежнее значение.
    """
    if not isinstance(raw, Mapping):
        return target
    known = {field_name for field_name in ExecutionPolicy.__dataclass_fields__}
    changes: dict[str, Any] = {}
    for key, value in raw.items():
        name = str(key).strip()
        if name not in known:
            raise ValueError(f"{scope}: неизвестный ключ политики {name!r}")
        changes[name] = _coerce(name, value, scope)
    return replace(target, **changes) if changes else target


def _coerce(name: str, value: Any, scope: str) -> Any:
    current = ExecutionPolicy.__dataclass_fields__[name].type
    try:
        if "int" in str(current):
            return int(value)
        if "float" in str(current):
            return float(value)
        if "bool" in str(current):
            return bool(value)
        if "tuple" in str(current):
            return _parse_field_list(value)
        return str(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{scope}: ключ {name!r} имеет неверное значение {value!r}") from exc


def resolve_policy(
    base: ExecutionPolicy,
    *,
    capability: str = "",
    tool: str = "",
    overrides: Mapping[str, Any] | None = None,
) -> ExecutionPolicy:
    """Разрешить политику вызова: платформа → capability → операция.

    ``overrides`` — секции capability из ``platform.json``. Уровень операции
    перекрывает всё: у конкретной операции могут быть другие и потолок, и
    необходимость сохранять крупный ответ.
    """
    scopes = overrides or {}
    policy = _override(base, "execution", scopes.get("execution"))
    for name in (f"{capability}.execution", f"{tool}.execution"):
        if name in scopes:
            policy = _override(policy, name, scopes[name])
    return policy
