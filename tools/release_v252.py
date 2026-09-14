#!/usr/bin/env python3
"""Создать GitHub Release для тега v2.5.2 через gh CLI (fallback: curl).

Режимы:
  --dry-run   — печатает полный payload (tag/title/body) в stdout, ничего
                не создаёт и не пишет на диск. По умолчанию.
  --curl      — печатает готовую curl-команду для ручного запуска
                (нужно подставить $GITHUB_TOKEN).
  без флагов  — вызывает `gh release create`, читая payload из временного
                файла, который удаляется сразу после команды.

Usage:
  python tools/release_v252.py            # эквивалент --dry-run
  python tools/release_v252.py --dry-run  # печать payload
  python tools/release_v252.py --curl     # печать curl-команды
  python tools/release_v252.py --run      # реальный gh release create

Замечание: при --run / без флагов сначала пишется временный payload.json,
вызывается gh, после — файл удаляется в `finally`. Это поведение
унаследовано от release_v251.py.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "AlexEgorov85/workspaces_nanobot"
TAG = "v2.5.2"
TITLE = "v2.5.2"

CHANGELOG_BLOCK_HEADER = "## [2.5.2] — 2026-09-14"
NEXT_BLOCK_HEADER = "## [2.5.1] — 2026-09-13"

BODY = """**PATCH-релиз v2.5.2:** две группы доработок поверх v2.5.1 — (1) **NFS-совместимость runtime-кеша** (DuckDB ATTACH flock не работает на NFS — серия из 4 коммитов + 1 feat + safe default); (2) **наблюдаемость sync-путей** PG→DuckDB (единый конвейер sync-событий через `emit_sync_event`/`DbLoggingService`, видимость ошибок `preload` и channel-циклов в `agent_gateway_logs`).

## Fixed — NFS / DuckDB cache

- **`DuckDbCacheStore.publish()`** больше не падает молча на stale `.tmp` (`605660b`): `tmp.unlink()` возвращает `False` с `sync_publish_failed` событием вместо `except OSError: pass`; имя `.tmp` уникальное на каждый вызов (`<name>.<pid>.<ms>.tmp`); ATTACH обёрнут в retry с экспоненциальным backoff (5 попыток: 0.1/0.2/0.4/0.8/1.6 с). Это правильная гигиена + читаемая диагностика; **корень NFS-несовместимости лечится safe default ниже**.
- **`gateway.py` startup cleanup** теперь удаляет `cache.duckdb` **и** `cache.duckdb.tmp` (`652b09d`) — раньше `.tmp` оставался залоченным через NFS `lockd` при крахе между `ATTACH` и `os.replace`, и следующий publish сразу отстреливал `PID 0`.
- **`preload_service.preload_vector_indexes`** — добавлен недостающий `import logging` + `logger = logging.getLogger(__name__)` (`48575e9`): `NameError: name 'logger' is not defined` ловил все ошибки `preload_indexes` в тестах и в реальном рантайме.
- **`resolve_publish_path()` — единый механизм** вычисления пути к `cache.duckdb` (`85cad2a`, `b1d2e21`): если `gateway.cache.local_path` не задан, снимок уходит в `~/.cache/nanobot/duckdb/cache.duckdb` (POSIX `fcntl` работает там штатно), а не в legacy `<workspace>/data_store/duckdb/` — который на NFS роняет каждый sync-цикл с непонятным traceback. Подтверждено эмпирически: перенос workspace с NFS на ext4 полностью устраняет проблему.
- **`build_cache_provider()` и `get_in_memory_cache_path()` тоже зовут `resolve_publish_path()`** (`b1d2e21`): до этого CLI/skill-слой хардкодил `table_registry.snapshot_path(workspace_root)`, и после safe-default фикса в gateway тот писал в одно место, а skill читал из другого — скилл видел устаревший/пустой снимок. v2.5.2+ оба слоя вызывают одну pure-функцию с одними `gateway.cache.*` настройками.
- **`_warn_if_publish_path_on_nfs(publish_path)`** — Linux-only проверка `/proc/mounts`: если снимок всё-таки попал на NFS (через symlink), печатает громкое WARNING в logging И в stderr с конкретными инструкциями. Защита от регрессии.

