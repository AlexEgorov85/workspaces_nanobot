"""Страж: cache-API не возвращается в код агента.

Фаза 5 (``enterprise-mcp-platform``, п. 5.6) сняла снимок, FAISS-индексы
и эмбеддинги с агента: снимком владеет capability ``data`` платформы,
индексами — ``vectors``, эмбеддингами — ``llm``. Расхождение моделей
владельца (агент) и владельца данных (платформа) даёт второй путь записи
в один и тот же DuckDB-файл, поэтому «вернуть на всякий случай» здесь
недопустимо.

Раньше страж жил в ``TestNoCacheApiRemains`` внутри
``tests/test_skill_config_api.py`` и проверял один модуль —
``lib/core/skill_config.py``. Теперь этот модуль удалён (0 production-импортёров),
а страж переехал сюда и **расширен на весь код агента**: одна вырожденная
функция в другом модуле вернула бы ровно тот же второй путь записи, что
и проверяемый раньше ``build_cache_provider``, но не была бы поймана.

Проверяется на заведомо плохих данных: верни в любой модуль ``lib/``
функцию ``build_cache_provider`` или импорт ``lib.services.cache_provider_impl``
— тест упадёт.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LIB = _ROOT / "lib"

#: Все функции, выдававшие агента владельцем снимка/индексов/эмбеддингов.
#: Реконструкция по симметрии: любая из них даёт агенту право добывать или
#: писать то, чем владеет платформа.
GONE = (
    "build_cache_provider",
    "get_in_memory_cache_path",
    "get_vector_index_path",
    "get_vector_db_table",
    "get_vector_indexes",
    "get_embedding_config",
    "get_embedding_model",
    "_vector_indexes_list",
)

#: Модули-индикаторы «агент снова полез в хранилище».
_FORBIDDEN_IMPORT_MARKERS = (
    "cache_provider",
    "cache_provider_impl",
    "duckdb",
    "vector_index_service",
    "cache_load_service",
)


def _lib_modules() -> list[Path]:
    if not _LIB.is_dir():
        return []
    return sorted(
        p
        for p in _LIB.rglob("*.py")
        if p.is_file() and not p.name.startswith("_")
    )


class TestNoCacheApiRemains:
    def test_lib_tree_is_scanned(self) -> None:
        """Guard сам на себе: если обход вернул 0 файлов, он «зелёный» вхолостую.

        Модулей в ``lib/`` десятки; пустой результат — признак того, что путь
        обхода сломан (переименование каталога), а не того, что всё чисто.
        """
        modules = _lib_modules()
        assert len(modules) > 10, (
            f"обход lib/ нашёл всего {len(modules)} модулей — "
            "страж не проверяет ничего, проверь путь"
        )

    def test_no_lib_module_defines_cache_api(self) -> None:
        offenders: list[str] = []
        for path in _lib_modules():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover — битый файл ловит pytest
                continue
            for node in tree.body:
                if not isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue
                if node.name in GONE:
                    rel = path.relative_to(_ROOT)
                    offenders.append(f"{rel}:{node.lineno} {node.name}")
        assert offenders == [], (
            "cache-API вернулся в код агента — снимком/индексами/эмбеддингами "
            "владеет capability data/vectors/llm платформы, второй путь записи "
            "в тот же файл даёт гонку:\n  " + "\n  ".join(offenders)
        )

    def test_no_lib_module_imports_cache_storage(self) -> None:
        """Даже без кэш-API модуль не должен тянуть хранилище «на вход»."""
        offenders: list[str] = []
        for path in _lib_modules():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:  # pragma: no cover
                continue
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                    names.extend(
                        f"{node.module}.{alias.name}" for alias in node.names
                    )
                elif isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                for name in names:
                    if any(m in name for m in _FORBIDDEN_IMPORT_MARKERS):
                        rel = path.relative_to(_ROOT)
                        offenders.append(f"{rel}:{node.lineno} {name}")
        assert offenders == [], (
            "skill-side API агента снова достаёт до хранилища напрямую:\n  "
            + "\n  ".join(offenders)
        )

    def test_skill_config_module_is_gone(self) -> None:
        """Сам ``lib/core/skill_config.py`` удалён, а не подписан в обход.

        Модуль держал skill-side API поверх снятого cache-API. Импортёров
        в production-коде не осталось (единственный был в мёртвом дереве
        ``workspace/skills/_legal_summarizer``), поэтому файл — tombstone.

        Принимаются оба конечных состояния: файл уже удалён
        (``git rm lib/core/skill_config.py``) ИЛИ он остался инертным
        tombstone'ом (удалять файл из репозитория нельзя лаунчером). Второе
        состояние — временное, поэтому проверяется строже: в нём не должно
        быть НИ ОДНОГО определения.
        """
        import ast

        path = _LIB / "core" / "skill_config.py"
        if not path.is_file():
            return  # tombstone уже удалён — состояние достигнуто

        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions = [
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        assert definitions == [], (
            "lib/core/skill_config.py должен быть инертным tombstone'ом, но "
            "содержит определения: " + ", ".join(n.name for n in definitions)
        )
        assert not [
            n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
        ], "tombstone не должен ничего импортировать"
