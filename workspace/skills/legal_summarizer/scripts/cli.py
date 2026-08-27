"""Точка входа: CLI с разбором аргументов и запуском суммаризации.

Примеры запуска:
    # Краткое саммари
    legal_summarize --file contract.pdf --length brief

    # Среднее (по умолчанию)
    legal_summarize --file contract.docx

    # Развёрнутое
    legal_summarize --file contract.txt --length detailed

    # С контекстом чата (история для LLM)
    legal_summarize --file contract.pdf --length medium \\
        --context '[{"role":"user","content":"сфокусируйся на сроках"}]'
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
        description="legal_summarizer — суммаризация юридических документов",
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
        "--context",
        default=None,
        type=json.loads,
        help='Контекст чата в формате JSON. Например: '
             '\'[{"role":"user","content":"сфокусируйся на рисках"}\']',
    )
    parser.add_argument(
        "--max-chunks",
        default=50,
        type=int,
        help="Жёсткий лимит чанков для map-reduce. По умолчанию 50 - "
             "покрывает большинство документов (ГК РФ = ~20 чанков, "
             "обычные договоры = 1-3 чанка). Если документ больше, "
             "skill вернёт структурированную ошибку с понятным сообщением. "
             "Для очень больших (>50 чанков) используйте --batch-size N "
             "для streaming-обработки.",
    )
    parser.add_argument(
        "--batch-size",
        default=None,
        type=int,
        help="Размер батча для streaming-режима: обработать за один "
             "вызов только указанное число чанков, вернуть partial-саммари "
             "со ссылкой на следующий batch_index. По умолчанию (None) "
             "- обычный режим без streaming.",
    )
    parser.add_argument(
        "--batch-index",
        default=0,
        type=int,
        help="С какого батча начать (0 = сначала). Используется вместе "
             "с --batch-size для resume: каждый следующий запуск skill "
             "получает partial_summary предыдущего батча в --context.",
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
        from skill_config import get_default_length
        from summarizer import load_text, summarize, summarize_batch

        length = args.length or get_default_length()
        text = load_text(Path(args.file))

        if args.batch_size is not None:
            result = summarize_batch(
                text,
                length=length,
                context=args.context,
                batch_size=args.batch_size,
                batch_index=args.batch_index,
            )
            _emit(_sanitize_value(result))
            return

        result = summarize(
            text,
            length=length,
            context=args.context,
            max_chunks=args.max_chunks,
        )
        out = prepare_output(result)
        _emit(_sanitize_value(out))
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


def _sanitize_value(obj):
    """Lazy import — избегаем подтягивания pydantic-валидации до старта CLI."""
    from output import _sanitize_value as _sv  # noqa: WPS433

    return _sv(obj)


if __name__ == "__main__":
    main()
