"""Контракт объявления платформы в штатном MCP.

Связывает три вещи, которые разъезжаются молча:

  * ``config.json → tools.mcpServers.enterprise`` — что нанобот поднимет;
  * ``mcp-platform/platform.json → execution.require_call_meta`` — открыт ли
    путь, по которому хук передаёт личность;
  * ``LEGACY_IDENTITY_KEYS`` в конвейере платформы — какие именно ключи
    сервер считает идентичностью и вырезает из аргументов.

Расхождение в любой из трёх не падает само: вызов либо уйдёт без
идентичности (``identity_missing``), либо с чужой. Поэтому сверка идёт с
ФАЙЛОМ платформы, а не с ожиданием, продублированным здесь: повторённое
ожидание разошлось бы вместе с оригиналом и не заметила бы этого.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLATFORM = REPO / "mcp-platform"
PIPELINE = PLATFORM / "libs" / "enterprise_common" / "execution" / "pipeline.py"
PLATFORM_JSON = PLATFORM / "platform.json"

#: Операции, объявленные модели. Ровно те, что раньше прятались за
#: самописными обёртками ``audit_analyzer_query`` / ``legal_summarizer_query``
#: / ``history_search_tool``, плюс ``read_result``: без него крупный результат,
#: сохранённый платформой под ссылкой ``session://results/...``, нечем прочесть.
EXPECTED_TOOLS = {
    "list_scripts",
    "run_script",
    "generate_sql",
    "vector_search",
    "query_operation",
    "history_search",
    "read_result",
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _server() -> dict:
    servers = _load_json(REPO / "config.json")["tools"]["mcpServers"]
    assert "enterprise" in servers, (
        "платформа не объявлена в tools.mcpServers — нанобот не поднимет "
        "операции, и модель не получит mcp_enterprise_*"
    )
    return servers["enterprise"]


def _client_settings() -> dict:
    """Секция ``gateway.agent.enterprise_mcp`` — то, чем агент ждёт вызов."""
    return _load_json(REPO / "config.json")["gateway"]["agent"]["enterprise_mcp"]


def _platform_settings() -> dict:
    """``platform.json`` платформы — объявления её собственных порогов."""
    return _load_json(REPO / "mcp-platform" / "platform.json")


def _legacy_identity_keys() -> tuple[str, ...]:
    """``LEGACY_IDENTITY_KEYS`` из исходника конвейера платформы."""
    tree = ast.parse(PIPELINE.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == (
            "LEGACY_IDENTITY_KEYS"
        ):
            return tuple(ast.literal_eval(node.value))
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "LEGACY_IDENTITY_KEYS" for t in node.targets
        ):
            return tuple(ast.literal_eval(node.value))
    pytest.fail("LEGACY_IDENTITY_KEYS не найден в " + str(PIPELINE))


class TestServerDeclaration:
    def test_declared_with_stdio_and_platform_root(self):
        spec = _server()
        assert spec["type"] == "stdio"
        assert spec["command"] == "${NANOBOT_PYTHON}"
        assert spec["cwd"] == "${NANOBOT_PROJECT_ROOT}/mcp-platform"
        assert spec["args"][:2] == ["-m", "servers.enterprise.server"]

    def test_declares_exactly_the_agent_facing_operations(self):
        assert set(_server()["enabled_tools"]) == EXPECTED_TOOLS

    def test_call_timeout_does_not_expire_before_the_platform_stops(self):
        """Клиент обязан ждать дольше, чем платформа готова работать.

        Бюджета времени два, и они живут в разных файлах: ``execution_timeout_sec``
        в ``platform.json`` — сколько сервер готов выполнять вызов,
        ``tool_timeout_sec`` в ``config.json`` — сколько агент готов его ждать.
        Зазор нужен в обе стороны.

        Случай «клиент короче» вреден по-тихому: клиент срывает вызов, рапортует
        модели об отказе, а сервер продолжает работу — жжёт слот пула и время LLM
        ради результата, который никто не прочитает, и повтор по советам модели
        запускает всё заново. Именно так вел себя ``generate_sql``: холодный
        вызов занимал 11.4 с при потолке 30 с, и запас кончался раньше, чем
        заканчивалась плата за уже начатую работу.

        Случай «клиент длиннее» — просто ожидание: модель смотрит в потолок
        вызова, а отказ приходит оттуда, кто действительно решил сдаться.
        """
        platform_budget = float(
            _platform_settings()["execution"]["execution_timeout_sec"]
        )
        client_budget = float(_client_settings()["tool_timeout_sec"])
        assert client_budget >= platform_budget, (
            f"клиент сдаётся через {client_budget} с, а платформа работает до "
            f"{platform_budget} с: вызов будет сорван на работающей стороне, и "
            "её работа пропадёт вместе с отказом"
        )

    def test_env_is_minimal_and_declares_workspace(self):
        """Окружение дочернего процесса собирает MCP SDK, а не агент.

        При ``env=None`` SDK отдаёт серверу ``get_default_environment()`` —
        безопасное подмножество, без ``DATABASE_URL`` и без
        ``NANOBOT_WORKSPACE``. Поэтому ``NANOBOT_WORKSPACE`` обязан быть
        объявлен: ``platform.json → execution.session_root`` ссылается на него.
        """
        env = _server()["env"]
        assert env["NANOBOT_WORKSPACE"] == "${NANOBOT_WORKSPACE}"
        # Секреты платформа достаёт из своего ``mcp-platform/.secrets.env``.
        # Дублировать их в объявлении агента нельзя: значение в окружении
        # перебило бы файл и тихо разошлось с ним.
        assert "DATABASE_URL" not in env
        assert not [k for k in env if "API_KEY" in k or "PASSWORD" in k]

    def test_profile_and_journal_level_arrive_as_values(self):
        """Имя контура и порог журнала едут значениями, а не флагами агента.

        ``args`` — список, и ``${VAR}`` подставляет только значение. Пустое
        значение платформа читает как «флага нет» и берёт базовый контур.
        """
        args = _server()["args"]
        assert "--profile" in args
        assert args[args.index("--profile") + 1] == "${NANOBOT_ENTERPRISE_MCP_PROFILE}"
        assert "--log-min-level" in args
        assert args[args.index("--log-min-level") + 1] == (
            "${NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL}"
        )


class TestPlatformGate:
    def test_transitional_window_is_open(self):
        """Хук передаёт личность аргументами — значит окно должно быть открыто.

        При ``true`` конвейер читает идентичность только из ``params._meta``,
        а нанобот его не передаёт, и КАЖДЫЙ вызов падал бы с
        ``identity_missing``. Обратная проверка: тот же вызов с
        ``require_call_meta=true`` отвергается (см. пробу миграции).
        """
        execution = _load_json(PLATFORM_JSON)["execution"]
        assert execution["require_call_meta"] is False

    def test_hook_keys_match_platform_keys(self):
        """Имена ключей хука обязаны совпадать с теми, что сервер вырезает.

        Расхождение не падает: сервер просто не увидит личность и отвергнет
        вызов — но уже в рантайме и с чужой формулировкой отказа.
        """
        from lib.hooks.mcp_identity_hook import IDENTITY_KEYS

        assert set(IDENTITY_KEYS) == set(_legacy_identity_keys())


class TestProfileExport:
    @pytest.fixture
    def env(self, monkeypatch):
        """Чистое окружение с зарегистрированным откатом.

        ``_export_platform_process_env`` пишет в ``os.environ`` напрямую, и
        ``monkeypatch`` такой записи не откатывает. Поэтому значения
        сначала объявляются через ``setenv`` (это регистрирует originals),
        а потом удаляются — на откате вернётся то, что было до теста.
        """
        for name in (
            "NANOBOT_ENTERPRISE_MCP_PROFILE",
            "NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL",
            "NANOBOT_PROFILE",
        ):
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name, raising=False)
        return monkeypatch

    def test_prod_exports_empty_profile(self, env):
        """Пустое значение → платформа берёт базовый контур.

        Агентский клиент при ``prod`` флаг ``--profile`` не передаёт вовсе;
        здесь флаг есть всегда, поэтому «нет флага» выражается пустым
        значением, которое сервер разбирает как отсутствие.
        """
        import config as agent_config

        agent_config._export_platform_process_env({}, "prod")
        assert os.environ["NANOBOT_ENTERPRISE_MCP_PROFILE"] == ""

    def test_test_profile_is_exported(self, env):
        import config as agent_config

        agent_config._export_platform_process_env({}, "test")
        assert os.environ["NANOBOT_ENTERPRISE_MCP_PROFILE"] == "test"

    def test_journal_level_reads_lifted_path(self, env):
        """Ключ поднят в корень из ``gateway.agent`` — читать надо корень."""
        import config as agent_config

        cfg = {"logging": {"db": {"min_level": "WARNING"}}}
        agent_config._export_platform_process_env(cfg, "prod")
        assert os.environ["NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL"] == "WARNING"

    def test_declared_path_matches_the_client(self):
        """Путь к порогу объявлен в двух модулях — и обязан совпадать.

        Объявить его один раз нельзя: ``config`` — базовый модуль и потянуть в
        него сервисный слой нельзя, а клиент MCP не должен знать про
        ``config.py``. Поэтому равенство проверяется здесь, а не полагается на
        внимательность автора правки.
        """
        import config as agent_config
        from lib.services.enterprise_mcp_client import JOURNAL_MIN_LEVEL_PATH

        assert agent_config.JOURNAL_MIN_LEVEL_PATH == JOURNAL_MIN_LEVEL_PATH

    def test_environment_cannot_override_profile(self, env):
        """Профиль задаётся ``--profile`` у входа, а не окружением.

        Присваивание, а не ``setdefault``: иначе внешнее окружение снова стало
        бы вторым источником профиля, что удалено намеренно (change
        ``remove-profile-environment-selection``).
        """
        import config as agent_config

        env.setenv("NANOBOT_ENTERPRISE_MCP_PROFILE", "prod")
        agent_config._export_platform_process_env({}, "test")
        assert os.environ["NANOBOT_ENTERPRISE_MCP_PROFILE"] == "test"

    def test_historical_profile_variable_stays_dead(self, env):
        """``NANOBOT_PROFILE`` не воскресает как источник профиля."""
        import config as agent_config

        agent_config._export_platform_process_env({}, "test")
        assert "NANOBOT_PROFILE" not in os.environ


class TestInventory:
    def test_hook_is_in_canonical_inventory(self):
        """Иначе ``tools/diagnose_startup.py`` увидит drift по составу хуков."""
        from lib.services.runtime_inventory import canonical_framework_hooks

        names = {spec.name for spec in canonical_framework_hooks()}
        assert "McpIdentityHook" in names
