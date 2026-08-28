from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workspace.utils.office_files import extract_structure

SUPPORTED = {"docx", "pdf", "pptx", "xlsx", "xls", "csv", "txt"}


def _iter_files(target: Path):
    if target.is_dir():
        for f in target.rglob("*"):
            if f.is_file() and f.suffix.lower().lstrip(".") in SUPPORTED:
                yield f
    else:
        yield target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Извлечь структуру офисных файлов (название/начало/окончание) без LLM в JSON.",
    )
    ap.add_argument("path", help="файл или каталог с офисными файлами")
    ap.add_argument("-o", "--out", help="куда записать JSON (по умолчанию stdout)")
    ap.add_argument("--begin", type=int, default=800, help="символов в начале")
    ap.add_argument("--end", type=int, default=800, help="символов в окончании")
    ap.add_argument("--no-text", action="store_true", help="не включать полный текст")
    args = ap.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        print(f"Файл/каталог не найден: {root}", file=sys.stderr)
        return 2

    results = []
    for f in _iter_files(root):
        try:
            rec = extract_structure(
                f,
                begin_chars=args.begin,
                end_chars=args.end,
                include_text=not args.no_text,
            )
            rec["source"] = str(f)
        except Exception as e:
            rec = {"source": str(f), "error": str(e)}
        results.append(rec)

    payload = json.dumps(results, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"Записано {len(results)} записей в {args.out}", file=sys.stderr)
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
