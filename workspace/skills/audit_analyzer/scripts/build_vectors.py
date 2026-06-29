"""
Инкрементальная сборка векторных индексов из исходных таблиц.

Поддерживает чанкование длинных текстов: если колонка в
embedding_columns помечена "chunk": true, её текст разбивается
на перекрывающиеся сегменты, каждый сегмент → отдельный вектор.

row_data (JSONB) всегда содержит ПОЛНУЮ строку исходной таблицы,
независимо от количества чанков.

Конфиг читается из oarb.vector_index_config (БД) с fallback
на секцию vector_indexes в config.json.

Запуск:
    # Инкрементальное обновление (только новые строки)
    python build_vectors.py

    # Полная перестройка
    python build_vectors.py --full-rebuild

    # Показать что будет добавлено
    python build_vectors.py --dry-run

    # Настроить чанкование
    python build_vectors.py --chunk-size 800 --chunk-overlap 150

Может вызываться по cron:
    0 3 * * * cd /path && python build_vectors.py >> build.log 2>&1
"""

import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

# Принудительно UTF-8 для консоли (надо chcp 65001 в PowerShell)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8')

_SCRIPTS_DIR = Path(__file__).resolve().parent
_WORKSPACE_DIR = _SCRIPTS_DIR.parents[2]
_NANOBOT_DIR = _SCRIPTS_DIR.parents[3]

for p in [str(_WORKSPACE_DIR), str(_NANOBOT_DIR)]:
    if p not in sys.path:
        sys.path.insert(0, p)

from config import get_vector_indexes, get_embedding_config
from utils.db import configure, execute, fetch
from text_splitter import build_chunks
import gateway_settings


def fetchone(sql, *args):
    """Вернуть первую строку как dict или None."""
    rows = fetch(sql, *args)
    return rows[0] if rows else None


# =============================================================================
# EMBEDDING
# =============================================================================

def _get_embeddings(texts: list[str], retries: int = 3) -> Optional[list[list[float]]]:
    """Получить эмбеддинги для списка текстов через Ollama /api/embed."""
    cfg = get_embedding_config()
    url = cfg.get("base_url")
    model = cfg.get("model", "mxbai-embed-large:latest")

    if not url:
        print("  ОШИБКА: embedding base_url не задан в config.json")
        return None

    payload = {"model": model, "input": texts}

    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=120) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()

            embeddings = data.get("embeddings")
            if embeddings and isinstance(embeddings, list) and len(embeddings) > 0:
                return embeddings

            print(f"  Пустой ответ эмбеддинга")
            return None

        except Exception as e:
            if attempt < retries:
                delay = 2 ** attempt
                print(f"  Retry {attempt}/{retries} через {delay}с: {e}")
                time.sleep(delay)
            else:
                print(f"  ОШИБКА эмбеддинга после {retries} попыток: {e}")
                return None

    return None


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

