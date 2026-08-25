"""
Инкрементальная сборка векторных индексов из исходных таблиц.

Поддерживает чанкование длинных текстов: если колонка в
embedding_columns помечена "chunk": true, её текст разбивается
на перекрывающиеся сегменты, каждый сегмент → отдельный вектор.

row_data (JSONB) всегда содержит ПОЛНУЮ строку исходной таблицы,
независимо от количества чанков.

Конфиг читается из public.agent_vector_index_config (БД) — единственный
источник; конфигурация из project.json не подставляется.

Запуск (из корня проекта):
    # Инкрементальное обновление (только новые строки)
    python tools/build_vectors.py

    # Подробное логирование каждого чанка/строки (уровень DEBUG)
    python tools/build_vectors.py --verbose

    # Полная перестройка
    python tools/build_vectors.py --full-rebuild

    # Показать что будет добавлено
    python tools/build_vectors.py --dry-run

    # Настроить чанкование
    python tools/build_vectors.py --chunk-size 800 --chunk-overlap 150

    # При ошибке получения эмбеддинга: ждать это время (сек, default 5) и повторить один раз
    python tools/build_vectors.py --embedding-retry-wait 5

Может вызываться по cron:
    0 3 * * * cd /path && python tools/build_vectors.py --check >> build.log 2>&1

Логирование — через loguru, единый поток в stderr (без ANSI-цветов), всё
пишется по этапам: конфиг → состояние БД/источника → классификация
(новые/изменённые/удалённые) → удаление → чанки → эмбеддинг с прогрессом
→ пересборка FAISS → итог. Ошибки любого этапа печатаются с traceback,
поэтому процесс не «молчит» при срыве. Один индекс/прогон не роняет весь
скрипт: сбой ловится и фиксируется в итоговой сводке.
"""

import hashlib
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

from loguru import logger

# Принудительно UTF-8 для консоли (надо chcp 65001 в PowerShell)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

