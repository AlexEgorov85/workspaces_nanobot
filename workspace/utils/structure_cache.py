from __future__ import annotations

import hashlib
import json
from pathlib import Path

from workspace.utils.office_files import extract_structure

_CACHE_DIR = Path("workspace/data_store/cache/structure")


def _key(path: Path) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{st.st_mtime}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def get_structure(
    path: str | Path,
    *,
    begin_chars: int = 800,
    end_chars: int = 800,
    include_text: bool = True,
) -> dict:
    """Получить структуру документа с кэшированием на диске.

    Повторные вызовы для того же файла (path+size+mtime не изменились)
    не перепарсивают документ, а читают готовый JSON из
    ``workspace/data_store/cache/structure/<key>.json``. Это делает
    извлечение ``title``/``begin``/``end``/``text`` дешёвым при
    многократном обращении (например, перезапуск ``legal_summarizer``
    по тому же файлу, streaming-батчи).

    Возвращает то же, что :func:`office_files.extract_structure`.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Файл не найден: {p}")
    key = _key(p)
    cache_file = _CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("size_bytes") == p.stat().st_size:
                return cached
        except Exception:
            pass
    struct = extract_structure(
        p,
        begin_chars=begin_chars,
        end_chars=end_chars,
        include_text=include_text,
    )
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(struct, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass
    return struct
