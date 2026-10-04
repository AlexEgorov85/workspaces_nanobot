"""Блок настроек агента: приём, слияние, владение и отказ.

Проверяется ровно то, что объявлено в ``specs/runtime/platform-settings``:

* словарь принимаемых ключей объявлен один раз — в реестре платформы;
* известный ключ применяется, и в отчёте виден ключом блока, а не именем
  переменной;
* отсутствующий файл — пустой блок, а не ошибка;
* нечитаемый блок, неизвестный ключ, ключ платформенной настройки и агентский
  ключ в ``platform.json`` — отказ с называнием ключа;
* окружение источником агентской настройки не является;
* блок применяется один раз, на старте;
* ключ доступа к данным в словарь попасть не может, и добавление такого ключа
  роняет тест, а не старт.

Фикстуры — временные файлы и словарь, собранный из реестра. Живой
``platform.json`` не читается: его объявление меняется вместе с другим
change'ом, и тест не должен краснеть из-за чужой правки.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from libs.enterprise_common.errors import InfrastructureError
from libs.enterprise_common.execution.factory import build_execution_layer
from libs.enterprise_common.settings import (
    BY_FILE_KEY,
    FROM_FILE,
    OWNER_AGENT,
    OWNER_PLATFORM,
    Settings,
    agent_settings_dictionary,
    agent_settings_summary,
    merge_agent_settings,
    read_agent_settings_file,
    read_platform_file,
    settings_owned_by,
)

from conftest import DUMMY_SECRETS

PLATFORM_ROOT = Path(__file__).resolve().parent.parent

#: Значение по типу настройки — чтобы фикстура platform.json не расходилась с
#: реестром при добавлении настройки: новый ключ получает значение сам.
VALUE_BY_KIND = {
    "secret": "s3cret",
    "str": "значение",
    "int": "1",
    "float": "1.0",
    "bool": "true",
    "list": "a,b",
    "table_list": "oarb.audits",
    "json": "{}",
}

#: Ключи, которых в словаре блока быть не должно ни при каких условиях: они
#: открывают доступ к данным, а блок — это файл, который агент заполняет
#: из своего конфига и передаёт платформе.
DATA_ACCESS_KEYS = (
    "db.dsn",
    "data.log_table",
    "data.task_table",
    "data.snapshot_path",
    "audit.tables",
    "pool.max_conn",
    "llm.key",
    "llm.embed_key",
    "vectors.indexes",
)


def platform_file(tmp_path: Path, **extra: object) -> Path:
    """Временный ``platform.json`` со всеми обязательными ключами.

    Обязательные (``FROM_FILE``) ключи объявляются все: реестр останавливает
    чтение на первом пропущенном, и тест про блок падал бы на отсутствии
    чужого ключа, а не на своём предмете.
    """
    body: dict[str, object] = {}
    for setting in settings_owned_by(OWNER_PLATFORM):
        if setting.default is not FROM_FILE:
            continue
        section, _, key = setting.key.partition(".")
        body.setdefault(section, {})[key] = VALUE_BY_KIND[setting.kind]  # type: ignore[index]
    for key, value in extra.items():
        section, _, name = str(key).partition(".")
        if name:
            body.setdefault(section, {})[name] = value  # type: ignore[index]
        else:
            body[str(key)] = value
    path = tmp_path / "platform.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def block_file(tmp_path: Path, block: object, *, name: str = "agent-settings.json") -> Path:
    path = tmp_path / name
    path.write_text(
        block if isinstance(block, str) else json.dumps(block, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def settings_with(
    tmp_path: Path, block: Path | None = None, **env: str
) -> Settings:
    return Settings(
        env={**DUMMY_SECRETS, **env},
        secrets={},
        file_path=platform_file(tmp_path),
        agent_settings_path=block,
    )


class TestDictionary:
    """Словарь объявлен платформой и только ею."""

    def test_declared_in_the_registry(self) -> None:
        owned = {
            s.key for s in settings_owned_by(OWNER_AGENT) if s.key
        }
        assert owned, "в реестре нет ни одной агентской настройки: блок нечем заполнить"
        assert set(agent_settings_dictionary()) == owned

    def test_known_key_is_there(self) -> None:
        assert "logging.db.min_level" in agent_settings_dictionary()

    def test_agent_keys_are_absent_from_the_platform_file_dictionary(self) -> None:
        # ``BY_FILE_KEY`` — словарь platform.json. Пересечение означало бы,
        # что один ключ объявлен в двух местах сразу, и вопрос «откуда взялось
        # значение» снова получил бы два ответа.
        assert not set(agent_settings_dictionary()) & set(BY_FILE_KEY)


class TestAcceptedBlock:
    """Известный ключ применяется и виден в отчёте."""

    def test_value_is_applied(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_level": "WARN"}))
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == "WARN"

    def test_source_is_the_block(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_level": "WARN"}))
        assert settings.source("ENTERPRISE_LOG_MIN_LEVEL") == "block:agent-settings.json"

    def test_report_names_the_block_key(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_level": "WARN"}))
        report = settings.agent_settings_report()
        assert report["declared"] is True
        assert report["received"] == 1
        assert report["applied"] == {"logging.db.min_level": "WARN"}

    def test_summary_line_names_the_applied_value(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_level": "WARN"}))
        summary = agent_settings_summary(settings)
        assert "block:agent-settings.json" in summary
        assert "logging.db_min_level" not in summary  # ключ блока, не имя переменной
        assert "logging.db.min_level='WARN'" in summary

    def test_reaches_the_journal_writer(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_level": "WARN"}))
        layer = build_execution_layer(settings, session_root=tmp_path / "sessions")
        assert layer.writer.min_level == "WARN"


class TestAbsentBlock:
    """Отсутствующий файл — пустые настройки, а не отказ."""

    def test_missing_file_is_not_an_error(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, tmp_path / "нет-такого.json")
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == ""
        assert settings.source("ENTERPRISE_LOG_MIN_LEVEL") == "default"

    def test_reader_returns_empty_for_a_missing_file(self, tmp_path: Path) -> None:
        assert read_agent_settings_file(tmp_path / "нет-такого.json") == {}

    def test_reader_returns_empty_without_a_path(self) -> None:
        assert read_agent_settings_file(None) == {}

    def test_empty_block_is_visible_in_the_summary(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, tmp_path / "нет-такого.json")
        assert agent_settings_summary(settings) == (
            "блок настроек агента block:нет-такого.json: ключей нет"
        )

    def test_no_block_at_all_is_visible_in_the_summary(self, tmp_path: Path) -> None:
        assert agent_settings_summary(settings_with(tmp_path)) == (
            "блок настроек агента не передан"
        )


class TestRefusedBlock:
    """Недопустимый блок — отказ, называющий ключ."""

    def test_unreadable_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError, match="agent-settings.json: не читается"):
            settings_with(tmp_path, block_file(tmp_path, "не json"))

    def test_block_that_is_not_an_object_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError, match="ожидался объект"):
            settings_with(tmp_path, block_file(tmp_path, [1, 2]))

    def test_unknown_key_is_refused_with_the_dictionary(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError) as excinfo:
            settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_lvl": "WARN"}))
        message = str(excinfo.value)
        assert "logging.db.min_lvl" in message
        assert "logging.db.min_level" in message  # принятый словарь назван

    def test_summary_counts_accepted_and_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError) as excinfo:
            settings_with(
                tmp_path,
                block_file(
                    tmp_path,
                    {"logging.db.min_level": "WARN", "logging.db.min_lvl": "WARN"},
                ),
            )
        assert "принято 1 из 2" in str(excinfo.value)

    def test_platform_key_is_refused_by_ownership(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError) as excinfo:
            settings_with(tmp_path, block_file(tmp_path, {"data.log_table": "agent_gateway_logs"}))
        message = str(excinfo.value)
        assert "data.log_table" in message
        assert "не перенаправляет доступ к данным" in message

    def test_known_key_survives_a_foreign_neighbour(self, tmp_path: Path) -> None:
        # Отказ называет отклонённое, а не «применённое половину»: частичное
        # применение оставило бы процесс в состоянии, которое никто не
        # объявлял.
        with pytest.raises(InfrastructureError) as excinfo:
            settings_with(
                tmp_path,
                block_file(
                    tmp_path,
                    {"logging.db.min_level": "WARN", "data.snapshot_path": "/tmp/x.duckdb"},
                ),
            )
        assert "data.snapshot_path" in str(excinfo.value)

    def test_agent_key_in_platform_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError) as excinfo:
            read_platform_file(platform_file(tmp_path, **{"logging": {"db": {"min_level": "WARN"}}}))
        message = str(excinfo.value)
        assert "logging.db.min_level" in message
        assert "блоком настроек агента" in message

    def test_declaration_in_platform_file_is_not_a_setting(self, tmp_path: Path) -> None:
        # Верхнеуровневый ``agent_settings`` — объявление пути, а не значение.
        # Чтение файла обязано его пропускать: иначе объявление блока, ради
        # которого всё затевалось, ломало бы старт платформы.
        raw = platform_file(tmp_path).read_text(encoding="utf-8")
        patched = json.loads(raw)
        patched["agent_settings"] = "${NANOBOT_WORKSPACE}/data_store/agent-settings.json"
        (tmp_path / "platform.json").write_text(
            json.dumps(patched, ensure_ascii=False), encoding="utf-8"
        )
        assert read_platform_file(tmp_path / "platform.json")

    def test_wrong_type_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InfrastructureError) as excinfo:
            merge_agent_settings({"logging.db.min_level": {"уровень": "WARN"}})
        message = str(excinfo.value)
        assert "logging.db.min_level" in message
        assert "ожидалась строка" in message


class TestDataAccessIsOutOfReach:
    """Страж: словарь не может открыть доступ к данным.

    Проверка перечислением, а не декларацией. Если в словарь добавят ключ
    доступа к данным, краснеет **этот тест** — то есть проверку нельзя
    «починить», не заметив расхождения.
    """

    @pytest.mark.parametrize("key", DATA_ACCESS_KEYS)
    def test_key_is_not_in_the_dictionary(self, key: str) -> None:
        assert key not in agent_settings_dictionary()

    @pytest.mark.parametrize("key", DATA_ACCESS_KEYS)
    def test_key_in_the_block_is_refused(self, tmp_path: Path, key: str) -> None:
        with pytest.raises(InfrastructureError, match=key):
            merge_agent_settings({key: "что-нибудь"})

    def test_only_agent_owned_settings_are_accepted(self) -> None:
        names = {s.name for s in agent_settings_dictionary().values()}
        assert names <= {s.name for s in settings_owned_by(OWNER_AGENT)}


class TestSingleSource:
    """Окружение и platform.json источниками блока не являются."""

    def test_environment_is_not_a_source(self, tmp_path: Path) -> None:
        settings = settings_with(
            tmp_path, ENTERPRISE_LOG_MIN_LEVEL="ERROR"
        )
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == ""
        assert settings.source("ENTERPRISE_LOG_MIN_LEVEL") == "default"

    def test_block_wins_over_environment_when_both_exist(self, tmp_path: Path) -> None:
        settings = settings_with(
            tmp_path,
            block_file(tmp_path, {"logging.db.min_level": "WARN"}),
            ENTERPRISE_LOG_MIN_LEVEL="ERROR",
        )
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == "WARN"
        assert settings.source("ENTERPRISE_LOG_MIN_LEVEL").startswith("block:")


class TestAppliedOnce:
    """Блок — состояние старта, а не подписка на файл."""

    def test_rewrite_after_construction_changes_nothing(self, tmp_path: Path) -> None:
        block = block_file(tmp_path, {"logging.db.min_level": "WARN"})
        settings = settings_with(tmp_path, block)
        block.write_text(json.dumps({"logging.db.min_level": "ERROR"}), encoding="utf-8")
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == "WARN"

    def test_file_appearing_later_is_not_picked_up(self, tmp_path: Path) -> None:
        block = tmp_path / "agent-settings.json"
        settings = settings_with(tmp_path, block)
        block.write_text(json.dumps({"logging.db.min_level": "ERROR"}), encoding="utf-8")
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == ""

    def test_writer_does_not_re_read_the_block(self, tmp_path: Path) -> None:
        block = block_file(tmp_path, {"logging.db.min_level": "WARN"})
        settings = settings_with(tmp_path, block)
        layer = build_execution_layer(settings, session_root=tmp_path / "sessions")
        block.write_text(json.dumps({"logging.db.min_level": "ERROR"}), encoding="utf-8")
        assert layer.writer.min_level == "WARN"


class TestNoThresholdMeansNoFilter:
    """Пустое значение — «порог не задан», а не ``INFO``."""

    def test_empty_block_value_means_write_everything(self, tmp_path: Path) -> None:
        settings = settings_with(tmp_path, block_file(tmp_path, {"logging.db.min_level": ""}))
        assert settings.get("ENTERPRISE_LOG_MIN_LEVEL") == ""
        layer = build_execution_layer(settings, session_root=tmp_path / "sessions")
        assert layer.writer.min_level is None

    def test_offline_default_still_applies_without_the_setting(self, tmp_path: Path) -> None:
        # Словарь значений без блока (сборка вне агента): дефолт писателя.
        layer = build_execution_layer({}, session_root=tmp_path / "sessions")
        assert layer.writer.min_level == "INFO"


class TestOldDeliveryIsGone:
    """Снятие флага, доставлявшего значение, — часть того же изменения."""

    @staticmethod
    def _flag_usages(path: Path) -> list[str]:
        """Где флаг встречается как значение кода, а не как проза.

        Доктрины, объясняющие, почему флага нет, — законная часть модуля: они
        и объясняют следующему читателю, что искать. Проверяется поэтому AST,
        а не текст: литерал в коде означает доставку значения, литерал в
        доктрине — её описание.
        """
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
                    found.append(f"{node.value!r}")
            elif isinstance(node, ast.Name) and "LOG_MIN_LEVEL_FLAG" in node.id:
                found.append(node.id)
            elif isinstance(node, ast.Attribute) and "LOG_MIN_LEVEL_FLAG" in node.attr:
                found.append(node.attr)
        return found

    def test_flag_is_absent_from_the_platform_tree(self) -> None:
        offenders = {
            str(path.relative_to(PLATFORM_ROOT)): usages
            for path in PLATFORM_ROOT.rglob("*.py")
            if "tests" not in path.parts
            and "__pycache__" not in path.parts
            and (usages := self._flag_usages(path))
        }
        assert offenders == {}, (
            "флаг --log-min-level ещё доставляет значение в коде платформы: "
            f"{offenders}"
        )

    def test_server_parses_the_block_flag(self) -> None:
        """Разбор флага есть и он попадает в ``build`` — иначе блок мёртв.

        Флаг, который никто не читает, — это настройка, которая выглядит
        рабочей и не действует: ровно тот класс дефекта, который страж обязан
        ловить.
        """
        from servers.enterprise import server as enterprise_server

        parsed = enterprise_server._agent_settings_path_from_argv(  # noqa: SLF001
            ["--agent-settings-file", "C:/ws/agent-settings.json"]
        )
        assert parsed is not None and parsed.name == "agent-settings.json"
        source = (PLATFORM_ROOT / "servers" / "enterprise" / "server.py").read_text(
            encoding="utf-8"
        )
        assert "agent_settings_path=_agent_settings_path_from_argv(" in source, (
            "main не передаёт разобранный путь в build(): флаг разбирается и "
            "не применяется"
        )


#: Настройки транспорта, которые блок обязан нести. В реестр их вносит владелец
#: ``libs/enterprise_common/settings.py``; здесь они нужны как **контракт**: по
#: ним видно, что именно объявление должно закрыть, и по ним же проверяется,
#: что до объявления значение действительно не доезжает.
TRANSPORT_DECLARATION = (
    ("ENTERPRISE_TRANSPORT_MODE", "str", "transport.mode"),
    ("ENTERPRISE_TRANSPORT_BIND", "str", "transport.bind"),
    ("ENTERPRISE_TRANSPORT_PORT", "int", "transport.port"),
    ("ENTERPRISE_TRANSPORT_NOTIFY_FD", "int", "transport.notify_fd"),
)


def _declare_transport_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Внести объявление транспорта в реестр — только на время теста.

    Подмена нужна не для того, чтобы обойти проверку, а чтобы доказать путь
    доставки, пока объявление в реестре ещё не внесено. Продакшн-код при этом
    не трогается: подменяется кортеж настроек в модуле реестра, и ``Settings``
    читает его при разборе блока.
    """
    from libs.enterprise_common import settings as registry

    extra = tuple(
        registry.Setting(
            name=name,
            kind=kind,
            default=registry.OPTIONAL,
            owner=OWNER_AGENT,
            reader="servers/enterprise/http_transport.py:requested_transport",
            purpose="адрес транспорта приходит блоком агента",
            file_key=key,
        )
        for name, kind, key in TRANSPORT_DECLARATION
    )
    monkeypatch.setattr(registry, "SETTINGS", registry.SETTINGS + extra)


