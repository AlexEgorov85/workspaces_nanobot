import csv
import io
import json
import re
import uuid
from pathlib import Path
from datetime import datetime, timezone
UTC = timezone.utc
from typing import Optional

# Characters invalid in directory names across platforms (Windows, macOS, Linux).
# On Windows: \ / : * ? " < > |
# On Linux:  / (null byte handled separately)
# We treat the full Windows set as reserved for portability.
_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|]+')


def safe_session_key(key: str) -> str:
    """Replace characters unsafe for directory names with ``_``."""
    return _INVALID_FS_CHARS.sub("_", key)


def _csv_val(v):
    return "" if v is None else str(v)


def prepare_content(content: str) -> tuple[str, str]:
    """Normalize tool result content and choose file extension.

    Returns ``(content, ext)`` where ``ext`` is ``.json``, ``.csv``, or ``.txt``.
    JSON-like content is pretty-printed and optionally converted to CSV
    if it has a tabular structure (list-of-dicts or dict with rows/columns).
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
    def __init__(self, base_dir: Path):
        cache = base_dir / "cache"
        self.base = cache / "sessions"
        self.base.mkdir(parents=True, exist_ok=True)
        self.archive_dir = cache / "archive"
        self.archive_dir.mkdir(exist_ok=True)

    def _get_session_dir(self, session_key: str) -> Path:
        sdir = self.base / safe_session_key(session_key)
        sdir.mkdir(exist_ok=True)
        (sdir / "results").mkdir(exist_ok=True)
        return sdir

    def _ensure_metadata(self, session_key: str) -> None:
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
        self._ensure_metadata(session_key)
        sdir = self._get_session_dir(session_key)

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        entry_id = uuid.uuid4().hex[:8]
        filename = f"{ts}_{source_tool}_{entry_id}{ext}"
        filepath = sdir / "results" / filename

        filepath.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8"))

        idx_path = sdir / "INDEX.json"
        idx = []
        if idx_path.exists():
            try: idx = json.loads(idx_path.read_text())
            except: idx = []
        idx.append({
            "id": entry_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "source_tool": source_tool,
            "size_bytes": size,
            "format": ext.lstrip("."),
            "file_path": f"results/{filename}",
            "preview": content[:250]
        })
        idx_path.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")

        meta_path = sdir / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["last_activity"] = datetime.now(UTC).isoformat()
        meta["file_count"] = len(idx)
        meta["total_bytes"] += size
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return {
            "session_key": session_key,
            "id": entry_id,
            "path": f"cache/sessions/{safe_session_key(session_key)}/results/{filename}",
            "size_kb": round(size / 1024, 2),
            "format": ext.lstrip(".")
        }

    def archive_session(self, session_key: str) -> bool:
        src = self.base / safe_session_key(session_key)
        dst = self.archive_dir / f"{safe_session_key(session_key)}_{datetime.now(UTC).strftime('%Y%m%d')}"
        if src.exists() and not dst.exists():
            import shutil
            shutil.move(str(src), str(dst))
            return True
        return False
