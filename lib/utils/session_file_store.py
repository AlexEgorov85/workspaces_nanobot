import base64
import csv
import hashlib
import io
import json
import mimetypes
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

UTC = UTC

"""Хранилище файлов сессии: вложения канала и результаты инструментов.

Каталог сессии и раскладка внутри него — **не здесь**. Хранилище получает
каталог от :class:`~lib.services.session_files.SessionFileResolver` (функция
``session_dir_for``) и раскладывает файлы по объявленной платформой раскладке:
``files/results/`` и ``files/attachments/`` рядом с файлами, которые пишет хук
перенаправления. Прежняя собственная раскладка (сессии и архив под каталогом
кэша) стоила расхождения, при котором вложения из PostgreSQL ложились не туда,
куда их потом искал хук, поэтому и имя каталога, и собственный корень, и
каталог архива убраны.

Архивации сессии больше нет: каталог сессии перемещает его владелец
(``SessionWorkspace``), а у хранилища на это ни вызывающего, ни прав, и каталог
архива создавался впустую при каждой инициализации.
"""

#: Подкаталог сессии, в который агент пишет файлы. Объявлен платформой
#: (``mcp-platform/servers/enterprise/tools/session_files.py::FILES_SUBDIR`` и
#: ``libs/enterprise_common/session/workspace.py::SESSION_SUBDIRS``), поэтому
#: повторяется здесь строкой: операция отдаёт агенту готовый ``files_dir``, а
#: хранилище пишет по тому же имени, что и хук перенаправления. Проверяет
#: ``tests/test_no_hardcoded_session_paths.py`` и контракт раскладки.
FILES_SUBDIR = "files"

#: Подкаталог сессии с выгрузками инструментов.
RESULTS_SUBDIR = "results"

#: Служебный файл хранилища: счётчики файлов и байт сессии. Лежит в корне
#: каталога сессии рядом с подкаталогами и принадлежит той же сессии.
METADATA_FILE = "metadata.json"


def guess_ext_from_mime(mime_type: str, default_ext: str = ".bin") -> str:
    """Единая точка: от MIME-типа к расширению файла.

    Эквивалент прежних ``SessionFileStore._guess_ext_from_mime`` и
    прежняя ``_get_extension_from_mime`` (была одна и та же логика
    ``mimetypes.guess_extension`` с разным дефолтом).
    Отбирает параметры (``text/html; charset=utf-8`` → ``.html``).

    Args:
        mime_type: MIME-тип (или пустая строка).
        default_ext: расширение при неизвестном типе — ``.bin`` для
            хранилища вложений, ``""`` для web-UI (без подстановки).

    Returns:
        Расширение с ведущей точкой (``.png``, ``.html``) или ``default_ext``.
    """
    if not mime_type:
        return default_ext
    mime = mime_type.split(";")[0].strip().lower()
    ext = mimetypes.guess_extension(mime) or ""
    if not ext:
        return default_ext
    return ext if ext.startswith(".") else f".{ext}"


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


