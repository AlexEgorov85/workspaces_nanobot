"""``cli_query.py`` — follow-up-запросы по сохранённой operation_id.

Нужен, чтобы агент мог отвечать на уточняющие вопросы по документу
("сколько статей?", "какие разделы?", "что в чанке 12?") **без
перепарсинга PDF** через ``exec``+pdfplumber. Читает manifest/result/chunks
навыка ``legal_summarizer``, уже лежащие в
``data_store/cache/skills/legal_summarizer/<operation_id>/``, и возвращает
JSON с нужным полем.

Чисто stdlib (``json``, ``pathlib``, ``argparse``) — кросс-платформенный
(Windows + Linux). Кодировка вывода UTF-8 (см. ``PYTHONIOENCODING`` на
entry-points gateway/cli_agent/streamlit_app).

Использование::

    python workspace/skills/legal_summarizer/scripts/cli_query.py \
        --operation-id <op_id> [--field stats|articles|chunks|sections|tree|all]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "legal_summarizer query — read manifest/result/chunks по "
            "operation_id без перепарсинга PDF."
        ),
    )
    parser.add_argument(
        "--operation-id",
        required=True,
        help="operation_id ранее выполненного summarize (поле result.operation_id).",
    )
    parser.add_argument(
        "--field",
        default="stats",
        choices=["stats", "articles", "chunks", "sections", "tree", "all"],
        help=(
            "Что вернуть: stats — ключевые метрики (включая article_count), "
            "articles — только число статей, chunks — список chunk_id + summary, "
            "sections — список section_path, tree — иерархия sections, "
            "all — весь manifest.json."
        ),
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help=(
            "Корень репозитория. По умолчанию — выводится из расположения "
            "скрипта (кросс-платформенно)."
        ),
    )
    parser.add_argument(
        "--max-chunk-summary-chars",
        type=int,
        default=1500,
        help="Обрезка текста summary чанка для поля --field chunks (default 1500).",
    )
    return parser


def _emit(payload: dict) -> None:
    """Печатает JSON в UTF-8 (не sentinel — это короткий запрос)."""
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _resolve_workspace_root(arg: str | None) -> Path:
    """Кросс-платформенный путь к корню репо."""
    if arg:
        return Path(arg).resolve()
    # Стабильный якорь: <repo>/workspace/skills/legal_summarizer/scripts/cli_query.py
    return Path(__file__).resolve().parents[4]


def _load_manifest_or_none(operation_id: str, workspace_root: Path) -> dict[str, Any] | None:
    """Прочитать manifest.json. None если файла нет."""
    from manifest import _read_json, manifest_path  # type: ignore

    return _read_json(manifest_path(operation_id, workspace_root))


def _load_chunk_summaries(
    operation_id: str,
    workspace_root: Path,
    *,
    max_summary_chars: int,
) -> list[dict[str, Any]]:
    """Прочитать per-chunk файлы; обрезать summary до ``max_summary_chars``."""
    from manifest import chunks_dir  # type: ignore

    out: list[dict[str, Any]] = []
    cd = chunks_dir(operation_id, workspace_root)
    if not cd.is_dir():
        return out
    for path in sorted(cd.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        summary = data.get("summary") or ""
        if len(summary) > max_summary_chars:
            summary = summary[:max_summary_chars] + "…"
        out.append({
            "chunk_id": data.get("chunk_id") or path.stem,
            "section_id": data.get("section_id"),
            "section_path": data.get("section_path"),
            "page_start": data.get("page_start"),
            "page_end": data.get("page_end"),
            "summary": summary,
        })
    return out


def _build_sections_tree(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Плоский список секций с section_path + heading."""
    sections = manifest.get("sections") or {}
    out: list[dict[str, Any]] = []
    for sid, sec in sections.items():
        if not isinstance(sec, dict):
            continue
        out.append({
            "section_id": sid,
            "section_path": sec.get("section_path"),
            "heading": sec.get("heading"),
            "block_count": sec.get("block_count"),
        })
    out.sort(key=lambda x: str(x.get("section_path") or ""))
    return out


def _field_stats(manifest: dict[str, Any]) -> dict[str, Any]:
    """Основные метрики для follow-up'ов (включая article_count).

    Возвращаемые ключи НЕ пересекаются с ``status``/``field`` на верхнем
    уровне payload'а (там — статус самого query, ``"ok"|"error"``), чтобы
    ``{**stats, "status": "ok"}`` не затёрло статус операции.
    """
    return {
        "operation_id": manifest.get("operation_id"),
        "operation_status": manifest.get("status"),
        "chars_in": manifest.get("chars_in"),
        "chunks_total": manifest.get("chunks_total"),
        "context_batches_total": manifest.get("context_batches_total"),
        "sections_total": sum(
            1 for k in (manifest.get("sections") or {}) if k != "ROOT"
        ),
        "batches_done": manifest.get("batches_done"),
        "batches_failed": manifest.get("batches_failed"),
        "article_count": manifest.get("article_count"),
        "duration_sec": manifest.get("duration_sec"),
        "started_at": manifest.get("started_at"),
        "completed_at": manifest.get("completed_at"),
    }


def main() -> int:
    # Кросс-платформенная UTF-8 для собственного stdout/stderr argparse —
    # на Windows гарантирует кириллицу без кракозябр в --help/ошибках.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    args = _build_parser().parse_args()
    workspace_root = _resolve_workspace_root(args.workspace_root)
    manifest = _load_manifest_or_none(args.operation_id, workspace_root)
    if manifest is None:
        _emit({
            "status": "error",
            "error_type": "manifest_not_found",
            "operation_id": args.operation_id,
            "workspace_root": str(workspace_root),
            "message": (
                f"manifest.json для operation_id={args.operation_id!r} не найден "
                f"в {workspace_root / 'workspace' / 'data_store' / 'cache' / 'skills' / 'legal_summarizer' / args.operation_id}. "
                "Возможно, прогон был удалён или operation_id указан неверно."
            ),
        })
        return 1

    field = args.field
    payload: dict[str, Any]
    if field == "stats":
        payload = {"status": "ok", "field": field, **_field_stats(manifest)}
    elif field == "articles":
        payload = {
            "status": "ok",
            "field": field,
            "operation_id": manifest.get("operation_id"),
            "article_count": manifest.get("article_count"),
        }
    elif field == "sections":
        payload = {
            "status": "ok",
            "field": field,
            "operation_id": manifest.get("operation_id"),
            "sections": _build_sections_tree(manifest),
        }
    elif field == "tree":
        # Псевдо-дерево: родитель → дети, по section_path.
        tree = manifest.get("sections") or {}
        nodes: list[dict[str, Any]] = []
        for sid, sec in tree.items():
            if not isinstance(sec, dict):
                continue
            nodes.append({
                "section_id": sid,
                "section_path": sec.get("section_path"),
                "heading": sec.get("heading"),
            })
        payload = {
            "status": "ok",
            "field": field,
            "operation_id": manifest.get("operation_id"),
            "sections": sorted(nodes, key=lambda x: str(x.get("section_path") or "")),
        }
    elif field == "chunks":
        chunks = _load_chunk_summaries(
            args.operation_id,
            workspace_root,
            max_summary_chars=args.max_chunk_summary_chars,
        )
        payload = {
            "status": "ok",
            "field": field,
            "operation_id": manifest.get("operation_id"),
            "chunk_count": len(chunks),
            "chunks": chunks,
        }
    else:  # "all"
        payload = {"status": "ok", "field": field, "manifest": manifest}

    _emit(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
