#!/usr/bin/env python3
"""Создать GitHub Release для тега v2.5.1 через gh CLI (fallback: curl)."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "AlexEgorov85/workspaces_nanobot"
TAG = "v2.5.1"
TITLE = "v2.5.1"

CHANGELOG_BLOCK_HEADER = "## [2.5.1] — 2026-09-13"
NEXT_BLOCK_HEADER = "## [2.5.0] — 2026-09-11"

BODY = """**PATCH-релиз:** регрессии и доработки после v2.5.0 — закрытие lifecycle-deadlock `postgres_channel` при `stream_end` с пустым delta, удаление agent-tools `duckdb_query` и `vector_search` (Phase 8 Resource Model Refactoring), перенос конфига vector-индексов из PG-реестра в `project.json::gateway.vector.index.indexes.*` + хардкод эмбеддинга, DB-first `scripts/predefined` в `audit_analyzer` (+ удаление `tools/generate_predefined_scripts_sql.py`), `tools/build_vectors.py --validate-only` + ETA прогресса, стабилизация порядка таблиц в `lib/utils/duckdb_query.build_schema`, перенос тестов `audit_analyzer` в `workspace/skills/audit_analyzer/tests/`, синхронизация архитектурной документации и README «Что нового».

Изменения конфигурации: `config.json` — провайдер LLM `qwen3.6-35b-a3b` через `https://api.neuraldeep.ru/v1/`, `contextWindowTokens: 40000` (см. `e06b2b0`).

## Fixed

- **postgres_channel lifecycle deadlock** — при `stream_end` с пустым `delta` и потерянном `origin_message_id` polling мог остановиться: `exchange._inflight` оставался занятым, жизненный цикл задачи был размазан между путями финализации (`send`/`send_delta`/`_finalize_turn`/`_mark_failed`) (`71cfcde`).
- **audit_analyzer: REGISTRY dead-code** — удалён; predefined-скрипты читаются из `workspace/skills/audit_analyzer/scripts/predefined/` + `sql/audit_analyzer/seed_predefined_scripts.sql` (DB-first) (`79e0e63`).
- **PG-реестр `public.agent_vector_index_config`** больше не читается кодом (`bf59b5a`) — конфиг индексов перенесён в `project.json::gateway.vector.index.indexes.*`. Legacy SQL-артефакт оставлен как исторический след (см. `docs/VECTOR_INDEXES.md`).
- **`lib/utils/sql_safety`** синхронизирован с Phase 8: убрана ссылка на удалённый `workspace/tools/duckdb_query_tool.py` (`a8e03e8`).
- **`lib/utils/duckdb_query.build_schema`** теперь возвращает таблицы в порядке входного списка (стабильный вывод для `format_schema` и LLM-промпта); колонки внутри таблицы — в порядке `ordinal_position` из `information_schema` (`a8e03e8`).
- **`tools/build_vectors.py`**: pre-flight валидация конфига (`--validate-only`) и индикатор прогресса эмбеддинга с ETA — предотвращает запуск сборки при невалидных настройках и делает длительные прогоны наблюдаемыми (`8b70383`).
- **Тесты `_mark_failed` / `_unstick_processing`** покрыты unit-тестами + интеграционный S6 (полный poll-цикл через `MessageExchange`) для приёмки lifecycle-фикса `71cfcde` (`b3a05a3`).

## Changed

- **Vector-инфраструктура: эмбеддинг захардкожен** — параметры подключения (`base_url`, `model`, `dimension`, `timeout`, `retries`) переехали в константы `_EMBED_*` (`lib/services/cache_provider_impl.py`); bearer-токен берётся из `EMBED_TOKEN` env (`bf59b5a`).
- **`audit_analyzer`**: layout переехал из `predefined/scripts.py` в `scripts/predefined/` (layered package); добавлен `scripts/predefined/db_loader.py` для чтения seed-SQL (`79e0e63`).
- **Skill-локальные тесты** `audit_analyzer` перенесены из `tests/test_audit_analyzer_*.py` в `workspace/skills/audit_analyzer/tests/`; `tests/conftest.py` очищен от skill-специфичных fixtures; `pyproject.toml` унифицирован (`pythonpath` — multi-line массив, добавлен `testpath` для skill-tests) (`10771cc`).
- **Документация**: `docs/TARGET_ARCHITECTURE.md`, `docs/ARCHITECTURE.md`, `docs/INTERNAL_API.md`, `docs/skill-tool-architecture.md`, `docs/VECTOR_INDEXES.md`, `docs/MIGRATION.md`, `docs/SKILL_AUTHORING.md`, `docs/skill-tool-inventory.md`, `docs/architecture/nanobot-inventory.*`, `workspace/skills/audit_analyzer/SKILL.md`, `README.md`, `workspace/tools/example.py`, `sql/audit_analyzer/fix_audit_types_stats_avg.sql` — синхронизированы под фактический код (`e06b2b0`, `4eb5fb5`, `18f70e5`).
- **`config.json`**: провайдер LLM переключён на `qwen3.6-35b-a3b` (`custom`, `https://api.neuraldeep.ru/v1/`, `apiKey: ${LLM_API_KEY}`); `contextWindowTokens: 65536 → 40000` (`e06b2b0`).

