"""Startup schema validation — проверка наличия обязательных runtime-таблиц.

Единая точка проверки перед подъёмом сервисов ``ApplicationContext.start()``.
Источник имён — ``SETTINGS["channels"]["postgres"]`` и
``SETTINGS["logging"]["db"]`` (те же 6 ключей, что проходят
``validate_runtime_isolation`` в ``config.py``). Имена не зашиты в
коде проверки — они передаются как параметры SQL.

Failure mode: при отсутствии любой из таблиц выбрасывается
``SchemaValidationError`` (наследник ``config.ConfigurationError``).
Существующие startup-boundary в ``gateway.main()`` / ``cli_agent.main()``
ловят ``ConfigurationError`` и превращают в ``exit 2`` + ``stderr`` —
никаких изменений в entrypoint не требуется.

См. ``openspec/specs/runtime/startup-schema-validation/spec.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from loguru import logger

from config import ConfigurationError

DEFAULT_TIMEOUT_SEC = 5.0

_EXPECTED_KEYS: tuple[tuple[str, ...], ...] = (
    ("channels", "postgres", "table_name"),
    ("channels", "postgres", "messages_table"),
    ("channels", "postgres", "meta_table"),
    ("logging", "db", "table_name"),
    ("logging", "db", "question_runs_table"),
)


def _hint_for_profile(profile: str) -> str:
    """Actionable-подсказка для создания runtime-таблиц по профилю.

    Возвращает команду CLI, которую оператор должен запустить,
    чтобы создать недостающие таблицы. Для известных профилей
    возвращается конкретная команда; для неизвестных —
    generic-вариант без указания конкретной утилиты.

    Examples:
        >>> _hint_for_profile("prod")
        'python tools/migrate.py --apply'
        >>> _hint_for_profile("test")
        'python tools/apply_test_profile_tables.py'
        >>> _hint_for_profile("dev")
        'примените миграции для выбранного профиля'
    """
    if profile == "prod":
        return "python tools/migrate.py --apply"
    if profile == "test":
        return "python tools/apply_test_profile_tables.py"
    return "примените миграции для выбранного профиля"


def _unwrap_settings(settings: Any) -> dict[str, Any]:
    """Развернуть ``_LazySettings`` proxy в сырой ``dict``.

    ``ctx.settings`` хранит ``_LazySettings`` (см. ``config.py``),
    который НЕ является наследником ``dict`` (mapping-style proxy).
    ``isinstance(proxy, dict) == False``, поэтому обход через
    ``cur[k]`` с проверкой ``isinstance(cur, dict)`` ломается на
    корне. Здесь используем тот же паттерн, что в
    ``config.require_setting`` / ``config.get_setting`` —
    ``SETTINGS._inner_dict if isinstance(SETTINGS, _LazySettings)
    else SETTINGS``.
    """
    if settings is None:
        return {}
    if hasattr(settings, "_inner_dict") and isinstance(
        getattr(settings, "_inner_dict", None), dict
    ):
        return settings._inner_dict
    if isinstance(settings, dict):
        return settings
    return {}


@dataclass(frozen=True)
class MissingTable:
    """Описание одной недостающей таблицы.

    Attributes:
        schema: имя схемы (например, ``"public"``).
        name: короткое имя таблицы.
    """

    schema: str
    name: str

    @property
    def full_name(self) -> str:
        """Полное имя в формате ``schema.table``."""
        return f"{self.schema}.{self.name}"


class SchemaValidationError(ConfigurationError):
    """Блокирует старт при отсутствии обязательных runtime-таблиц.

    Наследник ``ConfigurationError`` попадает в единый startup-boundary
    ``gateway.main()`` / ``cli_agent.main()`` (exit 2 + stderr).
    """

    def __init__(self, missing: list[MissingTable], profile: str) -> None:
        self.missing = list(missing)
        self.profile = profile
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        lines: list[str] = [
            f"Не найдены обязательные runtime-таблицы "
            f"(profile={self.profile!r}):",
            "Отсутствуют таблицы:",
        ]
        for t in self.missing:
            lines.append(f"  - {t.full_name}")
        lines.append(f"Подсказка: {_hint_for_profile(self.profile)}")
        return "\n".join(lines)


class _MissingConfigKeys(SchemaValidationError):
    """Срабатывает, когда в settings отсутствуют ожидаемые ключи.

    Это уже ошибка конфигурации, но используем тот же класс, чтобы
    entrypoint не различал «нет таблиц» / «нет ключей в settings».
    Формат сообщения — отдельный (``MissingTable`` тут неуместен:
    ключ конфига и таблица БД — разные сущности).
    """

    def __init__(self, missing_keys: list[str], profile: str) -> None:
        super().__init__([], profile)
        self.missing_config_keys: list[str] = list(missing_keys)
        # Перезаписать message после init родителя.
        Exception.__init__(self, self._build_config_message())

    def _build_config_message(self) -> str:
        lines: list[str] = [
            f"Не найдены обязательные ключи конфигурации "
            f"(profile={self.profile!r}):",
            "Отсутствуют ключи:",
        ]
        for k in self.missing_config_keys:
            lines.append(f"  - {k}")
        lines.append(
            "Подсказка: определите ключи в project.json "
            "в секциях channels.postgres.* / logging.db.*"
        )
        return "\n".join(lines)


class SchemaValidationService:
    """Сервис pre-startup проверки схемы.

    Использование::

        svc = SchemaValidationService()
        svc.validate(ctx.settings, fetch=fetch)  # None → ОК
        # либо
        try:
            svc.validate(ctx.settings, fetch=fetch)
        except SchemaValidationError as exc:
            ...  # exc.missing: list[MissingTable], exc.profile
    """

    DEFAULT_TIMEOUT_SEC = DEFAULT_TIMEOUT_SEC

    @staticmethod
    def expected_table_names(settings: Any) -> list[tuple[str, str]]:
        """Извлечь список ``(schema, table_name)`` из merged SETTINGS.

        Принимает как сырой dict, так и ``_LazySettings`` proxy
        (разворачивается через ``_inner_dict``). Источник истины —
        5 ключей ``channels.postgres.{table_name, messages_table,
        meta_table}`` + ``logging.db.{table_name, question_runs_table}``.
        Если какого-то ключа нет — выбрасывается ``_MissingConfigKeys``
        (наследник ``SchemaValidationError`` → ``ConfigurationError``).
        """
        raw = _unwrap_settings(settings)
        missing_keys: list[str] = []
        names: list[tuple[str, str]] = []
        for path in _EXPECTED_KEYS:
            cur: Any = raw
            ok = True
            for k in path:
                if not isinstance(cur, dict) or k not in cur:
                    ok = False
                    break
                cur = cur[k]
            if not ok or not isinstance(cur, str) or not cur:
                missing_keys.append(".".join(path))
                continue
            names.append(("public", cur))
        if missing_keys:
            profile = str(raw.get("profile", "<unknown>"))
            raise _MissingConfigKeys(missing_keys, profile)
        return names

    @staticmethod
    def check_tables(
        fetch: Callable[..., list[dict[str, Any]]],
        expected: list[tuple[str, str]],
        *,
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> list[MissingTable]:
        """Один SELECT к ``information_schema.tables``.

        Args:
            fetch: callable с сигнатурой ``(sql, *params) -> list[dict]``
                — адаптер для ``utils.db.fetch`` или mock.
            expected: список ``(schema, table_name)``.
            timeout_sec: параметр для совместимости с вызывающим кодом
                (сама логика таймаута реализуется на уровне пула).

        Returns:
            Список недостающих таблиц; пустой, если всё на месте.
        """
        del timeout_sec
        if not expected:
            return []
        schemas = sorted({s for s, _ in expected})
        names = [n for _, n in expected]
        placeholders = ",".join(["%s"] * len(names))
        schema_placeholders = ",".join(["%s"] * len(schemas))
        sql = (
            "SELECT table_schema, table_name "
            "FROM information_schema.tables "
            f"WHERE table_schema IN ({schema_placeholders}) "
            f"  AND table_type = 'BASE TABLE' "
            f"  AND table_name IN ({placeholders})"
        )
        rows = fetch(sql, *schemas, *names)
        existing = {(r["table_schema"], r["table_name"]) for r in rows}
        return [
            MissingTable(schema=s, name=n)
            for s, n in expected
            if (s, n) not in existing
        ]

    @classmethod
    def validate(
        cls,
        settings: Any,
        *,
        fetch: Callable[..., list[dict[str, Any]]],
        timeout_sec: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        """Верхний уровень: получить expected → проверить → raise при missing.

        Args:
            settings: merged SETTINGS (с ``profile``). Может быть
                сырым dict или ``_LazySettings`` proxy.
            fetch: адаптер для SELECT (обычно ``utils.db.fetch``).
            timeout_sec: пробрасывается в ``check_tables``.

        Raises:
            SchemaValidationError: при non-empty ``missing`` ИЛИ при
                отсутствии ожидаемых ключей в settings.
        """
        raw = _unwrap_settings(settings)
        expected = cls.expected_table_names(raw)
        missing = cls.check_tables(fetch, expected, timeout_sec=timeout_sec)
        if missing:
            profile = str(raw.get("profile", "<unknown>"))
            logger.error(
                "не пройдена startup-проверка схемы: profile={} отсутствуют={}",
                profile,
                [m.full_name for m in missing],
            )
            raise SchemaValidationError(missing, profile)


__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "MissingTable",
    "SchemaValidationError",
    "SchemaValidationService",
    "_hint_for_profile",
]
