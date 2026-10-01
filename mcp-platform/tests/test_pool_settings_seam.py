"""Страж: настройки пула приходят из MCP-конфигурации, а не из кода.

Дефект, который ловит этот файл
--------------------------------

Размеры и таймауты пула жили в ``_DEFAULT_POOL`` внутри
``libs/enterprise_data/db.py``, и изменить их было нечем: сервер
``set_pool_config`` не звал, в реестре настроек для них не было записей, а
секрет между ними был один — «настройки MCP описаны в MCP».

Отсюда был и худший вид отказа: ``platform.json`` читался, проверялся
стражем и выглядел рабочим, а на процессе действовали значения из кода.
Файл с настройками, который ни на что не влияет, хуже отсутствующего: он
снимает вопрос «а настроено ли это?».

Что здесь проверяется по существу:

* значение из ``platform.json`` доезжает до пула;
* окружение старше файла (иначе файл нельзя менять, пока агент экспортирует);
* **плохое значение поднимается на старте**, а не молча заменяется дефолтом;
* список ключей пула и список настроек — один и тот же, иначе новая ручка
  снова появится в коде;
* ``server.py`` не читает ``os.environ`` ни разу: единственный разбор
  настроек должен быть один, иначе файл влияет на часть сервера.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM_ROOT))

from libs.enterprise_common.errors import InfrastructureError  # noqa: E402
from libs.enterprise_common.settings import (  # noqa: E402
    BY_NAME,
    FROM_FILE,
    PLATFORM_CONFIG_PATH,
    POOL_SETTING_KEYS,
    Settings,
    pool_config,
)

#: Подстановки ``platform.json`` для тестов, которым нужен полностью
#: разрешённый файл: DSN и ключ провайдера. Настоящие секреты живут в
#: ``mcp-platform/.secrets.env`` и в тесты не попадают; набор тот же, что
#: задаёт conftest на сессию, и расти он обязан вместе с файлом.
DUMMY_SECRETS: dict[str, str] = {
    "DB_USER": "test",
    "DB_PASSWORD": "test",
    "DB_HOST": "localhost",
    "DB_PORT": "5432",
    "DB_NAME": "test",
    "LLM_API_KEY": "test",
}

def _settings(
    env: dict | None = None,
    *,
    file_path: Path | None = None,
    with_dsn_defaults: bool = True,
) -> Settings:
    """Настройки для теста: явное окружение, пустые секреты, файл по умолчанию.

    Три изоляции разом, потому что поодиночке они не работают:

    * ``secrets={}`` — иначе локальный ``mcp-platform/.secrets.env`` подставит
      свои значения и результат станет зависимым от машины;
    * ``env`` дополнен заглушками подстановок DSN — иначе разбор файла
      падает на ``db.dsn`` в тесте, который проверяет вообще другое. Тестам
      про сами подстановки заглушки не нужны, и они выключаются флагом;
    * ``file_path`` по умолчанию настоящий — иначе тест проверяет огрызок
      конфигурации, а не ту, с которой сервер поднимается.
    """
    base = DUMMY_SECRETS if with_dsn_defaults else {}
    return Settings(
        env={**base, **(env or {})},
        secrets={},
        file_path=file_path,
    )


SERVER_PATH = PLATFORM_ROOT / "servers" / "enterprise" / "server.py"


@pytest.fixture(autouse=True)
def _restore_pool_config():
    """Вернуть глобальную конфигурацию пула: её меняет каждый тест ниже."""
    from libs.enterprise_data import db as data_db

    saved = dict(data_db._pool_cfg)
    try:
        yield
    finally:
        data_db._pool_cfg = saved


def _pool_cfg() -> dict:
    from libs.enterprise_data import db as data_db

    return dict(data_db._pool_cfg)


#: Полный набор ключей пула с нейтральными значениями. Основа для тестовых
#: файлов: раздел обязан быть полным, иначе реестр откажется его читать.
POOL_BASE: dict[str, object] = {
    "min_conn": 1,
    "max_conn": 4,
    "pool_timeout": 5.0,
    "queue_maxsize": 10000,
    "reconnect_backoff_sec": 1.0,
    "reconnect_backoff_max_sec": 60.0,
    "connect_max_retries": 5,
    "idle_timeout_sec": 60.0,
    "job_max_retries": 3,
    "print_activity": False,
}


def _config_file(tmp_path: Path, section: str, values: dict) -> Path:
    """Копия настоящего ``platform.json`` с подменённой секцией.

    Не огрызок: файл обязан быть полным, иначе реестр честно откажется его
    читать. Тесты, которые строили ``{"pool": {...}}``, проверяли не сборку
    настроек, а одну функцию — и молчали о том, что остальные настройки в
    такой сборке не приходят вовсе.
    """
    raw = json.loads((PLATFORM_ROOT / "platform.json").read_text(encoding="utf-8"))
    raw[section] = {**raw.get(section, {}), **values}
    target = tmp_path / "platform.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return target


def _pool_file(tmp_path: Path, **overrides: object) -> Path:
    """``platform.json`` с подменёнными ключами секции ``pool``."""
    return _config_file(tmp_path, "pool", overrides)


class TestPoolIsConfiguredFromSettings:
    def test_no_pool_values_are_declared_in_code(self) -> None:
        """В коде нет ни одного значения пула — только контракт ключей.

        ``platform.json`` — единственное место, где живут значения. Список
        дефолтов рядом с кодом был третьим местом: он тихо перебивал файл, и
        вопрос «что применяется» получал два ответа. Это не теория: пока
        размеры пула жили в ``_DEFAULT_POOL``, задать их было нечем, а файл
        с настройками выглядел рабочим и не влиял ни на что.
        """
        source = (PLATFORM_ROOT / "libs" / "enterprise_data" / "db.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source, filename="db.py")
        offenders = [
            node.target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_DEFAULT_POOL"
        ]
        assert not offenders, (
            f"в db.py снова появился словарь значений пула: {offenders}"
        )

    def test_pool_settings_have_no_value_in_the_registry(self) -> None:
        """Объявление настройки пула не содержит значения.

        Проверяется форма, а не числа: сравнение с файлом проходило бы и на
        состоянии, которое запрещает, — ручной список, повторяющий файл.
        """
        carried = sorted(
            name
            for name in POOL_SETTING_KEYS.values()
            if BY_NAME[name].default is not FROM_FILE
        )
        assert not carried, (
            "у настроек пула в коде остались значения: "
            f"{carried}. Значение обязано жить в platform.json."
        )

    def test_every_pool_key_is_a_declared_setting(self) -> None:
        """Ключ пула, не объявленный настройкой, — ручка вне реестра.

        Настоящий отказ, который этот тест ловит: добавили ``idle_timeout_sec``
        в контракт пула, а настройки нет — и файл не может его задать, при
        том что оператор уверен, что настроек полно.
        """
        missing = sorted(
            name for name in POOL_SETTING_KEYS.values() if name not in BY_NAME
        )
        assert not missing, f"ключи пула ссылаются на необъявленные настройки: {missing}"

    def test_pool_contract_matches_the_settings(self) -> None:
        """Контракт пула и настройки — один и тот же набор ключей."""
        from libs.enterprise_data import db as data_db

        assert set(data_db._POOL_SPEC) == set(POOL_SETTING_KEYS)

    def test_file_value_reaches_the_pool(self, tmp_path: Path) -> None:
        """Значение из файла доезжает до пула, а не остаётся в файле.

        Конец цепочки проверяется на настоящем объекте пула: реестр, файл и
        ``set_pool_config`` могут быть правы по отдельности, и разорваться
        может шов между ними — он и рвётся, как только появляется ещё одно
        звено.
        """
        from servers.enterprise import server as enterprise_server

        file_path = _pool_file(tmp_path, min_conn=2, max_conn=7)
        enterprise_server._apply_pool_settings(_settings(file_path=file_path))

        cfg = _pool_cfg()
        assert cfg["min_conn"] == 2, cfg
        assert cfg["max_conn"] == 7, cfg
        # Файл обязан быть полным: «остальное как было» означало бы, что у
        # пула есть запасные значения где-то ещё. Поэтому здесь ровно столько
        # ключей, сколько объявлено контрактом.
        assert set(cfg) == set(POOL_SETTING_KEYS), cfg
        assert cfg["queue_maxsize"] == POOL_BASE["queue_maxsize"], cfg

    def test_incomplete_pool_section_is_refused(self, tmp_path: Path) -> None:
        """Файл без части ключей — ошибка конфигурации, а не добор из кода.

        Проверка на заведомо плохих данных: именно такой файл и породил
        исходный дефект, когда пул молча работал на дефолтах из кода.
        """
        raw = json.loads((PLATFORM_ROOT / "platform.json").read_text(encoding="utf-8"))
        raw["pool"].pop("queue_maxsize")
        file_path = tmp_path / "platform.json"
        file_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(InfrastructureError, match="queue_maxsize"):
            _settings(file_path=file_path)

    def test_environment_wins_over_file(self, tmp_path: Path) -> None:
        """Окружение старше файла — намеренно, а не по недосмотру.

        Пока агент экспортирует значения в окружение процесса, файл не может
        быть сильнее: иначе оператор правил бы файл, а на процессе действовало
        бы чужое значение из окружения, и «применилось» означало бы разное на
        разных машинах.
        """
        from servers.enterprise import server as enterprise_server

        file_path = _pool_file(tmp_path, max_conn=7)
        settings = _settings(env={"ENTERPRISE_POOL_MAX_CONN": "3"}, file_path=file_path)
        assert settings.get("ENTERPRISE_POOL_MAX_CONN") == 3
        assert settings.source("ENTERPRISE_POOL_MAX_CONN") == "env:ENTERPRISE_POOL_MAX_CONN"

        enterprise_server._apply_pool_settings(settings)
        assert _pool_cfg()["max_conn"] == 3

    def test_file_value_is_reported_as_such(self, tmp_path: Path) -> None:
        """Баннер должен называть значения, пришедшие из файла.

        Иначе нельзя объяснить, почему пул не такой, как ожидал оператор:
        «дефолт» и «файл» выглядят одинаково.
        """
        file_path = _pool_file(tmp_path, max_conn=7)
        settings = _settings(file_path=file_path)
        assert "ENTERPRISE_POOL_MAX_CONN" in settings.file_backed()

    def test_bad_value_stops_the_server(self, tmp_path: Path) -> None:
        """Мусор в файле поднимается на старте, а не заменяется дефолтом.

        Молчаливый откат выглядит как «настройка не применилась»: оператор
        ищет опечатку не там, а пул с неверными размерами работает.
        """
        file_path = _pool_file(tmp_path, max_conn="много")
        with pytest.raises(InfrastructureError, match="ENTERPRISE_POOL_MAX_CONN"):
            _settings(file_path=file_path)

    def test_pool_config_shape_matches_set_pool_config(self) -> None:
        """Словарь, который уходит в пул, не должен содержать мусорных ключей.

        ``set_pool_config`` неизвестные ключи игнорирует: опечатка в названии
        настройки уехала бы в никуда, и размер пула остался бы прежним без
        единого сообщения.
        """
        from libs.enterprise_data import db as data_db

        config = pool_config(_settings())
        assert set(config) == set(data_db._POOL_SPEC)
        assert set(config) == set(POOL_SETTING_KEYS)


class TestListSettingsKeepBothSeparators:
    def test_newline_separated_list_is_split(self) -> None:
        """Перевод строки — разделитель наравне с запятой.

        Агент экспортирует список таблиц по одной на строку. Список, склеенный
        в одно имя из семи таблиц, выглядел бы как одна несуществующая
        таблица, и capability ``audit`` отвечала бы «таблица не найдена» на
        вполне корректной конфигурации.
        """
        settings = _settings(env={"ENTERPRISE_AUDIT_TABLES": "oarb.audits\noarb.violations"})
        assert [name for name, _ in settings.get("ENTERPRISE_AUDIT_TABLES")] == [
            "oarb.audits",
            "oarb.violations",
        ]

    def test_mixed_separators_are_split(self) -> None:
        settings = _settings(
            env={"ENTERPRISE_AUDIT_TABLES": "oarb.audits, oarb.violations\noarb.reports"}
        )
        assert [name for name, _ in settings.get("ENTERPRISE_AUDIT_TABLES")] == [
            "oarb.audits",
            "oarb.violations",
            "oarb.reports",
        ]


class TestDsnIsConfigurableInTheFile:
    """DSN настраивается в MCP-конфигурации — с паролем, не уезжающим в git.

    Требование владельца: DSN должен быть виден в ``platform.json``. Отсюда
    риск, который закрывается конструкцией, а не запретом: в файле остаются
    хост, порт и имя базы, а логин с паролем — подстановками ``${...}``,
    которые разворачиваются из окружения процесса. Так ``platform.json``
    говорит «к какой базе подключается процесс» и не становится файлом с
    паролем от рабочей базы под git.
    """

    TEMPLATE = "postgresql://${DB_USER}:${DB_PASSWORD}@db-host:5432/oarb"

    def _file(self, tmp_path: Path, dsn: str) -> Path:
        return _config_file(tmp_path, "db", {"dsn": dsn})

    def test_file_dsn_reaches_the_pool(self, tmp_path: Path) -> None:
        from servers.enterprise import server as enterprise_server

        file_path = self._file(tmp_path, self.TEMPLATE)
        settings = _settings(
            env={"DB_USER": "svc", "DB_PASSWORD": "p@ss:word"},
            file_path=file_path,
        )
        assert settings.get("DATABASE_URL") == "postgresql://svc:p@ss:word@db-host:5432/oarb"
        assert settings.source("DATABASE_URL") == "file:platform.json"

        enterprise_server._configure_dsn(settings)
        from libs.enterprise_data import db as data_db

        assert data_db.resolve_dsn() == "postgresql://svc:p@ss:word@db-host:5432/oarb"
        data_db._dsn = ""

    def test_file_beats_inherited_environment(self, tmp_path: Path) -> None:
        """Файл важнее унаследованного ``DATABASE_URL`` агента.

        Процесс ``enterprise-mcp`` наследует ``dict(os.environ)`` целиком, и
        ``DATABASE_URL`` в нём — секрет агента, а не решение платформы о
        своей базе. С общим приоритетом «окружение > файл» значение из файла
        было бы декоративным: сервер показывал бы DSN в конфигурации, а
        подключался бы к чужой базе.
        """
        file_path = self._file(tmp_path, self.TEMPLATE)
        settings = _settings(
            env={
                "DATABASE_URL": "postgresql://agent@agent-host/agent-db",
                "DB_USER": "svc",
                "DB_PASSWORD": "secret",
            },
            file_path=file_path,
        )
        assert settings.get("DATABASE_URL") == "postgresql://svc:secret@db-host:5432/oarb"
        assert settings.source("DATABASE_URL") == "file:platform.json"

    def test_environment_still_works_without_a_file_dsn(self, tmp_path: Path) -> None:
        """Пустой ``db.dsn`` — не поломка, а «бери из окружения».

        Файл есть всегда (он под git), и пустое значение в нём не должно
        ломать развёртывание, где DSN приходит из окружения.
        """
        file_path = self._file(tmp_path, "")
        settings = _settings(
            env={"DATABASE_URL": "postgresql://from-env@host/db"}, file_path=file_path
        )
        assert settings.get("DATABASE_URL") == "postgresql://from-env@host/db"
        assert settings.source("DATABASE_URL") == "env:DATABASE_URL"

    def test_pg_dsn_is_still_the_fallback(self, tmp_path: Path) -> None:
        file_path = self._file(tmp_path, "")
        settings = _settings(env={"PG_DSN": "postgresql://from-pg@host/db"}, file_path=file_path)
        assert settings.get("DATABASE_URL") == "postgresql://from-pg@host/db"
        assert settings.source("DATABASE_URL") == "env:PG_DSN"

    def test_missing_substitution_is_an_error(self, tmp_path: Path) -> None:
        """Незаданная переменная подстановки — отказ, а не пустая учётная часть.

        ``postgresql://:@host`` дал бы отказ соединения там, где виновата
        опечатка в имени переменной: диагностика указывала бы не туда.
        """
        file_path = self._file(tmp_path, self.TEMPLATE)
        with pytest.raises(InfrastructureError, match="DB_PASSWORD"):
            _settings(
                env={"DB_USER": "svc"},
                file_path=file_path,
                with_dsn_defaults=False,
            )

    def test_substitution_does_not_touch_environment_values(self, tmp_path: Path) -> None:
        """Значение из окружения не разворачивается.

        Разворачивать его повторно опасно: пароль, содержащий ``${`` (обычная
        последовательность), был бы съеден, и платформа подключилась бы с
        изменённым паролем. Проверяется на пути, где DSN действительно приходит
        из окружения, — пустой ключ в файле.
        """
        file_path = self._file(tmp_path, "")
        settings = _settings(
            env={"DATABASE_URL": "postgresql://u:pa${ss}@h/db"},
            file_path=file_path,
        )
        assert settings.get("DATABASE_URL") == "postgresql://u:pa${ss}@h/db"
        assert settings.source("DATABASE_URL") == "env:DATABASE_URL"

    def test_dsn_is_never_logged(self, tmp_path: Path) -> None:
        """Баннер показывает источник, но не значение.

        Маскирование уже сделано типом ``secret``; здесь проверяется, что
        список «пришло из файла» тоже не разворачивается в текст.
        """
        file_path = self._file(tmp_path, self.TEMPLATE)
        settings = _settings(
            env={"DB_USER": "svc", "DB_PASSWORD": "s3cret"}, file_path=file_path
        )
        assert settings.as_dict()["DATABASE_URL"] == "***"
        assert "DATABASE_URL" in settings.file_backed()


class TestSingleReadPath:
    def test_server_reads_no_environment_directly(self) -> None:
        """``server.py`` не читает ``os.environ`` ни разу.

        Это и есть «настройки MCP — в MCP». Пока каждая функция bootstrap'а
        читала окружение сама, ``platform.json`` был прочитан тестами и
        только ими: файл выглядел рабочим и не влиял ни на что.
        """
        tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            dotted = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            owner = func.value if isinstance(func, ast.Attribute) else None
            reads_environ = (
                isinstance(owner, ast.Attribute) and owner.attr == "environ"
            ) or dotted == "getenv"
            if dotted in ("get", "getenv") and reads_environ and node.args:
                offenders.append(ast.unparse(node))
        assert not offenders, (
            "server.py читает окружение напрямую — настройки разбираются в "
            f"обход реестра: {offenders}"
        )

    def test_server_does_not_import_os(self) -> None:
        """Модуль ``os`` в bootstrap'е не нужен: окружения там нет."""
        tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(alias.name != "os" for alias in node.names), (
                    "server.py импортирует os, но читать из него нечего"
                )
            if isinstance(node, ast.ImportFrom):
                assert node.module != "os", "server.py импортирует os поимённо"

    def test_pool_is_not_configured_with_a_literal(self) -> None:
        """Пул настраивается реестром, а не словарём в месте вызова.

        ``set_pool_config({"max_conn": 32})`` в bootstrap'е выглядит безобидно и
        работает, но это ровно та ручка, ради которой переезжали в MCP: значение
        в коде, мимо файла, и оператор, читающий ``platform.json``, о ней не
        знает.
        """
        tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"), filename=str(SERVER_PATH))
        offenders = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "set_pool_config"
            and node.args
            and isinstance(node.args[0], (ast.Dict, ast.List, ast.Tuple))
        ]
        assert not offenders, (
            f"конфигурация пула задана литералом в bootstrap: {offenders}"
        )