## Removed

- **agent-tools `duckdb_query` и `vector_search`** — выпилены вместе с тестами (`workspace/tools/duckdb_query_tool.py`, `workspace/tools/vector_search_tool.py`, `tests/integration/test_vector_search_real_faiss.py`, `tests/test_duckdb_query_tool.py`, `tests/test_vector_search_tool.py`); доступ к DuckDB-кэшу и FAISS-индексам остался через CLI skill'ов и `CacheProvider` API (`12bf182`, Phase 8).
- **`tools/generate_predefined_scripts_sql.py`** удалён — заменён на `sql/audit_analyzer/seed_predefined_scripts.sql` (`79e0e63`).
- **`workspace/skills/audit_analyzer/references/`** удалён — консолидирован в `SKILL.md` (`18f70e5`).
- **`workspace/skills/audit_analyzer/predefined/scripts.py`** удалён — переименован/перенесён в `scripts/predefined/` (`79e0e63`).
- **`lib/core/table_registry`**: `set_embedding_config` / `embedding_config()` удалены (Resource Model Refactoring) (`bf59b5a`).
- **`lib/core/project_settings`**: `EmbeddingSettings` удалён; добавлен `VectorIndexConfig` (`extra="forbid"`) и `VectorIndexSettings.indexes` (`bf59b5a`).
- **`lib/core/skill_registration.register_embedding_config`** удалён (`bf59b5a`).
- **`tools/build_vectors.py`**: убраны `register_embedding_config`, `_persist_index_build_params` и CLI-флаги `--metric` / `--chunk-size` / `--chunk-overlap` — источник конфига — настройки (`bf59b5a`).

---

Полный changelog по подсистемам — в [CHANGELOG.md → 2.5.1](CHANGELOG.md#251--2026-09-13)."""


def _print_curl(token: str) -> None:
    import json

    payload = {"tag_name": TAG, "name": TITLE, "body": BODY}
    body_json = json.dumps(payload, ensure_ascii=False)
    print(
        "gh CLI не найден. Запустите вручную (требуется GITHUB_TOKEN "
        "с правами repo):\n"
    )
    print(f"$env:GITHUB_TOKEN = '<PAT>'")
    print(
        f'curl -X POST -H "Authorization: Bearer $env:GITHUB_TOKEN" '
        f'-H "Accept: application/vnd.github+json" '
        f'https://api.github.com/repos/{REPO}/releases '
        f"-d @{_payload_path()} --fail-with-body -o response.json"
    )


def _payload_path() -> Path:
    p = Path("release_v251_payload.json")
    import json
    p.write_text(json.dumps({"tag_name": TAG, "name": TITLE, "body": BODY},
                            ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def _run_gh() -> int:
    if not shutil.which("gh"):
        print("gh CLI не найден в PATH.", file=sys.stderr)
        return 2
    payload = _payload_path()
    try:
        cmd = [
            "gh", "release", "create", TAG,
            "--repo", REPO,
            "--title", TITLE,
            "--notes-file", str(payload),
        ]
        print("$ " + " ".join(cmd))
        return subprocess.call(cmd)
    finally:
        payload.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="только показать payload и команды, не создавать")
    parser.add_argument("--curl", action="store_true",
                        help="печатать curl-команду вместо gh CLI")
    args = parser.parse_args()

    if args.dry_run or args.curl:
        p = _payload_path()
        print(f"Payload записан в {p}")
        if args.curl:
            _print_curl(token="${GITHUB_TOKEN}")
        return 0

    return _run_gh()


if __name__ == "__main__":
    raise SystemExit(main())