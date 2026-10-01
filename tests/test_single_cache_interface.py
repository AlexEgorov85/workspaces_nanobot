"""Guard: **один файл кэша — один интерфейс — одна реализация.**

Модель держится только до тех пор, пока её явно не нарушат, поэтому каждое
правило проверяется автоматически.

**Проверка по AST, а не по тексту.** Первая версия этого guard'а искала
подстроки и срабатывала на собственные же объясняющие docstring'и
(``_publish_path`` внутри ``resolve_publish_path``, ``file.locked()`` внутри
слова «предварительная проверка запрещена»). Такой guard хуже отсутствия
guard'а: он учит людей его ослаблять. Здесь разбираются только реальные
идентификаторы, имена атрибутов и вызовы — проза и комментарии не считаются.

Исключения осознанные и узкие:

* ``tests/`` и ``*/tests/`` — тесты реализации законно называют конкретный
  класс и собирают DuckDB-фикстуры;
* ``tools/release_v*.py`` — исторические release-заметки, а не код.
"""
from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

import pytest

from lib.services.cache_provider import CacheIngestion, CacheProvider, CacheStore
from lib.services.duckdb_cache_store import DuckDbCacheStore

_ROOT = Path(__file__).resolve().parent.parent

# Методы записи. Их НЕТ в роли чтения — и именно поэтому «write-метод,
# используемый рантаймом, но не объявленный в контракте» больше не может
# произойти молча.
_WRITE_METHODS = {"upsert_records", "replace_records", "ensure_schema"}

# Единственные места, где имя конкретной реализации законно.
#
# ``duckdb_cache_store.py`` — сама реализация.
# ``cache_provider.py`` — точка создания: именно она связывает интерфейс
# с реализацией. Если бы и она не могла назвать класс, интерфейс был бы
# абстракцией без носителя.
# ``cache_provider_impl.py`` — общие помощники реализации.
_IMPL_ALLOWED = {
    "lib/services/cache_provider.py",
    "lib/services/duckdb_cache_store.py",
    "lib/services/cache_provider_impl.py",
}

_SCAN_ROOTS = ("lib", "workspace", "tools", "benchmarks")
_EXCLUDE_DIR_NAMES = {"tests", "test", "__pycache__", "node_modules", ".git"}


def _rel(path: Path) -> str:
    return path.relative_to(_ROOT).as_posix()


def _is_excluded(path: Path) -> bool:
    """Пропустить этот файл при проверке правил.

    ``True`` = файл вне области проверки (слой реализации, тесты,
    исторические release-заметки).
    """
    rel = _rel(path)
    if rel in _IMPL_ALLOWED:
        return True
    if any(part in _EXCLUDE_DIR_NAMES for part in path.parts):
        return True
    return path.name.startswith("test_") or path.name.startswith("release_v")


def _iter_sources():
    for root_name in _SCAN_ROOTS:
        root = _ROOT / root_name
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if _is_excluded(path):
                continue
            yield path


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:  # pragma: no cover — битый файл ловит другой тест
        return None


def _names_used(tree: ast.Module) -> set[str]:
    """Все имена, реально упомянутые в коде (без docstring'ов)."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _dotted_chain(node: ast.AST) -> list[str]:
    """``a.b.c`` → ``['a', 'b', 'c']``; иначе пустой список."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    else:
        return []
    return list(reversed(parts))


class TestSingleImplementation:
    """Ровно одна реализация интерфейса."""

    def test_exactly_one_concrete_implementation(self) -> None:
        """Вторая реализация расходится с основной по enforcement'у и диагностике."""
        found: list[str] = []
        for path in _iter_python_all():
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                for base in node.bases:
                    if isinstance(base, ast.Name) and base.id == "CacheStore":
                        found.append(f"{_rel(path)}::{node.name}")
        assert found == ["lib/services/duckdb_cache_store.py::DuckDbCacheStore"], (
            f"Ожидалась ровно одна реализация CacheStore, найдено: {found}"
        )

    def test_concrete_class_declares_inheritance(self) -> None:
        """Иначе enforcement query_sql и типизированные ошибки не имеют носителя."""
        assert issubclass(DuckDbCacheStore, CacheStore)
        assert not DuckDbCacheStore.__abstractmethods__, (
            f"Не реализованы методы ABC: {DuckDbCacheStore.__abstractmethods__}"
        )


