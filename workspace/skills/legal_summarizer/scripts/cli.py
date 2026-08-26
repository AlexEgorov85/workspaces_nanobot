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
             '\'[{"role":"user","content":"сфокусируйся на рисках"}]\'',
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


def main() -> None:
    try:
        parser = _build_parser()
        args = parser.parse_args()

        from output import prepare_output
        from skill_config import get_default_length
        from summarizer import load_text, summarize

        length = args.length or get_default_length()
        text = load_text(Path(args.file))
        result = summarize(text, length=length, context=args.context)
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
