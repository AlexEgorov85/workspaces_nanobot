# Tasks — cache snapshot reuse TTL

## 1. Настройка и типы

- [x] 1.1 Добавить `reuse_ttl_hours: float = Field(default=23.0, ge=0.0)`
  в `CacheSettings` (`lib/core/project_settings.py`), описать семантику
  (дефолт, `0` как безопасный режим, чтение по `mtime`).
- [x] 1.2 Задокументировать ключ в `project.json` (секция `gateway.cache`)
  с указанием цены решения — свежесть против времени старта.
- [x] 1.3 Отразить ключ в `AGENTS.md` (раздел Configuration) и
  `docs/DATABASE.md` (таблица настроек + lifecycle снапшота).

## 2. Чистое решение о свежести

- [x] 2.1 Реализовать `evaluate_cache_snapshot(path, ttl_hours, now=…)` →
  `CacheSnapshotDecision` в `lib/core/application_context.py`: ровно один
  `stat`, без БД и DuckDB; причины `fresh` / `stale` / `ttl_disabled` /
  `snapshot_missing`.
- [x] 2.2 Считать возраст по `mtime`, а не по дате создания; обосновать в
  докстринге (POSIX `st_ctime`, Windows-семантика).
- [x] 2.3 Добавить `format_cache_age()` для человекочитаемого лога.

## 3. Связка решения с поведением gateway

- [x] 3.1 Прочитать `gateway.cache.reuse_ttl_hours` из `ctx.settings`;
  нечитаемое значение → безопасный дефолт `0` + предупреждение, без падения
  старта.
- [x] 3.2 Удаление снапшота и `.tmp` выполнять **только** когда решение
  `reuse=False`.
- [x] 3.3 Переиспользование допускать только при `cache_store.is_ready()`;
  иначе принудительный пересоздан + предупреждение с возрастом.
- [x] 3.4 При пропуске выставить `ctx.skip_data_load`, поставить
  `cache_ready_signal` ДО старта каналов и вывести строку решения в лог.

## 4. Контракт runtime

- [x] 4.1 Объявить `ApplicationContext.skip_data_load: bool = False` как
  атрибут контракта.
- [x] 4.2 Вынести `_start_sync_or_skip()` из `start()` — так ветку можно
  проверить тестом без пула БД и schema validation; при флаге синхронизатор
  не стартует и не регистрируется в shutdown.

## 5. Тесты и приёмка

- [x] 5.1 `tests/test_cache_snapshot_ttl.py` (15): решение TTL, границы,
  `ttl=0`, отрицательное значение, `snapshot_missing`, чтение по `mtime`
  вместо `ctime`, человекочитаемый формат возраста.
- [x] 5.2 `tests/test_application_context_skip_data_load.py` (8): контракт
  `skip_data_load` на реальном `_start_sync_or_skip` + проверка того, что
  `start()` delegate'ит в метод решения.
- [x] 5.3 `tests/test_gateway_startup_gate.py` (+6): пропуск на свежем
  снапшоте, пересоздание на протухшем, `ttl=0`, сброс при неоткрытом
  снапошоте, отсутствие зависания гейта, отсутствие снапшота.
- [x] 5.4 Проверить гарды подсадкой дефекта в живой код: отсутствие сигнала
  гейту (виснет 30 с), отключённый reuse, невызванный метод решения.
- [x] 5.5 Изолировать `apply_template_overrides` в тесте контракта — реальный
  `start()` глобально меняет Jinja-loader и роняет чужой
  `test_consolidator_locale::test_missing_dir_is_noop` в полном прогоне.
- [x] 5.6 Живая приёмка на gateway с подтверждением роли OWNER по логу:
  протухший кэш → `decision=stale`, 6 таблиц загружено, 3 индекса; свежий →
  `decision=fresh reuse=True`, публикаций 0, 3 индекса из файла, снапшот
  на месте.
- [x] 5.7 Замерить, что путь переиспользования НЕ пишет в файл (`mtime` до и
  после совпадает) — иначе TTL нечестен, кэш омолаживается вечно.

## Не в этом change'е

- **Фоновое обновление по таймеру при пропуске.** До истечения TTL кэш не
  обновляется вовсе. Сейчас это сознательный размен; отдельный change, если
  понадобится.
- **Версионирование формата снапшота.** Требует миграции файла, а не TTL.
- **4 падающих теста вне этого change'а** (`TestFlushIntervalSecPropagation`,
  `test_cli_agent.py::TestHookLoader`): падают и на чистом HEAD — коммит
  `0d7368ca` запретил `profile=` в `ApplicationContext.create()`, а тесты его
  всё ещё передают. Принадлежит change'у по профилям, не этому.
- **Дрейф-конкретика `cache-architecture-alignment`** (`enable_audit`,
  `CacheProvider` ABC, fencing, heartbeat) — другой change, 38 открытых
  пунктов; сюда не смешивался.