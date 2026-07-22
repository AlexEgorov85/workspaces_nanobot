"""
Миграция векторных индексов из FAISS-файлов + JSON-метаданных
в таблицу Greenplum oarb.audit_vectors.

Заменяет локальное хранение (.faiss + _metadata.json) на БД.
После миграции достаточно указать "db_table" в config.json
и векторный поиск будет загружать данные из GP.

Запуск:
    python migrate_vectors_to_db.py

Зависимости: faiss, numpy, psycopg2 (уже есть в проекте)
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skill_config import get_vector_index_path, get_tool_config
from utils.db import configure, fetch, execute, fetchone


def _load_metadata(meta_path: str) -> dict:
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def _get_existing_ids(source: str) -> set[int]:
    """Вернуть множество pk_value уже загруженных в БД для данного source."""
    rows = fetch(
        "SELECT pk_value FROM oarb.audit_vectors WHERE source = %s AND pk_value IS NOT NULL",
        source,
    )
    return {r["pk_value"] for r in rows} if rows else set()


def _count_existing(source: str) -> int:
    row = fetchone(
        "SELECT COUNT(*) AS cnt FROM oarb.audit_vectors WHERE source = %s",
        source,
    )
    return row["cnt"] if row else 0


def migrate_index(index_dir: str, index_name: str, dry_run: bool = False) -> dict:
    """
    Перенести один FAISS-индекс из файлов в GP-таблицу.

    Args:
        index_dir: Директория с .faiss и _metadata.json файлами.
        index_name: Имя индекса (без расширения).
        dry_run: Если True — не вставлять, только подсчитать.

    Returns:
        dict со статистикой: {index_name, total, skipped, inserted, errors}
    """
    import faiss
    import numpy as np

    index_path = os.path.join(index_dir, f"{index_name}.faiss")
    meta_path = os.path.join(index_dir, f"{index_name}_metadata.json")

    if not os.path.exists(index_path):
        return {"index_name": index_name, "error": f"Файл не найден: {index_path}"}

    print(f"\n=== {index_name} ===")
    print(f"  FAISS: {index_path}")
    print(f"  Meta:  {meta_path}")

    idx = faiss.read_index(index_path)
    n_total = idx.ntotal
    print(f"  Векторов в индексе: {n_total}")

    if not os.path.exists(meta_path):
        return {"index_name": index_name, "error": f"Метаданные не найдены: {meta_path}"}

    meta = _load_metadata(meta_path)
    meta_items = meta.get("metadata", {})
    print(f"  Записей в метаданных: {len(meta_items)}")

    existing = _get_existing_ids(index_name)
    print(f"  Уже в БД: {len(existing)}")

    if not idx.supports_reconstruct():
        print(f"  ! Индекс не поддерживает reconstruct, скипаем")
        return {
            "index_name": index_name,
            "error": "Index does not support reconstruct",
        }

    inserted = 0
    skipped = 0
    errors = 0

    for i in range(n_total):
        doc_id = str(i)
        if doc_id in existing:
            skipped += 1
            continue

        item = meta_items.get(doc_id, {})
        if not item:
            skipped += 1
            continue

        try:
            vec = idx.reconstruct(i)
        except Exception:
            errors += 1
            continue

        # Нормализация — убедимся, что вектор float32
        embedding = vec.astype(np.float32).tolist()

        row_data = item.get("row", {})

        if dry_run:
            inserted += 1
            continue

        execute(
            """
            INSERT INTO oarb.audit_vectors
                (source, content, search_text, "table", pk_value, row_data, embedding)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            index_name,
            item.get("content", item.get("search_text", "")),
            item.get("search_text", ""),
            item.get("table", ""),
            item.get("pk_value"),
            json.dumps(row_data, ensure_ascii=False) if row_data else None,
            embedding,
        )
        inserted += 1

        if inserted % 100 == 0:
            print(f"  ... вставлено {inserted}")

    return {
        "index_name": index_name,
        "total": n_total,
        "inserted": inserted,
        "skipped": skipped,
        "errors": errors,
    }


def main():
    # Настройка подключения к БД из gateway_settings
    p_scripts = Path(__file__).resolve().parent
    base_dir = p_scripts.parents[3]  # workspace/
    nanobot_root = p_scripts.parents[4]  # ~/.nanobot/
    for p in [str(base_dir), str(nanobot_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)

    from config import SETTINGS as _SETTINGS
    from utils.db import configure as db_configure

    pg_cfg = _SETTINGS.get("postgresql", {})
    dsn = pg_cfg.get("dsn", "") if isinstance(pg_cfg, dict) else ""
    if not dsn:
        print("ОШИБКА: PG_DSN не задан в .env")
        sys.exit(1)

    db_configure(dsn)
    print(f"Подключение к БД: {dsn}")

    # Проверка существования таблицы
    try:
        row = fetchone(
            "SELECT COUNT(*) AS cnt FROM information_schema.tables "
            "WHERE table_schema = 'oarb' AND table_name = 'audit_vectors'"
        )
        if not row or row["cnt"] == 0:
            print()
            print("=" * 60)
            print("Таблица oarb.audit_vectors не найдена.")
            print("Сначала выполните SQL:")
            print("  create_audit_vectors_table_gp.sql")
            print("=" * 60)
            sys.exit(1)
    except Exception as e:
        print(f"Ошибка проверки таблицы: {e}")
        sys.exit(1)

    # Поиск FAISS-файлов в директории индексов
    index_dir = get_vector_index_path()
    print(f"\nДиректория индексов: {index_dir}")
    if not os.path.isdir(index_dir):
        print(f"ОШИБКА: директория не найдена: {index_dir}")
        sys.exit(1)

    faiss_files = sorted(f for f in os.listdir(index_dir) if f.endswith(".faiss"))
    index_names = [os.path.splitext(f)[0] for f in faiss_files]

    if not index_names:
        print("FAISS-файлы не найдены.")
        return

    print(f"Найдено индексов: {len(index_names)}")
    for name in index_names:
        print(f"  - {name}")

    print()
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("РЕЖИМ: dry-run (без вставки)")

    results = []
    for name in index_names:
        result = migrate_index(index_dir, name, dry_run=dry_run)
        results.append(result)
        if "error" in result:
            print(f"  ! {result['error']}")

    print("\n" + "=" * 60)
    print("ИТОГО:")
    total_inserted = sum(r.get("inserted", 0) for r in results)
    total_skipped = sum(r.get("skipped", 0) for r in results)
    total_errors = sum(r.get("errors", 0) for r in results)
    print(f"  Вставлено:  {total_inserted}")
    print(f"  Пропущено:  {total_skipped}")
    print(f"  Ошибок:     {total_errors}")

    if not dry_run and total_inserted > 0:
        print()
        print("Миграция завершена. Теперь укажите в config.json:")
        print('  "vector": {')
        print('    "enabled": true,')
        print('    "db_table": "oarb.audit_vectors"')
        print("  }")


if __name__ == "__main__":
    main()