def _iter_python_all():
    for root_name in _SCAN_ROOTS:
        root = _ROOT / root_name
        if not root.exists():
            continue
        yield from root.rglob("*.py")


@lru_cache(maxsize=1)
def _analysis() -> tuple[tuple[str, frozenset[str], tuple[str, ...]], ...]:
    """Разбор репозитория один раз на прогон.

    Без кэша guard парсил бы всё дерево по разу на каждое правило и сам
    становился источником медленных прогонов.

    Returns:
        ``[(относительный путь, использованные имена, dotted-цепочки), ...]``
    """
    out: list[tuple[str, frozenset[str], tuple[str, ...]]] = []
    for path in _iter_python_all():
        tree = _parse(path)
        if tree is None:
            continue
        chains: list[str] = []
        for node in ast.walk(tree):
            chain = _dotted_chain(node) if isinstance(node, ast.Attribute) else []
            if chain:
                chains.append(".".join(chain))
        out.append((_rel(path), frozenset(_names_used(tree)), tuple(chains)))
    return tuple(out)


def _iter_cached(*, only_skills: bool = False):
    for rel, names, chains in _analysis():
        if only_skills:
            # Тесты скилла законно собирают DuckDB-фикстуры: контракт
            # «skill не трогает хранилище» относится к исполняемому коду.
            if "skills" not in Path(rel).parts or _is_excluded(_ROOT / rel):
                continue
        elif _is_excluded(_ROOT / rel):
            continue
        yield rel, names, chains


class TestNoNamingImplementationOutsideLayer:
    """Конкретный класс хранилища не упоминается в вызывающем коде."""

    def test_no_store_class_reference_outside_impl(self) -> None:
        offenders = [
            rel for rel, names, _ in _iter_cached()
            if "DuckDbCacheStore" in names
        ]
        assert not offenders, (
            "Конкретный класс хранилища назван вне слоя реализации: "
            f"{offenders}. Правильный путь — open_cache_provider() "
            "с возвращаемым типом CacheProvider."
        )

    def test_no_removed_provider_reference(self) -> None:
        """Удалённая вторая реализация не должна возвращаться."""
        offenders = [
            rel for rel, names, _ in _iter_cached()
            if "PostgresDuckDbProvider" in names or "open_cache" in names
        ]
        assert not offenders, f"Упоминания удалённой реализации: {offenders}"


class TestSkillDoesNotTouchStorage:
    """Skill ходит в кэш через интерфейс, а не трогает хранилище."""

    @pytest.mark.parametrize("attr", [
        "connect",           # duckdb.connect(...)
        "open_cache",        # удалённый метод второй реализации
    ])
    def test_skill_never_connects_to_duckdb(self, attr: str) -> None:
        offenders: list[str] = []
        for rel, names, chains in _iter_cached(only_skills=True):
            hits_duckdb = any(
                c.startswith("duckdb.") and attr in c.split(".") for c in chains
            )
            if hits_duckdb or (attr == "open_cache" and attr in names):
                offenders.append(rel)
        assert not offenders, (
            f"Skill обращается к хранилищу напрямую ({attr}): {offenders}. "
            "Доступ к кэшу — только через CacheProvider."
        )

    def test_skill_does_not_read_cache_setting(self) -> None:
        """Путь к файлу кэша вычисляет runtime, не skill."""
        offenders: list[str] = []
        for path in _iter_sources():
            if "skills" not in path.parts:
                continue
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value == "local_path":
                        offenders.append(_rel(path))
                        break
        assert not offenders, f"Skill читает настройку пути к кэшу: {offenders}"


