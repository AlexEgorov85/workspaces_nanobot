"""Инкрементальный эмбеддинг: строки источника → векторы в таблице хранения.

Портировано из агента: ``tools/build_vectors.py`` (``build_index`` и его
помощники). Удаление агентской копии — фазы 3/5 (пункт 3.6).

**Писатель PostgreSQL, а не снимка.** Это главное отличие от прочтения
индекса. Владелец индексов (``libs/vectors/owner.py``) читает вектора из
снимка и никогда в него не пишет, поэтому «кто посчитал вектора» — вопрос с
другим владельцем: таблица хранения векторов живёт в PostgreSQL, и её
единственный писатель — этот модуль. Снимок получает эти вектора тем же
путём, что и любую другую таблицу: загрузкой. Следствие, которое важно помнить
при эксплуатации: **после сборки требуется перезагрузка снимка**, иначе
поиск продолжит видеть прежние вектора. Раньше это было неявным (агентский
``build_vectors.py`` прогревал FAISS у себя в процессе и тут же им
пользовался); здесь прогрева нет намеренно — короткоживущий операторский
процесс не должен становиться вторым владельцем индекса.

**Один владелец записи.** Загрузчик снимка — тоже писатель, но другой
таблицы и другой момент времени. Пересечения двух процессов записи не
разруливается: файл/таблица пишутся по очереди, операторский запуск сборки
выполняется вне загрузки. Это то же допущение, на котором уже стоит работа
снимка («в системе один gateway, writer один и известен заранее»), и
расширять его на второй параллельный писатель незачем.

**Ничего не читается из окружения.** Путь к таблице, объявления индексов,
chunk-параметры и эмбеддер приходят параметрами: окружение читает только
реестр (``Settings``), а этот модуль работает с тем, что ему дали. Модуль не
знает ни адреса провайдера, ни модели, ни ключа — это зона ``libs/llm``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from libs.vectors.text_splitter import build_chunks

logger = logging.getLogger(__name__)

#: Имя таблицы приходит из конфигурации, а не от модели, но подставляется в
#: текст SQL — поэтому проверяется как идентификатор, а не цитируется «как
#: получится». Молчаливый SQL из строки настроек был бы худшим из двух
#: вариантов: нечитаемая ошибка при опечатке в конфигурации.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def quote_identifier(name: str) -> str:
    """Одно имя (колонка) в кавычках, с той же проверкой идентификатора.

    Отдельная функция, а не переиспользование :func:`quote_table`, потому что
    сообщение об ошибке должно называть то, что проверяется: для колонки
    «имя таблицы не является идентификатором» — враньё, которое заставит
    искать опечатку не там.
    """
    text = str(name or "")
    if not _IDENTIFIER.match(text):
        raise ValueError(f"имя колонки не является идентификатором: {name!r}")
    return f'"{text}"'


def quote_table(name: str) -> str:
    """Квалифицированное имя таблицы для подстановки в SQL.

    Args:
        name: ``table`` либо ``schema.table``.

    Returns:
        Имя в кавычках: ``"schema"."table"``.

    Raises:
        ValueError: имя не является идентификатором(ами). Это ошибка
            конфигурации, и она обязана быть громкой: молчаливый обрыв SQL
            выглядел бы как «индекс не собрался».
    """
    parts = [p for p in str(name or "").split(".") if p]
    if not parts or not all(_IDENTIFIER.match(p) for p in parts):
        raise ValueError(
            f"имя таблицы не является идентификатором: {name!r} "
            f"(ожидается table либо schema.table)"
        )
    return ".".join(f'"{p}"' for p in parts)


# ---------------------------------------------------------------------------
# Вспомогательные функции — перенесены из агента без изменения смысла
# ---------------------------------------------------------------------------


def norm_pk(raw: Any) -> str:
    """Привести значение PK к канонической строке.

    ``pk_value`` в таблице хранения — TEXT, а в исходной таблице бывает
    BIGINT/INTEGER/UUID. Обе стороны сравнения приводятся к нормализованной
    строке (float ``1.0`` → ``"1"``, а не ``"1.0"``), иначе несовпадение
    выглядело бы как «строка изменилась» при каждой пересборке.
    """
    if raw is None:
        return ""
    if isinstance(raw, float):
        raw = int(raw)
    return str(raw)


def normalize_embedding_cols(embedding_cols: Sequence[Any]) -> list[str]:
    """Привести ``embedding_columns`` к списку имён колонок.

    В объявлении индекса колонка может быть строкой (``"col1"``) либо
    объектом (``{"column": "col", "chunk": true}``) — второй формой
    пользуются навыки, задающие разбиение. Здесь нужны только имена.
    """
    out: list[str] = []
    for c in embedding_cols or ():
        if isinstance(c, Mapping):
            col = c.get("column")
            if col:
                out.append(str(col))
        elif isinstance(c, str) and c:
            out.append(c)
    return out


def build_search_text(row: Mapping[str, Any], embedding_cols: Sequence[str]) -> str:
    """Собрать ``search_text`` с метками колонок (``"col: значение"``)."""
    labeled: dict[str, str] = {}
    for col in embedding_cols:
        val = row.get(col)
        if val and str(val).strip():
            labeled[col] = str(val).strip()
    if not labeled:
        return ""
    return ". ".join(f"{k}: {v}" for k, v in labeled.items())


def content_hash(text: str) -> str:
    """Хеш ``search_text`` — по нему решается, нужен ли переэмбеддинг."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def format_content(row: Mapping[str, Any], columns: Sequence[str]) -> str:
    """Собрать ``content`` из указанных колонок (то, что увидит человек)."""
    parts = [str(row[c]).strip() for c in columns if row.get(c) and str(row[c]).strip()]
    return ". ".join(parts) if parts else ""


