"""Точка входа: CLI с разбором аргументов и запуском суммаризации.

Phase 2B API: ``summarizer.run(text, ...)`` возвращает dict со статусом
``completed`` / ``confirmation_required`` / ``requires_continuation`` /
``failed``. CLI делает estimate первым проходом (без LLM-вызовов),
показывает его пользователю и либо сразу выполняет (если короткая
операция), либо запрашивает `--confirm` (как в ARCHITECTURE.md
invariant #16).

Примеры запуска::

    # Оценка + исполнение короткого документа
    legal_summarize --file contract.pdf --length medium

    # Длинный документ: estimate + явное подтверждение
    legal_summarize --file gkodeksrf.pdf --length medium --confirm

    # С фокусом
    legal_summarize --file contract.pdf --focus "сроки и штрафы"

    # С контекстом чата (история для LLM)
    legal_summarize --file contract.pdf --length medium \\
        --context '[{"role":"user","content":"сфокусируйся на рисках"}]'
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="legal_summarizer — суммаризация юридических документов (Phase 2B)",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Путь к документу (.pdf, .docx, .txt, …) — см. office_files.detect_format",
    )
    parser.add_argument(
        "--length",
        default=None,
        choices=["brief", "medium", "detailed"],
        help="Объём саммари: brief (150–250 слов), medium (400–600, "
             "по умолч.), detailed (800–1200). По умолчанию — из project.json.",
    )
    parser.add_argument(
        "--focus",
        default=None,
        help="Тема, на которой фокусироваться при суммаризации (например, "
             "\"сроки и штрафы\"). Передаётся в LLM как instruction.",
    )
    parser.add_argument(
        "--context",
        default=None,
        type=json.loads,
        help='Контекст чата в формате JSON. Например: '
             '\'[{"role":"user","content":"сфокусируйся на рисках"}]\'',
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Подтвердить выполнение длинной операции (по умолчанию для "
             "коротких — авто). Без флага при confirmation_required CLI "
             "выводит estimate и завершает работу.",
    )
    parser.add_argument(
        "--max-chunks",
        default=None,
        type=int,
        help="Override ``max_chunks_for_execution`` из конфига. По "
             "умолчанию берётся из project.json::skills.legal_summarizer."
             "execution.max_chunks_for_execution.",
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="Только оценить документ (без LLM-вызовов). Печатает "
             "Inspection (chunks, batches, sections) + Estimate "
             "(duration, llm_calls). Полезно для предварительной проверки.",
    )
    parser.add_argument(
        "--operation-id",
        default=None,
        help="Передать явный operation_id (иначе — автогенерация из текста).",
    )
    return parser


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _error(status_message: str, exc: Exception | None = None) -> dict:
    out: dict = {
        "mode": "summarize",
        "status": "error",
        "message": status_message,
    }
    if exc is not None and not isinstance(exc, (FileNotFoundError, ValueError, argparse.ArgumentTypeError)):
        out["traceback"] = traceback.format_exc()
    return out


def _ensure_registered() -> None:
    """Зарегистрировать legal_summarizer и runtime-инфраструктуру в ``table_registry``.

    Standalone CLI не имеет ``ApplicationContext`` (его поднимает gateway),
    поэтому skill сам себя регистрирует из ``project.json::skills.legal_summarizer``
    через ``lib.core.skill_registration.register_skill_from_config``. Идемпотентно:
    если skill уже зарегистрирован (например, gateway-populated реестр),
    повторная регистрация игнорируется.

    Для skill'а без vector-инфраструктуры и без PG-таблиц вызовы
    ``register_vector_storage`` и ``register_embedding_config`` будут no-op,
    но вызываются для единообразия с полными skill'ами (audit_analyzer).
    """
    from lib.core.infra_registration import register_vector_storage
    from lib.core.skill_registration import (
        register_embedding_config,
        register_skill_from_config,
    )
    from config import SETTINGS

    legal_cfg = SETTINGS.get("skills", {}).get("legal_summarizer", {})
    register_skill_from_config("legal_summarizer", legal_cfg)
    register_vector_storage()
    register_embedding_config()


def main() -> None:
    try:
        _ensure_registered()
        parser = _build_parser()
        args = parser.parse_args()

        from output import prepare_output
        from summarizer import (
            estimate as _estimate,
            inspect as _inspect,
            load_text,
            needs_confirmation,
            run,
        )
        from skill_config import get_default_length

        length = args.length or get_default_length()
        text = load_text(Path(args.file))

        if args.estimate_only:
            insp = _inspect(text, document_path=str(args.file))
            est = _estimate(insp)
            _emit({
                "mode": "estimate_only",
                "status": "ok",
                "chars_in": len(text),
                "chunks_total": len(insp.chunks),
                "context_batches_total": len(insp.context_batches),
                "sections_total": len(insp.tree.sections) if insp.tree else 0,
                "estimated_llm_calls": insp.estimated_llm_calls,
                "strategy": insp.strategy,
                "estimated_duration_min_sec": est.estimated_duration_min_sec,
                "estimated_duration_max_sec": est.estimated_duration_max_sec,
                "confirmation_threshold_sec": est.confirmation_threshold_sec,
                "needs_confirmation": needs_confirmation(est),
            })
            return

        kwargs: dict = {
            "length": length,
            "focus": args.focus,
            "operation_id": args.operation_id,
            "document_path": str(args.file),
            # Корень РЕПО (не workspace dir) — стабильный абсолютный путь,
            # выведенный из __file__. Раньше передавали None → скилл брал
            # относительный путь и при cwd=<workspace> создавал дубль
            # workspace/workspace/data_store/... (см. инцидент 2026-08-28).
            "workspace_root": Path(__file__).resolve().parents[4],
        }

        if not args.confirm:
            insp = _inspect(text, document_path=str(args.file))
            est = _estimate(insp)
            if needs_confirmation(est):
                _emit({
                    "mode": "summarize",
                    "status": "confirmation_required",
                    "estimate": {
                        "chars_in": len(text),
                        "chunks_total": len(insp.chunks),
                        "context_batches_total": len(insp.context_batches),
                        "estimated_llm_calls": insp.estimated_llm_calls,
                        "estimated_duration_min_sec": est.estimated_duration_min_sec,
                        "estimated_duration_max_sec": est.estimated_duration_max_sec,
                        "confirmation_threshold_sec": est.confirmation_threshold_sec,
                    },
                    "hint": (
                        f"Документ требует примерно {est.estimated_duration_min_sec:g}–"
                        f"{est.estimated_duration_max_sec:g} сек и "
                        f"{insp.estimated_llm_calls} вызовов LLM. "
                        "Перезапустите с --confirm для выполнения."
                    ),
                })
                return

        result = run(text, confirmed=args.confirm, **kwargs)
        out = prepare_output(result)
        _emit(out)
    except argparse.ArgumentTypeError as e:
        _emit(_error(str(e)))
        sys.exit(1)
    except FileNotFoundError as e:
        _emit(_error(str(e)))
        sys.exit(1)
    except ValueError as e:
        _emit(_error(str(e)))
        sys.exit(1)
    except Exception as e:
        _emit(_error(f"Внутренняя ошибка: {e}", e))
        sys.exit(1)


if __name__ == "__main__":
    main()