class TestOpenerIsTheCheck:
    """Проверкой занятости служит открытие, а не предварительный осмотр."""

    def test_no_precheck_of_file_lock(self) -> None:
        """Pre-check и открытие разделены во времени — это гонка.

        Файл кэша process-exclusive, поэтому конфликт обнаруживает сама
        попытка открытия. Предварительная проверка создаёт окно, в котором
        оба процесса увидят «свободно» и оба упадут.
        """
        forbidden = {
            "file_locked", "is_locked", "locked", "is_file_busy",
            "_is_file_busy", "check_lock", "probe_lock",
        }
        offenders = [
            rel for rel, _, chains in _iter_cached()
            if any(c.split(".")[-1] in forbidden for c in chains)
        ]
        assert not offenders, (
            f"Найдена предварительная проверка занятости файла: {offenders}. "
            "Проверкой MUST служить сама попытка открыть файл."
        )


class TestSnapshotModelGone:
    """Модель «снимок для читателей» снята."""

    def test_no_publish_method(self) -> None:
        """Файла кэша один, отдельного снимка для читателей нет."""
        assert not hasattr(DuckDbCacheStore, "publish"), (
            "publish() не должен существовать: файл кэша один"
        )

    def test_no_publish_path_attribute(self) -> None:
        """Проверяется атрибут, а не подстрока.

        ``_publish_path`` как подстрока встречается внутри имени функции
        ``resolve_publish_path`` — это другое и это допустимо.
        """
        offenders = [
            rel for rel, names, _ in _iter_cached() if "_publish_path" in names
        ]
        assert not offenders, f"Остался раздвоенный путь публикации: {offenders}"


class TestInterfaceSurface:
    """Поверхность ABC намеренно узкая."""

    def test_no_lifecycle_methods_on_abc(self) -> None:
        forbidden = {
            "open", "open_cache", "refresh", "check_stale", "try_claim",
            "heartbeat", "acquire_write_fence", "release",
            "start_heartbeat", "stop_heartbeat", "start", "stop", "publish",
        }
        for role in (CacheProvider, CacheIngestion):
            present = forbidden & set(vars(role))
            assert not present, (
                f"Lifecycle-методы в {role.__name__} недопустимы: {sorted(present)}"
            )

    def test_read_role_has_no_write_methods(self) -> None:
        """Роль чтения не содержит ни одного write-метода.

        Регрессия, которую этот guard закрывает: sync-слой дёргал
        ``replace_records`` и ``ensure_schema`` у экземпляра, объявленного
        как ``CacheProvider``, — то есть контракт врал, и «проверка ровно
        одной мутации» запрещала это исправить.
        """
        leaked = _WRITE_METHODS & set(vars(CacheProvider))
        assert not leaked, (
            f"Роль чтения не должна содержать write-методы: {sorted(leaked)}"
        )

    def test_write_role_declares_every_write_method(self) -> None:
        """Роль записи объявляет ровно три операции ingestion."""
        declared = {
            name for name, value in vars(CacheIngestion).items()
            if callable(value) and not name.startswith("_")
        }
        assert declared == _WRITE_METHODS, (
            f"Ожидались ровно {sorted(_WRITE_METHODS)}, найдено {sorted(declared)}"
        )

    def test_write_role_methods_are_abstract(self) -> None:
        """Роль записи — контракт, а не второй носитель реализации."""
        for name in _WRITE_METHODS:
            assert name in CacheIngestion.__abstractmethods__, (
                f"{name} MUST быть abstract в CacheIngestion"
            )

    def test_composite_exposes_both_roles(self) -> None:
        assert issubclass(CacheStore, CacheProvider)
        assert issubclass(CacheStore, CacheIngestion)