_ROOT = Path(__file__).resolve().parents[1]                    # корень проекта
_SKILL_ROOT = _ROOT / "workspace" / "skills" / "audit_analyzer"  # корень навыка (кэш/индексы)
for p in [str(_ROOT), str(_SKILL_ROOT)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import SETTINGS
from lib.services.cache_provider_impl import read_vector_index_config
from lib.services.text_splitter import build_chunks
from lib.services.vector_index_service import (
    VectorIndexBuildService,
    get_embedding,
)
from utils.db import configure, execute, fetch, resolve_dsn

_CFG = SETTINGS.get("skills", {}).get("audit_analyzer", {})

# Регистрируем таблицы в table_registry, чтобы vector_table() и
# embedding_config() работали без зависимости от audit_settings.
from lib.services.table_registry import (
    SkillRegistration,
    table_registry,
)
if not table_registry.get("audit_analyzer"):
    from lib.utils.table_utils import normalize_table_names
    table_registry.register(SkillRegistration(
        name="audit_analyzer",
        tables=tuple(_CFG.get("db_tables") or ()),
        additional_tables=tuple(normalize_table_names(_CFG.get("db_additional_tables"))),
        vector_table=_CFG.get("mode_vector_db_table", ""),
        db_schema=_CFG.get("db_schema", "main"),
    ))
    table_registry.set_embedding_config(
        base_url=_CFG.get("embedding_base_url", ""),
        model=_CFG.get("embedding_model", "mxbai-embed-large:latest"),
        dimension=_CFG.get("embedding_dimension", 1024),
        timeout_sec=_CFG.get("embedding_http_timeout_sec", 60.0),
    )


def fetchone(sql, *args):
    """Вернуть первую строку как dict или None."""
    rows = fetch(sql, *args)
    return rows[0] if rows else None


def _setup_logging(verbose: bool) -> None:
    """Настроить логгер: без ANSI-цветов (удобно при redirect в файл), без stderr-дубликата.

    Уровень DEBUG при ``--verbose`` — печатают каждый чанк/строку и конфиг;
    иначе INFO — только этапы и итоги (ошибки/предупреждения видны всегда).
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if verbose else "INFO",
        colorize=False,
        format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
    )


# =============================================================================
# ФОРМИРОВАНИЕ ТЕКСТОВ ДЛЯ ЭМБЕДДИНГА (С ЧАНКАМИ)
# =============================================================================

def _format_content(row: dict, columns: list[str]) -> str:
    """Собрать content из указанных колонок."""
    parts = [str(row[c]).strip() for c in columns if row.get(c) and str(row[c]).strip()]
    return ". ".join(parts) if parts else ""


# =============================================================================
# ПОИСК НОВЫХ СТРОК
# =============================================================================

def _norm_pk(raw: Any) -> str:
    """Привести значение PK к канонической строке.

    ``pk_value`` в векторной таблице хранится как TEXT, а в исходной таблице
    может быть BIGINT/INTEGER/UUID. Чтобы сравнение ``existing`` (TEXT из БД)
    с ключами, полученными из источника, было согласованным, обе стороны
    приводятся к нормализованной строке (float 1.0 → "1", а не "1.0").
    """
    if raw is None:
        return ""
    if isinstance(raw, float):
        raw = int(raw)
    return str(raw)


def _get_existing_entries(source: str, source_table: str, db_table: str) -> dict[str, dict]:
    """Вернуть {str(pk_value): {synced_at, content_hash, chunk_count}} существующих в векторной таблице."""
    rows = fetch(
        f'SELECT pk_value, synced_at, content_hash, chunk_count FROM {db_table} '
        f'WHERE source = %s AND "table" = %s AND pk_value IS NOT NULL',
        source, source_table,
    )
    return {_norm_pk(r["pk_value"]): dict(r) for r in rows} if rows else {}


def _get_source_rows(
    table: str,
    pk: str,
    order_column: Optional[str] = None,
) -> list[dict]:
    """Все строки из исходной таблицы (для сравнения)."""
    sql = f"SELECT * FROM {table} ORDER BY {order_column or pk}"
    return fetch(sql) or []


def _build_search_text(row: dict, embedding_cols: list[str]) -> str:
    """Собрать search_text с метками колонок."""
    labeled = {}
    for col in embedding_cols:
        val = row.get(col)
        if val and str(val).strip():
            labeled[col] = str(val).strip()
    if not labeled:
        return ""
    return ". ".join(f"{k}: {v}" for k, v in labeled.items())


def _content_hash(text: str) -> str:
    """Хеш search_text — для обнаружения изменений без переэмбеддинга."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _normalize_cols(embedding_cols: list) -> list[str]:
    """Привести embedding_cols к списку строк-имён колонок.

    Конфигурация в public.agent_vector_index_config может содержать как
    простые имена колонок (["col1", "col2"]), так и объекты
    ([{"column": "col", "chunk": true, ...}, ...]). Функции
    `_build_search_text` и `build_chunks` ожидают только имена колонок,
    поэтому извлекаем поле `column` из объектов.
    """
    out = []
    for c in embedding_cols:
        if isinstance(c, dict):
            col = c.get("column")
            if col:
                out.append(col)
        elif isinstance(c, str):
            out.append(c)
    return out


def _rebuild_faiss(index_name: str, db_table: str, rebuilt_only_deletion: bool = False) -> None:
    """Пересобрать FAISS-индекс через единый сервисный слой (vector_index_service).

    Инвалидирует кэш провайдера, читает векторы ``index_name`` из
    ``db_table``, строит индекс и сохраняет blob в
    ``public.agent_vector_index_store``. faiss/numpy отсутствуют — не фатально:
    векторы уже в БД, поиск просто будет недоступен до установки зависимостей.
    """
    try:
        svc = VectorIndexBuildService(_CFG, str(_SKILL_ROOT))
        count = svc.rebuild_and_store(index_name, db_table)
    except (ImportError, ModuleNotFoundError) as exc:
        logger.warning(f"  ПРЕДУПРЕЖДЕНИЕ: FAISS-индекс для '{index_name}' не собран — "
                       f"отсутствует зависимость ({exc.__class__.__name__}: {exc}). "
                       f"Поиск через vector_mode будет работать только после установки faiss-cpu + numpy.")
        return
    except Exception as exc:
        logger.error(f"  ОШИБКА сборки FAISS-индекса для '{index_name}': "
                     f"{exc.__class__.__name__}: {exc}\n{traceback.format_exc()}")
        return
    if count is None:
        logger.warning(f"  FAISS-индекс '{index_name}' не пересобран: нет векторов в {db_table}")
        return
    if rebuilt_only_deletion:
        logger.info(f"  FAISS-индекс '{index_name}' пересобран (только удаление): {count} векторов")
    else:
        logger.success(f"  FAISS-индекс '{index_name}' собран в памяти и сохранён в "
                       f"public.agent_vector_index_store ({count} векторов)")


# =============================================================================
# СБОРКА ОДНОГО ИНДЕКСА
# =============================================================================

def build_index(
    index_name: str,
    index_cfg: dict,
    db_table: str,
    batch_size: int,
    default_chunk_size: int,
    default_chunk_overlap: int,
    pause_sec: float,
    embedding_retry_wait: float = 5.0,
    full_rebuild: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Собрать/обновить векторы для одного индексного источника.

    Обрабатывает сценарии:
      - NEW: строки, которых нет в audit_vectors → INSERT
      - CHANGED: строки, у которых изменился content_hash → INSERT новых
        чанков, затем DELETE старых (по несовпадающему content_hash)
      - REMOVED: строки, которые есть в audit_vectors, но удалены из
        источника (или потеряли контент для эмбеддинга) → DELETE

    content_hash строится от search_text — если ни одна embedding_cols
    не изменилась, переэмбеддинга не происходит.

    Returns:
        dict: {index_name, total, inserted, updated, deleted, errors}
    """
    from datetime import datetime, timezone

    source_table = index_cfg["source_table"]
    pk_column = index_cfg["pk"]
    src_table = index_cfg["table"]
    content_cols = index_cfg.get("content_columns", [])
    raw_embedding_cols = index_cfg.get("embedding_columns", content_cols)
    embedding_cols = _normalize_cols(raw_embedding_cols)
    track_col = index_cfg.get("track_column", pk_column)

    tag = f"[{index_name}]"
    logger.info(f"{tag} ===== СТАРТ сбора индекса (source={source_table}) =====")
    logger.debug(f"{tag} конфиг: src_table={src_table}, pk={pk_column}, "
                 f"content_cols={content_cols}, embedding_cols={embedding_cols}, "
                 f"track_col={track_col}, chunk={default_chunk_size}/{default_chunk_overlap}, "
                 f"batch={batch_size}, pause={pause_sec}s, retry_wait={embedding_retry_wait}s, "
                 f"full_rebuild={full_rebuild}, dry_run={dry_run}")

    # 1. Загружаем текущее состояние из БД и источника
    existing = {} if full_rebuild else _get_existing_entries(index_name, source_table, db_table)
    logger.info(f"{tag} Векторов в БД (уникальных pk): {len(existing)}")

    src_rows = _get_source_rows(src_table, pk_column, order_column=pk_column)
    logger.info(f"{tag} Строк в исходной таблице: {len(src_rows)}")

    # Максимальное значение track_column в источнике — для сигнатуры (PG-формат)
    max_src_track = None
    row_max = fetchone(f"SELECT MAX({track_col})::TEXT AS mx FROM {src_table}")
    if row_max and row_max.get('mx'):
        max_src_track = row_max['mx']
    logger.debug(f"{tag} max_src_track={max_src_track!r}")

    # 2. Классифицируем изменения
    new_rows: list[dict] = []        # нет в existing
    changed_rows: list[dict] = []    # в existing, content_hash изменился
    unchanged = 0
    empty_search_pks: set[str] = set()  # в existing, но контент для эмбеддинга пуст
    src_pks: set[str] = set()
    pk_row_map: dict[str, dict] = {}

    for row in src_rows:
        pk_val = _norm_pk(row[pk_column])
        pk_row_map[pk_val] = row
        src_pks.add(pk_val)

        search_text = _build_search_text(row, embedding_cols)
        if not search_text:
            # Контента для эмбеддинга нет — если раньше уже был вектор, пометим к удалению
            if pk_val in existing:
                empty_search_pks.add(pk_val)
            continue
        h = _content_hash(search_text)
        prev = existing.get(pk_val)

        if prev is None:
            new_rows.append(row)
        elif prev.get("content_hash") != h:
            changed_rows.append(row)
        else:
            unchanged += 1

    removed_pks = [pk for pk in existing if pk not in src_pks]
    logger.info(f"{tag} Классификация: новых={len(new_rows)}, изменённых={len(changed_rows)}, "
                f"без изменений={unchanged}, удалённых из источника={len(removed_pks)}, "
                f"пустой контент (к удалению)={len(empty_search_pks)}")

    to_insert = new_rows + changed_rows
    immediate_delete: set[str] = set(removed_pks) | empty_search_pks

    if not to_insert and not immediate_delete:
        logger.success(f"{tag} Изменений не обнаружено, пропускаю")
        return {
            "index_name": index_name,
            "total": 0,
            "inserted": 0,
            "updated": 0,
            "deleted": 0,
            "errors": 0,
        }

    # 3. Удаляем устаревшие векторы
    #    (только убранные из источника или потерявшие контент; изменённые — ПОСЛЕ вставки новых)
    deleted = 0
    if immediate_delete and not dry_run and not full_rebuild:
        for pk in immediate_delete:
            try:
                execute(
                    f'DELETE FROM {db_table} WHERE source = %s AND "table" = %s AND pk_value = %s',
                    index_name, source_table, pk,
                )
                deleted += 1
                logger.debug(f"{tag} удалён pk={pk}")
            except Exception as e:
                logger.warning(f"{tag} ошибка удаления pk={pk}: {e}")
        logger.info(f"{tag} Удалено устаревших pk: {deleted}")

    if full_rebuild and not dry_run:
        try:
            execute(
                f'DELETE FROM {db_table} WHERE source = %s AND "table" = %s',
                index_name, source_table,
            )
            logger.info(f"{tag} Полная очистка: удалены все векторы")
        except Exception as e:
            logger.warning(f"{tag} ошибка полной очистки: {e}")

    # 4. Строим чанки для новых/изменённых строк
    if not to_insert:
        changed = bool(immediate_delete)
        if changed and not dry_run:
            _rebuild_faiss(index_name, db_table, rebuilt_only_deletion=True)
        return {
            "index_name": index_name,
            "total": 0,
            "inserted": 0,
            "updated": 0,
            "deleted": deleted,
            "errors": 0,
        }

    all_chunks: list[dict] = []
    for row in to_insert:
        pk_val = _norm_pk(row[pk_column])
        chunks = build_chunks(row, embedding_cols, default_chunk_size, default_chunk_overlap)
        base_content = _format_content(row, content_cols)
        search_text = _build_search_text(row, embedding_cols)
        h = _content_hash(search_text)
        now = datetime.now(timezone.utc).isoformat()

        for i, c in enumerate(chunks):
            all_chunks.append({
                "pk": pk_val,
                "chunk_index": i,
                "chunk_count": len(chunks),
                "search_text": c["search_text"],
                "content": base_content + c["content_suffix"],
                "content_hash": h,
                "synced_at": now,
                "max_src_track": max_src_track,
            })
    logger.info(f"{tag} Всего чанков для вставки: {len(all_chunks)} "
                f"(строк на обработку: {len(to_insert)})")

    # 5. Отправляем по одному тексту в Ollama и вставляем
    inserted = 0
    errors = 0
    inserted_ok_pks: set[str] = set()

    for idx, chunk in enumerate(all_chunks, start=1):
        text = chunk["search_text"]

        if idx == 1 or idx % max(batch_size, 1) == 0 or idx == len(all_chunks):
            logger.info(f"{tag} Прогресс эмбеддинга: {idx}/{len(all_chunks)}")

        logger.debug(f"{tag} эмбеддинг pk={chunk['pk']} chunk={chunk['chunk_index'] + 1}/"
                     f"{chunk['chunk_count']}, text[:80]={text[:80]!r}")
        emb = get_embedding(text)
        if emb is None or not isinstance(emb, list) or len(emb) == 0:
            logger.warning(f"{tag} нет эмбеддинга для pk={chunk['pk']} "
                           f"chunk={chunk['chunk_index'] + 1}/{chunk['chunk_count']} — "
                           f"повтор через {embedding_retry_wait}с")
            time.sleep(embedding_retry_wait)
            emb = get_embedding(text)
        if emb is None or not isinstance(emb, list) or len(emb) == 0:
            logger.warning(f"{tag} ошибка эмбеддинга (после повтора) для pk={chunk['pk']} "
                           f"chunk={chunk['chunk_index'] + 1}/{chunk['chunk_count']}")
            errors += 1
            continue

        row_data = pk_row_map[chunk["pk"]]

        if dry_run:
            logger.info(f"{tag} [dry-run] pk={chunk['pk']}, chunk={chunk['chunk_index'] + 1}/"
                        f"{chunk['chunk_count']}, content={chunk['content'][:60]}...")
            inserted += 1
            inserted_ok_pks.add(chunk["pk"])
            continue

        try:
            execute(
                f"""
                INSERT INTO {db_table}
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
                json.dumps(row_data, ensure_ascii=False, default=str),
                emb,
                chunk["content_hash"],
                chunk["max_src_track"],
                chunk["synced_at"],
            )
            inserted += 1
            inserted_ok_pks.add(chunk["pk"])
            logger.debug(f"{tag} вставлен вектор pk={chunk['pk']} "
                         f"chunk={chunk['chunk_index'] + 1}/{chunk['chunk_count']}")
        except Exception as e:
            logger.warning(f"{tag} ошибка вставки pk={chunk['pk']} "
                           f"chunk={chunk['chunk_index']}: {e}")
            errors += 1

        if idx < len(all_chunks):
            time.sleep(pause_sec)

    # 6. Удаляем старые векторы изменённых строк ПОСЛЕ успешной вставки новых
    #    (избегаем потери данных, если новый эмбеддинг не удался)
    updated = 0
    if changed_rows and not dry_run:
        for row in changed_rows:
            pk = _norm_pk(row[pk_column])
            if pk not in inserted_ok_pks:
                logger.warning(f"{tag} pk={pk}: новый контент не вставлен, старый вектор сохранён")
                continue
            new_hash = _content_hash(_build_search_text(row, embedding_cols))
            try:
                execute(
                    f'DELETE FROM {db_table} WHERE source = %s AND "table" = %s '
                    f'AND pk_value = %s AND content_hash <> %s',
                    index_name, source_table, pk, new_hash,
                )
                updated += 1
                logger.debug(f"{tag} pk={pk}: старый вектор удалён после замены")
            except Exception as e:
                logger.warning(f"{tag} ошибка удаления старого вектора pk={pk}: {e}")

    logger.info(f"{tag} Итог: вставлено={inserted}, переиндексировано={updated}, "
                f"удалено={deleted}, ошибок={errors}")

    if not dry_run and (inserted > 0 or deleted > 0):
        _rebuild_faiss(index_name, db_table)

    return {
        "index_name": index_name,
        "total": len(to_insert),
        "inserted": inserted,
        "updated": updated,
        "deleted": deleted,
        "errors": errors,
    }


# =============================================================================
# Быстрая проверка изменений (при старте)
# =============================================================================

def _filter_unchanged(enabled: dict, db_table: str) -> dict:
    """
    Оставить только те индексы, где данные изменились.

    Сравнивает сигнатуру (число УНИКАЛЬНЫХ строк + MAX track_column)
    исходной таблицы и audit_vectors. Если совпадает — изменений нет.

    Используется COUNT(DISTINCT pk_value), чтобы число чанков (строк
    векторов) не отличалось от числа строк источника при чанковании.
    """
    result = {}
    for name, cfg in enabled.items():
        track_col = cfg.get("track_column", cfg.get("pk", "id"))
        src_table = cfg["table"]

        src = fetchone(
            f"SELECT COUNT(*) AS cnt, COALESCE(MAX({track_col})::TEXT, '') AS mx FROM {src_table}"
        )
        vec = fetchone(
            f"SELECT COUNT(DISTINCT pk_value) AS cnt, COALESCE(MAX(max_src_track), '') AS mx "
            f"FROM {db_table} WHERE source = %s AND pk_value IS NOT NULL",
            name,
        )

        src_sig = f"{src['cnt']}|{src['mx']}" if src else "0|"
        vec_sig = f"{vec['cnt']}|{vec['mx']}" if vec else "0|"

        if src_sig != vec_sig:
            logger.info(f"  {name}: изменения обнаружены ({src_sig} vs {vec_sig})")
            result[name] = cfg
        else:
            logger.info(f"  {name}: без изменений (пропускаем)")

    return result


# =============================================================================
# CLI
# =============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Сборка векторных индексов из исходных таблиц"
    )
    parser.add_argument("--full-rebuild", action="store_true",
                        help="Полная перестройка (все строки, не только новые)")
    parser.add_argument("--index", dest="index_name", default=None,
                        help="Собрать только конкретный индекс")
    parser.add_argument("--batch-size", type=int, default=10,
                        help="Размер батча для эмбеддинга")
    parser.add_argument("--chunk-size", type=int,
                        default=int(_CFG.get("text_chunk_size", 500)),
                        help="Размер чанка в символах (default из text_chunk_size)")
    parser.add_argument("--chunk-overlap", type=int,
                        default=int(_CFG.get("text_chunk_overlap", 80)),
                        help="Перекрытие чанков в символах (default из text_chunk_overlap)")
    parser.add_argument("--pause-sec", type=float,
                        default=float(_CFG.get("build_pause_sec", 5.0)),
                        help="Пауза между запросами эмбеддинга, сек "
                             "(default 5.0 или build_pause_sec из config.json)")
    parser.add_argument("--embedding-retry-wait", type=float, default=5.0,
                        help="При ошибке получения эмбеддинга: подождать это время (сек) и повторить "
                             "один раз (default 5.0)")
    parser.add_argument("--status", action="store_true",
                        help="Показать состояние всех индексов (кол-во векторов, размерность, актуальность)")
    parser.add_argument("--check", action="store_true",
                        help="Быстрая проверка при старте: сравнивает сигнатуру "
                             "(COUNT DISTINCT pk + MAX) и запускает синхронизацию только если данные изменились")
    parser.add_argument("--dry-run", action="store_true",
                        help="Режим проверки без вставки")
    parser.add_argument("--db-table",
                        default=None,
                        help="Таблица векторов в БД (default из конфига mode_vector_db_table)")
    parser.add_argument("--verbose", action="store_true",
                        help="Подробное логирование (уровень DEBUG): конфиг, каждый чанк/строка")

    args = parser.parse_args()
    _setup_logging(args.verbose)

    # Предупреждение о FAISS-зависимостях (для rebuild_and_store_index)
    try:
        import faiss  # noqa: F401
        import numpy  # noqa: F401
    except ImportError as exc:
        logger.warning(f"ПРЕДУПРЕЖДЕНИЕ: {exc.__class__.__name__}: {exc}")
        logger.warning("  Вектора будут вставлены в oarb.audit_vectors, но FAISS-индекс не соберётся.")
        logger.warning("  Поставьте: pip install faiss-cpu numpy")

    dsn = resolve_dsn()
    if not dsn:
        logger.error("ОШИБКА: DSN не задан. Укажите DATABASE_URL (channels.postgres.dsn в project.json)")
        sys.exit(1)
    configure(dsn)
    logger.info(f"Подключение к БД настроено (dsn={dsn.split('@')[-1] if '@' in dsn else ''})")

    from lib.services.table_registry import table_registry
    vector_table = table_registry.vector_table() or ""
    if not vector_table:
        logger.error(
            "table_registry.vector_table() пуст — зарегистрируйте skill "
            "через table_registry.register(...) или укажите --db-table."
        )
        return 1
    if "." not in vector_table:
        logger.error(f"vector_table должен быть в формате 'schema.table': {vector_table}")
        return 1
    db_schema, db_table = vector_table.split(".", 1)
    if args.db_table:
        db_table = args.db_table

    row = fetch(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        db_schema, db_table,
    )
    if not row:
        logger.error(f"ОШИБКА: таблица {vec_cfg.mode_vector_db_table} не создана")
        logger.error("Сначала выполните sql/audit_analyzer/create_oarb_audit_vectors.sql")
        sys.exit(1)

    indexes = read_vector_index_config(_CFG)
    if not indexes:
        logger.error("Нет конфигурации vector_indexes")
        sys.exit(1)

    enabled = {name: cfg for name, cfg in indexes.items()
               if cfg.get("enabled", True)}

    if not enabled:
        logger.error("Нет включённых индексов")
        sys.exit(1)

    if args.index_name:
        if args.index_name not in enabled:
            logger.error(f"Индекс '{args.index_name}' не найден или отключён")
            sys.exit(1)
        enabled = {args.index_name: enabled[args.index_name]}

    logger.info(f"Таблица векторов: {args.db_table}")
    logger.info(f"Индексы из конфига (включённые): {', '.join(enabled.keys())}")

    # --status: показать состояние, ничего не синхронизировать
    if args.status:
        for name, cfg in enabled.items():
            vec = fetchone(
                f"SELECT COUNT(*) AS cnt, "
                f"COALESCE(MAX(ARRAY_LENGTH(embedding, 1)), 0) AS dim, "
                f"COALESCE(MAX(synced_at)::TEXT, 'никогда') AS last_sync "
                f"FROM {args.db_table} WHERE source = %s",
                name,
            )
            src = fetchone(
                f"SELECT COUNT(*) AS cnt FROM {cfg['table']}"
            )
            logger.info(f"  {name}")
            logger.info(f"    векторов: {vec['cnt'] if vec else 0}")
            logger.info(f"    размерность: {vec['dim'] if vec else '-'}")
            logger.info(f"    строк в источнике: {src['cnt'] if src else 0}")
            logger.info(f"    последняя синхр.: {vec['last_sync'] if vec else '-'}")
        return

    # --check: быстрая сигнатура, синхронизация только если изменилось
    if args.check and not args.full_rebuild:
        logger.info("Режим: CHECK (при старте)")
        enabled = _filter_unchanged(enabled, args.db_table)
        if not enabled:
            logger.info("  Все индексы актуальны, синхронизация не требуется")
            return

    mode_label = "CHECK+SYNC" if args.check else "DRY-RUN" if args.dry_run else "FULL REBUILD" if args.full_rebuild else "INCREMENTAL"
    logger.info(f"Режим: {mode_label}")
    logger.info(f"Батч: {args.batch_size}, чанк: {args.chunk_size} симв., перекрытие: {args.chunk_overlap}, "
                f"пауза: {args.pause_sec}с, retry_wait: {args.embedding_retry_wait}с")
    logger.info("Источник конфига: таблица public.agent_vector_index_config (БД)")

    results = []
    for name, cfg in enabled.items():
        try:
            result = build_index(
                name, cfg,
                db_table=args.db_table,
                batch_size=args.batch_size,
                default_chunk_size=args.chunk_size,
                default_chunk_overlap=args.chunk_overlap,
                pause_sec=args.pause_sec,
                embedding_retry_wait=args.embedding_retry_wait,
                full_rebuild=args.full_rebuild,
                dry_run=args.dry_run,
            )
        except Exception as e:
            logger.error(f"Индекс '{name}' упал: {e.__class__.__name__}: {e}\n{traceback.format_exc()}")
            result = {
                "index_name": name, "total": 0, "inserted": 0,
                "updated": 0, "deleted": 0, "errors": 1,
            }
        results.append(result)

    logger.info("=" * 60)
    logger.info("ИТОГО:")
    total_inserted = sum(r.get("inserted", 0) for r in results)
    total_updated = sum(r.get("updated", 0) for r in results)
    total_deleted = sum(r.get("deleted", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)

    for r in results:
        logger.info(f"  {r['index_name']}: +{r.get('inserted', 0)} векторов, "
                    f"~{r.get('updated', 0)} обновлено, "
                    f"-{r.get('deleted', 0)} удалено, "
                    f"ошибок: {r.get('errors', 0)}")

    logger.info(f"  Вставлено: {total_inserted}, обновлено: {total_updated}, "
                f"удалено: {total_deleted}, ошибок: {total_errors}")


if __name__ == "__main__":
    main()