class TestPortKey:
    """Порт транспорта приходит блоком, а не аргументом запуска (Ф1.4)."""

    def test_port_never_appears_in_argv(self) -> None:
        """Ни одного флага про порт в production-модулях платформы.

        По AST, а не по тексту: имя флага может появиться в строке внутри
        докстринга, и grep-ствраж тогда орал бы на безобидную прозу, а
        пропускал бы константу в коде.
        """
        found: dict[str, list[str]] = {}
        for path in PLATFORM_ROOT.rglob("*.py"):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
                and ast.get_docstring(node)
                and isinstance(node.body[0], ast.Expr)
            }
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and node.value.startswith("--")
                    and "port" in node.value.lower()
                ):
                    found.setdefault(str(path.relative_to(PLATFORM_ROOT)), []).append(
                        node.value
                    )
        assert found == {}, f"порт появился в argv платформы: {found}"

    def test_block_value_reaches_the_transport_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Путь доставки целиком: значение блока → запрос транспорта.

        Проверяется с объявлением, **подставленным в реестр на время теста**:
        так доказывается, что читатель платформы устроен верно, и падение после
        объявления в реестре невозможно — оно этот тест не сломает.
        """
        from servers.enterprise import http_transport

        _declare_transport_settings(monkeypatch)
        block = block_file(
            tmp_path,
            {
                "transport.mode": "http",
                "transport.bind": "127.0.0.1",
                "transport.port": 8790,
                "transport.notify_fd": 7,
            },
        )
        request = http_transport.requested_transport(settings_with(tmp_path, block))
        assert request.mode == "http"
        assert request.bind == "127.0.0.1"
        assert request.port == 8790
        assert request.notify_fd == 7

    def test_absent_port_key_means_ephemeral(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Нет ключа ``port`` при ``http`` — ``0``, «выдай свободный»."""
        from servers.enterprise import http_transport

        _declare_transport_settings(monkeypatch)
        block = block_file(
            tmp_path, {"transport.mode": "http", "transport.notify_fd": 7}
        )
        request = http_transport.requested_transport(settings_with(tmp_path, block))
        assert request.port == 0

    def test_empty_value_means_the_same_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Пустой ``port`` — тоже «выдай свободный», а не «не число».

        Реестр отдаёт пустое значение ключа маркером ``OPTIONAL``, и если бы
        читатель не отличал его от отсутствия, оператор получил бы отказ
        «не число» на значении, которое он не писал.
        """
        from servers.enterprise import http_transport

        _declare_transport_settings(monkeypatch)
        block = block_file(
            tmp_path,
            {"transport.mode": "http", "transport.port": "", "transport.notify_fd": 7},
        )
        request = http_transport.requested_transport(settings_with(tmp_path, block))
        assert request.port == 0

    def test_port_arrives_in_block(self, tmp_path: Path) -> None:
        """Тот же путь доставки на **живом** реестре, без подстановки.

        Ключи транспорта внесены в реестр платформы
        (``libs/enterprise_common/settings.py``, ``owner=OWNER_AGENT``), поэтому
        блок с ними принимается. Метка ``xfail`` снята 2026-10-04.

        ``notify_fd`` в блоке обязателен: при ``mode=http`` фактический адрес
        назначает ОС в момент бинда, и сообщить его больше нечем, кроме
        дескриптора, который открывает агент. Без него транспорт отказывает
        раньше, чем что-либо привяжет, — и это правильно.
        """
        from servers.enterprise import http_transport

        block = block_file(
            tmp_path,
            {
                "transport.mode": "http",
                "transport.bind": "127.0.0.1",
                "transport.port": 8790,
                "transport.notify_fd": 7,
            },
        )
        request = http_transport.requested_transport(settings_with(tmp_path, block))
        assert request.mode == "http"
        assert request.port == 8790
