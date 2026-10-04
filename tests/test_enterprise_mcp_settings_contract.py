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

Теперь контракт такой: **каждое значение лежит в ``platform.json``, кроме
тех, что объявлены владельцем «агент»; их приносит блок настроек файлом**
``--agent-settings-file`` (``specs/runtime/platform-settings``), и окружение
процесса в этом канале не участвует вовсе. Словарь таких ключей
объявляет платформа, в реестре, с владельцем ``OWNER_AGENT``.

Проверяются все три утверждения — иначе страж был бы вхолостую: агент не
экспортирует значений в дочерний процесс, у платформы ровно те
агентские настройки, что образуют словарь блока, и агентский ключ не может
появиться в ``platform.json``.

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

    def test_agent_owned_settings_are_exactly_the_block_dictionary(self) -> None:
        """Агентских настроек столько, сколько ключей в словаре блока.

        Прежний страж требовал обратного — «у платформы нет ни одной
        настройки, которой владеет агент», — и был верен для доставки
        флагом: тогда канала не существовало и объявление владельца было
        недостижимым. Канал появился (блок), и требование «объявление
        владельца есть, и оно наполнено» стало условием работоспособности:
        словарь приёма объявлен реестром, и пустой он означал бы, что блок
        нечем наполнить.
        """
        registry = _load_registry()
        agent_owned = sorted(
            s.name for s in registry.SETTINGS if s.owner == registry.OWNER_AGENT
        )
        dictionary = sorted(registry.agent_settings_dictionary())
        assert agent_owned, (
            "в реестре нет ни одной настройки с владельцем «агент»: блок "
            "настроек нечем наполнить, и канал приёма не работает"
        )
        assert dictionary, (
            "словарь блока пуст: платформа не принимает ни одного ключа, "
            "и объявление владельца в реестре недостижимо"
        )
        assert sorted(registry.BY_NAME[name].key for name in agent_owned) == (
            dictionary
        ), (
            f"ключи блока {dictionary} не совпадают с агентскими настройками "
            f"реестра {agent_owned}: словарь приёма и объявление разошлись"
        )
        assert not set(dictionary) & set(registry.BY_FILE_KEY), (
            "ключ блока объявлен ещё и в platform.json: у значения "
            "появилось два владельца, и один из них перекроет другой молча"
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


class TestClientDoesNotOwnTheSessionRoot:
    """Корень файлов сессии объявляет платформа, и клиент его не везёт.

    Агентский ``gateway.agent.session_files.root`` действует только при
    выключенной платформе — тогда платформы, которая объявила бы второй корень,
    просто нет. Если бы клиент всё равно вез объявление агента в процесс, то
    при включённой платформе у одного значения появилось бы два источника, и
    более приоритетный (аргументы процесса) тихо победил бы.
    """

    def _client(self, tmp_path: Path) -> object:
        from lib.services.enterprise_mcp_client import client_from_settings

        client = client_from_settings(
            {
                "enterprise_mcp": {
                    "enabled": True,
                    "command": "python",
                    "args": ["-m", "servers.enterprise.server"],
                },
                "session_files": {"root": str(tmp_path / "agent-sessions")},
            }
        )
        assert client is not None, "раздел enterprise_mcp включён, клиент обязан собраться"
        return client

    def test_process_args_carry_no_session_root(self, tmp_path: Path) -> None:
        client = self._client(tmp_path)
        args = [str(arg) for arg in (client.describe()["args"] or [])]

        assert not [arg for arg in args if "agent-sessions" in arg or "session_root" in arg], (
            f"корень файлов сессии попал в аргументы процесса: {args}"
        )

    def test_child_env_adds_no_root_variable(self, tmp_path: Path) -> None:
        """Сверх наследования — только принудительная кодировка.

        Форма проверки выбрана так, чтобы результат не зависел от того, что
        окружение процесса уже содержит: сравниваются ключи, которых в нём не
        было, а не их значения. Кодировка из исключения — её же отдельно
        охраняет ``test_child_env_adds_nothing_but_encoding``, и на её фоне
        любая другая новая переменная была бы незаметна.
        """
        import os

        client = self._client(tmp_path)
        env = client._child_env()
        new_keys = set(env) - set(os.environ) - {"PYTHONIOENCODING"}
        changed = {
            key for key, value in env.items() if key in os.environ and os.environ[key] != value
        }

        assert not new_keys, (
            f"в окружение процесса добавлены свои переменные: {sorted(new_keys)} — "
            f"корень файлов сессии объявляет платформа, и переменная перебила бы "
            f"её platform.json"
        )
        assert changed <= {"PYTHONIOENCODING"}, (
            f"в окружении процесса переопределено: {sorted(changed)} — кроме "
            f"кодировки агент не переписывает ничего"
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
