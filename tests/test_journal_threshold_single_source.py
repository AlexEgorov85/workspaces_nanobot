"""Уровень порога журнала настраивается в ОДНОМ месте — без правки кода.

Требование заказчика дословно: «уровень логирования регулируется в одном месте,
без переписывания кода». Место — ключ
``config.json → gateway.agent.logging.db.min_level``, и он уже протянут в
писатель (``ApplicationContext._make_db_logging``).

**Одно место — это один ИСТОЧНИК, а не один читатель.** Место, которое правит
человек, остаётся единственным — ``config.json``; читать его значение должны
две половины агента, иначе платформа половину времени пишет по чужой политике:

* ``ApplicationContext._make_db_logging`` — порог писателя журнала агента;
* ``client_from_settings`` — тот же порог, отданный платформе флагом запуска
  ``--log-min-level`` (платформа применяет его у себя; см.
  ``mcp-platform/servers/enterprise/capabilities/data/service/main.py``).

Поэтому этот страж запрещает не «второе чтение», а **второй источник**:
чтение не из того ключа, молчаливый дефолт вместо значения из конфигурации,
чтение из переменной окружения и — главное — расхождение значений, дошедших до
писателя и до MCP. Проверяются оба чтения поимённо, потому что «ровно два
читателя этого ключа» — тоже утверждение, которое легко сломать.

Файл закрывает несколько классов дефектов, и все они молчаливые.

**Опечатка в пороге.** На месте чтения стояло ``db_cfg.get("min_level",
"INFO")``, то есть «verbose», «info» или «WARN» с опечаткой съедались и
деградировали в ``INFO``: порог включался не тот, и об этом не узнавал никто.
Платформа на незнакомом уровне отказывает — расхождение поведения между
половинами журнала. Теперь присутствующее значение проверяется на старте, и
ошибка называет и значение, и ключ.

**Второй регулятор.** Пока порог можно было задать откуда угодно ещё (переменная
окружения, второй ключ, второй вызов конструктора с литералом), требование «в
одном месте» не выполнялось, хотя выглядело выполненным. Появление такого
регулятора обязано ронять тест, поэтому «одно место» здесь проверяется
структурно: какие места читают порог и из какого ключа, есть ли у порога
молчаливый дефолт, читается ли порог из окружения, единственный ли вызов
писателя передаёт его явно, и совпадает ли значение у писателя со значением,
ушшим в MCP.

Правило по умолчанию при ОТСУТСТВИИ ключа — не «второе место», а объявленный
дефолт писателя ``DEFAULT_MIN_LEVEL``: отсутствие ключа в конфиге законно, и
заменять его на отказ значило бы заставить каждого развертывания объявлять
порог явно. Для платформы отсутствие флага тоже законно — она пишет всё.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from config import runtime_table

AGENT_ROOT = Path(__file__).resolve().parent.parent
APPLICATION_CONTEXT = AGENT_ROOT / "lib" / "core" / "application_context.py"
DB_LOGGING_SERVICE = AGENT_ROOT / "lib" / "services" / "db_logging_service.py"
CONFIG_JSON = AGENT_ROOT / "config.json"

#: Где в дереве агента вообще может появиться второй регулятор порога. Список
#: тот же, что у ``tests/test_journal_event_name_alignment.py``: библиотека
#: агента, его tool'ы и оба входа в процесс.
AGENT_SOURCE_ROOTS: tuple[Path, ...] = (
    AGENT_ROOT / "lib",
    AGENT_ROOT / "workspace",
    AGENT_ROOT / "tools",
)
AGENT_SOURCE_FILES: tuple[Path, ...] = (
    AGENT_ROOT / "gateway.py",
    AGENT_ROOT / "cli_agent.py",
    AGENT_ROOT / "config.py",
)

#: Путь настройки в ``config.json`` — как её видит потребитель: секция
#: ``gateway.agent.*`` поднимается в корень тем же
#: ``config._lift_agent_sections``, что и в ``resolve_application_config``.
CONFIG_KEY_PATH: tuple[str, ...] = ("logging", "db", "min_level")

#: Ровно два чтения порога в дереве агента, и оба — из ``CONFIG_KEY_PATH``.
#: Одно место, которое правит человек, — ``config.json``; читателей два,
#: потому что половины журнала пишутся двумя процессами, и без второго
#: читателя платформа писала бы по своей политике, тихо и всегда.
DECLARED_READERS: tuple[tuple[str, str], ...] = (
    # (файл, функция, которая читает порог)
    ("lib/core/application_context.py", "_make_db_logging"),
    ("lib/services/enterprise_mcp_client.py", "_journal_min_level"),
)

#: Имя переменной окружения, которой не должно быть. Ловит самый естественный
#: второй регулятор — «тот же ключ, но из окружения, чтобы не править файл».
#: ``gateway.log_level`` под это не попадает намеренно: это уровень
#: stdlib-логирования процесса, а не порог строк в ``agent_gateway_logs``, и он
#: настраивается своим ключом в своей подсистеме.
ENV_NAME_RE = re.compile(r"min_?level", re.IGNORECASE)

#: Рабочие данные, а не код. В ``workspace/data_store/cache/sessions`` лежат
#: копии сессий, а среди них — целые снимки проекта; скан по ним искал бы
#: «второй регулятор» в чужом бэкапе и падал бы без причины.
RUNTIME_DIRS = frozenset(
    {"__pycache__", "data_store", "sessions", "memory", ".nanobot", "cron", ".git"}
)


def _is_source(path: Path) -> bool:
    # Относительно корня репозитория: сам корень лежит в каталоге
    # ``.nanobot``, и абсолютный разбор частей пути отсеял бы всё дерево.
    try:
        parts = path.relative_to(AGENT_ROOT).parts
    except ValueError:
        parts = path.parts
    return not any(part in RUNTIME_DIRS for part in parts)


def _agent_sources() -> list[Path]:
    paths: list[Path] = []
    for root in AGENT_SOURCE_ROOTS:
        paths.extend(path for path in sorted(root.rglob("*.py")) if _is_source(path))
    paths.extend(path for path in AGENT_SOURCE_FILES if path.exists())
    return paths


def _configured_threshold() -> Any:
    """Значение порога из ``config.json`` — как его видит потребитель файла.

    ``gateway.agent.*`` поднимается в корень тем же
    ``config._lift_agent_sections``, что и в ``resolve_application_config``,
    поэтому и проверяется по путям из ``SETTINGS``
    (``logging.db.min_level``), а не по физическому расположению в файле.
    """
    from config import _lift_agent_sections, _strip_jsonc_comments

    data = json.loads(_strip_jsonc_comments(CONFIG_JSON.read_text(encoding="utf-8")))
    _lift_agent_sections(data)
    for key in CONFIG_KEY_PATH:
        assert isinstance(data, dict) and key in data, f"в config.json нет {key!r}"
        data = data[key]
    return data


def _relative(path: Path) -> str:
    return path.relative_to(AGENT_ROOT).as_posix()


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


def _path_constants(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    """Константы-пути: ``ИМЯ = ("logging", "db", "min_level")``.

    Отдельная форма чтения настройки: ключ берётся не литералом в ``.get(...)``,
    а проходом по кортежу. Такой источник обязан попадать в перечень читателей
    так же, как прямой ``.get("min_level")``, — иначе страж объявил бы зелёным
    дерево, где порог читается в обход объявления (было именно так: чтение по
    пути в клиенте MCP не виделось сканеру прямых литералов).
    """
    constants: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            names = [node.targets[0]]
            value = node.value
        elif isinstance(node, ast.AnnAssign):  # ``ИМЯ: tuple[str, ...] = (...)``
            names = [node.target]
            value = node.value
        else:
            continue
        if not isinstance(value, ast.Tuple) or not value.elts:
            continue
        parts = tuple(
            element.value
            for element in value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        )
        if len(parts) != len(value.elts):
            continue
        for name in names:
            if isinstance(name, ast.Name):
                constants[name.id] = parts
    return constants


def _reads_of(tree: ast.AST, *, path_constants: dict[str, tuple[str, ...]]) -> list[tuple[int, tuple[str, ...]]]:
    """Чтения настройки в дереве: ``(строка, путь ключа)``.

    Две формы, обе — реальные чтения:

    * по литералу — ``x.get("min_level")`` и ``x["min_level"]``;
    * по константе-пути — ``for key in PATH: node.get(key)``.

    Всё остальное (сообщения об ошибке, имя флага, докстринги) чтением
    настройки не является и в перечень не попадает.
    """
    reads: list[tuple[int, tuple[str, ...]]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"get", "settings_section"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            reads.append((node.lineno, (node.args[0].value,)))
        elif (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            reads.append((node.lineno, (node.slice.value,)))
        elif isinstance(node, ast.For) and isinstance(node.iter, ast.Name):
            path = path_constants.get(node.iter.id)
            target = node.target
            if not path or not isinstance(target, ast.Name):
                continue
            # Цикл, в котором результат читается по переменной итерации.
            if any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get"
                and inner.args
                and isinstance(inner.args[0], ast.Name)
                and inner.args[0].id == target.id
                for inner in ast.walk(node)
            ):
                reads.append((node.lineno, path))
    return reads


def _is_threshold_key(keys: tuple[str, ...]) -> bool:
    """Похож ли этот ключ на порог журнала.

    Ловится всё ``*min_level*`` в любом регистре и написании — прямой ключ,
    второй регулятор рядом (``log_min_level``) и имя переменной окружения.
    Ключ ``gateway.log_level`` под это не подходит намеренно: это разборчивость
    собственных сообщений агента, а не порог журнала, и смешивать их — значит
    завести два места ещё и здесь.
    """
    return "min_level" in keys[-1].lower()


def _threshold_reads() -> list[tuple[str, int, tuple[str, ...]]]:
    """Все чтения порога в дереве агента: ``(файл, строка, путь ключа)``."""
    found: list[tuple[str, int, tuple[str, ...]]] = []
    for path in _agent_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        constants = _path_constants(tree)
        for line, path_keys in _reads_of(tree, path_constants=constants):
            if _is_threshold_key(path_keys):
                found.append((_relative(path), line, path_keys))
    return found


def _settings(*, min_level: Any = None, present: bool = True) -> dict:
    """Набор настроек, общий для писателя журнала и для клиента MCP.

    Один словарь, а не два похожих: иначе сравнение значений в
    ``test_writer_and_mcp_client_resolve_the_same_value`` проверяло бы два
    разных набора и всегда было бы зелёным. Пути — как их видит потребитель:
    ``gateway.agent.*`` поднят в корень (см. ``CONFIG_KEY_PATH``).
    """
    db: dict[str, Any] = {
        "enabled": True,
        # Имена таблиц берутся из того же объявления, что и в бою: зашитая
        # строка сделала бы тест слепым к переименованию таблицы и нарушила бы
        # страж «никаких реальных имён таблиц в тестах».
        "table_name": runtime_table("gateway_logs"),
        "question_runs_table": runtime_table("question_runs"),
        "schema": "public",
        "flush_interval_sec": 5.0,
    }
    if present:
        db["min_level"] = min_level
    return {
        "logging": {"db": db},
        "channels": {"postgres": {"dsn": "postgresql://test"}},
        "enterprise_mcp": {
            "enabled": True,
            "command": "python",
            "args": ["-m", "servers.enterprise.server"],
        },
    }


def _context(
    *, min_level: Any = None, present: bool = True, settings: dict | None = None
) -> Any:
    """Контекст, из которого собирается писатель журнала.

    Двойник вместо ``ApplicationContext`` намеренно: проверяется ровно
    разрешение порога, а не весь lifecycle контекста. Поля — те, что читает
    ``_make_db_logging``.

    ``settings`` — когда передан, поля берутся из него: писатель и клиент MCP
    обязаны читать один и тот же набор, иначе сверка их значений ничего не
    доказывает.
    """
    resolved = (
        settings if settings is not None else _settings(min_level=min_level, present=present)
    )
    logging_section = resolved.get("logging") or {}
    channels_section = resolved.get("channels") or {}
    flush_interval = float(
        ((logging_section.get("db") or {}).get("flush_interval_sec")) or 5.0
    )

    class _ConfigService:
        @staticmethod
        def settings_section(name: str) -> dict:
            if name == "logging":
                return logging_section
            if name == "channels":
                return channels_section
            return {}

    return SimpleNamespace(
        config_service=_ConfigService(),
        project_settings=SimpleNamespace(
            logging=SimpleNamespace(db=SimpleNamespace(flush_interval_sec=flush_interval))
        ),
    )


def _writer(settings: dict) -> Any:
    """Настоящий писатель журнала агента, собранный из набора настроек."""
    from lib.core.application_context import _make_db_logging

    return _make_db_logging(_context(settings=settings))


def _build(**kwargs: Any) -> Any:
    from lib.core.application_context import _make_db_logging

    return _make_db_logging(_context(**kwargs))


class TestConfigValueIsResolvedOnceAndLoudly:
    """Значение из конфигурации доезжает до писателя — или называет ошибку."""

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("DEBUG", "DEBUG"),
            ("INFO", "INFO"),
            ("WARN", "WARN"),
            ("ERROR", "ERROR"),
            # Написания, которые приходят из привычного ``logging`` и из ручной
            # правки файла: разбор синонима обязателен и у порога, иначе второй
            # разошёлся бы с уровнем события.
            ("warning", "WARN"),
            (" warn ", "WARN"),
            ("info", "INFO"),
        ],
    )
    def test_configured_threshold_reaches_the_writer(
        self, configured: str, expected: str
    ) -> None:
        service = _build(min_level=configured)
        assert service._min_level == expected

    def test_absent_key_falls_back_to_the_declared_default(self) -> None:
        from lib.services.db_logging_service import DEFAULT_MIN_LEVEL

        service = _build(present=False)
        assert service._min_level == DEFAULT_MIN_LEVEL

    @pytest.mark.parametrize("typo", ["verbose", "info!", "TRACE", "5", "отладка", "WARNINGS"])
    def test_unknown_threshold_stops_startup_by_name(self, typo: str) -> None:
        """Опечатка обязана быть замечена на старте, а не съедена дефолтом.

        До правки здесь стояло ``db_cfg.get("min_level", "INFO")``, и «verbose»
        включал боевой порог ``INFO`` молча: оператор считал, что поднял
        подробность, а получал противоположное, и узнать об этом можно было
        только по таблице.
        """
        from config import ConfigurationError

        with pytest.raises(ConfigurationError) as excinfo:
            _build(min_level=typo)

        message = str(excinfo.value)
        assert "logging.db.min_level" in message, (
            "ошибка не называет место настройки — искать придётся по всему конфигу"
        )
        assert repr(typo) in message or typo in message, (
            "ошибка не называет само значение"
        )

    def test_broken_threshold_does_not_produce_a_writer(self) -> None:
        """Отказ — до сборки писателя, а не «собрали и молча фильтруем не так»."""
        from config import ConfigurationError

        with pytest.raises(ConfigurationError):
            _build(min_level="verbose")


class TestOneSourceWithTwoReaders:
    """Один источник — ``config.json``; читателей ровно два, и оба его."""

    def test_only_the_two_declared_readers_read_the_threshold(self) -> None:
        """Порог читают ровно два места, и оба — объявленные.

        Третий читатель — это третий ответ на вопрос «каким уровнем пишется
        журнал», и он появился бы в коде, который никто не правил. Сканер
        ловит и прямое чтение по литералу, и чтение по константе-пути:
        второе не хуже первого, а при прежней версии этого стража оно было
        не видно вовсе.
        """
        reads = _threshold_reads()
        files = sorted({file for file, _, _ in reads})
        assert files == sorted(file for file, _ in DECLARED_READERS), (
            "порог журнала читают не те файлы: "
            f"{[(file, line) for file, line, _ in reads]}. Объявлены читатели "
            f"{[file for file, _ in DECLARED_READERS]} — писатель журнала и "
            "клиент MCP, и оба читают ключ logging.db.min_level."
        )

    def test_each_reader_reads_it_once(self) -> None:
        """Внутри читателя ключ читается один раз.

        Два чтения в одном месте — это два пути внутри одного пути, и какой из
        них сработает, тот и решит, каким порогом пишется журнал.
        """
        for file, _ in DECLARED_READERS:
            sites = [
                (line, keys) for read_file, line, keys in _threshold_reads() if read_file == file
            ]
            assert len(sites) == 1, (
                f"{file}: порог читается {len(sites)} раз(ы) — {sites}. "
                "Один читатель, одно чтение."
            )

    def test_both_readers_resolve_the_same_config_path(self) -> None:
        """Оба читателя идут по одному пути — ``logging → db → min_level``.

        Это и есть «одно место» по смыслу: неважно, сколько раз ключ прочитан,
        важно, что он один. Писатель читает его по подсекции
        ``settings_section("logging") → db``, клиент MCP — по константе-пути,
        и сверяются они тут, а не на словах в докстрингах.
        """
        for file, function in DECLARED_READERS:
            source = (AGENT_ROOT / file).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file)
            reads = _reads_of(
                _function(tree, function), path_constants=_path_constants(tree)
            )
            level_reads = [
                (line, keys) for line, keys in reads if _is_threshold_key(keys)
            ]
            assert len(level_reads) == 1, (
                f"{file}::{function}: чтений порога {len(level_reads)} — {level_reads}"
            )
            _, keys = level_reads[0]
            if len(keys) == len(CONFIG_KEY_PATH):
                # Чтение по константе-пути: путь известен целиком.
                assert keys == CONFIG_KEY_PATH, (
                    f"{file}::порог читается по пути {' → '.join(keys)}, "
                    f"а не по {' → '.join(CONFIG_KEY_PATH)}"
                )
            else:
                # Прямое чтение: путь восстанавливается по ключам, которые
                # функция читает до него (секция и подсекция).
                before = [key for line, keys in reads if line < level_reads[0][0] for key in keys]
                assert "logging" in before and "db" in before, (
                    f"{file}::{function}: порог читается не из logging → db "
                    f"(ключи до него: {before})"
                )

    def test_writer_and_mcp_client_resolve_the_same_value(self) -> None:
        """Оба читателя на ОДНИХ настройках дают одно значение.

        Самая ценная проверка файла: писатель и флаг запуска MCP собираются из
        одного и того же словаря настроек, и значение, ушедшее в MCP, должно
        означать для платформы ровно то, чем фильтрует писатель. Сравниваются
        приведённые значения: писатель нормализует при сборке, а клиент
        отдаёт строку как есть, и приводит её уже платформа (её правило разбора
        — то же, что у писателя).
        """
        from lib.services.db_logging_service import normalize_journal_level
        from lib.services.enterprise_mcp_client import (
            LOG_MIN_LEVEL_FLAG,
            client_from_settings,
        )

        for configured in ("DEBUG", "INFO", "WARN", "ERROR", "warning"):
            settings = _settings(min_level=configured)
            writer = _writer(settings)
            args = client_from_settings(settings).describe()["args"]
            assert LOG_MIN_LEVEL_FLAG in args, (
                f"при min_level={configured!r} флаг {LOG_MIN_LEVEL_FLAG} не ушёл "
                f"в MCP: {args}. Платформа осталась бы со своей политикой."
            )
            forwarded = args[args.index(LOG_MIN_LEVEL_FLAG) + 1]
            assert normalize_journal_level(forwarded) == writer._min_level, (
                f"писатель фильтрует по {writer._min_level!r}, а в MCP ушло "
                f"{forwarded!r} — половины журнала пишутся по разному."
            )

    def test_absent_key_gives_no_flag_and_no_policy_change(self) -> None:
        """Ключа нет — писателю дефолт, платформе флаг не уходит.

        Отсутствие ключа законно и разбирается по-разному: у писателя оно
        законченное состояние (``DEFAULT_MIN_LEVEL``), у платформы — «пишем всё,
        как писали до флага». Подстановка дефолта в флаг сделала бы второе
        место, объявления которого нет.
        """
        from lib.services.db_logging_service import DEFAULT_MIN_LEVEL
        from lib.services.enterprise_mcp_client import (
            LOG_MIN_LEVEL_FLAG,
            client_from_settings,
        )

        settings = _settings(min_level=None, present=False)
        assert _writer(settings)._min_level == DEFAULT_MIN_LEVEL
        args = client_from_settings(settings).describe()["args"]
        assert LOG_MIN_LEVEL_FLAG not in args, (
            f"без ключа в конфигурации платформе отдан порог {args} — "
            "значение, которого в конфигурации нет."
        )

    def test_the_threshold_has_no_silent_default_at_the_read_site(self) -> None:
        """У ключа не может быть молчаливого дефолта.

        Именно ``.get("min_level", "INFO")`` съедал опечатку. Отсутствие ключа
        законно и разбирается отдельно (``present=False`` выше), но подставлять
        заданное значение вместо названного нельзя — тогда читатель не знает,
        что он настроил.
        """
        offenders: list[str] = []
        for path in _agent_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "get" or len(node.args) < 2:
                    continue
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and first.value == "min_level"):
                    continue
                if not isinstance(node.args[1], ast.Constant) or node.args[1].value is not None:
                    offenders.append(f"{_relative(path)}:{node.lineno}")
        assert not offenders, (
            "у порога журнала появился молчаливый дефолт: "
            f"{offenders}. Опечатка обязана называться на старте."
        )

    def test_no_environment_variable_can_override_the_threshold(self) -> None:
        """Порог не читается из окружения.

        Второй регулятор, добавленный «чтобы не править файл», делает
        настройку неуправляемой: два значения у одной таблицы, и видно только
        то, что попало в конфиг.
        """
        offenders: list[str] = []
        for path in _agent_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_env_read = (
                    isinstance(func, ast.Name)
                    and func.id in {"getenv", "environ_get"}
                ) or (
                    isinstance(func, ast.Attribute)
                    and func.attr in {"get", "getenv"}
                    and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"
                )
                if not is_env_read or not node.args:
                    continue
                first = node.args[0]
                if isinstance(first, ast.Constant) and ENV_NAME_RE.search(str(first.value)):
                    offenders.append(f"{_relative(path)}:{node.lineno} {first.value!r}")
        assert not offenders, (
            "порог журнала начал читаться из переменной окружения: "
            f"{offenders}. Настройка обязана быть в одном месте — config.json."
        )

    def test_the_writer_is_built_once_and_always_gets_the_threshold(self) -> None:
        """Единственная сборка писателя и единственный вызов с порогом.

        Второй вызов с литералом — это тот же второй регулятор, только на
        словаре: «у нас тут для тестов/для стенда свой порог», и в журнале
        оказывается два разных фильтра.
        """
        callers: list[str] = []
        for path in _agent_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", "")
                )
                if name != "DbLoggingService":
                    continue
                has_threshold = any(kw.arg == "min_level" for kw in node.keywords)
                if not has_threshold:
                    callers.append(f"{_relative(path)}:{node.lineno} БЕЗ min_level")

        assert callers == [], (
            "писатель журнала собирается без порога — это второй регулятор по "
            f"умолчанию: {callers}"
        )

    def test_the_default_threshold_is_declared_once(self) -> None:
        """Дефолт конструктора — то же объявление, а не второй литерал.

        Второй литерал в сигнатуре выглядит безобидно, пока однажды не станет
        правдой: порог по умолчанию у сервиса и порог, объявленный в словаре
        писателя, разъедутся, и никто этого не увидит.
        """
        tree = ast.parse(
            DB_LOGGING_SERVICE.read_text(encoding="utf-8"), filename="db_logging_service.py"
        )
        defaults: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
                continue
            args = node.args
            positional = [*getattr(args, "posonlyargs", []), *args.args]
            padded = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
            for arg, default in [*zip(positional, padded), *zip(args.kwonlyargs, args.kw_defaults)]:
                if arg.arg == "min_level":
                    defaults.append(ast.unparse(default) if default is not None else "<нет>")
        assert defaults == ["DEFAULT_MIN_LEVEL"], (
            "дефолт порога в конструкторе должен быть единственным объявлением "
            f"DEFAULT_MIN_LEVEL, а не своим значением: {defaults}"
        )

    def test_the_configured_value_is_a_level_of_the_shared_scale(self) -> None:
        """Значение в боевом конфиге обязано быть уровнем из общей шкалы.

        Проверяется принадлежность шкале, а не конкретное значение: порог —
        настройка оператора, и менять его не значит чинить код.
        """
        from lib.services.db_logging_service import (
            JOURNAL_LEVELS,
            normalize_journal_level,
        )

        value = _configured_threshold()
        assert value is not None, (
            "в config.json нет logging.db.min_level — порог нечем настроить, "
            "и дефолт писателя станет неявным регулятором"
        )
        # Приводится тем же правилом, что и уровень события, — иначе
        # «DEBUG» и «debug» в конфиге вели бы себя по-разному.
        assert normalize_journal_level(value) in JOURNAL_LEVELS
