"""Follow Up 2.0 — загрузка вложений (ответы профильников для скилла
«Контроль исполнения поручений»).

Файл конвертируется в текст сразу при загрузке; оригинал удаляется после
парсинга (K1-документ не должен задерживаться на диске DataLab). Текст
живёт в памяти процесса до конца сессии приложения.
"""
from __future__ import annotations

import hashlib
import logging
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

from backend.config import get_settings

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/files", tags=["files"])

# Реестр вложений: attachment_id → {text, filename, chars, created_at}
_attachments: Dict[str, Dict] = {}
_lock = threading.Lock()
_TTL_SEC = 4 * 3600  # вложение живёт 4 часа


def get_attachment(attachment_id: str) -> Optional[Dict]:
    with _lock:
        return _attachments.get(attachment_id)


def put_attachment(text: str, filename: str) -> str:
    """Положить готовый текст вложением и вернуть его идентификатор.

    Нужна навыку внешнего агента: письмо профильника туда приезжает файлом,
    который агент уже сохранил у себя, а не через HTTP-загрузку. Реестр
    вложений один на оба пути — иначе карточка из единого окна собиралась бы
    из другого источника, чем карточка из собственного интерфейса.
    """
    _cleanup_expired()
    text = (text or "").strip()
    if not text:
        raise ValueError("Пустой текст вложения")
    attachment_id = hashlib.md5(
        (filename + str(time.time())).encode() + text[:1024].encode()
    ).hexdigest()[:16]
    with _lock:
        _attachments[attachment_id] = {
            "text": text,
            "filename": filename,
            "chars": len(text),
            "created_at": time.time(),
        }
    return attachment_id


def extract_text_from_path(path: Path) -> str:
    """Текст из файла на диске — теми же парсерами, что и при загрузке."""
    ext = path.suffix.lower()
    if ext not in (".docx", ".txt", ".pdf"):
        raise ValueError(
            f"Формат {ext or '(без расширения)'} не поддержан — "
            f"нужен .docx, .txt или .pdf")
    if not path.is_file():
        raise ValueError(f"Файл не найден: {path}")
    if ext == ".txt":
        text = path.read_bytes().decode("utf-8", errors="replace")
    elif ext == ".docx":
        text = _docx_to_text(path)
    else:
        text = _pdf_to_text(path)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _cleanup_expired() -> None:
    now = time.time()
    with _lock:
        expired = [k for k, v in _attachments.items()
                   if now - v["created_at"] > _TTL_SEC]
        for k in expired:
            del _attachments[k]


def _docx_to_text(path: Path) -> str:
    """Плоский текст из .docx: абзацы + таблицы построчно."""
    import docx  # python-docx уже в зависимостях (конвертер актов)
    doc = docx.Document(str(path))
    parts = []
    for para in doc.paragraphs:
        t = para.text.strip()
        if t:
            parts.append(t)
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            line = " | ".join(x for x in cells if x)
            if line:
                parts.append(line)
    return "\n".join(parts)


def _pdf_to_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise HTTPException(
            status_code=415,
            detail="PDF не поддержан в этой сборке (нет pypdf) — "
                   "сохраните документ как .docx или вставьте текст в чат")
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Принимает .docx / .txt / .pdf, возвращает attachment_id."""
    cfg = get_settings()
    _cleanup_expired()

    filename = file.filename or "document"
    ext = Path(filename).suffix.lower()
    if ext not in (".docx", ".txt", ".pdf"):
        raise HTTPException(
            status_code=415,
            detail=f"Формат {ext or '(без расширения)'} не поддержан. "
                   f"Загрузите .docx, .txt или .pdf")

    raw = await file.read()
    max_bytes = cfg.upload_max_mb * 1024 * 1024
    if len(raw) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Файл больше {cfg.upload_max_mb} МБ")
    if not raw:
        raise HTTPException(status_code=400, detail="Пустой файл")

    # Парсим во временном файле, оригинал не сохраняем
    if ext == ".txt":
        text = raw.decode("utf-8", errors="replace")
    else:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = Path(tmp.name)
        try:
            text = _docx_to_text(tmp_path) if ext == ".docx" else _pdf_to_text(tmp_path)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"[Files] Ошибка парсинга {filename}: {e}")
            raise HTTPException(status_code=422,
                                detail=f"Не удалось прочитать файл: {e}")
        finally:
            tmp_path.unlink(missing_ok=True)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise HTTPException(status_code=422,
                            detail="В файле не найдено текста")

    attachment_id = hashlib.md5(
        (filename + str(time.time())).encode() + raw[:1024]).hexdigest()[:16]
    with _lock:
        _attachments[attachment_id] = {
            "text": text,
            "filename": filename,
            "chars": len(text),
            "created_at": time.time(),
        }

    logger.info(f"[Files] Загружено: {filename} → {len(text)} симв. "
                f"(id={attachment_id})")
    return {
        "attachment_id": attachment_id,
        "filename": filename,
        "chars": len(text),
        "preview": text[:300],
    }
