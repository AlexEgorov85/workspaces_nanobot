"""Контракт объявления платформы в штатном MCP.

Связывает три вещи, которые разъезжаются молча:

  * ``config.json → tools.mcpServers.enterprise`` — что нанобот поднимет;
  * ``mcp-platform/platform.json → execution.require_call_meta`` — открыт ли
    путь, по которому хук передаёт личность;
  * ``LEGACY_IDENTITY_KEYS`` в конвейере платформы — какие именно ключи
    сервер считает идентичностью и вырезает из аргументов.

Плюс четвёртая связь, того же рода: у агента **две** ноги доступа к операциям
платформы, у каждой свой потолок ожидания, и обе обязаны быть строго длиннее
платформенного бюджета ``platform.json → execution.execution_timeout_sec``.
Раньше правило «клиент не короче платформы» было, но охраняло только фоновую
ногу — а модельная, отказ которой уходит в контекст модели, не проверялась
ничем.

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


def _declared_server() -> dict | None:
    """Объявление платформы для модели; ``None``, если его нет в конфигурации.

    Отсутствие — законное состояние: ``tools.mcpServers`` не обязан быть
    объявлен, и тогда ноги модели у агента просто нет.
    """
    servers = _load_json(REPO / "config.json").get("tools", {}).get("mcpServers") or {}
    return servers.get("enterprise")


def _server() -> dict:
    spec = _declared_server()
    assert spec is not None, (
        "платформа не объявлена в tools.mcpServers — нанобот не поднимет "
        "операции, и модель не получит mcp_enterprise_*"
    )
    return spec


def _client_settings() -> dict:
    """Секция ``gateway.agent.enterprise_mcp`` — то, чем агент ждёт вызов."""
    return _load_json(REPO / "config.json")["gateway"]["agent"]["enterprise_mcp"]


def _platform_settings() -> dict:
    """``platform.json`` платформы — объявления её собственных порогов."""
    return _load_json(REPO / "mcp-platform" / "platform.json")


def _declared_legs() -> dict[str, float]:
    """Потолок ожидания каждой **объявленной** ноги доступа к операциям.

    Ключ — путь к настройке в ``config.json``, значение — её бюджет в секундах.
    Ключ, а не позиция в списке: сообщение об отказе обязано называть ногу по
    имени, иначе добавление третьей ноги переставит строки и отказ станет
    безымянным.

    Необъявленная нога в словарь не попадает. ``tools.mcpServers.enterprise``
    может отсутствовать — это состояние без ошибки, и требовать значения,
    которого в конфигурации нет, значило бы проверять отсутствие.
    """
    legs: dict[str, float] = {}
    server = _declared_server()
    if server is not None:
        legs["tools.mcpServers.enterprise.tool_timeout"] = float(
            server["tool_timeout"]
        )
    client = _client_settings()
    assert "tool_timeout_sec" in client, (
        "у фонового клиента агента нет tool_timeout_sec — сравнивать нечего, "
        "а отказ по несуществующей настройке вводит в заблуждение"
    )
    legs["gateway.agent.enterprise_mcp.tool_timeout_sec"] = float(
        client["tool_timeout_sec"]
    )
    return legs


def _require_strict_margin(path: str, budget: float, platform_budget: float) -> None:
    """Бюджет одной ноги обязан быть строго больше платформенного.

    Равные бюджеты отвергаются ОТДЕЛЬНО от более коротких: сообщение обязано
    называть равенство, а не разницу, иначе читатель ищет несуществующую
    разницу в числе, которого нет.

    Raises:
        AssertionError: инвариант нарушен — названы обе стороны и зазор.
    """
    if budget == platform_budget:
        raise AssertionError(
            f"бюджеты равны: {path}={budget:g} с и "
            f"platform.json → execution.execution_timeout_sec={platform_budget:g} с. "
            "Клиент истечёт в тот же момент, когда платформа закончила, и модель "
            "получит отказ вместо результата: зазор должен быть строго "
            "положительным, а не нулевым"
        )
    if budget < platform_budget:
        raise AssertionError(
            f"{path}={budget:g} с короче платформенного бюджета "
            f"platform.json → execution.execution_timeout_sec={platform_budget:g} с "
            f"на {platform_budget - budget:g} с: вызов будет сорван на работающей "
            "стороне, её работа пропадёт вместе с отказом"
        )


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
        """Каждая объявленная нога ждёт строго дольше, чем платформа работает.

        Бюджет работы один — ``platform.json → execution.execution_timeout_sec``,
        сколько сервер готов выполнять вызов, — а потолков ожидания два, и оба
        живут в ``config.json``:

        * ``tools.mcpServers.enterprise.tool_timeout`` — модельная нога; её
          применяет штатный MCP-провайдер нанобота
          (``nanobot/agent/tools/mcp.py``: ``asyncio.wait_for(..., timeout=...)``,
          отказ уходит модели как ``ToolResult.error``);
        * ``gateway.agent.enterprise_mcp.tool_timeout_sec`` — фоновый клиент
          (``lib/services/enterprise_mcp_client.py``), которым ходят службы вне
          оборота.

        Ног две, а правило было одно и охраняло только фоновую: модельная —
        та, где отказ уходит в контекст модели и где пользователь видит ложное
        «операция не удалась», — не проверялась ничем.

        Сравнение строгое, а не «не меньше». Равенство пропускает ровно то
        состояние, от которого правило защищает: клиент истекает в тот же
        момент, когда платформа закончила, и модель получает отказ вместо
        результата. Зазор нужен на то, чтобы дождаться ответа по уже
        работающему вызову, — на равенстве его нет вовсе.

        Случай «клиент короче» вреден по-тихому: клиент срывает вызов, рапортует
        модели об отказе, а сервер продолжает работу — жжёт слот пула и время LLM
        ради результата, который никто не прочитает, и повтор по советам модели
        запускает всё заново. Именно так вел себя ``generate_sql``: холодный
        вызов занимал 11.4 с при потолке 30 с, и запас кончался раньше, чем
        заканчивалась плата за уже начатую работу.

        Про число 11.4 с честно: замеров этого прогона в репозитории нет — ни
        логов, ни артефактов, ни иных измерений. Единственный источник
        длительности — эта самая цитата, поэтому величина запаса из данных не
        выводится и остаётся объявленным решением, а не измеренной
        величиной. Сам страж чисел о запасе не знает и не закрепляет: он читает
        обе стороны из конфигурации и сравнивает, поэтому подъём бюджета
        платформы требует правки ``config.json``, а не правки теста.

        Необъявленная нога не проверяется: без ``tools.mcpServers.enterprise``
        модель к платформе не ходит, и падать на отсутствии значения было бы
        проверкой отсутствия, а не инварианта.
        """
        platform_budget = float(
            _platform_settings()["execution"]["execution_timeout_sec"]
        )
        legs = _declared_legs()
        assert legs, (
            "ни одна нога доступа к операциям платформы не объявлена — "
            "сравнивать нечего, и инвариант молча ничего не охраняет"
        )
        for path, budget in legs.items():
            _require_strict_margin(path, budget, platform_budget)

    def test_call_timeout_guard_actually_goes_red(self):
        """Ломка стража: и равенство, и более короткий бюджет должны падать.

        Проверка, которая зелена на объявленных значениях, ничего не говорит о
        том, что она ловит. Поэтому те же сравнения прогоняются на заведомо
        негодных бюджетах: четверть платформенного бюджета — это ровно то
        состояние, в котором страж был зелёным (30 с против 120 с), а
        равенство — состояние, которое прежнее «не меньше» пропускало.

        Числа берутся от объявленного бюджета платформы, а не зашиты: иначе
        страж начал бы краснеть на правке ``execution_timeout_sec`` вместо
        сути инварианта.
        """
        platform_budget = float(
            _platform_settings()["execution"]["execution_timeout_sec"]
        )
        path = "tools.mcpServers.enterprise.tool_timeout"

        with pytest.raises(AssertionError) as shorter:
            _require_strict_margin(path, platform_budget / 4, platform_budget)
        assert "короче" in str(shorter.value)
        assert f"на {platform_budget - platform_budget / 4:g} с" in str(shorter.value)

        with pytest.raises(AssertionError) as equality:
            _require_strict_margin(path, platform_budget, platform_budget)
        assert "равны" in str(equality.value)
        # При равенстве разницы в числе не существует, и называть её — выдумка;
        # «короче» тут просто неверно, бюджеты-то равны.
        assert "короче" not in str(equality.value)

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

    def test_profile_and_block_path_arrive_as_values(self):
        """Имя контура и путь к блоку едут значениями, а не флагами агента.

        ``args`` — список, и ``${VAR}`` подставляет только значение. Пустое
        значение платформа читает как «флага нет» и берёт базовый контур;
        для блока пустое значение означает «настроек агента нет».

        Порога журнала в ``args`` нет и быть не должно: значение едет в
        файле блока, а командная строка процесса видна всем, кто может её
        прочитать.
        """
        args = _server()["args"]
        assert "--profile" in args
        assert args[args.index("--profile") + 1] == "${NANOBOT_ENTERPRISE_MCP_PROFILE}"
        assert "--agent-settings-file" in args
        assert args[args.index("--agent-settings-file") + 1] == (
            "${NANOBOT_ENTERPRISE_MCP_AGENT_SETTINGS}"
        )
        assert "--log-min-level" not in args


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

    def test_block_path_reads_lifted_section(self, env, tmp_path):
        """Корень платформы берётся из поднятой секции, а не из вложенной.

        ``_lift_agent_sections`` убирает ``gateway.agent`` из конфигурации,
        поэтому читать вложенный путь значит держать вторую копию одного
        объявления. Путь к блоку, в свою очередь, **не вычисляется**: он
        приходит из ``platform.json → agent_settings``, и страж проверяет
        именно это.
        """
        import config as agent_config

        platform = tmp_path / "platform.json"
        platform.write_text(
            json.dumps(
                {"agent_settings": "${NANOBOT_WORKSPACE}/block.json"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        agent_config._export_platform_process_env(
            {"enterprise_mcp": {"cwd": str(tmp_path)}}, "prod"
        )
        monkey = os.environ.get("NANOBOT_WORKSPACE") or ""
        assert os.environ["NANOBOT_ENTERPRISE_MCP_AGENT_SETTINGS"] == str(
            Path(monkey) / "block.json"
        )
        assert "NANOBOT_ENTERPRISE_MCP_LOG_MIN_LEVEL" not in os.environ

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
