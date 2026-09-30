## MODIFIED Requirements

### Requirement: FAISS собирается в памяти из DuckDB-снапшота

Система SHALL build FAISS-индексы через `tools/build_vectors.py` и
`lib/services/vector_index_service.py`. Система SHALL NOT персистить FAISS-блобы
(ни файлами под `gateway.vector.index.default_root`, ни строками в таблице,
заданной `gateway.vector.index.signature_table`); вместо этого FAISS-индекс
SHALL собираться в памяти по требованию из DuckDB-снапшота таблицы-источника,
заданной `gateway.vector.index.storage_table`.

Изменяется момент открытия файла. Раньше snapshot-файл удерживался открытым на
протяжении работы процесса. Теперь после пересоздания и прогона кэша файл
освобождается, поэтому при сборке индекса он MUST открываться **на чтение на
время операции** и закрываться сразу после неё.

Сам состав DuckDB-снапшота не меняется: локальный кэш сохраняется и по-прежнему
содержит доменные таблицы навыка и таблицу сырых эмбеддингов. Данные ядра в
кэше не появляются, и таблица-источник эмбеддингов по-прежнему задаётся
конфигурацией, а не зашивается в код спецификации.

#### Scenario: Сборка индекса через build_vectors.py

- **WHEN** `tools/build_vectors.py` завершил запись строк в таблицу-источник (`gateway.vector.index.storage_table`)
- **THEN** он SHALL вызвать `provider.preload_indexes(db_table)`, чтобы прогреть per-process FAISS-кэш.
- **AND** он SHALL NOT делать INSERT/UPDATE в таблицу-сигнатуру (`gateway.vector.index.signature_table`) и SHALL NOT писать файлы `<default_root>/<index_name>.faiss`.

#### Scenario: Загрузка индекса при поиске

- **WHEN** `CacheProvider.search_vector` вызывается с `index_name`
- **THEN** FAISS-индекс SHALL собираться (или читаться из `self._index_cache`) через SELECT строк таблицы-источника (`gateway.vector.index.storage_table`) по `source = ?` из DuckDB-снапшота и вызов `build_faiss_index(records, metric)`.
- **AND** payload (`content` / `search_text` / `row`) SHALL подтягиваться для каждого FAISS-hit'а через SELECT той же строки из DuckDB-снапшота по `(source, pk_value, chunk_index)`.

#### Scenario: Стоимость холодного старта

- **WHEN** первый `search_vector` для `index_name` вызван после старта процесса
- **THEN** сборка индекса SHALL завершаться за ≤ 5 секунд на эталонной рабочей станции для индексов до 20 000 векторов × 1024.

#### Scenario: Смена таблицы через настройки

- **WHEN** оператор меняет значение `gateway.vector.index.storage_table` или `gateway.vector.index.signature_table` в `project.json`
- **THEN** система SHALL использовать новые имена без изменений в коде спецификации или runtime-коде, требующих релизов.

#### Scenario: Файл открывается на время сборки индекса

- **WHEN** FAISS-индекс собирается в памяти
- **THEN** файл кэша MUST открываться в read-only режиме на время сборки
- **AND** MUST закрываться сразу после неё
- **AND** постоянного соединения на время работы процесса MUST NOT удерживаться
- **AND** блоб индекса MUST NOT сохраняться на диск

#### Scenario: Файл открывается на время поиска

- **WHEN** выполняется `CacheProvider.search_vector`
- **THEN** файл кэша MUST открываться в read-only режиме на время операции
- **AND** MUST закрываться сразу после получения результата, включая выборку
  payload по найденным hit'ам
- **AND** MUST NOT удерживаться открытым между вызовами

#### Scenario: FAISS-индекс в памяти не зависит от удержания файла

- **GIVEN** индексы прогреты в памяти на стадии запуска
- **WHEN** выполняется повторный поиск
- **THEN** сам индекс MUST обслуживаться из памяти
- **AND** обращение к DuckDB-снапшоту MUST происходить только за payload,
  а не за самими векторами