# ---------------------------------------------------------------------------
# Контракты зависимостей
# ---------------------------------------------------------------------------


class Database(Protocol):
    """Минимум пула PostgreSQL, нужный сборке.

    Структурный тип, а не импорт ``libs.enterprise_data.db``: сборщик не
    обязан знать, кто владеет пулом. В рантайме это
    ``libs.enterprise_data.db`` (тот же пул, что у загрузчика снимка).
    """

    def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        ...

    def fetchone(self, sql: str, *args: Any) -> dict[str, Any] | None:
        ...

    def execute(self, sql: str, *args: Any) -> Any:
        ...


#: Эмбеддер: текст → вектор или ``None``. Тот же контракт, что у
#: ``libs.vectors.embedding.Embedder`` и у владельца индексов, поэтому один
#: и тот же объект годится и поиску, и сборке.
Embedder = Callable[[str], "list[float] | None"]


@dataclass(frozen=True)
class BuildProgress:
    """Событие прогресса. Рендеринг — дело вызывающей стороны."""

    index_name: str
    done: int
    total: int
    started_at: float


@dataclass
class IndexBuildResult:
    """Итог сборки одного индекса. Числа, а не проза, — их читают и CI,
    и человек, поэтому они обязаны быть одного типа."""

    index_name: str
    source_table: str = ""
    total: int = 0
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    unchanged: int = 0
    errors: int = 0
    skipped: bool = False
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.errors == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index_name": self.index_name,
            "source_table": self.source_table,
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "deleted": self.deleted,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "skipped": self.skipped,
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Сборщик
# ---------------------------------------------------------------------------