class TestRuntimeUsesDeclaredContract:
    """Рантайм не может вызывать у провайдера то, чего нет в контракте.

    Это тот класс дефекта, который статические проверки «есть ли вторая
    реализация» и «нет ли lifecycle-методов» не видят: метод существует у
    concrete-класса, вызывающий типизирован ``CacheProvider``, и всё
    работает — пока однажды не перестаёт.
    """

    # Места, где провайдер кэша реально передаётся и вызывается.
    # Список явный, а не «все переменные с именем store»: имена ``store`` и
    # ``provider`` в других модулях принадлежат другим объектам (сессии,
    # LLM-провайдер), и такой guard ловил бы чужой код.
    #
    # Change ``drop-local-cache-read-from-pg``: запись в кэш выполняет
    # ``CacheLoadService``, который держит роль ``CacheStore`` как
    # ``self._store`` — это отслеживается через ``_WIRING_SITES_ATTRS``,
    # потому что ресивер здесь не простое имя, а атрибут экземпляра.
    _WIRING_SITES: dict[str, set[str]] = {
        "lib/core/application_context.py": {"store", "writer", "provider"},
        "lib/services/preload_service.py": {"store"},
    }
    _WIRING_SITES_ATTRS: dict[str, set[str]] = {
        "lib/services/cache_load_service.py": {"_store"},
    }

    def _declared(self) -> set[str]:
        names: set[str] = set()
        for role in (CacheProvider, CacheIngestion):
            names |= {n for n in dir(role) if not n.startswith("_")}
        return names

    def _accesses(self) -> list[tuple[str, int, str, str]]:
        found: list[tuple[str, int, str, str]] = []
        for rel, receivers in self._WIRING_SITES.items():
            path = _ROOT / rel
            if not path.exists():
                continue
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in receivers
                    and not node.attr.startswith("_")
                ):
                    found.append((rel, node.lineno, node.value.id, node.attr))
        for rel, attrs in self._WIRING_SITES_ATTRS.items():
            path = _ROOT / rel
            if not path.exists():
                continue
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Attribute)
                    and node.value.attr in attrs
                    and not node.attr.startswith("_")
                ):
                    found.append((rel, node.lineno, node.value.attr, node.attr))
        return found

    def test_every_used_method_is_declared(self) -> None:
        declared = self._declared()
        offenders = [
            f"{rel}:{line} {recv}.{attr}"
            for rel, line, recv, attr in self._accesses()
            if attr not in declared
        ]
        assert not offenders, (
            "Рантайм вызывает у провайдера кэша методы, которых нет в "
            f"CacheProvider/CacheIngestion: {offenders}"
        )

    def test_wiring_sites_are_actually_seen(self) -> None:
        """Защита от вакуума: правило выше должно что-то находить.

        Порог — 4, это фактическое число точек обращения к контракту кэша в
        рантайме: ``ensure_schema`` и ``replace_records`` в ``CacheLoadService``
        (единственный writer), ``is_ready`` и ``preload_indexes`` в прогреве.
        Если он упадёт до нуля, значит правило проверяет пустоту.
        """
        accesses = self._accesses()
        assert len(accesses) >= 4, (
            f"Найдено всего {len(accesses)} обращений к провайдеру в местах "
            "обвязки — проверка выше рискует ничего не проверять"
        )