class TestLlmGetsResolvedValues:
    def test_as_env_carries_resolved_values(self, tmp_path: Path) -> None:
        """``libs/llm`` получает то, что разрешил реестр, а не сырое окружение.

        Второй разбор — это «настройка прописана в двух местах»: файл меняет
        значение для одного потребителя, а для другого остаётся окружение.
        """
        file_path = _config_file(tmp_path, "data", {"max_rows": 25})
        settings = _settings(
            env={"ENTERPRISE_LLM_MODEL": "qwen3:8b", **DUMMY_SECRETS},
            file_path=file_path,
        )

        resolved = settings.as_env()
        assert resolved["ENTERPRISE_MAX_ROWS"] == "25", resolved
        assert resolved["ENTERPRISE_LLM_MODEL"] == "qwen3:8b", resolved

    def test_as_env_encodes_lists_for_string_lookups(self) -> None:
        """Список уезжает строкой, потому что потребитель ищет по строке."""
        settings = _settings(env={"ENTERPRISE_AUDIT_TABLES": "a,b\nc"})
        assert settings.as_env()["ENTERPRISE_AUDIT_TABLES"] == "a,b,c"

    def test_as_env_skips_unset_values(self, tmp_path) -> None:
        """Без значения — отсутствие ключа, а не пустая строка.

        Пустая строка для ``int``-настройки не «не задана», а значение: пустой
        ``ENTERPRISE_LLM_MAX_TOKENS`` приводится как ``None`` реестром, и
        потребитель обязан увидеть именно отсутствие.

        Берём необязательную настройку capability ``llm``: у обязательной
        (``FROM_FILE``) отсутствие ключа в файле — ошибка конфигурации с
        именем ключа, а вовсе не «unset».
        """
        raw = json.loads(PLATFORM_CONFIG_PATH.read_text(encoding="utf-8"))
        raw["llm"].pop("max_tokens")
        config = tmp_path / "platform.json"
        config.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

        resolved = _settings(file_path=config).as_env()
        assert "ENTERPRISE_LLM_MAX_TOKENS" not in resolved