def _try_convert_to_csv(data) -> str | None:
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
    def __init__(
        self,
        session_dir_for: Callable[[str], Path],
        max_files: int = 0,
        max_age_hours: int = 0,
        attachments_subdir: str = "attachments",
    ):
        """Инициализирует хранилище файлов сессии.

        Аргументы:
            session_dir_for: функция ``(session_key) -> Path``, отдающая каталог
                сессии. Её реализация — резолвер: имя каталога и его корень
                приходят оттуда, а не вычисляются здесь. Хранилище не создаёт
                каталогов в конструкторе: дерево сессии создаёт его владелец,
                и пустая инициализация не должна оставлять после себя
                ``cache/``, в котором ничего не лежит.
            max_files: Максимальное количество файлов результатов на сессию
                (0 — без ограничения).
            max_age_hours: Максимальный возраст файлов результатов в часах
                (0 — без ограничения).
            attachments_subdir: Имя подкаталога вложений пользователя внутри
                ``files/`` сессии. Не пересекается с ``results``.
        """
        self._session_dir_for = session_dir_for
        self.max_files = max_files
        self.max_age_hours = max_age_hours
        self.attachments_subdir = attachments_subdir

    def _get_session_dir(self, session_key: str) -> Path:
        """Каталог сессии от резолвера, с раскладкой платформы.

        Подкаталоги создаются здесь, потому что вложение приходит извне
        (декодируется каналом) и каталога сессии, к которому у агента может не
        быть обращения, ещё не существует.
        """
        sdir = Path(self._session_dir_for(session_key))
        sdir.mkdir(parents=True, exist_ok=True)
        files_dir = sdir / FILES_SUBDIR
        (files_dir / RESULTS_SUBDIR).mkdir(parents=True, exist_ok=True)
        (files_dir / self.attachments_subdir).mkdir(exist_ok=True)
        return sdir

    def _resolve_results_dir(self, session_key: str) -> Path:
        """Каталог результатов сессии — ``files/results/``.

        Тот же подкаталог, который ищет хук перенаправления: результат
        инструмента и вложение канала лежат рядом, и агент находит оба по
        одному пути.
        """
        sdir = self._get_session_dir(session_key)
        return sdir / FILES_SUBDIR / RESULTS_SUBDIR

    def _resolve_attachments_dir(self, session_key: str) -> Path:
        """Каталог вложений сессии — ``files/attachments/``.

        Отдельно от ``files/results/``, чтобы источник файла читался по
        каталогу: ``results/`` — выгрузки инструментов,
        ``attachments/`` — то, что прислал пользователь.
        """
        sdir = self._get_session_dir(session_key)
        adir = sdir / FILES_SUBDIR / self.attachments_subdir
        adir.mkdir(exist_ok=True)
        return adir

    @staticmethod
    def _sanitize_filename(name: str | None) -> str:
        """Очистить имя файла: оставить ``[\\w.-]``, пробелы, остальное в ``_``."""
        if not name:
            return ""
        base = Path(name).name.strip()
        return re.sub(r"[^\w.\- ]", "_", base).strip()

    @staticmethod
    def _guess_ext_from_mime(mime_type: str) -> str:
        return guess_ext_from_mime(mime_type or "")

    def save_attachment(
        self,
        session_key: str,
        data_url: str | None,
        *,
        filename: str | None = None,
    ) -> dict | None:
        """Сохранить вложение (data URL или путь/сырые байты) в каталоге сессии.

        Принимает:
          * ``data_url`` вида ``data:<mime>;base64,<payload>`` — кодированное
            вложение от пользователя;
          * строку локального пути — содержимое читается и копируется;
          * строку-URL (http/https) — внешняя ссылка, не сохраняется
            (возвращается ``None``, вызывающий решает как с ней быть).

        Возвращает ``{"path", "filename", "size"}`` для использования в
        подсказках агенту, либо ``None``, если сохранить нельзя.

        Файл получает имя ``{uuid12}_{original_or_mime_ext}`` — выживает после
        многих вложений с одинаковым именем и сохраняет оригинальное имя
        пользователя в суффиксе.
        """
        if not data_url or not isinstance(data_url, str):
            return None

        if data_url.startswith(("http://", "https://")):
            return None

        if data_url.startswith("data:"):
            m = re.match(r"^data:([^;,]+)(?:;[^,]*)*;base64,(.+)$", data_url)
            if not m:
                return None
            mime_type = m.group(1).strip().lower()
            try:
                raw = base64.b64decode(m.group(2))
            except Exception:
                return None
            clean = self._sanitize_filename(filename)
            if clean:
                dest_name = f"{uuid.uuid4().hex[:12]}_{clean}"
            else:
                dest_name = f"{uuid.uuid4().hex[:12]}{self._guess_ext_from_mime(mime_type)}"
        else:
            p = Path(data_url).expanduser()
            if not p.is_file():
                return None
            raw = p.read_bytes()
            mime_type, _ = mimetypes.guess_type(str(p))
            mime_type = mime_type or "application/octet-stream"
            clean = self._sanitize_filename(p.name)
            dest_name = f"{uuid.uuid4().hex[:12]}_{clean or ('file' + self._guess_ext_from_mime(mime_type))}"

        adir = self._resolve_attachments_dir(session_key)
        dest = adir / dest_name
        dest.write_bytes(raw)

        self._ensure_metadata(session_key)
        meta_path = self._get_session_dir(session_key) / METADATA_FILE
        meta = json.loads(meta_path.read_text())
        meta["last_activity"] = datetime.now(UTC).isoformat()
        meta["file_count"] = meta.get("file_count", 0) + 1
        meta["total_bytes"] = meta.get("total_bytes", 0) + len(raw)
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        return {
            "path": str(dest),
            "filename": clean or dest.name,
            "size": len(raw),
        }

    def _ensure_metadata(self, session_key: str) -> None:
        """Создаёт ``metadata.json`` для сессии, если его ещё нет."""
        meta_path = self._get_session_dir(session_key) / METADATA_FILE
        if not meta_path.exists():
            meta_path.write_text(json.dumps({
                "session_key": session_key,
                "created_at": datetime.now(UTC).isoformat(),
                "last_activity": datetime.now(UTC).isoformat(),
                "status": "active",
                "file_count": 0,
                "total_bytes": 0
            }, indent=2), encoding="utf-8")

    def _find_existing_for_hash(self, session_key: str, content_hash: str, ext: str) -> str | None:
        """Вернуть путь уже сохранённого файла с таким хешем содержимого.

        Сканирует ``files/results/`` сессии в поисках файла с суффиксом
        ``__<hash>`` и подходящим расширением. Сканирование ограничено одной
        сессией.
        """
        results_dir = self._resolve_results_dir(session_key)
        if not results_dir.exists():
            return None
        marker = f"__{content_hash}{ext}"
        for f in results_dir.iterdir():
            if not f.is_file():
                continue
            if f.name.endswith(marker):
                return str(f.name)
        return None

    def save(
        self,
        session_key: str,
        content: str,
        source_tool: str,
        ext: str = ".json",
        dedupe: bool = True,
    ) -> dict:
        """Сохраняет содержимое как файл результата в сессии.

        Аргументы:
            session_key: Ключ сессии.
            content: Содержимое файла.
            source_tool: Имя инструмента-источника.
            ext: Расширение файла (по умолчанию .json).
            dedupe: Если True, при повторном сохранении содержимого с тем же
                хешем возвращается уже существующий файл (без новой записи).

        Возвращает словарь с информацией о сохранённом файле
        (ключ сессии, id, путь, размер, формат). При dedupe-совпадении
        ``id``/``path`` указывают на уже существующий файл, ``deduped=True``.
        """
        content_hash = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
        self._ensure_metadata(session_key)
        sdir = self._get_session_dir(session_key)
        results_dir = self._resolve_results_dir(session_key)

        existing = (
            self._find_existing_for_hash(session_key, content_hash, ext)
            if dedupe
            else None
        )
        if existing is not None:
            existing_path = results_dir / existing
            try:
                size = existing_path.stat().st_size
            except OSError:
                size = len(content.encode("utf-8"))
            return {
                "session_key": session_key,
                "id": existing.split("_")[-1].split(".")[0],
                "path": f"{FILES_SUBDIR}/{RESULTS_SUBDIR}/{existing}",
                "size_kb": round(size / 1024, 2),
                "format": ext.lstrip("."),
                "deduped": True,
            }

        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        entry_id = uuid.uuid4().hex[:8]
        filename = f"{ts}_{source_tool}_{entry_id}__{content_hash}{ext}"
        filepath = results_dir / filename

        filepath.write_text(content, encoding="utf-8")
        size = len(content.encode("utf-8"))

        meta_path = sdir / METADATA_FILE
        meta = json.loads(meta_path.read_text())
        meta["last_activity"] = datetime.now(UTC).isoformat()
        meta["file_count"] = meta.get("file_count", 0) + 1
        meta["total_bytes"] += size
        meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

        self.cleanup(session_key)

        return {
            "session_key": session_key,
            "id": entry_id,
            "path": f"{FILES_SUBDIR}/{RESULTS_SUBDIR}/{filename}",
            "size_kb": round(size / 1024, 2),
            "format": ext.lstrip("."),
            "deduped": False,
        }

    def cleanup(self, session_key: str) -> None:
        """Удаляет устаревшие файлы результатов согласно лимитам max_files / max_age_hours."""
        max_files = self.max_files
        max_age_hours = self.max_age_hours
        if max_files <= 0 and max_age_hours <= 0:
            return

        results_dir = self._resolve_results_dir(session_key)
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
            meta_path = self._get_session_dir(session_key) / METADATA_FILE
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

