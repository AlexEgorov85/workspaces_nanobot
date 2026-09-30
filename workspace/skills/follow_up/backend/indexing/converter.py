"""
Follow Up 2.0 — Document Converter.

Конвертирует .docx → .md через python-docx.
Структура папок: data/raw/{pocket}/{dirname}/{file}.docx

Извлекает check_id (КМ-99-XXXXX) из:
  1. Имени папки
  2. Имени файла
  3. Тела документа (первые 100 абзацев ≈ 2 страницы)
"""
from __future__ import annotations

import hashlib
import logging
import re
import uuid
from pathlib import Path
from typing import Callable, Dict, List, Optional

from backend.config import get_settings
from backend.core import identity

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# Check ID extraction
# ──────────────────────────────────────────────────────────────────

# Формы записи и шаблон канона — backend/core/identity.py. Три локальные копии
# регекса (здесь, в памяти диалога и в понимании запроса) расходились между
# собой; теперь владелец один.

def extract_check_id(name: str) -> str:
    """
    Извлекает и нормализует номер КМ из строки (имя папки, файла, текст).

    Разбор — `core.identity` (единственный владелец формы номера). Здесь
    сохраняется только контракт возврата: пустая строка вместо None, потому
    что вызывающий код сравнивает результат со строкой и пишет «UNKNOWN».
    """
    kms = identity.parse(name)
    return kms[0] if kms else ""


def _extract_check_id_from_body(docx_path: Path, max_paragraphs: int = 150) -> str:
    """
    Ищет КМ-номер в первых max_paragraphs абзацах документа.
    Используется когда номер не найден ни в имени папки, ни в имени файла.
    """
    try:
        import docx as _docx
        doc = _docx.Document(str(docx_path))
    except Exception as e:
        logger.debug(f"[Converter] Не удалось открыть для поиска КМ {docx_path.name}: {e}")
        return ""

    count = 0
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            check_id = extract_check_id(text)
            if check_id:
                logger.debug(f"[Converter] КМ найден в теле документа: {check_id}")
                return check_id
        count += 1
        if count >= max_paragraphs:
            break
    return ""


def _make_stable_file_id(path: Path) -> str:
    """
    Детерминированный file_id — SHA-256 от абсолютного пути.
    Позволяет при повторной индексации находить уже существующие записи.
    """
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:32]


# ──────────────────────────────────────────────────────────────────
# .docx → Markdown
# ──────────────────────────────────────────────────────────────────

def _docx_to_markdown(docx_path: Path, md_path: Path) -> bool:
    """
    Конвертирует .docx → .md через python-docx API.

    Распознаёт заголовки двумя способами:
      1. Стиль Word: "Heading N" / "Заголовок N"
      2. Эвристика: короткий абзац целиком заглавными буквами
    """
    try:
        import docx as _docx
        doc = _docx.Document(str(docx_path))
    except Exception as e:
        logger.error(f"[Converter] Ошибка открытия {docx_path}: {e}")
        return False

    lines: List[str] = []

    for block in doc.element.body:
        tag = block.tag.split("}")[-1] if "}" in block.tag else block.tag

        if tag == "p":
            try:
                from docx.text.paragraph import Paragraph
                para = Paragraph(block, doc)
                text = para.text.strip()
                style_name = (para.style.name or "").lower() if para.style else ""
            except Exception:
                text = ""
                style_name = ""

            if not text:
                lines.append("")
                continue

            # Заголовок по стилю Word
            if "heading" in style_name or "заголовок" in style_name:
                level_match = re.search(r"(\d)", style_name)
                level = min(int(level_match.group(1)) if level_match else 1, 4)
                lines.append(f"{'#' * level} {text}")
            # Эвристика: короткий абзац целиком CAPS → заголовок 2-го уровня
            elif (
                len(text) <= 120
                and text == text.upper()
                and any(c.isalpha() for c in text)
                and not text.startswith("|")  # не строка таблицы
            ):
                lines.append(f"## {text}")
            else:
                lines.append(text)

        elif tag == "tbl":
            try:
                from docx.table import Table
                tbl = Table(block, doc)
                if tbl.rows:
                    rows_data = []
                    for row in tbl.rows:
                        # Дедупликация объединённых ячеек по id XML-элемента
                        seen: set = set()
                        cells: List[str] = []
                        for cell in row.cells:
                            cid = id(cell._tc)
                            if cid not in seen:
                                seen.add(cid)
                                # Убираем переносы строк внутри ячейки
                                cells.append(cell.text.replace("\n", " ").replace("|", "│").strip())
                        rows_data.append(cells)

                    if not rows_data:
                        continue

                    n_cols = max(len(r) for r in rows_data)
                    for row in rows_data:
                        while len(row) < n_cols:
                            row.append("")

                    lines.append("| " + " | ".join(rows_data[0]) + " |")
                    lines.append("| " + " | ".join(["---"] * n_cols) + " |")
                    for row in rows_data[1:]:
                        lines.append("| " + " | ".join(row) + " |")
                    lines.append("")
            except Exception as e:
                logger.debug(f"[Converter] Ошибка таблицы: {e}")

    md_content = "\n\n".join(l for l in lines if l is not None)
    md_content = re.sub(r"\n{3,}", "\n\n", md_content).strip()
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(md_content, encoding="utf-8")
    return True