class TestDiscoveryRequiresProvider:
    """Дискавери читает через интерфейс, а не открывает файл кэша сам.

    Регрессия: ``fetch_fn`` стал обязательным, но ни один из трёх вызывающих
    не был переведён на него. Итог был тихим: ``--list-indexes`` отдавал
    ``store_unavailable``, а health-summary прогрева глотал исключение и
    печатал ``orphan (0) / stale (0)`` — то есть «всё зелёное» при
    недоступном runtime. Обязательный параметр без подключённых мест вызова
    должен ломаться громко, поэтому он проверяется здесь.
    """

    _CALLEE = "list_runtime_vector_indexes"

    #: Места вызова, которые должны остаться в дереве. Навык
    #: ``audit_analyzer`` раньше был третьим и уехал на платформу (фаза 9);
    #: список назван поимённо, потому что «не меньше N» перестаёт работать
    #: ровно тогда, когда вызывающий уходит штатно.
    _EXPECTED_CALL_SITES = frozenset({
        "lib/services/preload_service.py",
        "tools/check_indexes.py",
    })

    def test_every_call_site_passes_provider(self) -> None:
        offenders: list[str] = []
        for path in _iter_sources():
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                chain = _dotted_chain(node.func)
                if not chain or chain[-1] != self._CALLEE:
                    continue
                if "provider" in {kw.arg for kw in node.keywords}:
                    continue
                offenders.append(f"{_rel(path)}:{node.lineno}")
        assert not offenders, (
            "Вызовы list_runtime_vector_indexes() без provider= "
            f"(каждый такой вызов упадёт в рантайме): {offenders}"
        )

    def test_call_sites_exist(self) -> None:
        """Защита от вакуума: правило выше не должно молча ничего не проверять.

        Порог-«не меньше N» здесь был плохой защитой: он ломался на каждом
        переезде кода, ничего не говоря о том, что именно должно остаться.
        Третий вызывающий — навык `audit_analyzer` — уехал на платформу в
        фазе 9, и число мест вызова уменьшилось с трёх до двух штатно, а не
        по поломке. Поэтому проверяется не количество, а наличие известных
        продакшн-мест: если они исчезнут все, правило начнёт проверять пустоту.
        """
        found: set[str] = set()
        for path in _iter_sources():
            tree = _parse(path)
            if tree is None:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    chain = _dotted_chain(node.func)
                    if chain and chain[-1] == self._CALLEE:
                        found.add(_rel(path))
        assert self._EXPECTED_CALL_SITES <= found, (
            "ожидались места вызова "
            f"{sorted(self._EXPECTED_CALL_SITES)}, найдено {sorted(found)} — "
            "правило выше перестало что-либо проверять"
        )

    def test_missing_provider_raises(self) -> None:
        """Без провайдера — исключение, а не тихая пустая выдача."""
        from lib.services.cache_provider_impl import list_runtime_vector_indexes

        with pytest.raises(ValueError, match="требует provider"):
            list_runtime_vector_indexes()

    def test_unavailable_cache_is_not_reported_as_empty(self) -> None:
        """Ошибка чтения MUST подниматься, а не выглядеть как «индексов нет»."""

        class _Broken:
            is_ready = True

            def query_sql(self, sql: str) -> dict:
                return {"status": "error", "error": "IO Error: cannot open database file"}

        from lib.services.cache_provider_impl import list_runtime_vector_indexes

        with pytest.raises(RuntimeError, match="failed"):
            list_runtime_vector_indexes(provider=_Broken())

    def test_missing_storage_table_means_no_indexes(self) -> None:
        """Отсутствие таблицы-хранилища — нормальное «индексов нет»."""

        class _Empty:
            is_ready = True

            def query_sql(self, sql: str) -> dict:
                return {
                    "status": "error",
                    "error": 'Catalog Error: Table with name oarb.audit_vectors does not exist!',
                }

        from lib.services.cache_provider_impl import list_runtime_vector_indexes

        assert list_runtime_vector_indexes(provider=_Empty()) == []

    def test_reads_through_query_sql(self) -> None:
        """Строки приходят из ``query_sql`` провайдера, а не из своего соединения."""
        seen: list[str] = []

        class _Ok:
            is_ready = True

            def query_sql(self, sql: str) -> dict:
                seen.append(sql)
                return {
                    "status": "success",
                    "row_count": 1,
                    "columns": ["source", "vector_count"],
                    "rows": [{"source": "audits_index", "vector_count": 10}],
                }

        from lib.services.cache_provider_impl import list_runtime_vector_indexes

        rows = list_runtime_vector_indexes(provider=_Ok())
        assert [r["source"] for r in rows] == ["audits_index"]
        assert rows[0]["vector_count"] == 10
        assert seen and "GROUP BY source" in seen[0]
