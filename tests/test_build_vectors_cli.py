"""Smoke-тест CLI ``tools/build_vectors.py``.

Сам скрипт дёргает реальную PG (см. ``utils.db.configure``/``fetch``/``execute``),
поэтому «герметичный» прогон через monkey-patch слишком хрупок (слишком
много SQL-путей внутри ``build_index``). Этот тест проверяет только
**CLI-обвязку**: парсинг аргументов, ``--help``, fail-fast ветки без DSN,
и что ``--metric`` действительно попадает в argparse без падения.

Регрессии в самой сборке (классификация NEW/CHANGED/REMOVED,
чанкование, signature) уже покрыты модульными тестами:
``tests/test_vector_index_signature.py``, ``tests/test_text_splitter.py``,
``tests/test_cache_provider_meta.py``.

Прогресс-хелперы (``_fmt_eta``/``_interactive_stderr``/``_print_progress``)
тестируются здесь же unit-тестами — они не дёргают БД.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tools.build_vectors import _fmt_eta, _interactive_stderr, _print_progress, _validate_index_config

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _PROJECT_ROOT / "tools" / "build_vectors.py"


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    run_env = dict(os.environ)
    if env:
        run_env.update(env)
    run_env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(_PROJECT_ROOT),
        env=run_env,
        timeout=30,
    )


def test_help_exits_zero_and_lists_metric_flag() -> None:
    """``--help`` не должен падать и должен рекламировать ``--metric``."""
    proc = _run("--help")
    assert proc.returncode == 0, proc.stderr
    assert "--metric" in proc.stdout
    assert "cosine" in proc.stdout
    assert "inner_product" in proc.stdout


def test_metric_default_is_cosine() -> None:
    """``--help`` показывает ``default: cosine`` — единый дефолт для signature."""
    proc = _run("--help")
    assert "default: cosine" in proc.stdout


def test_metric_unknown_choice_rejected() -> None:
    """Любое значение вне ``{cosine, inner_product}`` должно быть отвергнуто argparse."""
    proc = _run("--metric", "l2", "--help")
    assert proc.returncode != 0
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "invalid choice" in combined.lower() or "argparse" in combined.lower()


def _run_bootstrap(setup_code: str) -> subprocess.CompletedProcess:
    """Запустить ``tools/build_vectors.py`` в subprocess после мутации SETTINGS.

    ``config.SETTINGS`` — глобальный ``AttrDict``, но модули вроде
    ``utils.db`` уже импортировали свою ссылку на него. Чтобы подменить
    storage_table до того, как ``build_vectors`` начнёт читать конфиг,
    запускаем его в ОТДЕЛЬНОМ subprocess через bootstrap-скрипт:
    он мутирует ``config.SETTINGS`` в ОДНОМ пространстве имён subprocess'а,
    после чего ``build_vectors`` подхватывает изменение.
    """
    bootstrap = _PROJECT_ROOT / "tests" / "_build_vectors_bootstrap.py"
    bootstrap.write_text(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        + setup_code
        + "\nsys.argv = ['build_vectors.py', '--dry-run']\n"
        "src = open('tools/build_vectors.py', encoding='utf-8').read()\n"
        "ns = {'__name__': '__main__', '__file__': 'tools/build_vectors.py'}\n"
        "exec(src, ns)\n",
        encoding="utf-8",
    )
    try:
        return subprocess.run(
            [sys.executable, str(bootstrap)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(_PROJECT_ROOT),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            timeout=30,
        )
    finally:
        bootstrap.unlink(missing_ok=True)


def test_storage_table_must_have_schema_qualifier() -> None:
    """``storage_table`` без ``schema.`` должен быть отвергнут до подключения к БД.

    Раньше эта проверка жила только в ветке ``table_registry.vector_names()[0]``
    (которая вообще не срабатывала, если первое имя было корректным).
    Сейчас ``storage_table`` приходит из ``gateway.vector.index.storage_table``
    или ``--db-table`` — формат ``schema.table`` обязателен, чтобы
    ``information_schema.tables`` lookup не развалился.
    """
    proc = _run_bootstrap(
        "import config\n"
        "config.SETTINGS['gateway']['vector']['index']['storage_table'] = 'no_schema_only'\n",
    )
    assert proc.returncode != 0
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "schema.table" in combined


def test_storage_table_missing_exits_with_clear_error() -> None:
    """Без ``storage_table`` (ни в конфиге, ни в ``--db-table``) утилита
    должна явно сказать, куда его положить — и не пытаться угадывать по
    ``vector_names()[0]`` (это и был фикс #1).
    """
    proc = _run_bootstrap(
        "import config\n"
        "config.SETTINGS['gateway']['vector']['index'].pop('storage_table', None)\n",
    )
    assert proc.returncode != 0
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "storage_table" in combined
    assert "gateway.vector.index.storage_table" in combined


# --- Прогресс-хелперы ------------------------------------------------------


def test_fmt_eta_human_readable() -> None:
    """ETА форматируется кратко и читаемо: сек/минуты/часы."""
    assert _fmt_eta(0) == "0с"
    assert _fmt_eta(45) == "45с"
    assert _fmt_eta(60) == "1м 00с"
    assert _fmt_eta(272) == "4м 32с"
    assert _fmt_eta(3905) == "1ч 05м"


def test_interactive_stderr_false_for_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """В pipe/redirect-режиме (как в cron и CI) прогресс должен быть линейным."""
    class _FakeStream:
        def isatty(self) -> bool:  # noqa: D102
            return False

    monkeypatch.setattr(sys, "stderr", _FakeStream())
    assert _interactive_stderr() is False


def test_interactive_stderr_true_for_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """В живом терминале — перезаписывающий прогресс (``\\r``)."""
    class _FakeStream:
        def isatty(self) -> bool:  # noqa: D102
            return True

    monkeypatch.setattr(sys, "stderr", _FakeStream())
    assert _interactive_stderr() is True


def test_print_progress_non_interactive_logs_per_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """При redirect в файл прогресс пишется в loguru только на границах batch_size."""
    import io

    from loguru import logger

    sink = io.StringIO()

    class _FakeStream:
        def isatty(self) -> bool:  # noqa: D102
            return False

        def write(self, s: str) -> None:  # noqa: D102
            sink.write(s)

    monkeypatch.setattr(sys, "stderr", _FakeStream())
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    logger.remove()
    logger.add(sink, level="INFO", colorize=False)

    try:
        _print_progress("[t]", 10, 100, 0.0, False, batch_size=10)
        _print_progress("[t]", 11, 100, 0.0, False, batch_size=10)   # не граница — молчит
    finally:
        logger.remove()

    text = sink.getvalue()
    assert "10/100" in text
    assert text.count("Прогресс") == 1


def test_print_progress_interactive_overwrites_via_cr(monkeypatch: pytest.MonkeyPatch) -> None:
    """В TTY прогресс пишется в stderr c ``\\r`` и содержит ETA/скорость."""
    import io
    import time

    captured: list[str] = []
    flushed: list[str] = []

    class _FakeStream:
        def isatty(self) -> bool:  # noqa: D102
            return True

        def write(self, s: str) -> None:  # noqa: D102
            captured.append(s)

        def flush(self) -> None:  # noqa: D102
            flushed.append("")

    monkeypatch.setattr(sys, "stderr", _FakeStream())
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    started = time.time()
    _print_progress("[t]", 50, 100, started, True, batch_size=10)

    line = "".join(captured)
    assert line.startswith("\r[t] Эмбеддинг: 50/100")
    assert "50%" in line
    assert "осталось ~" in line
    assert "чанк/с" in line
    assert flushed  # flush вызван — строка не должна «зависнуть» в буфере


# --- Валидация конфига (_validate_index_config) -----------------------------


def _make_cfg(**overrides) -> dict:
    """Создать минимальный конфиг индекса для тестов."""
    base = {
        "table": "oarb.violations",
        "pk": "id",
        "source_table": "violations",
        "content_columns": ["description", "recommendation"],
        "embedding_columns": ["description"],
        "track_column": "updated_at",
    }
    base.update(overrides)
    return base


def _fake_fetch(columns: list[dict]):
    """Заменить ``utils.db.fetch`` для тестов — возвращает фиктивную схему таблицы."""
    def _fetch(sql: str, *args):
        return columns
    return _fetch


def test_validate_valid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Валидный конфиг — ошибок нет."""
    monkeypatch.setattr("tools.build_vectors.fetch", _fake_fetch([
        {"column_name": "id"}, {"column_name": "description"},
        {"column_name": "recommendation"}, {"column_name": "updated_at"},
    ]))
    result = _validate_index_config(
        "test_index", _make_cfg(), chunk_size=500, chunk_overlap=80, metric="cosine",
    )
    assert result["errors"] == []


def test_validate_empty_embedding_cols_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пустой embedding_cols — предупреждение, не ошибка."""
    monkeypatch.setattr("tools.build_vectors.fetch", _fake_fetch([
        {"column_name": "id"}, {"column_name": "description"},
        {"column_name": "updated_at"},
    ]))
    result = _validate_index_config(
        "test_index", _make_cfg(embedding_columns=[]),
        chunk_size=500, chunk_overlap=80, metric="cosine",
    )
    assert result["errors"] == []
    assert any("пуст" in w for w in result["warnings"])


def test_validate_nonexistent_column_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding_cols ссылается на несуществующую колонку — ошибка с подсказкой."""
    monkeypatch.setattr("tools.build_vectors.fetch", _fake_fetch([
        {"column_name": "id"}, {"column_name": "description"},
        {"column_name": "updated_at"},
    ]))
    result = _validate_index_config(
        "test_index", _make_cfg(embedding_columns=["descroption"]),
        chunk_size=500, chunk_overlap=80, metric="cosine",
    )
    assert len(result["errors"]) >= 1
    assert any("descroption" in e for e in result["errors"])
    # Должна быть подсказка с похожей колонкой
    assert any("description" in e for e in result["errors"])


def test_validate_object_without_column_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """embedding_cols содержит объект без ключа 'column' — ошибка."""
    monkeypatch.setattr("tools.build_vectors.fetch", _fake_fetch([
        {"column_name": "id"}, {"column_name": "description"},
        {"column_name": "updated_at"},
    ]))
    result = _validate_index_config(
        "test_index",
        _make_cfg(embedding_columns=[{"chunk": True, "chunk_size": 500}]),
        chunk_size=500, chunk_overlap=80, metric="cosine",
    )
    assert len(result["errors"]) >= 1
    assert any('"column"' in e for e in result["errors"])


def test_validate_bad_chunk_overlap_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """chunk_overlap >= chunk_size — ошибка."""
    monkeypatch.setattr("tools.build_vectors.fetch", _fake_fetch([
        {"column_name": "id"}, {"column_name": "description"},
        {"column_name": "updated_at"},
    ]))
    result = _validate_index_config(
        "test_index", _make_cfg(),
        chunk_size=100, chunk_overlap=100, metric="cosine",
    )
    assert any("chunk_overlap" in e for e in result["errors"])


def test_validate_pk_not_found_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """pk_column не существует в таблице — ошибка с подсказкой."""
    monkeypatch.setattr("tools.build_vectors.fetch", _fake_fetch([
        {"column_name": "pk_id"}, {"column_name": "description"},
        {"column_name": "updated_at"},
    ]))
    result = _validate_index_config(
        "test_index", _make_cfg(pk="id"),
        chunk_size=500, chunk_overlap=80, metric="cosine",
    )
    assert any("pk_column" in e and "id" in e for e in result["errors"])


def test_validate_only_flag_in_cli() -> None:
    """``--validate-only`` не упадает и показывает валидацию (без реальной БД)."""
    proc = _run("--validate-only", "--index", "does_not_exist")
    # Индекс не найден → sys.exit(1) ДО валидации, но флаг парсится
    assert proc.returncode != 0
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "не найден" in combined or "not found" in combined.lower()


def test_validate_only_help_shows_flag() -> None:
    """``--help`` показывает ``--validate-only``."""
    proc = _run("--help")
    assert proc.returncode == 0
    assert "--validate-only" in proc.stdout
