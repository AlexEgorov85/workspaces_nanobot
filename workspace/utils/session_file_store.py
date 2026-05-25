import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional

class SessionFileStore:
    def __init__(self, base_dir: Path):
        cache = base_dir / "cache"
        self.base = cache / "sessions"
        self.base.mkdir(parents=True, exist_ok=True)
        self.archive_dir = cache / "archive"
        self.archive_dir.mkdir(exist_ok=True)

    def _get_session_dir(self, session_key: str) -> Path:
        sdir = self.base / session_key
        sdir.mkdir(exist_ok=True)
        (sdir / "results").mkdir(exist_ok=True)
        return sdir

    def _ensure_metadata(self, session_key: str) -> None:
        meta_path = self.base / session_key / "metadata.json"
        if not meta_path.exists():
            meta_path.write_text(json.dumps({
                "session_key": session_key,
                "created_at": datetime.utcnow().isoformat(),
                "last_activity": datetime.utcnow().isoformat(),
                "status": "active",
                "file_count": 0,
                "total_bytes": 0
            }, indent=2), encoding="utf-8")

    def save(self, session_key: str, content: str, source_tool: str, ext: str = ".json") -> dict:
        self._ensure_metadata(session_key)
        sdir = self._get_session_dir(session_key)

        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
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
            "timestamp": datetime.utcnow().isoformat(),
            "source_tool": source_tool,
            "size_bytes": size,
            "format": ext.lstrip("."),
            "file_path": f"results/{filename}",
            "preview": content[:250]
        })
        idx_path.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")

        meta_path = sdir / "metadata.json"
        meta = json.loads(meta_path.read_text())
        meta["last_activity"] = datetime.utcnow().isoformat()
        meta["file_count"] = len(idx)
        meta["total_bytes"] += size
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return {
            "session_key": session_key,
            "id": entry_id,
            "path": f"cache/sessions/{session_key}/results/{filename}",
            "size_kb": round(size / 1024, 2),
            "format": ext.lstrip(".")
        }

    def archive_session(self, session_key: str) -> bool:
        src = self.base / session_key
        dst = self.archive_dir / f"{session_key}_{datetime.utcnow().strftime('%Y%m%d')}"
        if src.exists() and not dst.exists():
            import shutil
            shutil.move(str(src), str(dst))
            return True
        return False