class VectorBuilder:
    """Инкрементальная сборка векторов одного или нескольких индексов.

    Экземпляр создаётся один раз на процесс и переиспользуется между
    индексами: он держит только объявления и эмбеддер, а накопленного
    состояния между индексами не имеет.
    """

    def __init__(
        self,
        *,
        db: Database,
        embed: Embedder,
        storage_table: str,
        indexes: Mapping[str, Mapping[str, Any]],
        chunk_defaults: Mapping[str, Any] | None = None,
        embedding_retry_wait: float = 5.0,
        full_rebuild: bool = False,
        dry_run: bool = False,
        progress: Callable[[BuildProgress], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._db = db
        self._embed = embed
        self._storage_table = quote_table(storage_table)
        self._storage_table_raw = str(storage_table)
        self._indexes = dict(indexes or {})
        defaults = dict(chunk_defaults or {})
        self._chunk_size = int(defaults.get("chunk_size") or 500)
        self._chunk_overlap = int(defaults.get("chunk_overlap") or 80)
        self._retry_wait = float(embedding_retry_wait)
        self._full_rebuild = bool(full_rebuild)
        self._dry_run = bool(dry_run)
        self._progress = progress
        self._sleep = sleeper

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def _enabled_indexes(self) -> dict[str, Mapping[str, Any]]:
        return {
            name: cfg
            for name, cfg in self._indexes.items()
            if (cfg or {}).get("enabled", True)
        }

    def _unknown_index_message(self, name: str) -> str:
        enabled = self._enabled_indexes()
        known = ", ".join(sorted(enabled)) or "ни одного"
        if name in self._indexes:
            return f"индекс {name!r} объявлен, но выключен; доступны: {known}"
        return f"индекс {name!r} не объявлен; доступны: {known}"

    def index_names(self, only: str | None = None) -> list[str]:
        """Индексы к сборке: все объявленные и включённые либо один.

        ``only`` — имя индекса. Неизвестное имя не превращается в пустой
        список («собрал ноль индексов, ошибок нет»): это ошибка вызова,
        и она выглядит как успех ровно настолько же правдоподобно, насколько
        неправдоподобна.
        """
        enabled = self._enabled_indexes()
        if only is None:
            return sorted(enabled)
        if only not in enabled:
            raise ValueError(self._unknown_index_message(only))
        return [only]

    def build(self, only: str | None = None) -> list[IndexBuildResult]:
        """Собрать индексы по очереди. Ошибка одного не роняет остальные."""
        return [self.build_index(name) for name in self.index_names(only)]

    def build_index(self, index_name: str) -> IndexBuildResult:
        """Собрать/обновить векторы одного индексного источника.

        Сценарии (поведение перенесено из агента без изменений):

        * ``NEW`` — строки, которых нет в таблице хранения → вставка;
        * ``CHANGED`` — изменился ``content_hash`` → вставка новых чанков,
          затем удаление старых по несовпадающему хешу;
        * ``REMOVED`` — строки, пропавшие из источника или потерявшие
          контент для эмбеддинга → удаление.

        Порядок удаления изменённых — **после** успешной вставки новых:
        иначе неудачный эмбеддинг оставил бы строку вовсе без вектора.
        """
        cfg = self._indexes.get(index_name)
        if cfg is None:
            # Неизвестное имя — ошибка вызова, а не «индекс выключен» и не
            # «объявление неполное»: без этой проверки опечатка в аргументе
            # возвращала бы вежливый отчёт «пропущено», и сборка всех индексов
            # выглядела бы успешной, хотя не сделала ничего.
            raise ValueError(self._unknown_index_message(index_name))
        source_table = str(cfg.get("source_table") or cfg.get("table") or "")
        src_table = str(cfg.get("table") or "")
        pk_column = str(cfg.get("pk") or "")
        content_cols = list(cfg.get("content_columns") or [])
        embedding_cols = normalize_embedding_cols(
            cfg.get("embedding_columns") or content_cols
        )
        track_col = str(cfg.get("track_column") or pk_column)

        result = IndexBuildResult(index_name=index_name, source_table=source_table)

        missing = [
            name
            for name, value in (
                ("table", src_table),
                ("pk", pk_column),
            )
            if not value
        ]
        if missing or not embedding_cols:
            result.skipped = True
            result.reason = (
                "объявление индекса неполное: "
                + ", ".join(f"нет {m}" for m in missing)
                + ("" if embedding_cols else ", нет embedding_columns")
            )
            return result

        existing = (
            {} if self._full_rebuild else self._existing_entries(index_name, source_table)
        )
        src_rows = self._source_rows(src_table, pk_column)
        max_src_track = self._max_track(src_table, track_col)

        new_rows: list[dict[str, Any]] = []
        changed_rows: list[dict[str, Any]] = []
        unchanged = 0
        empty_search_pks: set[str] = set()
        src_pks: set[str] = set()
        pk_row_map: dict[str, dict[str, Any]] = {}

        for row in src_rows:
            pk_val = norm_pk(row.get(pk_column))
            if not pk_val:
                continue
            pk_row_map[pk_val] = dict(row)
            src_pks.add(pk_val)

            search_text = build_search_text(row, embedding_cols)
            if not search_text:
                # Контента для эмбеддинга нет. Если вектор был — он больше
                # не описывает строку и должен уйти, иначе поиск продолжит
                # выдавать её по старому тексту.
                if pk_val in existing:
                    empty_search_pks.add(pk_val)
                continue

            prev = existing.get(pk_val)
            if prev is None:
                new_rows.append(dict(row))
            elif prev.get("content_hash") != content_hash(search_text):
                changed_rows.append(dict(row))
            else:
                unchanged += 1

        removed_pks = [pk for pk in existing if pk not in src_pks]
        result.unchanged = unchanged
        result.total = len(new_rows) + len(changed_rows)
        result.deleted = len(removed_pks) + len(empty_search_pks)

        logger.info(
            "[%s] строк в источнике=%d, векторов в таблице=%d, новых=%d, "
            "изменённых=%d, без изменений=%d, удаляемых=%d",
            index_name, len(src_rows), len(existing), len(new_rows),
            len(changed_rows), unchanged, len(removed_pks) + len(empty_search_pks),
        )

        if not new_rows and not changed_rows and not removed_pks and not empty_search_pks:
            result.skipped = True
            result.reason = "изменений нет"
            return result

        if self._dry_run:
            logger.info(
                "[%s] dry-run: вставить чанков для %d строк, удалить %d",
                index_name, result.total, result.deleted,
            )
            return result

        # Полная очистка — до вставки, иначе новые векторы попадут под нож.
        if self._full_rebuild:
            self._delete_where(index_name, source_table, None, None)

        # Удаление безусловно устаревших (нет в источнике / нет контента) —
        # до вставки: их векторы не участвуют в замене.
        deleted = 0
        for pk in sorted(set(removed_pks) | empty_search_pks):
            if self._delete_where(index_name, source_table, pk, None):
                deleted += 1
        result.deleted = deleted

        to_insert = new_rows + changed_rows
        if not to_insert:
            return result

        chunks = self._plan_chunks(
            to_insert, pk_column, content_cols, embedding_cols, max_src_track
        )
        logger.info("[%s] чанков к вставке: %d", index_name, len(chunks))

        inserted_ok: set[str] = set()
        started_at = time.monotonic()
        for i, chunk in enumerate(chunks, start=1):
            if self._progress is not None:
                self._progress(
                    BuildProgress(
                        index_name=index_name,
                        done=i,
                        total=len(chunks),
                        started_at=started_at,
                    )
                )
            pk_val = chunk["pk"]
            vector = self._embed(chunk["search_text"])
            if not vector:
                # Один повтор: эмбеддер сам разбирает свои сетевые ошибки, а
                # повтор здесь — про «модель перегружена», а не про исключение.
                self._sleep(self._retry_wait)
                vector = self._embed(chunk["search_text"])
            if not vector:
                logger.warning(
                    "[%s] нет эмбеддинга pk=%s чанк %d/%d",
                    index_name, pk_val, chunk["chunk_index"] + 1, chunk["chunk_count"],
                )
                result.errors += 1
                continue
            try:
                self._insert(
                    index_name, source_table, chunk, vector, pk_row_map[pk_val]
                )
                result.inserted += 1
                inserted_ok.add(pk_val)
            except Exception as exc:  # noqa: BLE001 - одна строка не должна ронять индекс
                logger.warning(
                    "[%s] ошибка вставки pk=%s чанк %d: %s",
                    index_name, pk_val, chunk["chunk_index"], exc,
                )
                result.errors += 1

        # Старые векторы изменённых строк — только после успешной вставки, и
        # только для тех pk, где новый вектор действительно записан.
        for row in changed_rows:
            pk_val = norm_pk(row.get(pk_column))
            if pk_val not in inserted_ok:
                logger.warning(
                    "[%s] pk=%s: новый вектор не записан, старый сохранён",
                    index_name, pk_val,
                )
                continue
            new_hash = content_hash(build_search_text(row, embedding_cols))
            if self._delete_where(index_name, source_table, pk_val, new_hash):
                result.updated += 1

        logger.info(
            "[%s] итог: вставлено=%d, переиндексировано=%d, удалено=%d, ошибок=%d",
            index_name, result.inserted, result.updated, result.deleted, result.errors,
        )
        return result

    # ------------------------------------------------------------------
    # Чтение состояния
    # ------------------------------------------------------------------

    def _existing_entries(
        self, index_name: str, source_table: str
    ) -> dict[str, dict[str, Any]]:
        rows = self._db.fetch(
            f"SELECT pk_value, content_hash, chunk_count FROM {self._storage_table} "
            f'WHERE source = %s AND "table" = %s AND pk_value IS NOT NULL',
            index_name,
            source_table,
        )
        return {norm_pk(r.get("pk_value")): dict(r) for r in rows or []}

    def _source_rows(self, table: str, pk_column: str) -> list[dict[str, Any]]:
        quoted = quote_table(table)
        rows = self._db.fetch(
            f"SELECT * FROM {quoted} ORDER BY {quote_identifier(pk_column)}"
        )
        return [dict(r) for r in rows or []]

    def _max_track(self, table: str, track_col: str) -> str | None:
        row = self._db.fetchone(
            f"SELECT MAX({quote_identifier(track_col)})::TEXT AS mx "
            f"FROM {quote_table(table)}"
        )
        if not row or row.get("mx") in (None, ""):
            return None
        return str(row["mx"])

    # ------------------------------------------------------------------
    # Запись
    # ------------------------------------------------------------------

    def _plan_chunks(
        self,
        rows: Sequence[Mapping[str, Any]],
        pk_column: str,
        content_cols: Sequence[str],
        embedding_cols: Sequence[str],
        max_src_track: str | None,
    ) -> list[dict[str, Any]]:
        """Разложить строки на чанки с общим хешем и отметкой времени."""
        now = datetime.now(UTC).isoformat()
        planned: list[dict[str, Any]] = []
        for row in rows:
            pk_val = norm_pk(row.get(pk_column))
            chunks = build_chunks(
                dict(row),
                list(embedding_cols),
                self._chunk_size,
                self._chunk_overlap,
            )
            base_content = format_content(row, content_cols)
            h = content_hash(build_search_text(row, embedding_cols))
            for i, chunk in enumerate(chunks):
                planned.append(
                    {
                        "pk": pk_val,
                        "chunk_index": i,
                        "chunk_count": len(chunks),
                        "search_text": chunk["search_text"],
                        "content": base_content + chunk.get("content_suffix", ""),
                        "content_hash": h,
                        "synced_at": now,
                        "max_src_track": max_src_track,
                    }
                )
        return planned

    def _insert(
        self,
        index_name: str,
        source_table: str,
        chunk: Mapping[str, Any],
        vector: Sequence[float],
        row_data: Mapping[str, Any],
    ) -> None:
        self._db.execute(
            f"""
            INSERT INTO {self._storage_table}
                (source, content, search_text, "table", pk_value,
                 chunk_index, chunk_count, row_data, embedding,
                 content_hash, max_src_track, synced_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            index_name,
            chunk["content"],
            chunk["search_text"],
            source_table,
            chunk["pk"],
            chunk["chunk_index"],
            chunk["chunk_count"],
            json.dumps(dict(row_data), ensure_ascii=False, default=str),
            list(vector),
            chunk["content_hash"],
            chunk["max_src_track"],
            chunk["synced_at"],
        )

    def _delete_where(
        self,
        index_name: str,
        source_table: str,
        pk_value: str | None,
        keep_hash: str | None,
    ) -> bool:
        """Удалить векторы источника: по pk, по устаревшему хешу, либо все.

        Один метод вместо трёх — потому что все три отличаются только
        условием, и раздельные копии однажды разъедутся по набору колонок.
        """
        sql = (
            f"DELETE FROM {self._storage_table} "
            f'WHERE source = %s AND "table" = %s'
        )
        args: list[Any] = [index_name, source_table]
        if pk_value is not None:
            sql += " AND pk_value = %s"
            args.append(pk_value)
        if keep_hash is not None:
            sql += " AND content_hash <> %s"
            args.append(keep_hash)
        try:
            self._db.execute(sql, *args)
        except Exception as exc:  # noqa: BLE001 - потеря одной строки не должна ронять сборку
            logger.warning(
                "[%s] ошибка удаления pk=%s: %s", index_name, pk_value, exc
            )
            return False
        return True
