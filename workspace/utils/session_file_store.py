import csv
import io
import json
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
UTC = timezone.utc
from typing import Optional

"""Модуль для хранения сессий в файловой системе.

SessionFileStore управляет сохранением, архивацией и очисткой
файлов результатов сессий инструментов.
"""

# Characters invalid in directory names across platforms (Windows, macOS, Linux).
# On Windows: \ / : * ? " < > |
# On Linux:  / (null byte handled separately)
# We treat the full Windows set as reserved for portability.
_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|]+')


def safe_session_key(key: str) -> str:
    """Заменяет символы, небезопасные для имён директорий, на ``_``."""
    return _INVALID_FS_CHARS.sub("_", key)


def _csv_val(v):
    """Возвращает пустую строку для None, иначе строковое представление значения."""
    return "" if v is None else str(v)


def prepare_content(content: str) -> tuple[str, str]:
    """Нормализует содержимое результата инструмента и выбирает расширение файла.

    Возвращает ``(content, ext)``, где ``ext`` — ``.json``, ``.csv`` или ``.txt``.
    JSON-подобное содержимое форматируется с отступами и опционально
    преобразуется в CSV, если имеет табличную структуру (список словарей
    или словарь со строками/колонками).
    """
    stripped = content.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return content, ".txt"
        csv_str = _try_convert_to_csv(data)
        if csv_str is not None:
            return csv_str, ".csv"
        return json.dumps(data, ensure_ascii=False, indent=2), ".json"
    return content, ".txt"


def _try_convert_to_csv(data) -> Optional[str]:
    """Пытается преобразовать данные (list/dict) в CSV с BOM.

    Проверяет несколько распространённых структур: список словарей,
    словарь с ключами results/rows+columns/data.
    Возвращает строку CSV или None, если данные не табличные.
    """
    rows = None
    columns = None

    if isinstance(data, dict):
        if "results" in data and isinstance(data["results"], list) and data["results"] and isinstance(data["results"][0], dict):
            rows = data["results"]
            columns = list(data["results"][0].keys())
        elif "rows" in data and "columns" in data and isinstance(data["rows"], list):
            rows = data["rows"]
            columns = data["columns"]
        elif "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            if "rows" in inner and "columns" in inner and isinstance(inner["rows"], list):
                rows = inner["rows"]
                columns = inner["columns"]
            elif "results" in inner and isinstance(inner["results"], list) and inner["results"] and isinstance(inner["results"][0], dict):
                rows = inner["results"]
                columns = list(inner["results"][0].keys())
    elif isinstance(data, list) and data and isinstance(data[0], dict):
        rows = data
        columns = list(data[0].keys())

    if rows is None or columns is None or not rows:
        return None

    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in rows:
        if isinstance(row, dict):
            writer.writerow([_csv_val(row.get(col)) for col in columns])
        elif isinstance(row, (list, tuple)):
            writer.writerow([_csv_val(v) for v in row])
    return output.getvalue()


