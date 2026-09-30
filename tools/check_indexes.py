#!/usr/bin/env python3
"""``tools/check_indexes.py`` — declared-vs-runtime diff для vector-индексов.

Сравнивает:
  * ``project.json::gateway.vector.index.indexes.*`` — декларация
    (что должно быть построено);
  * runtime-состояние, которое видит ``search_vector`` — индексы,
    перечисленные ``list_runtime_vector_indexes`` из
    ``lib.services.cache_provider_impl`` по данным DuckDB-кэша
    ``gateway.vector.index.storage_table`` (что реально собрано).

Используется как точка входа для CI / pre-deploy / при ручной проверке.

Exit codes
----------
* ``0`` — нет расхождений;
* ``1`` — есть расхождение (declared-but-missing / orphan / stale /
  invalid signature);
* ``2`` — инфраструктурная ошибка (DuckDB-кэш недоступен, project.json не
  валиден и т.п.).

Зачем
----
Раньше ``--list-indexes`` в ``workspace/skills/audit_analyzer/scripts/cli.py``
читал **только** декларацию из JSON и возвращал «обещания» — было непонятно,
что реально доступно в runtime. CLI теперь показывает **runtime**
(``list_runtime_vector_indexes``); этот скрипт делает diff между
декларацией и runtime:

  * декларация (``project.json``) — desired state;
  * runtime (DuckDB-кэш ``storage_table``) — actual state;
  * этот скрипт — контроль расхождения.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_REPO_ROOT), str(_REPO_ROOT / "workspace")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from lib.services.cache_provider_impl import (  # noqa: E402
    list_runtime_vector_indexes,
    read_vector_index_config,
    verify_index_signature,
)

EXIT_OK = 0
EXIT_DIVERGENCE = 1
EXIT_INFRA_ERROR = 2


def _diff(
    declared: dict[str, Any] | Exception,
    runtime: list[dict[str, Any]] | Exception,
) -> dict[str, Any]:
    """Сравнить декларацию (JSON) и runtime (DuckDB-кэш). Возвращает структурированный diff.

    Возвращает dict с ключами:
      ``declared``: {name: {…из declared JSON…}}, ``source='json'`` или
        ``source='error', message=...`` при ошибке;
      ``runtime``: [{...поля list_runtime_vector_indexes()}], или
        ``source='error', message=...``;
      ``status``: ``OK`` / ``DIVERGENCE`` / ``INFRA_ERROR``;
      ``divergence``: dict c подробностями (см. ниже).
    """
    if isinstance(declared, Exception):
        return {
            "declared": {"source": "error", "error_type": "config_unavailable",
                          "message": str(declared)},
            "runtime": {"source": "error", "error_type": "config_unavailable",
                        "message": "skipped — declared side failed"},
            "status": "INFRA_ERROR",
            "divergence": {
                "error": str(declared),
            },
        }
    if isinstance(runtime, Exception):
        return {
            "declared": {"source": "json", "items": list(declared.keys())},
            "runtime": {"source": "error", "error_type": "store_unavailable",
                        "message": str(runtime)},
            "status": "INFRA_ERROR",
            "divergence": {
                "error": str(runtime),
            },
        }

    declared_map: dict[str, Any] = dict(declared)
    runtime_by_name: dict[str, dict[str, Any]] = {
        r["source"]: r for r in runtime if r.get("source")
    }

    missing_in_runtime: list[dict[str, Any]] = []  # declared but not built
    orphan_in_runtime: list[dict[str, Any]] = []  # built but not declared
    stale_or_invalid: list[dict[str, Any]] = []   # built and declared, but signature mismatch

    # declared - runtime
    for name, cfg in declared_map.items():
        row = runtime_by_name.get(name)
        if row is None:
            missing_in_runtime.append({
                "name": name,
                "source_table": cfg.get("source_table"),
                "embedding_columns": cfg.get("embedding_columns"),
                "chunk_size": cfg.get("chunk_size"),
            })
            continue
        status = verify_index_signature(
            row.get("metadata") or {}, cfg,
        )
        if status in ("STALE", "INVALID"):
            stored_sig = (row.get("metadata") or {}).get("signature")
            current_sig = None
            try:
                from lib.services.cache_provider_impl import compute_index_signature
                current_sig = compute_index_signature(cfg)
            except Exception:
                pass
            stale_or_invalid.append({
                "name": name,
                "status": status,
                "stored_signature": (stored_sig or "")[:16] or None,
                "current_signature": (current_sig or "")[:16] or None,
                "reason": (
                    "Index config changed (embedding model / dimension / chunk / "
                    "source cols); rebuild via ``tools/build_vectors.py``."
                )
                if status == "STALE"
                else "Stored metadata has no signature or it's corrupt; "
                     "index may be from a legacy build.",
            })

    # runtime - declared
    for name in sorted(runtime_by_name.keys()):
        if name not in declared_map:
            row = runtime_by_name[name]
            orphan_in_runtime.append({
                "name": name,
                "vectors": row.get("vector_count"),
                "dimension": row.get("dimension"),
                "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
                "metric": row.get("metric"),
            })

    status = (
        "DIVERGENCE"
        if (missing_in_runtime or orphan_in_runtime or stale_or_invalid)
        else "OK"
    )

    return {
        "declared": {"source": "json", "items": sorted(declared_map.keys())},
        "runtime": {"source": "duckdb_snapshot", "items": sorted(runtime_by_name.keys())},
        "status": status,
        "divergence": {
            "missing_in_runtime": missing_in_runtime,  # объявлен, но векторов нет → search ничего не вернёт
            "orphan_in_runtime": orphan_in_runtime,    # векторы есть, но в JSON не объявлены → мусор
            "stale_or_invalid": stale_or_invalid,      # векторы есть, но сигнатура не совпадает → search даст STALE/INVALID hits
        },
    }


def _load_declared() -> dict[str, Any] | Exception:
    try:
        return read_vector_index_config() or {}
    except Exception as exc:
        return exc


def _open_provider() -> Any:
    """Открыть DuckDB-кэш в read-only через единственную точку создания.

    Инструмент читает кэш, а не PG, и не создаёт собственного соединения:
    ``list_runtime_vector_indexes`` работает через интерфейс провайдера.
    """
    from lib.services.cache_provider import CacheAccessMode
    from lib.services.cache_provider import open_cache_provider

    return open_cache_provider(mode=CacheAccessMode.READ_ONLY)


def _load_runtime() -> list[dict[str, Any]] | Exception:
    try:
        provider = _open_provider()
    except Exception as exc:
        return exc
    try:
        return list_runtime_vector_indexes(provider=provider)
    except Exception as exc:
        return exc


def _format_text(result: dict[str, Any]) -> str:
    """Человеко-читаемый вывод для terminal/pre-commit."""
    lines: list[str] = []
    status = result["status"]
    if status == "INFRA_ERROR":
        lines.append(f"INFRA_ERROR: {result['divergence'].get('error')}")
        return "\n".join(lines)

    lines.append(f"=== Vector index discovery check ===")
    lines.append(f"status: {status}")
    lines.append("")
    decl = result["declared"]
    rt = result["runtime"]
    lines.append(f"DECLARED ({len(decl.get('items', []))}): {', '.join(decl.get('items', [])) or '-'}")
    lines.append(f"RUNTIME  ({len(rt.get('items', []))}): {', '.join(rt.get('items', [])) or '-'}")
    lines.append("")
    div = result["divergence"]
    if div.get("missing_in_runtime"):
        lines.append("MISSING (declared, but no vectors in storage snapshot — search will return nothing):")
        for item in div["missing_in_runtime"]:
            lines.append(f"  ! {item['name']}  source={item.get('source_table')}  chunk={item.get('chunk_size')}")
        lines.append("")
    if div.get("orphan_in_runtime"):
        lines.append("ORPHAN (vectors exist in storage snapshot, but not declared — dead data):")
        for item in div["orphan_in_runtime"]:
            lines.append(f"  ! {item['name']}  vectors={item.get('vectors')}  dim={item.get('dimension')}")
        lines.append("")
    if div.get("stale_or_invalid"):
        lines.append("STALE/INVALID (signature mismatch — search hits will be flagged):")
        for item in div["stale_or_invalid"]:
            lines.append(
                f"  ! {item['name']}  status={item['status']}  "
                f"stored={item['stored_signature']} current={item['current_signature']}"
            )
            if item.get("reason"):
                lines.append(f"    reason: {item['reason']}")
        lines.append("")

    if status == "OK":
        lines.append("All declared indexes have valid runtime artifacts.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="вывести структурированный JSON вместо человеко-читаемого текста",
    )
    args = parser.parse_args(argv)

    # Standalone-утилита: нет entrypoint, который бы опубликовал SETTINGS
    # (тот же паттерн, что в tools/build_vectors.py). Оверлей профиля `test`
    # меняет только имена таблиц каналов/логов — `workspace_path` и
    # `gateway.cache.local_path` для этого инструмента те же, что у prod.
    import config as _cfg

    if not _cfg.is_settings_initialized():
        _cfg._initialize_settings(profile="test")

    declared = _load_declared()
    runtime = _load_runtime()
    result = _diff(declared, runtime)

    if result["status"] == "INFRA_ERROR":
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(_format_text(result))
        return EXIT_INFRA_ERROR

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(_format_text(result))

    if result["status"] == "DIVERGENCE":
        return EXIT_DIVERGENCE
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
