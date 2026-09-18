# VectorIndexService

## Назначение

Определяет логическую модель для векторных индексов: источник конфигурации, жизненный цикл индексов, доступ Skills/сервисов и поведение при ошибках. Векторные индексы управляются централизованно и предоставляются через `CacheProvider.search_vector`.

## Источник истины

- Нормативные инварианты: `docs/TARGET_ARCHITECTURE.md` (раздел Vector).
- Реализация: `docs/VECTOR_INDEXES.md` и `lib/services/vector_index_service.py` (описательный).

## Ответственность

Компонент отвечает за:
- управление конфигурацией векторных индексов;
- сборку FAISS-индексов;
- предоставление единого интерфейса векторного поиска;
- регистрацию таблиц хранения векторов.

## Граница

Владеет:
- конфигурацией векторных индексов в `project.json`.
- FAISS индексами в `<default_root>/<index_name>`.
- Таблицей хранения векторов.

Может зависеть от:
- `CacheProvider` для доступа к поиску.
- Конфигурации `gateway.vector.index.*`.

Не должен зависеть от:
- Прямого доступа Skills к FAISS индексам.
- Legacy таблицы `public.agent_vector_index_config`.

## Публичный контракт

- `VectorIndexService.build_index(index_name)` — сборка индекса.
- `CacheProvider.search_vector(index_name, query, top_k)` — поиск по индексу.
- Регистрация через `register_vector_storage(table_name)`.

## Входы

- Конфигурация из `gateway.vector.index.indexes.*`.
- Данные для индексирования.
- Запросы на векторный поиск.

## Выходы

- FAISS индексы в файловой системе.
- Результаты векторного поиска.

## Состояние

Хранит:
- Загруженные FAISS индексы в памяти (on demand).
- Пути к индексам в файловой системе.

## Зависимости

- `CacheProvider` — единая точка доступа к поиску.
- FAISS библиотека.
- PostgreSQL таблица хранения векторов.
- Конфигурация `project.json::gateway.vector.index.*`.

## Конфигурация

- `gateway.vector.index.indexes.<name>` — декларация индексов.
- `gateway.vector.index.storage_table` — таблица хранения.
- `gateway.vector.index.default_root` — корневой путь к индексам.

## Жизненный цикл

1. Чтение конфигурации из `project.json`.
2. Регистрация таблицы хранения через `register_vector_storage`.
3. Сборка индекса через `tools/build_vectors.py`.
4. Сохранение FAISS индекса в `<default_root>/<index_name>`.
5. Загрузка индекса on demand при поиске.

## Владение данными

Владеет конфигурацией индексов и путями к FAISS файлам.

## Поведение при ошибке

- Ошибка чтения конфигурации: fail fast, нет fallback на legacy table.
- Ошибка FAISS: возврат ошибки, нет silent fallback на non-FAISS backend.
- Ошибка поиска: возврат ошибки потребителю.

## Инварианты

- Конфигурация читается только из `project.json::gateway.vector.index.indexes.*`.
- Хранение векторов зарегистрировано через `register_vector_storage`.
- Единый доступ через `CacheProvider.search_vector`.
- FAISS — единственный бэкенд.

## Запрещённое поведение

Система НЕ ДОЛЖНА:
- читать конфигурацию векторных индексов из legacy таблицы `public.agent_vector_index_config` (оставлена для исторической справки, не авторитетна).
- вводить второй бэкенд векторного хранилища помимо FAISS без явного OpenSpec change.
- молча fallback на non-FAISS backend при ошибках FAISS.
- обходить `CacheProvider.search_vector` из кода Skills.
- читать legacy ключ конфигурации `gateway.vector_index.*` (удалён; runtime-mute если присутствует).

## Потребители

- Skills (через `CacheProvider.search_vector`).
- audit_analyzer Skill.
- legal_summarizer Skill.

## Реализация

Основная реализация:
- `lib/services/vector_index_service.py:VectorIndexService`
- `tools/build_vectors.py` — сборка индексов.

Связанные компоненты:
- `lib/services/cache_provider.py:CacheProvider`
- `lib/core/infra_registration.py:register_vector_storage`

## Проверка

- Архитектурные тесты: проверка отсутствия прямого доступа к FAISS из Skills.
- Тесты конфигурации: проверка чтения только из `project.json`.
- Code review: проверка отсутствия legacy configuration keys.
