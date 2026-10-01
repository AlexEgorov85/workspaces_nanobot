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
    POOL_SETTING_KEYS,
    Settings,
    pool_config,
    pool_defaults,
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


def _write_file(tmp_path: Path, pool: dict) -> Path:
    target = tmp_path / "platform.json"
    target.write_text(json.dumps({"pool": pool}), encoding="utf-8")
    return target


class TestPoolIsConfiguredFromSettings:
    def test_pool_defaults_come_from_the_registry(self) -> None:
        """Дефолты пула объявлены в реестре, а не вторым списком в коде.

        Два списка дефолтов разъезжаются при первом же изменении: пул пошёл бы
        по одному, а ``platform.json`` обещал бы оператору другое.
        """
        from libs.enterprise_data import db as data_db

        assert data_db._DEFAULT_POOL == pool_defaults()
        assert set(data_db._DEFAULT_POOL) == set(POOL_SETTING_KEYS)

    def test_pool_defaults_are_not_declared_in_code(self) -> None:
        """Словарь дефолтов в коде запрещён — проверяется форма, а не числа.

        Сравнение значений для этого не годится: ручной список, повторяющий
        реестр, даёт тот же результат и разъезжается при первом же правом
        изменении в любую сторону. Такой страж зеленел бы ровно на том
        состоянии, которое запрещает.
        """
        source = (PLATFORM_ROOT / "libs" / "enterprise_data" / "db.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source, filename="db.py")
        offenders = [
            ast.unparse(node)
            for node in ast.walk(tree)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_DEFAULT_POOL"
            and not (
                isinstance(node.value, ast.Call)
                and getattr(node.value.func, "id", "") == "pool_defaults"
            )
        ]
        assert not offenders, (
            "_DEFAULT_POOL обязан выводиться из реестра (pool_defaults()), а "
            f"не объявляться в коде: {offenders}"
        )

    def test_every_pool_key_is_a_declared_setting(self) -> None:
        """Ключ пула, не объявленный настройкой, — ручка вне реестра.

        Настоящий отказ, который этот тест ловит: добавили ``idle_timeout_sec``
        в ``_DEFAULT_POOL``, а настройки нет — и изменить значение нечем, при
        том что оператор уверен, что реестр полон.
        """
        missing = sorted(
            name for name in POOL_SETTING_KEYS.values() if name not in BY_NAME
        )
        assert not missing, f"ключи пула ссылаются на необъявленные настройки: {missing}"

    def test_file_value_reaches_the_pool(self, tmp_path: Path) -> None:
        """Значение из файла доезжает до пула, а не остаётся в файле.

        Конец цепочки проверяется на настоящем объекте пула: реестр, файл и
        ``set_pool_config`` могут быть правы по отдельности, и разорваться
        может шов между ними — он и рвётся, как только появляется ещё одно
        звено.
        """
        from servers.enterprise import server as enterprise_server

        file_path = _write_file(tmp_path, {"min_conn": 2, "max_conn": 7})
        enterprise_server._apply_pool_settings(Settings(env={}, file_path=file_path))

        cfg = _pool_cfg()
        assert cfg["min_conn"] == 2, cfg
        assert cfg["max_conn"] == 7, cfg
        # Значения, которых в файле нет, остались дефолтами — файл не должен
        # обнулять то, чего не касается.
        assert cfg["queue_maxsize"] == pool_defaults()["queue_maxsize"], cfg

    def test_environment_wins_over_file(self, tmp_path: Path) -> None:
        """Окружение старше файла — намеренно, а не по недосмотру.

        Пока агент экспортирует значения в окружение процесса, файл не может
        быть сильнее: иначе оператор правил бы файл, а на процессе действовало
        бы чужое значение из окружения, и «применилось» означало бы разное на
        разных машинах.
        """
        from servers.enterprise import server as enterprise_server

        file_path = _write_file(tmp_path, {"max_conn": 7})
        settings = Settings(env={"ENTERPRISE_POOL_MAX_CONN": "3"}, file_path=file_path)
        assert settings.get("ENTERPRISE_POOL_MAX_CONN") == 3
        assert settings.source("ENTERPRISE_POOL_MAX_CONN") == "env:ENTERPRISE_POOL_MAX_CONN"

        enterprise_server._apply_pool_settings(settings)
        assert _pool_cfg()["max_conn"] == 3

    def test_file_value_is_reported_as_such(self, tmp_path: Path) -> None:
        """Баннер должен называть значения, пришедшие из файла.

        Иначе нельзя объяснить, почему пул не такой, как ожидал оператор:
        «дефолт» и «файл» выглядят одинаково.
        """
        file_path = _write_file(tmp_path, {"max_conn": 7})
        settings = Settings(env={}, file_path=file_path)
        assert "ENTERPRISE_POOL_MAX_CONN" in settings.file_backed()

    def test_bad_value_stops_the_server(self, tmp_path: Path) -> None:
        """Мусор в файле поднимается на старте, а не заменяется дефолтом.

        Молчаливый откат выглядит как «настройка не применилась»: оператор
        ищет опечатку не там, а пул с неверными размерами работает.
        """
        file_path = _write_file(tmp_path, {"max_conn": "много"})
        with pytest.raises(InfrastructureError, match="ENTERPRISE_POOL_MAX_CONN"):
            Settings(env={}, file_path=file_path)

    def test_pool_config_shape_matches_set_pool_config(self) -> None:
        """Словарь, который уходит в пул, не должен содержать мусорных ключей.

        ``set_pool_config`` неизвестные ключи игнорирует: опечатка в названии
        настройки уехала бы в никуда, и размер пула остался бы прежним без
        единого сообщения.
        """
        from libs.enterprise_data import db as data_db

        config = pool_config(Settings(env={}))
        assert set(config) == set(data_db._DEFAULT_POOL)
        assert set(config) == set(POOL_SETTING_KEYS)


class TestListSettingsKeepBothSeparators:
    def test_newline_separated_list_is_split(self) -> None:
        """Перевод строки — разделитель наравне с запятой.

        Агент экспортирует список таблиц по одной на строку. Список, склеенный
        в одно имя из семи таблиц, выглядел бы как одна несуществующая
        таблица, и capability ``audit`` отвечала бы «таблица не найдена» на
        вполне корректной конфигурации.
        """
        settings = Settings(env={"ENTERPRISE_AUDIT_TABLES": "oarb.audits\noarb.violations"})
        assert settings.get("ENTERPRISE_AUDIT_TABLES") == ["oarb.audits", "oarb.violations"]

    def test_mixed_separators_are_split(self) -> None:
        settings = Settings(
            env={"ENTERPRISE_AUDIT_TABLES": "oarb.audits, oarb.violations\noarb.reports"}
        )
        assert settings.get("ENTERPRISE_AUDIT_TABLES") == [
            "oarb.audits",
            "oarb.violations",
            "oarb.reports",
        ]


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
        file_path = tmp_path / "platform.json"
        file_path.write_text(json.dumps({"data": {"max_rows": 25}}), encoding="utf-8")
        settings = Settings(env={"ENTERPRISE_LLM_MODEL": "qwen3:8b"}, file_path=file_path)

        resolved = settings.as_env()
        assert resolved["ENTERPRISE_MAX_ROWS"] == "25", resolved
        assert resolved["ENTERPRISE_LLM_MODEL"] == "qwen3:8b", resolved

    def test_as_env_encodes_lists_for_string_lookups(self) -> None:
        """Список уезжает строкой, потому что потребитель ищет по строке."""
        settings = Settings(env={"ENTERPRISE_AUDIT_TABLES": "a,b\nc"})
        assert settings.as_env()["ENTERPRISE_AUDIT_TABLES"] == "a,b,c"

    def test_as_env_skips_unset_values(self) -> None:
        """Без значения — отсутствие ключа, а не пустая строка.

        Пустая строка для ``int``-настройки не «не задана», а значение: пустой
        ``ENTERPRISE_EMBED_DIMENSION`` приводится как ``None`` реестром, и
        потребитель обязан увидеть именно отсутствие.
        """
        resolved = Settings(env={}).as_env()
        assert "ENTERPRISE_EMBED_DIMENSION" not in resolved
