"""Граница между агентом и платформой: настройки не пересекают её.

Дефект, который закрывает этот файл
-----------------------------------

Настройки MCP приходили в процесс из окружения агента: ``_child_env``
собирал ``ENTERPRISE_AUDIT_TABLES``, ``ENTERPRISE_VECTOR_*``,
``ENTERPRISE_EMBED_*``, ``ENTERPRISE_SNAPSHOT_PATH`` и
``ENTERPRISE_EXPECTED_TABLES``. Формально платформа читала настройки агента.

Хуже всего то, что это выглядело работающим. Окружение приоритетнее файла,
поэтому как только у платформы появлялось **собственное** объявление
настройки в ``platform.json``, старая переменная молча его затирала: файл
выглядел настроенным, а применялось чужое значение, и разница была видна
только в ``source()`` в баннере. А для объявлений с меткой (реестр
предустановленных скриптов) переменная была не просто неверной — она теряла
метку, и capability ``audit`` отвечала ``registry_unavailable`` на каждую
операцию.

Теперь контракт обратный: **агент не объявляет ничего для платформы, у
платформы нет ни одной настройки, которой владеет агент, а каждое значение
лежит в ``platform.json``.** Проверяются все три утверждения — иначе страж
был бы вхолостую.

Реестр читается как данные, а не импортируется как зависимость: агент и
платформа лежат в одном репозитории, и путь известен, но постоянная
зависимость тестов агента от внутреннего пакета платформы была бы
направлением, которое архитектурный страж платформы считает запрещённым.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "lib" / "services" / "enterprise_mcp_client.py"
APPLICATION_CONTEXT = REPO_ROOT / "lib" / "core" / "application_context.py"
PLATFORM_ROOT = REPO_ROOT / "mcp-platform"
PLATFORM_FILE = PLATFORM_ROOT / "platform.json"
SETTINGS_MODULE = PLATFORM_ROOT / "libs" / "enterprise_common" / "settings.py"

#: Без кавычек: в AST ``ast.Constant`` уже содержит содержимое строки, а не
#: её запись в исходнике. С требованием кавычек скан молча находил ноль имён
#: — и проверки проходили вхолостую.
EXPORTED_PATTERN = re.compile(r"ENTERPRISE_[A-Z0-9_]+")

#: Эти имена не настройки платформы, а соглашения о процессе и базе. Их
#: клиент не пишет — он наследует ``os.environ``, — но и запрещать их в
#: тексте модуля бессмысленно: они упоминаются в докстрингах и в проверках.
ALLOWED_NAMES = frozenset({"DATABASE_URL", "PG_DSN", "PYTHONIOENCODING"})


def _load_registry() -> object:
    """Загрузить модуль реестра платформы как данные."""
    if not SETTINGS_MODULE.exists():
        pytest.fail(f"реестр настроек не найден: {SETTINGS_MODULE}")
    spec = importlib.util.spec_from_file_location(
        "_mcp_settings_registry_for_test", SETTINGS_MODULE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # ``@dataclass`` на этапе создания класса ищет ``sys.modules[cls.__module__]``.
    # Без регистрации здесь он получает None и падает с AttributeError.
    sys.modules[spec.name] = module
    platform_str = str(PLATFORM_ROOT)
    added = platform_str not in sys.path
    if added:
        sys.path.insert(0, platform_str)
    try:
        spec.loader.exec_module(module)
    finally:
        if added and platform_str in sys.path:
            sys.path.remove(platform_str)
        sys.modules.pop(spec.name, None)
    return module


def _string_literals(source: str) -> set[str]:
    """Строковые литералы модуля без докстрингов.

    Докстринг пропускается: он описывает историю («раньше экспортировали
    ``ENTERPRISE_AUDIT_TABLES``»), а не то, что попадает в окружение.
    """
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def _mentioned_names(path: Path) -> set[str]:
    names: set[str] = set()
    for literal in _string_literals(path.read_text(encoding="utf-8")):
        names |= set(EXPORTED_PATTERN.findall(literal))
    return names


def _flatten(node: dict, prefix: str = "") -> set[str]:
    """Ключи файла в точечном виде.

    ``_about`` и прочие подчёркнутые — комментарии в данных, и реестр
    пропускает такие ключи на любом уровне.
    """
    out: set[str] = set()
    for key, value in node.items():
        if str(key).startswith("_"):
            continue
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and not str(key) in ("indexes",):
            out |= _flatten(value, f"{dotted}.")
        else:
            out.add(dotted)
    return out


class TestAgentExportsNothing:
    def test_client_writes_no_platform_settings(self) -> None:
        """Ядро контракта: из клиента в окружение процесса не уходит ничего.

        Любое ``ENTERPRISE_*`` в строковом литерале клиента — это попытка
        объявить настройку платформы из агента. Пока такое имя есть, оно
        перебивает значение из ``platform.json`` (окружение приоритетнее), и
        файл снова становится декорацией.
        """
        mentioned = _mentioned_names(CLIENT)
        offenders = sorted(mentioned - ALLOWED_NAMES)
        assert not offenders, (
            f"клиент упоминает настройки платформы в коде: {offenders}. "
            f"Объявления принадлежат mcp-platform/platform.json; экспорт "
            f"молча затирает файловое значение."
        )

    def test_child_env_adds_nothing_but_encoding(self) -> None:
        """В окружение процесса добавляется ровно одна переменная.

        ``_child_env`` наследует ``os.environ`` целиком и добавляет
        ``PYTHONIOENCODING``. Любая другая запись — это объявление платформы
        из агента, пусть и под другим именем.
        """
        source = CLIENT.read_text(encoding="utf-8")
        tree = ast.parse(source)
        docstrings = {
            id(node.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        }
        child_env = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_child_env"
        )
        added: set[str] = set()
        for node in ast.walk(child_env):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
                and "PYTHONIOENCODING" in node.value
            ):
                added.add(node.value)
        assert added <= {"PYTHONIOENCODING"}, (
            f"_child_env пишет в окружение: {sorted(added)} — кроме кодировки "
            f"процесс не должен получать от агента ничего"
        )

    def test_application_context_no_longer_computes_platform_declarations(self) -> None:
        """Агент не вычисляет то, что объявлено у платформы.

        Путь снимка и список таблиц для ``schema_check`` считались здесь и
        уходили в процесс. Теперь они живут в ``platform.json``, и функции,
        которые их считали, вырезаны: оставить их значило бы держать второе
        место, где то же самое вычисляется уже никем не используемым кодом.
        """
        source = APPLICATION_CONTEXT.read_text(encoding="utf-8")
        for dead in ("_expected_tables", "_resolve_snapshot_file", "_qualify"):
            assert f"def {dead}(" not in source, (
                f"{dead} снова объявлен: агент вычисляет то, чем владеет платформа"
            )


class TestPlatformDeclaresEverything:
    def test_registry_loads(self) -> None:
        assert _load_registry(), "реестр платформы пуст"

    def test_no_setting_is_owned_by_the_agent(self) -> None:
        """У платформы не осталось настроек, которые приходят от агента."""
        registry = _load_registry()
        agent_owned = [
            s.name for s in registry.SETTINGS if s.owner == registry.OWNER_AGENT
        ]
        assert not agent_owned, (
            f"настройки по-прежнему принадлежат агенту: {agent_owned}. "
            f"Пока они в реестре, платформа зависит от того, что агент "
            f"экспортирует, а окружение перебивает platform.json."
        )

    def test_every_file_key_is_a_platform_setting(self) -> None:
        """Ключ в файле — объявленная настройка платформы.

        Ключи берутся из реестра, а не выписываются здесь: иначе проверка
        подтверждала бы саму себя.
        """
        registry = _load_registry()
        raw = json.loads(PLATFORM_FILE.read_text(encoding="utf-8"))
        # ``profiles`` — оверлей имён таблиц, а не настройки: он разбирается
        # отдельно (read_profile_overlay) и проверяется своими тестами.
        # Здесь секция снимается, иначе любое объявление профиля падало бы
        # как «не зарегистрирован как настройка платформы».
        for section in getattr(registry, "RESERVED_SECTIONS", ()):
            raw.pop(section, None)
        file_keys = _flatten(raw)
        assert file_keys, "platform.json пуст — настройки платформы исчезли"
        assert "db.dsn" in file_keys, (
            "db.dsn пропал из platform.json — DSN вернулся к одному "
            "источнику, и настройка агента снова решает, к какой базе "
            "подключается процесс"
        )
        for key in sorted(file_keys):
            assert key in registry.BY_FILE_KEY, (
                f"{key!r} не зарегистрирован как настройка платформы"
            )
            name = registry.BY_FILE_KEY[key].name
            assert registry.BY_NAME[name].owner == registry.OWNER_PLATFORM, (
                f"{key!r} в файле, но {name} принадлежит агенту"
            )


class TestGuardIsNotVacuous:
    def test_the_scanner_actually_finds_names(self) -> None:
        """Если скан перестанет видеть имена, проверки выше станут зелёными
        вхолостую. Этот тест падает первым."""
        planted = 'env["ENTERPRISE_AUDIT_TABLES"] = "x"\n'
        assert EXPORTED_PATTERN.findall(planted) == ["ENTERPRISE_AUDIT_TABLES"]

    def test_the_scanner_skips_docstrings(self) -> None:
        """История в докстринге не должна читаться как объявление."""
        source = 'def f():\n    """Раньше экспортировали ENTERPRISE_AUDIT_TABLES."""\n    return 1\n'
        assert not _mentioned_names_from_source(source)

    def test_client_actually_builds_child_env(self) -> None:
        source = CLIENT.read_text(encoding="utf-8")
        assert "def _child_env" in source, "клиент потерял _child_env"
        assert "dict(os.environ)" in source, (
            "клиент перестал наследовать окружение агента: DSN и секреты "
            "не доедут до процесса, и он не поднимется"
        )


def _mentioned_names_from_source(source: str) -> set[str]:
    tree = ast.parse(source)
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    names: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            names |= set(EXPORTED_PATTERN.findall(node.value))
    return names
