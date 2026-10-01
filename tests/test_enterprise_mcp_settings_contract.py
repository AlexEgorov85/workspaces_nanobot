"""Контракт экспорта: агент не отдаёт настройки, которых нет в реестре MCP.

Дефект, который закрывает этот файл
-----------------------------------

``_child_env`` агента знал про ``ENTERPRISE_SCRIPTS_REGISTRY_TABLE`` и
``ENTERPRISE_AUDIT_TABLES`` недостаточно: обе переменные у платформы без
дефолта, и без явного экспорта capability ``audit`` отвечала
``registry_unavailable`` на каждую операцию. То есть способность выключалась
тихо — при зелёных юнит-тестах самой capability, которые ставят переменные
через ``monkeypatch.setenv``.

Сторож с другой стороны (в платформе) проверяет, что читается только
объявленное. Этот — что объявляется только читаемое. Вместе они закрывают
петлю: переменная, которую агент экспортирует, а платформа не читает, и
переменная, которую платформа читает, а агент не экспортирует, обе падают.

Реестр читается как данные, а не импортируется как зависимость: агент и
платформа лежат в одном репозитории, и путь известен, но постоянная
зависимость тестов агента от внутреннего пакета платформы была бы
направлением, которое архитектурный страж платформы считает запрещённым.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT = REPO_ROOT / "lib" / "services" / "enterprise_mcp_client.py"
PLATFORM_ROOT = REPO_ROOT / "mcp-platform"
SETTINGS_MODULE = PLATFORM_ROOT / "libs" / "enterprise_common" / "settings.py"

#: Имена, которые агент вынужден отдавать даже без реестра: они
#: достаются из окружения по договорённости, а не из конфигурации.
ENV_PASSTHROUGH = frozenset({"DATABASE_URL", "PG_DSN", "PYTHONIOENCODING"})

#: Без кавычек: в AST ``ast.Constant`` уже содержит содержимое строки, а не
#: её запись в исходнике. С требованием кавычек скан молча находил ноль имён
#: — и все проверки выше проходили вхолостую.
EXPORTED_PATTERN = re.compile(r"ENTERPRISE_[A-Z0-9_]+")


def _load_registry() -> object:
    """Загрузить модуль реестра платформы как данные."""
    if not SETTINGS_MODULE.exists():
        pytest.fail(f"реестр настроек не найден: {SETTINGS_MODULE}")
    spec = importlib.util.spec_from_file_location(
        "_mcp_settings_registry_for_test", SETTINGS_MODULE
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    import sys

    # ``@dataclass`` на этапе создания класса ищет ``sys.modules[cls.__module__]``.
    # Без регистрации здесь он получает None и падает с AttributeError.
    sys.modules[spec.name] = module
    # Модуль тянет libs.enterprise_common.errors — нужен корень платформы.
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


def _exported_names() -> set[str]:
    """Имена, которые агент пишет в окружение дочернего процесса."""
    tree = ast.parse(CLIENT.read_text(encoding="utf-8"), filename=str(CLIENT))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            names |= set(EXPORTED_PATTERN.findall(node.value))
    return names


class TestExportMatchesRegistry:
    def test_registry_loads(self) -> None:
        assert _load_registry(), "реестр платформы пуст"

    def test_every_exported_variable_is_declared(self) -> None:
        """Ядро контракта: экспортировать можно только объявленное."""
        registry = _load_registry()
        exported = _exported_names()
        assert exported, "экспорт не найден — тест деградирует вхолостую"
        undeclared = sorted(exported - set(registry.BY_ANY_NAME) - ENV_PASSTHROUGH)
        assert not undeclared, (
            f"агент экспортирует переменные, которых нет в реестре платформы: "
            f"{undeclared}. Платформа их не читает — экспорт бесполезен и "
            "вводит в заблуждение при отладке."
        )

    def test_agent_owned_settings_are_not_in_the_platform_file(self) -> None:
        """Настройка агента не должна просочиться в ``platform.json``.

        Дублирование значений в двух местах разъезжается при первой же
        правке, и виноват будет тот, кто чинит последствия. Ключи файла
        берутся из реестра, а не выписываются здесь: иначе проверка
        подтверждала бы саму себя.
        """
        import json

        registry = _load_registry()
        platform_file = PLATFORM_ROOT / "platform.json"
        raw = json.loads(platform_file.read_text(encoding="utf-8"))

        def flatten(node: dict, prefix: str = "") -> set[str]:
            out: set[str] = set()
            for key, value in node.items():
                dotted = f"{prefix}{key}"
                if isinstance(value, dict):
                    out |= flatten(value, f"{dotted}.")
                else:
                    out.add(dotted)
            return out

        file_keys = {k for k in flatten(raw) if not k.startswith("_")}
        assert file_keys, "platform.json пуст — настройки платформы исчезли"
        for key in file_keys:
            assert key in registry.BY_FILE_KEY, (
                f"{key!r} не зарегистрирован как настройка платформы"
            )
            name = registry.BY_FILE_KEY[key].name
            assert registry.BY_NAME[name].owner == registry.OWNER_PLATFORM, (
                f"{key!r} в файле, но {name} принадлежит агенту"
            )


class TestGuardIsNotVacuous:
    def test_exported_names_are_actually_found(self) -> None:
        """Если скан перестанет видеть экспорт, тесты выше станут зелёными
        вхолостую. Этот тест падает первым."""
        names = _exported_names()
        assert len(names) >= 10, f"найдено подозрительно мало имён: {sorted(names)}"
        assert "ENTERPRISE_SCRIPTS_REGISTRY_TABLE" in names
        assert "ENTERPRISE_AUDIT_TABLES" in names

    def test_client_actually_builds_child_env(self) -> None:
        source = CLIENT.read_text(encoding="utf-8")
        assert "def _child_env" in source, "клиент потерял _child_env"
        assert "ENTERPRISE_SCRIPTS_REGISTRY_TABLE" in source, (
            "экспорт реестра скриптов пропал — capability audit снова мертва"
        )
