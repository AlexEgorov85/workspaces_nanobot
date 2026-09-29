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

from lib.services.cache_provider import CacheProvider
from lib.services.duckdb_cache_store import DuckDbCacheStore

_ROOT = Path(__file__).resolve().parent.parent

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
                    if isinstance(base, ast.Name) and base.id == "CacheProvider":
                        found.append(f"{_rel(path)}::{node.name}")
        assert found == ["lib/services/duckdb_cache_store.py::DuckDbCacheStore"], (
            f"Ожидалась ровно одна реализация CacheProvider, найдено: {found}"
        )

    def test_concrete_class_declares_inheritance(self) -> None:
        """Иначе enforcement query_sql и типизированные ошибки не имеют носителя."""
        assert issubclass(DuckDbCacheStore, CacheProvider)
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
        present = forbidden & set(vars(CacheProvider))
        assert not present, f"Lifecycle-методы в ABC недопустимы: {sorted(present)}"

    def test_single_mutation_method(self) -> None:
        """Мутация в контракте ровно одна — ingestion от sync-слоя."""
        mutations = {
            "upsert_records", "replace_records", "insert_records",
            "delete_records", "update_records",
        }
        present = mutations & set(vars(CacheProvider))
        assert present == {"upsert_records"}, (
            f"Ожидалась ровно одна мутация upsert_records, найдено: {sorted(present)}"
        )
