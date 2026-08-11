# DEVELOPMENT — Служебные файлы разработчика

Эта директория содержит внутренние служебные скрипты и SQL-файлы для разработки и поддержки навыка `audit_analyzer`.

## 📁 Структура

| Файл | Назначение |
|------|-----------|
| `build_vectors.py` | Управление векторными индексами: перестройка, проверка, статус |
| `migrate_vectors_to_db.py` | Миграция векторных данных из файлов FAISS в PostgreSQL |
| `database.py` | Низкоуровневые утилиты работы с PostgreSQL (подключения, пулы) |
| `text_splitter.py` | Разбиение текста на чанки для векторизации |
| `VECTOR_INDEXING.md` | Подробная документация по векторной индексации |
| `create_audit_vectors_table_gp.sql` | DDL для таблицы векторов (Greenplum) |
| `create_vector_index_config_gp.sql` | DDL для таблицы конфигурации индексов (Greenplum) |

## 🚀 Использование

### Векторная индексация

```bash
# Полная перестройка индекса
python DEVELOPMENT/build_vectors.py --full-rebuild

# Проверка целостности
python DEVELOPMENT/build_vectors.py --check --index-name default_audit_index

# Статус всех индексов
python DEVELOPMENT/build_vectors.py --status

# Тестовый запуск без записи в БД
python DEVELOPMENT/build_vectors.py --full-rebuild --dry-run
```

### Миграция данных

```bash
# Миграция векторов из FAISS в PostgreSQL
python DEVELOPMENT/migrate_vectors_to_db.py --source-dir ./vectors --index-name legacy_index
```

### SQL-скрипты

SQL-файлы предназначены для ручного выполнения в БД при развёртывании или миграции:

```bash
# Для PostgreSQL
psql -h localhost -U nanobot -d nanobot -f DEVELOPMENT/create_audit_vectors_table_gp.sql

# Для Greenplum
psql -h gp-host -U nanobot -d nanobot -f DEVELOPMENT/create_vector_index_config_gp.sql
```

## 📖 Документация

- **Полная техническая документация:** [`DEVELOPMENT.md`](./DEVELOPMENT.md)
- **Векторная индексация:** [`DEVELOPMENT/VECTOR_INDEXING.md`](./DEVELOPMENT/VECTOR_INDEXING.md)
- **Пользовательская документация:** [`SKILL.md`](./SKILL.md)

## ⚠️ Важно

- Эти скрипты **не предназначены** для прямого использования конечными пользователями
- Для повседневной работы используйте CLI через `audit_analyze.bat` / `audit_analyze.sh` или `scripts/cli.py`
- Изменения в этих файлах могут повлиять на стабильность работы навыка

---

*Для разработчиков и DevOps-инженеров*
