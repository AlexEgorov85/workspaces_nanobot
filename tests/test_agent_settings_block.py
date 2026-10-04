"""Блок настроек агента на стороне агента: путь, чтение, публикация, argv.

Проверяется контракт ``specs/runtime/platform-settings`` со стороны агента:

* путь к блоку **объявляет платформа** (``platform.json → agent_settings``),
  и ``${NANOBOT_WORKSPACE}`` разворачивается тем же механизмом, что и у
  ``execution.session_root``;
* блок собирается из ``config.json`` агента и пишется один раз при старте;
* в argv уезжает ровно один аргумент — путь, и **ни одного значения**;
* отсутствующий файл читается как пустой блок, а неразбираемый — отказ с
  называнием файла;
* ключи блока совпадают с путями в конфигурации агента, поэтому добавление
  настройки — одна строка, а не правка кода запуска;
* платформа принимает ровно те ключи, которые агент наполняет.

Фикстуры — временные файлы и словари. Живой ``platform.json`` не читается:
он меняется вместе с чужим change'ом, и тест не должен зависеть от него.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from config import ConfigurationError
from lib.services import agent_settings as agent_settings_module
from lib.services.agent_settings import (
    AGENT_BLOCK_PATHS,
    AGENT_SETTINGS_DECLARATION,
    AGENT_SETTINGS_FILE_FLAG,
    JOURNAL_MIN_LEVEL_PATH,
    AgentSettingsBlock,
    block_values,
    declared_block_path,
    load_agent_settings,
    publish_agent_settings,
    read_declaration,
)
from lib.services.enterprise_mcp_client import client_from_settings

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_PACKAGE = PLATFORM_ROOT / "mcp-platform"


def platform_json(tmp_path: Path, declared: object = "__default__") -> Path:
    """Временный ``platform.json`` с объявлением пути к блоку."""
    path = tmp_path / "platform.json"
    if declared == "__default__":
        declared = "${NANOBOT_WORKSPACE}/data_store/agent-settings.json"
    body: dict[str, Any] = {"_about": "тестовая фикстура"}
    if declared is not None:
        body[AGENT_SETTINGS_DECLARATION] = declared
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def agent_config(min_level: object = "WARN") -> dict[str, Any]:
    """Конфигурация агента в той форме, в какой её видит ``client_from_settings``."""
    return {
        "profile": "",
        "enterprise_mcp": {"command": "python", "args": [], "enabled": True},
        "logging": {"db": {"min_level": min_level}},
    }


class TestDeclarationIsThePlatsform:
    """Путь объявляет платформа, агент его не вычисляет."""

    def test_key_is_top_level(self) -> None:
        assert AGENT_SETTINGS_DECLARATION == "agent_settings"

    def test_placeholder_is_resolved(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        resolved = declared_block_path(
            platform_json(tmp_path), env={"NANOBOT_WORKSPACE": str(workspace)}
        )
        assert resolved == workspace / "data_store" / "agent-settings.json"

    def test_other_placeholder_is_resolved(self, tmp_path: Path) -> None:
        # Тот же алфавит имён, что у ``${NANOBOT_WORKSPACE}`` в
        # ``execution.session_root``: латиница и подчёркивание.
        resolved = declared_block_path(
            platform_json(tmp_path, "${NANOBOT_TMP}/agent.json"),
            env={"NANOBOT_TMP": str(tmp_path / "elsewhere")},
        )
        assert resolved == tmp_path / "elsewhere" / "agent.json"

    def test_placeholder_of_unfamiliar_alphabet_is_left_alone(self, tmp_path: Path) -> None:
        # Неразвёрнутая подстановка видна в пути целиком, а не подставляется
        # пустотой: об этом говорит сам путь, и найти объявление можно глазами.
        resolved = declared_block_path(
            platform_json(tmp_path, "${БЛОКИ}/agent.json"), env={}
        )
        assert resolved == Path("${БЛОКИ}/agent.json")

    def test_missing_variable_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError) as excinfo:
            declared_block_path(platform_json(tmp_path), env={})
        assert "NANOBOT_WORKSPACE" in str(excinfo.value)

    def test_absent_declaration_is_not_an_error(self, tmp_path: Path) -> None:
        assert read_declaration(platform_json(tmp_path, None)) is None
        assert declared_block_path(platform_json(tmp_path, None)) is None

    def test_unreadable_declaration_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "platform.json"
        path.write_text("не json", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="не читается"):
            read_declaration(path)

    def test_broken_declaration_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="ожидался путь строкой"):
            read_declaration(platform_json(tmp_path, 42))


class TestReading:
    """Чтение блока: отсутствие — пусто, порча — отказ."""

    def test_reads_the_file(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-settings.json"
        path.write_text('{"logging.db.min_level": "WARN"}', encoding="utf-8")
        assert load_agent_settings(path) == {"logging.db.min_level": "WARN"}

    def test_absent_file_is_empty(self, tmp_path: Path) -> None:
        assert load_agent_settings(tmp_path / "нет-такого.json") == {}

    def test_unreadable_file_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-settings.json"
        path.write_text('{"logging.db.min_level": ', encoding="utf-8")
        with pytest.raises(ConfigurationError) as excinfo:
            load_agent_settings(path)
        assert "agent-settings.json" in str(excinfo.value)

    def test_file_that_is_not_an_object_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "agent-settings.json"
        path.write_text("[1, 2]", encoding="utf-8")
        with pytest.raises(ConfigurationError, match="ожидался объектом"):
            load_agent_settings(path)


class TestPublishing:
    """Блок собирается из конфигурации агента и пишется один раз."""

    def test_values_come_from_config(self) -> None:
        assert block_values(agent_config("WARN")) == {"logging.db.min_level": "WARN"}

    def test_empty_value_is_not_published(self) -> None:
        # «Оператор не задал» и «оператор задал пустое» — одно состояние; два
        # его представления разъезжаются при первом же чтении.
        assert block_values(agent_config("")) == {}
        assert block_values(agent_config(None)) == {}

    def test_absent_section_is_not_an_error(self) -> None:
        assert block_values({}) == {}

    def test_file_is_written_and_readable(self, tmp_path: Path) -> None:
        workspace = tmp_path / "workspace"
        block = publish_agent_settings(
            agent_config("WARN"),
            platform_json=platform_json(tmp_path),
            env={"NANOBOT_WORKSPACE": str(workspace)},
        )
        assert block is not None
        assert block.path == workspace / "data_store" / "agent-settings.json"
        assert load_agent_settings(block.path) == {"logging.db.min_level": "WARN"}

    def test_file_is_written_even_without_values(self, tmp_path: Path) -> None:
        block = publish_agent_settings(
            agent_config(""),
            platform_json=platform_json(tmp_path),
            env={"NANOBOT_WORKSPACE": str(tmp_path / "ws")},
        )
        assert block is not None
        assert block.path.exists()
        assert load_agent_settings(block.path) == {}

    def test_no_declaration_means_no_block(self, tmp_path: Path) -> None:
        assert (
            publish_agent_settings(
                agent_config(),
                platform_json=platform_json(tmp_path, None),
            )
            is None
        )

    def test_summary_names_the_values(self, tmp_path: Path) -> None:
        block = AgentSettingsBlock(
            path=tmp_path / "agent-settings.json",
            values={"logging.db.min_level": "WARN"},
        )
        assert "logging.db.min_level='WARN'" in block.summary()


class TestArgv:
    """В argv едет путь и ни одного значения."""

    def test_single_flag_and_path(self) -> None:
        path = Path("C:/ws/data_store/agent-settings.json")
        block = AgentSettingsBlock(path=path, values={"a": "b"})
        assert block.argv() == [AGENT_SETTINGS_FILE_FLAG, str(path)]

    def test_values_never_reach_argv(self) -> None:
        block = AgentSettingsBlock(
            path=Path("C:/ws/agent-settings.json"),
            values={"logging.db.min_level": "WARN"},
        )
        assert "WARN" not in " ".join(block.argv())

    def test_flag_is_a_literal_shared_with_the_platform(self) -> None:
        # Общего модуля у агента и платформы нет, поэтому литерал объявлен с
        # обеих сторон; равенство проверяет тест платформы, а этот — что флаг
        # не переименован по дороге.
        assert AGENT_SETTINGS_FILE_FLAG == "--agent-settings-file"

    def test_client_sends_the_block(self, tmp_path: Path) -> None:
        platform_json(tmp_path)
        config = agent_config("WARN")
        config["enterprise_mcp"]["cwd"] = str(tmp_path)
        client = client_from_settings(config)
        assert client is not None
        argv = list(client._args)  # noqa: SLF001 - проверяется контракт запуска
        assert AGENT_SETTINGS_FILE_FLAG in argv
        index = argv.index(AGENT_SETTINGS_FILE_FLAG)
        block_path = Path(argv[index + 1])
        assert load_agent_settings(block_path) == {"logging.db.min_level": "WARN"}

    def test_client_sends_no_values(self, tmp_path: Path) -> None:
        platform_json(tmp_path)
        config = agent_config("WARN")
        config["enterprise_mcp"]["cwd"] = str(tmp_path)
        client = client_from_settings(config)
        assert client is not None
        argv = list(client._args)  # noqa: SLF001
        assert "WARN" not in argv

    def test_client_without_declaration_sends_nothing(self, tmp_path: Path) -> None:
        platform_json(tmp_path, None)
        config = agent_config("WARN")
        config["enterprise_mcp"]["cwd"] = str(tmp_path)
        client = client_from_settings(config)
        assert client is not None
        assert AGENT_SETTINGS_FILE_FLAG not in list(client._args)  # noqa: SLF001

    def test_client_with_broken_declaration_fails_loudly(self, tmp_path: Path) -> None:
        # Объявленный, но негодный путь хуже необъявленного: по нему полагают
        # блок, которого не будет, и старт падает с называнием файла.
        (tmp_path / "platform.json").write_text("не json", encoding="utf-8")
        config = agent_config("WARN")
        config["enterprise_mcp"]["cwd"] = str(tmp_path)
        with pytest.raises(ConfigurationError, match="не читается"):
            client_from_settings(config)

    def test_client_without_cwd_sends_nothing(self) -> None:
        client = client_from_settings(agent_config())
        assert client is not None
        assert AGENT_SETTINGS_FILE_FLAG not in list(client._args)  # noqa: SLF001


class TestOnePlaceToAddASetting:
    """Добавление настройки — одна строка, и платформа её принимает."""

    def test_block_key_equals_the_agent_config_path(self) -> None:
        for key, path in AGENT_BLOCK_PATHS.items():
            assert key == ".".join(path), (
                f"{key!r} не совпадает с путём в конфигурации агента {path}. "
                "Совпадение и делает запись одной строкой."
            )

    def test_every_block_key_is_accepted_by_the_platform(self) -> None:
        # Граница между двумя деревьями проверяется здесь, а не в коде: в коде
        # такой импорт означал бы общий модуль, а общего модуля у агента и
        # платформы быть не должно.
        import sys

        root = str(PLATFORM_PACKAGE)
        added = root not in sys.path
        if added:
            sys.path.insert(0, root)
        try:
            from libs.enterprise_common.settings import agent_settings_dictionary
        finally:
            if added:
                sys.path.remove(root)
        unknown = sorted(set(AGENT_BLOCK_PATHS) - set(agent_settings_dictionary()))
        assert unknown == [], (
            f"агент наполняет ключи {unknown}, которых нет в словаре платформы: "
            "сервер откажет в блоке на старте"
        )

    def test_journal_threshold_path_is_unchanged(self) -> None:
        # Путь в конфигурации агента прежний: ``config.JOURNAL_MIN_LEVEL_PATH``
        # и ``tests/test_mcp_platform_declaration.py`` сверяют его равенство.
        assert JOURNAL_MIN_LEVEL_PATH == ("logging", "db", "min_level")

    def test_module_reexports_the_same_path(self) -> None:
        from lib.services import enterprise_mcp_client

        assert enterprise_mcp_client.JOURNAL_MIN_LEVEL_PATH == JOURNAL_MIN_LEVEL_PATH


class TestFilePermissions:
    """Файл блока создаётся с правами, не доступными группе.

    Проверяется сам вызов с объявленным режимом, а не ``stat``: на POSIX это
    дало бы ``0o600``, а на Windows ``os.chmod`` умеет только read-only бит,
    ``0o600`` читается как «не read-only», и ``stat().st_mode`` остаётся
    ``0o666`` — то есть ``stat`` проверял бы на Windows отсутствие того, чего
    Windows выразить не может в принципе. Там доступ ограничивает ACL,
    унаследованная от каталога.
    """

    def test_mode_is_applied_when_the_file_is_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        platform_json(tmp_path)
        seen: list[tuple[str, int]] = []
        real_chmod = os.chmod

        def spy(path, mode, *args, **kwargs):  # type: ignore[no-untyped-def]
            seen.append((str(path), mode))
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "chmod", spy)
        block = publish_agent_settings(
            agent_config("WARN"),
            platform_json=platform_json(tmp_path),
            env={"NANOBOT_WORKSPACE": str(tmp_path / "ws")},
        )
        assert block is not None
        assert seen == [(str(block.path), agent_settings_module.BLOCK_FILE_MODE)]
        assert agent_settings_module.BLOCK_FILE_MODE == 0o600

    @pytest.mark.skipif(os.name != "posix", reason="режим файла выражается только в POSIX")
    def test_group_cannot_read_the_file(self, tmp_path: Path) -> None:
        block = publish_agent_settings(
            agent_config("WARN"),
            platform_json=platform_json(tmp_path),
            env={"NANOBOT_WORKSPACE": str(tmp_path / "ws")},
        )
        assert block is not None
        assert block.path.stat().st_mode & 0o077 == 0


class TestOldDeliveryIsGone:
    """Флаг, доставлявший значение, снят в том же изменении."""

    def _flag_usages(self, path: Path) -> list[str]:
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", [])
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    docstrings.add(id(body[0].value))
        found: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                if "--log-min-level" in node.value:
                    found.append(repr(node.value))
            elif isinstance(node, ast.Name) and "LOG_MIN_LEVEL_FLAG" in node.id:
                found.append(node.id)
            elif isinstance(node, ast.Attribute) and "LOG_MIN_LEVEL_FLAG" in node.attr:
                found.append(node.attr)
        return found

    def test_flag_is_absent_from_the_agent_tree(self) -> None:
        offenders = {
            str(path.relative_to(PLATFORM_ROOT)): usages
            for path in PLATFORM_ROOT.joinpath("lib").rglob("*.py")
            if (usages := self._flag_usages(path))
        }
        assert offenders == {}, (
            "флаг --log-min-level ещё доставляет значение в коде агента: "
            f"{offenders}"
        )

    def test_flag_constant_is_gone(self) -> None:
        from lib.services import enterprise_mcp_client

        assert not hasattr(enterprise_mcp_client, "LOG_MIN_LEVEL_FLAG")
        assert not hasattr(agent_settings_module, "LOG_MIN_LEVEL_FLAG")

    def test_second_process_carries_the_block(self) -> None:
        """Второй процесс платформы получает тот же блок, что и клиент агента.

        Его поднимает штатный ``MCPProvider`` для ``mcp_enterprise_*``, и
        объявление у него своё: ``config.json → tools.mcpServers.enterprise``.
        Пока в нём жил флаг, доставка значения накрывала только клиента, а
        операции платформы в этом процессе писались по правилу, которое никто
        не объявлял.
        """
        raw = json.loads((PLATFORM_ROOT / "config.json").read_text(encoding="utf-8"))
        args = raw["tools"]["mcpServers"]["enterprise"]["args"]
        assert "--agent-settings-file" in args
        assert (
            args[args.index("--agent-settings-file") + 1]
            == "${NANOBOT_ENTERPRISE_MCP_AGENT_SETTINGS}"
        )
        assert "--log-min-level" not in args

    def test_threshold_is_no_longer_exported_to_the_child(self) -> None:
        """Порог не едет в окружении дочернего процесса.

        Окружение приоритетнее файла, и «экспорт» значения молча затирал бы
        объявление блока: файл выглядел бы настроенным, а применялось бы
        чужое. Канал один — файл.
        """
        import config as agent_config

        agent_config._export_platform_process_env(
            {"logging": {"db": {"min_level": "WARN"}}}, "prod"
        )
        assert "NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL" not in os.environ