## Added — NFS-safe cache path

- **`gateway.cache.local_path`** (`c522b55`) — **единственный** опциональный knob: абсолютный или относительный (от workspace) путь к локальной ФС для снимка `cache.duckdb`. Override над safe default; полезно когда у `~/.cache` нет места или нужна отдельная ФС.

Никаких escape-hatch'ей и mode'ов совместимости не предусмотрено: один механизм (`resolve_publish_path`), один путь (`local_path` или default `~/.cache/`). Legacy `<workspace>/data_store/duckdb/` на NFS больше не поддерживается.

## Fixed — observability (sync/logging)

- **Единый конвейер sync-событий через `emit_sync_event`/`DbLoggingService`** (`a1811c5`): вместо ad-hoc `logger.warning` в каждом месте sync-пути — один централизованный путь с event_type/payload/level/structured-summary. Под `preload_sync_event` / `sync_publish_failed` / `sync_skipped_*` / `sync_initial_loaded` / `sync_worker_paused` теперь есть полный trail в `agent_gateway_logs`.
- **PG→DuckDB sync-путь пишет события в `agent_gateway_logs`** (`f58c957`): `initial_load`, `poll_cycle`, `claim`, `release`, `error`, `reconnect` — всё логируется через `DbLoggingService`, а не теряется в stdout.
- **`sync_registry_initial_load_publish` — расширенное логирование «тихих» путей** (`d4558f9`): раньше ошибки в `register_resources`, `initial_load`, `publish` оставались только в `logger.warning` и не попадали в долговечный `agent_gateway_logs`. Теперь все три — структурированные события.
- **Видимость ошибок `preload` векторов и каналов в `agent_gateway_logs`** (`9fb88c4`): `ToolAuditHook` и `TerminalToolPrintHook` пишут под `event_type="preload_failed"` / `"channel_error"` с `tool_call_id` и `duration_ms`. До фикса ошибки `preload_indexes` и lease-loop глохли в logger'е без event-trail.

## Tests

- `tests/test_duckdb_cache_store.py` — все 43 теста проходят (включая новые пути под `tmp.<pid>.<ms>.tmp`).
- `tests/test_preload_service.py::test_error_returns_none` — зелёный (раньше падал с `NameError`).
- `tests/test_application_context.py::TestResolvePublishPath` (6 кейсов).
- `tests/test_application_context.py::TestSingleMechanism` (1 кейс — критическая инвариантна: gateway и CLI `build_cache_provider` возвращают **один и тот же путь** с default-конфигом).
- `tests/test_application_context.py::TestWarnIfPublishPathOnNfs` (2 кейса).

---

Полный changelog по подсистемам — в [CHANGELOG.md → 2.5.2](CHANGELOG.md#252--2026-09-14)."""


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
    # Сохраняем payload в tmp-файл ОДИН раз — curl требует файл через -d @.
    import tempfile

    tmp = Path(tempfile.gettempdir()) / f"release_v252_{TAG.replace('.', '_')}_body.json"
    tmp.write_text(json.dumps(_payload(), ensure_ascii=False),
                   encoding="utf-8")
    try:
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
        print(f"# (будет удалён при следующем запуске --curl)")
    finally:
        # Не удаляем сразу — пользователь может захотеть посмотреть.
        # Удалим через 1 час, если файл ещё существует. Самый простой
        # способ — оставить: `tempfile.gettempdir()` чистится ОС.
        pass


def _run_gh() -> int:
    if not shutil.which("gh"):
        print("gh CLI не найден в PATH.", file=sys.stderr)
        return 2
    import tempfile

    payload_file = Path(tempfile.gettempdir()) / f"release_v252_{TAG.replace('.', '_')}_body.json"
    payload_file.write_text(json.dumps(_payload(), ensure_ascii=False, indent=2),
                           encoding="utf-8")
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