# ──────────────────────────────────────────────────────────────────
# Main processing function
# ──────────────────────────────────────────────────────────────────

def process_documents(
    progress_callback: Optional[Callable[[str], None]] = None,
) -> List[Dict]:
    """
    Рекурсивно ищет .docx файлы в data/raw/ — поддерживает любую структуру:
      Плоская:      data/raw/*.docx
      Один уровень: data/raw/{папка}/*.docx
      Глубже:       data/raw/{карман}/{папка}/*.docx  (и т.д.)

    Приоритет поиска КМ-номера:
      1. Имена родительских папок (от ближайшей к data/raw/)
      2. Имя файла .docx
      3. Первые 100 абзацев тела документа
      4. "UNKNOWN" — если нигде не найден
    """
    cfg = get_settings()
    raw_dir = cfg.raw_dir
    md_dir = cfg.md_dir
    md_dir.mkdir(parents=True, exist_ok=True)

    if not raw_dir.exists():
        logger.warning(f"[Converter] Папка {raw_dir} не существует")
        return []

    def log(msg: str):
        logger.info(msg)
        if progress_callback:
            progress_callback(msg)

    # Рекурсивно собираем все .docx
    docx_files = sorted(raw_dir.rglob("*.docx"))
    if not docx_files:
        log(f"[Converter] .docx файлов не найдено в {raw_dir}")
        return []

    log(f"[Converter] Найдено .docx файлов: {len(docx_files)}")
    results: List[Dict] = []

    for docx_file in docx_files:
        # Ищем КМ в именах папок (от ближайшей к дальней, т.е. в reversed порядке)
        file_check_id = ""
        try:
            rel = docx_file.relative_to(raw_dir)
            # rel.parts = ('pocket', 'km_dir', 'file.docx') → папки = rel.parts[:-1]
            for part in reversed(rel.parts[:-1]):
                cid = extract_check_id(part)
                if cid:
                    file_check_id = cid
                    break
        except ValueError:
            pass

        # Имя файла
        if not file_check_id:
            file_check_id = extract_check_id(docx_file.stem)

        # Тело документа (первые 100 абзацев ≈ 2 страницы)
        if not file_check_id:
            file_check_id = _extract_check_id_from_body(docx_file)

        if not file_check_id:
            file_check_id = "UNKNOWN"
            log(f"[WARN] КМ не найден: {docx_file.name} — помечен как UNKNOWN")

        # Стабильный file_id — idempotent при повторных запусках
        file_id = _make_stable_file_id(docx_file)

        prefix = f"{file_check_id}_" if file_check_id != "UNKNOWN" else ""
        md_filename = f"{prefix}{docx_file.stem}.md"
        md_path = md_dir / md_filename

        if md_path.exists():
            log(f"[skip] {md_filename}")
        else:
            log(f"[conv] {docx_file.name} → {md_filename}")
            if not _docx_to_markdown(docx_file, md_path):
                log(f"[ERR]  Ошибка конвертации {docx_file.name}")
                continue

        results.append({
            "file_id": file_id,
            "filename": md_filename,
            "check_id": file_check_id,
            "original_path": str(docx_file),
            "md_path": str(md_path),
            "title": docx_file.stem,
        })

    log(f"[Converter] Обработано файлов: {len(results)}")
    return results
