#!/usr/bin/env python3
"""Создать GitHub Release для тега v2.5.4 через gh CLI (fallback: curl).

Режимы:
  --dry-run   — печатает полный payload (tag/title/body) в stdout, ничего
                не создаёт и не пишет на диск. По умолчанию.
  --curl      — печатает готовую curl-команду для ручного запуска
                (нужно подставить $GITHUB_TOKEN).
  --run       — реальный `gh release create` (явный флаг, чтобы нельзя было
                запустить публикацию случайно).

Usage:
  python tools/release_v254.py            # эквивалент --dry-run
  python tools/release_v254.py --dry-run  # печать payload
  python tools/release_v254.py --curl     # печать curl-команды
  python tools/release_v254.py --run      # реальный gh release create

Замечание: при --run сначала пишется временный payload.json, вызывается gh,
после — файл удаляется в `finally`. Поведение унаследовано от
release_v251.py / release_v252.py.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "AlexEgorov85/workspaces_nanobot"
TAG = "v2.5.4"
TITLE = "v2.5.4"

CHANGELOG_BLOCK_HEADER = "## [2.5.4] — 2026-10-09"
# Предыдущий релиз по семантике — v2.5.3 (2026-09-18). Его блок
# `## [2.5.3]` физически остался в ветке release/v2.5.3 и на этой линии
# отсутствует: в CHANGELOG.md здесь идёт 2.5.4 → 2.5.2.
NEXT_BLOCK_HEADER = "## [2.5.3] — 2026-09-18"

BODY = """**PATCH-релиз v2.5.4.** Первое — из изменений этого релиза:

- **Порядок старта «данные → векторы → каналы»** (`lib/services/startup_gate.py`). Каналы поднимаются только после того, как данные в памяти и FAISS-индексы собраны; готовность определяется сигналом, без таймаутов и ретраев. Политика `gateway.startup.vector_preload.on_unavailable`: `warn` (дефолт, degraded-старт) или `fail` (`exit 2`).
- **Доступ агента к аудиту — tool'ами, а не CLI.** `duckdb_query` и `vector_search` выполняются в процессе gateway на его живом `CacheProvider`. Вызывать `scripts/cli.py` из агента нельзя: DuckDB допускает одно соединение на файл.
- **Кэш: рабочая БД и снапшот разведены.** OWNER держит рабочую DuckDB в памяти, `cache.duckdb` — отдельный snapshot-файл (`adopt_snapshot()`), который открывается только на время публикации и при reuse. Плюс свежесть снимка — `gateway.cache.reuse_ttl_hours` (дефолт 23 ч).
- **Новый skill `audit_formulation_strengthener`** — оценка силы формулировок проверок, свои `SKILL.md` и тесты; прогон скилла добавлен в CI.

⚠️ Три BREAKING-изменения (полный список и ручные действия — в `docs/MIGRATION.md`, секция «v2.5.3 → v2.5.4»):

1. `python cli_agent.py --profile=prod` больше не работает — CLI имеет фиксированный профиль `test`; передача флага даёт `ConfigurationError` и `exit 2`.
2. Cron — только под gateway: `CronService` не создаётся при `role="cli"`, даже если `gateway.enable_cron = true`.
3. Старт блокируется при отсутствии runtime-таблиц (`SchemaValidationService` → `exit 2`): перед деплоем выполните `python tools/migrate.py --apply` (для test-профиля — `python tools/apply_test_profile_tables.py`).

## Changed

- Кэш и snapshot: `adopt_snapshot()` (ATTACH READ_ONLY → копирование таблиц → DETACH), конфигурация «рабочая БД = файл снапшота» отвергается явно; раньше второй процесс получал `File is already open ... (PID ...)` даже в `read_only`.
- `CacheProvider` стал ABC-контрактом (`lib/services/cache_provider.py` + `cache_provider_impl.py`); DuckDB-фабрика — `DuckDbCacheStore.open(path, mode)`; `query_sql()` валидирует DDL и отклоняет запись в `READ_ONLY`.
- Линт и зависимости: `ruff --check` по конфигу проекта как обязательный CI-гейт (было 677 нарушений), `chardet` закреплён на 5.2.0 из-за конфликта с nanobot-ai.