class SessionFileStore:
    def __init__(self, base_dir: Path, max_files: int = 0, max_age_hours: int = 0):
        """Инициализирует хранилище сессий.

        Аргументы:
            base_dir: Базовая директория (внутри неё создаются cache/sessions и cache/archive).
            max_files: Максимальное количество файлов на сессию (0 — без ограничения).
            max_age_hours: Максимальный возраст файлов в часах (0 — без ограничения).
        """
        cache = base_dir / "cache"
        self.base = cache / "sessions"
        self.base.mkdir(parents=True, exist_ok=True)
        self.archive_dir = cache / "archive"
        self.archive_dir.mkdir(exist_ok=True)
        self.max_files = max_files
        self.max_age_hours = max_age_hours

    def _get_session_dir(self, session_key: str) -> Path:
        """Возвращает директорию сессии, создавая её при необходимости."""
        sdir = self.base / safe_session_key(session_key)
        sdir.mkdir(exist_ok=True)
        (sdir / "results").mkdir(exist_ok=True)
        return sdir

    def _ensure_metadata(self, session_key: str) -> None:
        """Создаёт metadata.json для сессии, если его ещё нет."""
        sdir = self._get_session_dir(session_key)
        meta_path = sdir / "metadata.json"
        if not meta_path.exists():
            meta_path.write_text(json.dumps({
                "session_key": session_key,
                "created_at": datetime.now(UTC).isoformat(),
                "last_activity": datetime.now(UTC).isoformat(),
                "status": "active",
                "file_count": 0,
                "total_bytes": 0
            }, indent=2), encoding="utf-8")

    def save(self, session_key: str, content: str, source_tool: str, ext: str = ".json") -> dict:
        """Сохраняет содержимое как файл результата в сессии.

        Аргументы:
            session_key: Ключ сессии.
            content: Содержимое файла.
            source_tool: Имя инструмента-источника.
            ext: Расширение файла (по умолчанию .json).

        Возвращает словарь с информацией о сохранённом файле
        (ключ сессии, id, путь, размер, формат).
        """
        self._ensure_metadata(session_key)
        sdir = self._get_session_dir(session_key)

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        entry_id = uuid.uuid4().hex[:8]
        filename = f"{ts}_{source_tool}_{entry_id}{ext}"
        filepath = sdir / "results" / filename

        filepath.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8"))

        meta_path = sdir / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["last_activity"] = datetime.now(UTC).isoformat()
        meta["file_count"] = meta.get("file_count", 0) + 1
        meta["total_bytes"] += size
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        self.cleanup(session_key)

        return {
            "session_key": session_key,
            "id": entry_id,
            "path": f"cache/sessions/{safe_session_key(session_key)}/results/{filename}",
            "size_kb": round(size / 1024, 2),
            "format": ext.lstrip(".")
        }

    def cleanup(self, session_key: str) -> None:
        """Удаляет устаревшие файлы результатов согласно лимитам max_files / max_age_hours."""
        max_files = self.max_files
        max_age_hours = self.max_age_hours
        if max_files <= 0 and max_age_hours <= 0:
            return

        sdir = self._get_session_dir(session_key)
        results_dir = sdir / "results"
        if not results_dir.exists():
            return

        now = datetime.now(UTC)
        removed = 0

        # Remove by age
        if max_age_hours > 0:
            cutoff = now.timestamp() - max_age_hours * 3600
            for f in sorted(results_dir.iterdir(), key=lambda p: p.name):
                try:
                    ts_str = f.stem[:15]
                    file_ts = datetime.strptime(ts_str, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
                    if file_ts.timestamp() < cutoff:
                        f.unlink()
                        removed += 1
                    else:
                        break
                except (ValueError, IndexError, OSError):
                    continue

        # Remove by count (after age cleanup, so fewer to scan)
        if max_files > 0:
            files = sorted(results_dir.iterdir(), key=lambda p: p.name)
            if len(files) > max_files:
                for f in files[:len(files) - max_files]:
                    try:
                        f.unlink()
                        removed += 1
                    except OSError:
                        pass

        if removed > 0:
            meta_path = sdir / "metadata.json"
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text())
                    meta["last_activity"] = now.isoformat()
                    old_count = meta.get("file_count", 0)
                    meta["file_count"] = max(0, old_count - removed)
                    remaining_bytes = sum(
                        f.stat().st_size for f in results_dir.iterdir() if f.is_file()
                    )
                    meta["total_bytes"] = remaining_bytes
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                except (OSError, json.JSONDecodeError):
                    pass

    def archive_session(self, session_key: str) -> bool:
        """Перемещает директорию сессии в архив.

        Возвращает True, если архивация выполнена, иначе False.
        """
        src = self.base / safe_session_key(session_key)
        dst = self.archive_dir / f"{safe_session_key(session_key)}_{datetime.now(UTC).strftime('%Y%m%d')}"
        if src.exists() and not dst.exists():
            import shutil
            shutil.move(str(src), str(dst))
            return True
        return False