def _get_existing_entries(source: str, source_table: str, db_table: str) -> dict[int, dict]:
    """Вернуть {pk_value: {synced_at, content_hash, chunk_count}} существующих в векторной таблице."""
    rows = fetch(
        f'SELECT pk_value, synced_at, content_hash, chunk_count FROM {db_table} '
        f'WHERE source = %s AND "table" = %s AND pk_value IS NOT NULL',
        source, source_table,
    )
    return {r["pk_value"]: dict(r) for r in rows} if rows else {}


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
    full_rebuild: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Собрать/обновить векторы для одного индексного источника.

    Обрабатывает три сценария:
      - NEW: строки, которых нет в audit_vectors → INSERT
      - CHANGED: строки, у которых изменился content_hash → DELETE + INSERT
      - DELETED: строки, которые есть в audit_vectors, но удалены из источника → DELETE

    content_hash строится от search_text — если ни одна embedding_cols
    не изменилась, переэмбеддинг не происходит.

    Returns:
        dict: {index_name, total, inserted, updated, deleted, errors}
    """
    from datetime import datetime, timezone

    source_table = index_cfg["source_table"]
    pk_column = index_cfg["pk"]
    src_table = index_cfg["table"]
    content_cols = index_cfg.get("content_columns", [])
    embedding_cols = index_cfg.get("embedding_columns", content_cols)

    print(f"\n=== {index_name} ({source_table}) ===")

    track_col = index_cfg.get("track_column", pk_column)

    # 1. Загружаем текущее состояние из БД и источника
    existing = {} if full_rebuild else _get_existing_entries(index_name, source_table, db_table)
    src_rows = _get_source_rows(src_table, pk_column, order_column=pk_column)
    src_pks = set()

    # Максимальное значение track_column в источнике — для сигнатуры (PG-формат)
    max_src_track = None
    row_max = fetchone(f"SELECT MAX({track_col})::TEXT AS mx FROM {src_table}")
    if row_max and row_max.get('mx'):
        max_src_track = row_max['mx']

    print(f"  Векторов в БД (уникальных pk): {len(existing)}")
    print(f"  Строк в исходной таблице: {len(src_rows)}")

    # 2. Классифицируем изменения
    to_insert: list[dict] = []   # строки для векторизации
    to_delete: list[int] = []    # pk_value для удаления
    pk_row_map: dict[int, dict] = {}

    for row in src_rows:
        pk_val = row[pk_column]
        if isinstance(pk_val, float):
            pk_val = int(pk_val)
        pk_row_map[pk_val] = row
        src_pks.add(pk_val)

        search_text = _build_search_text(row, embedding_cols)
        if not search_text:
            continue
        h = _content_hash(search_text)
        prev = existing.get(pk_val)

        if prev is None:
            to_insert.append(row)
        elif prev.get("content_hash") != h:
            to_delete.append(pk_val)
            to_insert.append(row)

    # Удалённые из источника
    for pk_val in existing:
        if pk_val not in src_pks:
            to_delete.append(pk_val)

    print(f"  Новых: {sum(1 for r in to_insert if r[pk_column] not in existing)}")
    print(f"  Изменённых: {sum(1 for r in to_insert if r[pk_column] in existing)}")
    print(f"  Удалённых: {len(to_delete)}")

    if not to_insert and not to_delete:
        return {
            "index_name": index_name,
            "total": 0,
            "inserted": 0,
            "updated": 0,
            "deleted": 0,
            "errors": 0,
        }

    # 3. Удаляем устаревшие векторы
    deleted = 0
    if to_delete and not dry_run and not full_rebuild:
        for pk_val in to_delete:
            try:
                execute(
                    f'DELETE FROM {db_table} WHERE source = %s AND "table" = %s AND pk_value = %s',
                    index_name, source_table, pk_val,
                )
                deleted += 1
            except Exception as e:
                print(f"    ! Ошибка удаления pk={pk_val}: {e}")

    if full_rebuild and not dry_run:
        try:
            execute(
                f'DELETE FROM {db_table} WHERE source = %s AND "table" = %s',
                index_name, source_table,
            )
            print(f"  Полная очистка: удалены все векторы {index_name}")
        except Exception as e:
            print(f"    ! Ошибка очистки: {e}")

    # 4. Строим чанки для новых/изменённых строк
    if not to_insert:
        changed = bool(to_delete)
        if changed and not dry_run:
            try:
                from vector_mode import invalidate_cache
                invalidate_cache(index_name)
            except ImportError:
                pass
        return {
            "index_name": index_name,
            "total": 0,
            "inserted": 0,
            "updated": 0,
            "deleted": deleted,
            "errors": 0,
        }

    all_chunks: list[dict] = []
    inserted = 0
    errors = 0

    for row in to_insert:
        pk_val = row[pk_column]
        if isinstance(pk_val, float):
            pk_val = int(pk_val)

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

    print(f"  Всего чанков: {len(all_chunks)}")

    # 5. Отправляем батчами в Ollama и вставляем
    for start in range(0, len(all_chunks), batch_size):
        batch = all_chunks[start:start + batch_size]
        texts = [c["search_text"] for c in batch]

        print(f"  Батч {start // batch_size + 1}/{(len(all_chunks) - 1) // batch_size + 1}: "
              f"{len(texts)} текстов...")

        embeddings = _get_embeddings(texts)
        if embeddings is None or len(embeddings) != len(texts):
            print(f"    ! Ошибка получения эмбеддингов, батч пропущен")
            errors += len(batch)
            continue

        for chunk, emb in zip(batch, embeddings):
            row_data = pk_row_map[chunk["pk"]]

            if dry_run:
                print(f"    [dry-run] pk={chunk['pk']}, chunk={chunk['chunk_index'] + 1}/"
                      f"{chunk['chunk_count']}, content={chunk['content'][:60]}...")
                inserted += 1
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
            except Exception as e:
                print(f"    ! Ошибка вставки pk={chunk['pk']} chunk={chunk['chunk_index']}: {e}")
                errors += 1

        if start + batch_size < len(all_chunks):
            time.sleep(0.5)

    print(f"  Вставлено векторов: {inserted}, удалено pk: {deleted}, ошибок: {errors}")

    if not dry_run and (inserted > 0 or deleted > 0):
        try:
            from vector_mode import invalidate_cache, rebuild_and_store_index
            invalidate_cache(index_name)
            rebuild_and_store_index(index_name, db_table)
        except ImportError:
            pass

    return {
        "index_name": index_name,
        "total": len(to_insert),
        "inserted": inserted,
        "updated": len(to_insert) - sum(1 for r in to_insert if r[pk_column] not in existing),
        "deleted": deleted,
        "errors": errors,
    }


# =============================================================================
# Быстрая проверка изменений (при старте)
# =============================================================================

def _filter_unchanged(enabled: dict, db_table: str) -> dict:
    """
    Оставить только те индексы, где данные изменились.

    Сравнивает сигнатуру (количество строк + MAX track_column)
    исходной таблицы и audit_vectors. Если совпадает — изменений нет.
    """
    result = {}
    for name, cfg in enabled.items():
        track_col = cfg.get("track_column", cfg.get("pk", "id"))
        src_table = cfg["table"]

        src = fetchone(
            f"SELECT COUNT(*) AS cnt, COALESCE(MAX({track_col})::TEXT, '') AS mx FROM {src_table}"
        )
        vec = fetchone(
            f"SELECT COUNT(*) AS cnt, COALESCE(MAX(max_src_track), '') AS mx "
            f"FROM {db_table} WHERE source = %s",
            name,
        )

        src_sig = f"{src['cnt']}|{src['mx']}" if src else "0|"
        vec_sig = f"{vec['cnt']}|{vec['mx']}" if vec else "0|"

        if src_sig != vec_sig:
            print(f"  {name}: изменения обнаружены ({src_sig} vs {vec_sig})")
            result[name] = cfg
        else:
            print(f"  {name}: без изменений (пропускаем)")

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
    parser.add_argument("--chunk-size", type=int, default=500,
                        help="Размер чанка в символах (по умолч. 500)")
    parser.add_argument("--chunk-overlap", type=int, default=80,
                        help="Перекрытие чанков в символах")
    parser.add_argument("--status", action="store_true",
                        help="Показать состояние всех индексов (кол-во векторов, размерность, актуальность)")
    parser.add_argument("--check", action="store_true",
                        help="Быстрая проверка при старте: сравнивает сигнатуру (COUNT + MAX) "
                             "и запускает синхронизацию только если данные изменились")
    parser.add_argument("--dry-run", action="store_true",
                        help="Режим проверки без вставки")
    parser.add_argument("--db-table", default="oarb.audit_vectors",
                        help="Таблица векторов в БД")

    args = parser.parse_args()

    settings = gateway_settings.GatewaySettings()
    if not settings.pg.dsn:
        print("ОШИБКА: pg.dsn не задан в gateway_settings.py")
        sys.exit(1)
    configure(settings.pg.dsn)

    row = fetch(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'oarb' AND table_name = 'audit_vectors'"
    )
    if not row:
        print("ОШИБКА: таблица oarb.audit_vectors не создана")
        print("Сначала выполните create_audit_vectors_table_gp.sql")
        sys.exit(1)

    indexes = get_vector_indexes()
    if not indexes:
        print("Нет конфигурации vector_indexes")
        sys.exit(1)

    enabled = {name: cfg for name, cfg in indexes.items()
               if cfg.get("enabled", True)}

    if not enabled:
        print("Нет включённых индексов")
        sys.exit(1)

    if args.index_name:
        if args.index_name not in enabled:
            print(f"Индекс '{args.index_name}' не найден или отключён")
            sys.exit(1)
        enabled = {args.index_name: enabled[args.index_name]}

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
            print(f"  {name}")
            print(f"    векторов: {vec['cnt'] if vec else 0}")
            print(f"    размерность: {vec['dim'] if vec else '-'}")
            print(f"    строк в источнике: {src['cnt'] if src else 0}")
            print(f"    последняя синхр.: {vec['last_sync'] if vec else '-'}")
        return

    # --check: быстрая сигнатура, синхронизация только если изменилось
    if args.check and not args.full_rebuild:
        print("Режим: CHECK (при старте)")
        enabled = _filter_unchanged(enabled, args.db_table)
        if not enabled:
            print("  Все индексы актуальны, синхронизация не требуется")
            return

    mode_label = "CHECK+SYNC" if args.check else "DRY-RUN" if args.dry_run else "FULL REBUILD" if args.full_rebuild else "INCREMENTAL"
    print(f"Режим: {mode_label}")
    print(f"Батч: {args.batch_size}, чанк: {args.chunk_size} симв., перекрытие: {args.chunk_overlap}")
    print(f"Индексы: {', '.join(enabled.keys())}")
    print("Источник конфига: таблица oarb.vector_index_config -> config.json")

    results = []
    for name, cfg in enabled.items():
        result = build_index(
            name, cfg,
            db_table=args.db_table,
            batch_size=args.batch_size,
            default_chunk_size=args.chunk_size,
            default_chunk_overlap=args.chunk_overlap,
            full_rebuild=args.full_rebuild,
            dry_run=args.dry_run,
        )
        results.append(result)

    print("\n" + "=" * 60)
    print("ИТОГО:")
    total_inserted = sum(r.get("inserted", 0) for r in results)
    total_updated = sum(r.get("updated", 0) for r in results)
    total_deleted = sum(r.get("deleted", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)

    for r in results:
        print(f"  {r['index_name']}: +{r.get('inserted', 0)} векторов, "
              f"~{r.get('updated', 0)} обновлено, "
              f"-{r.get('deleted', 0)} удалено, "
              f"ошибок: {r.get('errors', 0)}")

    print(f"  Вставлено: {total_inserted}, обновлено: {total_updated}, "
          f"удалено: {total_deleted}, ошибок: {total_errors}")


if __name__ == "__main__":
    main()