## Added

- `lib/services/schema_validation.py` — pre-startup проверка 6 runtime-таблиц одним SELECT'ом к `information_schema.tables`.
- `lib/services/runtime_inventory.py` — single source of truth для startup-инвентаря + `tools/diagnose_startup.py` (OK / DRIFT / CRITICAL).
- `lib/services/project_tool_loader.py` — единый путь регистрации кастомных tool'ов из `workspace/tools/*.py`.
- `tools/audit_nanobot_contracts.py` — AST-аудит контрактов против свежего nanobot.

## Docs

- `CHANGELOG.md` приведён к Keep a Changelog: 26 дублирующихся заголовков свёрнуты в 9 канонических категорий, снятый Known Issue убран.
- `docs/MIGRATION.md` — добавлены три BREAKING-блока и новые ключи конфигурации.
- Архивированы changes `cache-snapshot-reuse-ttl` и `startup-vector-preload-gate` (канонические спеки созданы, `StartupGate` внесён в реестр компонентов).

---

Полный changelog по подсистемам — в [CHANGELOG.md → 2.5.4](CHANGELOG.md#254--2026-10-09)."""


def _payload() -> dict:
    return {"tag_name": TAG, "name": TITLE, "body": BODY}


def _print_payload() -> None:
    """Печать полного payload в stdout (для --dry-run). Без записи на диск."""
    # На Windows-cmd-PowerShell stdout по умолчанию cp1251 — переключаем на utf-8
    # чтобы корректно отдавать Unicode (эмодзи, стрелки, кириллица).
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    sys.stdout.write(json.dumps(_payload(), ensure_ascii=False, indent=2))
    sys.stdout.write("\n")


def _print_curl() -> None:
    """Печать готовой curl-команды (нужен $GITHUB_TOKEN). Без записи на диск."""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    import tempfile

    tmp = Path(tempfile.gettempdir()) / f"release_v254_{TAG.replace('.', '_')}_body.json"
    tmp.write_text(json.dumps(_payload(), ensure_ascii=False), encoding="utf-8")
    print("# Сохраните GITHUB_TOKEN в окружение, затем выполните:\n")
    print("$env:GITHUB_TOKEN = '<PAT>'   # Windows PowerShell")
    print("# export GITHUB_TOKEN='<PAT>' # bash\n")
    print(
        f'curl -X POST '
        f'-H "Authorization: Bearer $env:GITHUB_TOKEN" '
        f'-H "Accept: application/vnd.github+json" '
        f'https://api.github.com/repos/{REPO}/releases '
        f"--data-binary @{tmp} --fail-with-body -o response.json"
    )
    print(f"\n# body лежит во временном файле: {tmp}")


def _run_gh() -> int:
    if not shutil.which("gh"):
        print("gh CLI не найден в PATH.", file=sys.stderr)
        return 2
    import tempfile

    payload_file = (
        Path(tempfile.gettempdir()) / f"release_v254_{TAG.replace('.', '_')}_body.json"
    )
    payload_file.write_text(
        json.dumps(_payload(), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    try:
        cmd = [
            "gh", "release", "create", TAG,
            "--repo", REPO,
            "--title", TITLE,
            "--notes-file", str(payload_file),
        ]
        print("$ " + " ".join(cmd), file=sys.stderr)
        return subprocess.call(cmd)
    finally:
        payload_file.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="печатает полный JSON payload в stdout, ничего не создаёт")
    parser.add_argument("--curl", action="store_true",
                        help="печатает готовую curl-команду (нужен $GITHUB_TOKEN)")
    parser.add_argument("--run", action="store_true",
                        help="реальный gh release create (по умолчанию — dry-run)")
    args = parser.parse_args()

    # Дефолт — dry-run (безопасный). Явный --run нужен для боевого запуска.
    if args.run:
        return _run_gh()
    if args.curl:
        _print_curl()
        return 0
    # По умолчанию и при --dry-run — payload в stdout.
    _print_payload()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
