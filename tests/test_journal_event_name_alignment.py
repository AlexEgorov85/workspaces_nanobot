"""Единый эталон имён событий журнала и стражи на тихие места.

Словарь типов объявлен платформой
(``mcp-platform/libs/enterprise_common/eventing/types.py``), и агент шлёт
свои имена мимо него. Требование «Единый словарь имён событий» требует
привести имена с обеих сторон, и требование «Тихие места словаря
синхронизируются вместе с ним» — обновить вместе с ними те места, которые
расходятся **без ошибки**.

Живые имена агента сведены с каноническими, и
``data.log_unknown_event_type_policy`` переведён в ``strict``: имя вне
словаря теперь роняет батч, а не проходит. Поэтому живое имя и каноническое
совпадают — эталон ниже это и перечисляет, по одному имени на каноническое.

Разбросанные литералы по коду всё равно разъезжаются молча:
``history_search`` покажет модели имя, которого в журнале нет (пустая выдача
без ошибки), а код вычистки перестанет чистить (таблица растёт, сигнала нет).

Агент не импортирует словарь платформы — граница репозитория: ход к
платформе идёт через MCP-операции, а не через ``sys.path``. Поэтому словарь
и ``platform.json`` читаются здесь **как файлы**, разбором AST.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLATFORM_TYPES = (
    REPO_ROOT / "mcp-platform" / "libs" / "enterprise_common" / "eventing" / "types.py"
)
PLATFORM_JSON = REPO_ROOT / "mcp-platform" / "platform.json"
DATA_SERVICE_MAIN = (
    REPO_ROOT
    / "mcp-platform"
    / "servers"
    / "enterprise"
    / "capabilities"
    / "data"
    / "service"
    / "main.py"
)
#: Операция ``history_search`` на платформе. Раньше здесь был агентский tool
#: ``workspace/tools/history_search_tool.py`` — он удалён (change
#: ``2026-10-03-mcp-native-tools``, п. D6), и перечисление имён событий ушло
#: вместе с ним.
HISTORY_SEARCH_OPERATION = (
    REPO_ROOT
    / "mcp-platform"
    / "servers"
    / "enterprise"
    / "capabilities"
    / "data"
    / "tools"
    / "history_search.py"
)

#: Каталоги агента, в которых событие может быть порождено. ``mcp-platform``
#: сюда НЕ входит: там свои имена (``tool.*`` пишет конвейер исполнения), и
#: словарь платформы — отдельный проверяемый набор.
AGENT_SOURCE_ROOTS: tuple[Path, ...] = (
    REPO_ROOT / "lib",
    REPO_ROOT / "workspace",
    REPO_ROOT / "tools",
)
AGENT_SOURCE_FILES: tuple[Path, ...] = (
    REPO_ROOT / "gateway.py",
    REPO_ROOT / "cli_agent.py",
)


@dataclass(frozen=True)
class LiveName:
    """Одно живое имя агента.

    Attributes:
        site: ``путь:строка``, где имя задано литералом. Проверяется на
            существование файла и наличие имени в нём, но не на саму строку:
            номера строк в эмиттерах сдвигаются с каждой правкой.
        purged_as_empty_outbound: чистится ли код вычистки строки этого типа
            с пустым текстом доставки.
    """

    site: str
    purged_as_empty_outbound: bool = False


#: ЭТАЛОН СООТВЕТСТВИЯ. Единственное место, где перечислены имена, которые
#: агент пишет; всё остальное (стражи ниже) сверяется с ним.
#:
#: Ключ и есть каноническое имя: переименование выполнено, и отдельного
#: «живого» имени, сводимого в каноническое, больше не осталось. Записи с
#: несколькими местами записи объявлены в ``COLLISIONS`` — потеря различия
#: обязана быть видна в коде, а не обнаружена в запросе к журналу.
#:
#: ИМЕН, КОТОРЫХ ЗДЕСЬ НЕТ. Два имени не переименованы, а сняты вместе с
#: мёртвыми путями, и поэтому в эталон не внесены:
#:
#: * ``outbound_intermediate`` — промежуточные ``message(...)`` агента.
#:   Заказчик отнёс их к непокрытым этапам, которые сознательно остаются
#:   не-событиями, а канонического имени для них в словаре нет. Подставить
#:   чужое имя — ложь в журнале; оставить это — отказ батча при ``strict``.
#:   Шина их больше не пишет (``lib/services/db_logging_bus.py``).
#: * ``error`` — метод ``log_error``: вызывающего не было ни в ``lib/``, ни в
#:   ``workspace/``, ни в ``tools/``, ни в точках входа (только тесты).
#:   Оставить имя без потребителей — значило оставить мину на будущее: первое
#:   же возвращение ``log_error`` уронило бы батч. Метод снят.
ETALON: dict[str, LiveName] = {
    # --- границы оборота (пишет хук оборота) ----------------------------
    "agent.started": LiveName("lib/hooks/database_logging_hook.py:49"),
    "agent.responded": LiveName("lib/hooks/database_logging_hook.py:801"),
    "agent.completed": LiveName("lib/hooks/database_logging_hook.py:50"),
    "agent.failed": LiveName("lib/hooks/database_logging_hook.py:51"),
    "agent.compacted": LiveName("lib/services/context_compaction.py:365"),
    "agent.degraded": LiveName("lib/channels/postgres_channel.py:564"),
    # --- вход и доставка --------------------------------------------------
    "agent.received": LiveName("lib/services/db_logging_service.py:1188"),
    "agent.delivered": LiveName(
        "lib/services/db_logging_service.py:1239",
        purged_as_empty_outbound=True,
    ),
    # --- обращения к модели ------------------------------------------------
    "llm.requested": LiveName("lib/hooks/database_logging_hook.py:52"),
    "llm.completed": LiveName("lib/hooks/database_logging_hook.py:53"),
    "llm.exchanged": LiveName("lib/services/db_logging_service.py:1352"),
    # --- вызовы -----------------------------------------------------------
    "tool.started": LiveName("lib/services/db_logging_service.py:1265"),
    "tool.completed": LiveName("lib/services/db_logging_service.py:1304"),
    # Имя то же, что у платформенного отказа, и различает их ``metadata.source``.
    # Отдельного имени не заведено намеренно: при строгой политике неизвестных
    # имён ``tool.rejected`` не был бы записан вовсе.
    "tool.failed": LiveName("lib/services/db_logging_service.py:1304"),
    "tool.suppressed": LiveName("lib/hooks/repeat_guard_hook.py:384"),
}

#: ЯВНО НАЗВАННЫЕ ПОТЕРИ РАЗЛИЧИЯ. Каноническое имя, в которое пишет больше
#: одного места: по одному ``event_type`` эти строки не различить, и различие
#: обязано лежать в ``name``/``payload``/``level`` — то есть быть видимым в
#: данных, а не в догадке читателя.
#:
#: Ведётся по МЕСТАМ ЗАПИСИ, а не по старым именам: после переименования
#: «два живых имени, сводимые в одно каноническое» перестали существовать,
#: а «семь мест записи одного имени» — существуют. Считается сканером
#: стража, то есть объявить можно только то, что действительно пишется.
#:
#: Наборы различающие: ``agent.completed`` — итог оборота хуком, метрики
#: оборота подписчиком и итог подагента (в падающем пути патча);
#: ``agent.degraded`` — сбой поллинга канала (два места), сбой unstick-петли и
#: четыре события зеркала сессий; ``agent.failed`` — исход оборота хуком и
#: fallback-доставка (LogEvent и её ``try_log_event``);
#: ``agent.compacted`` — то же самое для факта сжатия.
COLLISIONS: dict[str, tuple[str, ...]] = {
    "agent.compacted": (
        "lib/services/context_compaction.py",
        "lib/services/context_compaction.py",
    ),
    "agent.completed": (
        "lib/hooks/database_logging_hook.py",
        "lib/services/runtime_events_subscriber.py",
        "lib/services/runtime_events_subscriber.py",
        "lib/services/runtime_patcher.py",
    ),
    "agent.degraded": (
        "lib/channels/postgres_channel.py",
        "lib/channels/postgres_channel.py",
        "lib/channels/postgres_channel.py",
        "lib/gateway/mirror/mirror_poller.py",
        "lib/gateway/mirror/mirror_poller.py",
        "lib/gateway/mirror/mirror_poller.py",
        "lib/gateway/mirror/session_mirror.py",
        "lib/gateway/mirror/session_mirror.py",
    ),
    "agent.failed": (
        "lib/hooks/database_logging_hook.py",
        "lib/services/turn_delivery_factory.py",
        "lib/services/turn_delivery_factory.py",
    ),
}

#: Обоснование мягкого режима. ``None`` означает «возвращаться нечем»:
#: агент пишет только объявленные имена, и смягчать нечего. Чтобы вернуть
#: ``soft``, нужно ЗАПИСАТЬ сюда причину — имя вне словаря, которое агент
#: пишет, и почему его нельзя свести к каноническому. Пустая строка и
#: «починим позже» обоснованием не считаются.
SOFT_JUSTIFICATION: str | None = None


# ---------------------------------------------------------------------------
# Чтение объявлений как файлов: словарь платформы и platform.json
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    """Разобрать исходник или упасть с внятной причиной.

    Обход ``IndentationError`` недопустим: страж, который не смог прочитать
    свой предмет, не имеет права сообщать «расхождения нет».
    """
    source = path.read_text(encoding="utf-8")
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise AssertionError(
            f"{path.relative_to(REPO_ROOT).as_posix()}:{exc.lineno} не разбирается: "
            f"{exc.msg}. Стражи тихих мест не могут быть выполнены, пока файл "
            "не восстановлен."
        ) from exc


def _declared_types() -> set[str]:
    """Прочитать ``EVENT_TYPES`` из словаря платформы разбором AST.

    Словарь собран из констант (``AGENT_STARTED = "agent.started"``), а не из
    литералов, поэтому значения резолвятся по именам — ровно так же, как их
    резолвит сама платформа.
    """
    tree = _parse(PLATFORM_TYPES)
    consts: dict[str, str] = {}
    event_types: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", ""):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name
        ):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts[name] = value.value
        elif name == "EVENT_TYPES":
            event_types = value
    assert event_types is not None, "EVENT_TYPES не найден в словаре платформы"
    args = getattr(event_types, "args", [])
    assert len(args) == 1 and isinstance(args[0], (ast.List, ast.Tuple, ast.Set)), (
        "структура EVENT_TYPES изменилась — разбор словаря надо переписать"
    )
    declared: set[str] = set()
    for element in args[0].elts:
        if isinstance(element, ast.Constant):
            declared.add(element.value)
        elif isinstance(element, ast.Name):
            declared.add(consts[element.id])
        else:  # pragma: no cover - защита от тихой потери проверки
            raise AssertionError(f"непрозрачный элемент EVENT_TYPES: {element!r}")
    return declared


def _declared_policy() -> str:
    return json.loads(PLATFORM_JSON.read_text(encoding="utf-8"))["data"][
        "log_unknown_event_type_policy"
    ]


# ---------------------------------------------------------------------------
# Чтение живого кода: кто какое имя реально пишет
# ---------------------------------------------------------------------------


def _string(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _agent_source_files() -> list[Path]:
    files: list[Path] = []
    for root in AGENT_SOURCE_ROOTS:
        if not root.is_dir():
            continue
        files.extend(sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts))
    files.extend(p for p in AGENT_SOURCE_FILES if p.is_file())
    return files


#: Пакет, в котором объявлена обёртка ``_publish`` фоновых подсистем.
_MIRROR_PACKAGE = "lib.gateway.mirror"


def _inherits_publish_wrapper(tree: ast.AST) -> bool:
    """Обёртка ``_publish`` унаследована, а не объявлена в этом файле.

    Зеркало разнесено на механизм (``mirror_poller.py``) и ресурс
    (``session_mirror.py``), и ресурс пишет в журнал через унаследованный
    ``_publish``. Правило «обёртка объявлена в этом же файле» перестаёт видеть
    места записи ресурса — молча, а эталон продолжает утверждать, что они
    учтены. Это ровно тот отказ стража, ради которого он и написан: новое место
    записи появилось и не попало ни под одну проверку.

    Признак узкий: файл импортирует имя из пакета зеркала и объявляет класс,
    чей базовый класс — одно из этих имён. Форма обёртки проверена отдельно
    сканированием самого ``mirror_poller.py`` — он в тех же каталогах.
    """
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
            node.module == _MIRROR_PACKAGE
            or node.module.startswith(f"{_MIRROR_PACKAGE}.")
        ):
            imported.update(alias.name for alias in node.names)
    if not imported:
        return False
    return any(
        isinstance(node, ast.ClassDef)
        and any(
            isinstance(base, ast.Name) and base.id in imported
            for base in node.bases
        )
        for node in ast.walk(tree)
    )


def _written_event_type_literals() -> dict[str, list[str]]:
    """Живые имена, которые агент реально отдаёт журналу, с местом появления.

    Разбором AST, а не поиском по тексту: строка ``event_type`` встречается в
    докстроках и в тексте сообщений журнала, и текстовый поиск нашёл бы слова,
    а не имена событий.

    Учитываются пять форм задания имени:

    * ``LogEvent(event_type="...")`` и ``try_log_event(..., event_type="...")``;
    * ``log_outbound(..., kind="...")`` — имя исходящего приходит параметром;
    * ``log_sync_event("...")`` — сквозной путь синхронизации, имя первым
      позиционным аргументом;
    * присваивание ``event_type = "..."`` / ``kind = "..."`` / тернарник и
      константы вида ``EV_*`` в ``database_logging_hook.py``;
    * значение по умолчанию параметра ``kind`` — иначе имя, задаваемое
      заглушкой, оказалось бы невидимым, и страж ругался бы на имя, которое
      агент всё-таки пишет.

    Имена, приходящие переменной, пропускаются: их значение статически не
    видно, и выдумывать его здесь нельзя.
    """
    found: dict[str, list[str]] = {}

    def _record(name: str, where: str) -> None:
        found.setdefault(name, []).append(where)

    for path in _agent_source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - исходник агента всегда валиден
            continue
        # ``kind`` в кодовой базе перегружен: в ``runtime_inventory.py`` им
        # помечают вид хука (framework/plugin/factory), и к журналу это
        # отношения не имеет. Носителем имени события ``kind`` является ровно у
        # ``log_outbound``, поэтому правило включается только там, где этот
        # вызов есть или он объявлен.
        journal_file = any(
            isinstance(node, ast.Call)
            and (getattr(node.func, "id", None) or getattr(node.func, "attr", None))
            == "log_outbound"
            for node in ast.walk(tree)
        ) or any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "log_outbound"
            for node in ast.walk(tree)
        )
        name_bearing = {"event_type", "kind"} if journal_file else {"event_type"}

        # Фоновые подсистемы пишут событие через СВОЮ обёртку
        # (``self._publish("имя", …)``), а не напрямую в писатель. Если такая
        # обёртка есть, её первым позиционным аргументом является имя события,
        # и сканер обязан её видеть: иначе места записи в этом файле молча
        # выпадают из проверки, а эталон продолжает утверждать, что они учтены.
        # Правило узкое и проверяемое: обёртка должна быть ОБЪЯВЛЕНА в этом же
        # файле, и первый её параметр после ``self``/``cls`` должен называться
        # ``event_type``. Проверка на ``args[0]`` здесь не годится: у метода
        # первым идёт ``self``, и правило молча не срабатывало бы — то есть
        # сканер выглядел бы рабочим, не видя ни одного нового места записи.
        publish_wrapper = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_publish"
            and any(
                arg.arg == "event_type"
                for arg in node.args.args
                if arg.arg not in ("self", "cls")
            )
            for node in ast.walk(tree)
        ) or _inherits_publish_wrapper(tree)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                for kw in node.keywords:
                    if kw.arg in name_bearing:
                        name = _string(kw.value)
                        if name:
                            _record(name, rel)
                if func == "log_sync_event" and node.args:
                    name = _string(node.args[0])
                    if name:
                        _record(name, rel)
                if publish_wrapper and func == "_publish" and node.args:
                    name = _string(node.args[0])
                    if name:
                        _record(name, rel)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {t.id for t in raw_targets if isinstance(t, ast.Name)}
                if not (names & name_bearing or any(n.startswith("EV_") for n in names)):
                    continue
                value = node.value
                candidates = [value]
                if isinstance(value, ast.IfExp):
                    candidates = [value.body, value.orelse]
                for candidate in candidates:
                    name = _string(candidate)
                    if name:
                        _record(name, rel)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Значение по умолчанию — такой же литерал имени, как и
                # аргумент: ``kind: str = "..."``.
                args = node.args
                positional = list(args.posonlyargs) + list(args.args)
                for arg, default in zip(positional, args.defaults):
                    if arg.arg in name_bearing:
                        name = _string(default)
                        if name:
                            _record(name, rel)
                for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                    if arg.arg in name_bearing and default is not None:
                        name = _string(default)
                        if name:
                            _record(name, rel)
    return found


def _history_search_event_type_enum() -> set[str]:
    """Перечисление имён событий, **видимое модели**, у операции history_search.

    Возвращает пустое множество, если перечисления нет: сейчас параметр
    ``event_type`` — свободная строка, и модель видит только
    ``description``. Это не ослабление стража, а смена его предмета: раньше
    список лежал в JSON-схеме агентского tool'а и мог устареть незаметно.
    Список, однажды появившись, обязан быть подмножеством эталона — это и
    проверяет ``TestHistorySearchEnumStaysInSync``.
    """
    tree = _parse(HISTORY_SEARCH_OPERATION)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if _string(key) != "event_type" or not isinstance(value, ast.Dict):
                continue
            for inner_key, inner_value in zip(value.keys, value.values):
                if _string(inner_key) != "enum":
                    continue
                names = {_string(item) for item in getattr(inner_value, "elts", [])}
                assert None not in names, "в enum history_search появилось не имя"
                return {n for n in names if n}
    return set()


def _empty_outbound_event_types() -> set[str]:
    """Прочитать ``EMPTY_OUTBOUND_EVENT_TYPES`` — имена, которые чистит код
    вычистки журнала."""
    tree = _parse(DATA_SERVICE_MAIN)
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or getattr(node.target, "id", "") != (
            "EMPTY_OUTBOUND_EVENT_TYPES"
        ):
            continue
        items = getattr(node.value, "elts", [])
        names = {_string(item) for item in items}
        assert None not in names, "в EMPTY_OUTBOUND_EVENT_TYPES появилось не имя"
        return {n for n in names if n}
    raise AssertionError("EMPTY_OUTBOUND_EVENT_TYPES не найден в коде вычистки")


# ---------------------------------------------------------------------------
# Стражи
# ---------------------------------------------------------------------------


class TestEtalonIsHonest:
    """Эталон обязан описывать реальность, иначе стражи ниже проверяют выдумку."""

    def test_etalon_sites_point_at_files_that_really_contain_the_name(self):
        for live, entry in ETALON.items():
            rel_path = entry.site.rsplit(":", 1)[0]
            source = REPO_ROOT / rel_path
            assert source.is_file(), f"{live}: нет файла {rel_path}"
            assert live in source.read_text(encoding="utf-8"), (
                f"{live}: имени нет в {rel_path} — эталон разошёлся с кодом"
            )

    def test_etalon_lists_exactly_the_names_the_agent_writes(self):
        """Сторона эмиттера и сторона эталон�� обязаны совпадать целиком.

        Раньше проверка шла в одну сторону («новое имя не внесено в эталон»),
        и этого хватало, пока эталон был таблицей соответствия. Теперь он
        перечисляет имена, которые агент пишет, и обратная сторона важна не
        меньше: имя в эталоне, которое никто не пишет, — это устаревшая
        запись, по которой ``strict``-решение принимается вслепую.
        """
        written = set(_written_event_type_literals())
        assert written == set(ETALON), (
            "эталон разошёлся с тем, что агент пишет: "
            f"пишется, но нет в эталоне — {sorted(written - set(ETALON))}; "
            f"в эталоне, но не пишется — {sorted(set(ETALON) - written)}"
        )

    def test_collisions_are_named_explicitly(self):
        """Потеря различия обязана быть объявлена, а не обнаружена в журнале.

        Сводить новое место записи на уже занятое каноническое имя можно
        намеренно, но тогда это перестаёт быть намерением и становится
        совпадением.
        """
        written = _written_event_type_literals()
        actual = {
            name: tuple(sorted(sites))
            for name, sites in written.items()
            if len(sites) > 1
        }
        assert actual == COLLISIONS, (
            "набор коллизий разошёлся с кодом: "
            f"фактически {sorted(actual)}, объявлено {sorted(COLLISIONS)}"
        )

    def test_etalon_names_are_declared_in_the_platform_dictionary(self):
        """Имя из эталона обязано существовать в словаре платформы.

        Это же условие держит ``strict``: имя вне словаря роняет батч.
        """
        declared = _declared_types()
        missing = sorted(set(ETALON) - declared)
        assert not missing, (
            f"эталон ссылается на необъявленные имена: {missing}. "
            "Они не объявлены в EVENT_TYPES, и strict отбросит батч."
        )


class TestHistorySearchEnumStaysInSync:
    """Имена событий, видимые модели, не могут разойтись с эталоном.

    Предмет стража сместился вместе с операцией: перечисление ушло из
    агентского tool'а на платформу (``history_search`` объявлена как
    ``mcp_enterprise_history_search``), и параметр ``event_type`` стал
    свободной строкой. Пока перечисления нет, проверять нечего — и это само
    по себе фиксируется: как только кто-то опубликует список имён, страж
    потребует, чтобы он был подмножеством эталона.

    Что при этом **потеряно** и осознанно: модель больше не видит перечень
    имён событий и вынуждена брать их из самого журнала. Это цена перехода,
    а не дефект; отражено в ``openspec/changes/2026-10-03-mcp-native-tools``.
    """

    def test_enum_offers_only_names_the_agent_actually_writes(self):
        """ГЛАВНЫЙ СТРАЖ. Старое имя, оставшееся в перечислении после
        переименования эмиттера, даёт модели фильтр, которому нечему ответить:
        пустая выдача без ошибки, и модель заключает, что истории нет."""
        enum = _history_search_event_type_enum()
        written = set(_written_event_type_literals())
        stale = sorted(enum - written)
        assert not stale, (
            "history_search предлагает имена, которых агент больше не пишет: "
            f"{stale}. Модель отфильтрует по ним и получит пустую выдачу без "
            "ошибки. Синхронизировать перечисление с эталоном."
        )

    def test_enum_comes_from_the_etalon(self):
        enum = _history_search_event_type_enum()
        outside = sorted(enum - set(ETALON))
        assert not outside, f"в перечислении history_search имена вне эталона: {outside}"


class TestPurgeCodeStaysInSync:
    """Тихое место №2: точные литералы в коде вычистки журнала."""

    def test_purge_tuple_knows_only_written_names(self):
        """Имя, которого агент не пишет, превращает вычистку в вечный no-op:
        таблица растёт неограниченно, и ни одного сигнала."""
        purge = _empty_outbound_event_types()
        written = set(_written_event_type_literals())
        dead = sorted(purge - written)
        assert not dead, (
            f"код вычистки знает имена, которых агент не пишет: {dead}. "
            "Вычистка молча отключилась."
        )

    def test_purge_tuple_covers_every_empty_outbound_name(self):
        """Обратная сторона: эталон объявляет исходящее, а код чистки о нём не
        знает — и тоже молча."""
        purge = _empty_outbound_event_types()
        expected = {
            live for live, entry in ETALON.items() if entry.purged_as_empty_outbound
        }
        missing = sorted(expected - purge)
        assert not missing, (
            f"код вычистки не знает исходящих имён: {missing} — пустые строки "
            "доставки перестанут вычищаться"
        )

    def test_purge_tuple_comes_from_the_etalon(self):
        purge = _empty_outbound_event_types()
        outside = sorted(purge - set(ETALON))
        assert not outside, f"в коде вычистки имена вне эталона: {outside}"


class TestStrictIsGuarded:
    """``strict`` — не украшение, а режим по умолчанию, и вернуть ``soft``
    молча нельзя.

    Требование «Единый словарь имён событий»: переход в ``strict`` — отдельный
    заход **после** приведения имён с обеих сторон. Включать раньше нельзя: в
    строгом режиме отбрасывается весь батч, то есть упал бы весь журнал
    агента. Обратный переход — тоже заход, и с записанной причиной.
    """

    def test_policy_matches_the_measured_alignment(self):
        """Страж в обе стороны.

        При ``strict`` расхождение недопустимо: платформа отбросит батч.
        При ``soft`` расхождение обязано оставаться измеримым — иначе
        переключатель нечем включать: «всё объявлено, а отказ выключен»
        означало бы, что заход забыли.
        """
        policy = _declared_policy()
        undeclared = sorted(set(ETALON) - _declared_types())
        if policy == "strict":
            assert not undeclared, (
                "включён strict, а агент пишет имена вне словаря: "
                f"{undeclared} — весь батч будет отброшен"
            )
        else:
            assert undeclared, (
                "soft включён, хотя все живые имена уже объявлены в словаре: "
                "переключатель надо выставить в strict отдельным заходом, "
                "иначе он остался бы декоративным"
            )

    def test_soft_requires_a_written_justification(self):
        """Возврат в ``soft`` обязан быть записанным решением, а не настройкой.

        Обоснование — конкретное имя вне словаря, которое агент пишет, и
        почему его нельзя свести к каноническому. Без записи в
        ``SOFT_JUSTIFICATION`` мягкий режим остался бы декоративным:
        плаформа продолжила бы принимать батчи, а проверять было бы нечего,
        и следующий читатель счёл бы словарь обязательным только на словах.
        """
        if _declared_policy() == "strict":
            return
        assert SOFT_JUSTIFICATION, (
            "data.log_unknown_event_type_policy=soft без обоснования: "
            f"SOFT_JUSTIFICATION={SOFT_JUSTIFICATION!r}. Агент пишет только "
            "объявленные имена, смягчать нечего. Либо strict, либо сначала "
            "внести новое имя в словарь платформы и в ETALON, а причину "
            "записать в SOFT_JUSTIFICATION этого файла."
        )

    def test_unknown_policy_value_is_not_silently_downgraded(self):
        """Значение не из двух допустимых — ошибка конфигурации, а не откат в
        ``soft``: тихая подстановка сделала бы переключатель декоративным."""
        assert _declared_policy() in ("soft", "strict"), (
            f"data.log_unknown_event_type_policy={_declared_policy()!r} — "
            "допустимо только 'soft' или 'strict'"
        )
