# Tasks: Классы работ в пуле PostgreSQL

Нормативная часть — `specs/runtime/db-queue-classes/spec.md`. Здесь
разбивка на работу и **владение файлами**.

## Правило владения

Каждый файл принадлежит ровно одному исполнителю. Пересекаться нельзя:
правка чужого файла — конфликт, а не cooperation. Все пути относительно
корня репозитория.

| Исполнитель | Файлы |
|---|---|
| **A — пул** | `mcp-platform/libs/enterprise_data/audience.py` (**новый**), `mcp-platform/libs/enterprise_data/db.py` |
| **B — сервис данных** | `mcp-platform/servers/enterprise/capabilities/data/service/registry.py` (**новый**), `.../data/service/main.py`, `.../data/service/writer.py` |
| **C — конфигурация и страж** | `mcp-platform/libs/enterprise_common/settings.py`, `mcp-platform/platform.json`, `mcp-platform/servers/enterprise/server.py`, `mcp-platform/tests/test_db_job_classes.py` (**новый**), `mcp-platform/tests/test_pool_settings_seam.py`, `mcp-platform/tests/test_enterprise_data_db.py` |

Исполнители **A** и **B** не трогают `platform.json` и `settings.py`:
значения настроек объявляет только **C**, потому что стражи падают на
любое значение в коде.

## Зафиксированные интерфейсы

Изменения этих имён после старта не согласованы: три исполнителя
собраны параллельно, и каждый считает их контрактом.

### A публикует

```python
# libs/enterprise_data/audience.py
JOB_AUDIENCE_MODEL: str = "model"
JOB_AUDIENCE_RUNTIME: str = "runtime"
ALL_AUDIENCES: frozenset[str]
```

```python
# libs/enterprise_data/db.py — новые ключи контракта пула
_POOL_SPEC["reserved_workers"]: int

# новая секция настроек, симметричная пулу
_JOB_CLASS_SPEC: dict[str, type]  # statement_timeout_ms:int, queue_maxsize:int,
                                  # wait_sec:float, leases:bool
def set_job_class_config(cfg: dict[str, dict[str, Any]]) -> None: ...
def job_class_config() -> dict[str, dict[str, Any]]: ...

# неблокирующая постановка; место сейчас — выполнить, места нет — отказ
def try_submit(job: Any) -> Any: ...          # module-level
def execute_values_on(conn: Any, sql: str, rows: list[tuple]) -> None: ...

class PoolBusyError(RuntimeError): ...         # признак «занято»
```

`execute_values_on` обязана быть в `libs/enterprise_data`, потому что
`psycopg2` в capability `data` импортировать запрещено. Она принимает
**уже имеющееся** соединение и ничего не ставит в пул: вызов изнутри
job'а в пул — заведомый тупик, о чём предупреждает `db.run`.

### B публикует

```python
# servers/enterprise/capabilities/data/service/registry.py
OPERATION_AUDIENCE: dict[str, str]   # имя метода DataService -> аудитория
```

`AUDIENCE_MODEL` и `AUDIENCE_RUNTIME` в `data/service/main.py`
**остаются** (их импортирует половина tool'ов) и обязаны быть
присваиваниями констант из `audience.py`, а не литералами: определение
имён аудиторий должно быть одно.

Сигнатуры:

```python
def submit(self, job: Any, *, audience: str) -> Any: ...
def submit_transaction(self, job: Any, *, audience: str) -> Any: ...
```

Дефолта у `audience` быть **не должно** ни в одном из двух.

### C объявляет в platform.json

```json
"pool": { ..., "reserved_workers": 2 },
"job_classes": {
  "model":   { "statement_timeout_ms": 15000, "queue_maxsize": 1,  "wait_sec": 2.0, "leases": false },
  "runtime": { "statement_timeout_ms": 5000,  "queue_maxsize": 64, "wait_sec": 5.0, "leases": true }
}
```

Ключи секции обязаны совпадать с `ALL_AUDIENCES`, объявленным в A.
Секция обязана быть полной — неполная, как и неполный `pool`, останавливает
сервер на старте.

Рекомендуемые значения `pool` для этой конфигурации:
`min_conn: 3`, `max_conn: 3`, `reserved_workers: 2`,
`pool_timeout: 5.0`, `queue_maxsize: 64`.

`wait_sec` модели ненулевой **намеренно**: мест у модели ровно одно —
гибкий воркер, каким заканчивается резерв, — и при `0.0` второй
параллельный модельный вызов отказывался бы, пока первый идёт. Ноль
ожидания не давал безопасности: резерв уже гарантирует системе два места,
а `_take_job` никогда не отдаёт работу модели зарезервированному воркеру.
Очередь модели остаётся на одну работу: гибкий воркер один.

## Порядок

Волна 1 — параллельно, A / B / C, файлы не пересекаются.
Волна 2 — сборка: прогон наборов, устранение несовпадений.
Волна 3 — прогон в чистом worktree на закоммиченном состоянии.

## Чек-лист

### A — пул

- [ ] `audience.py` с тремя именами выше; `ALL_AUDIENCES` покрывает обе
      аудитории.
- [ ] `_Job` получает поле аудитории; создание во всех точках
      (`_submit`, `run`, `execute`, `fetch`, `fetchone`, `fetchval`,
      `execute_values`, аренда) передаёт его.
- [ ] У воркера объявлен набор аудиторий; первые `reserved_workers`
      созданных воркеров берут только `runtime`.
- [ ] `_take_job` пропускает неподходящую работу и продолжает искать.
- [ ] `try_submit`: место есть — выполняется, места нет — `PoolBusyError`,
      в очередь не кладёт.
- [ ] Аренда: работа аудитории с `leases: false` отклоняется до
      постановки; ожидание аренды ограничено `wait_sec` класса, а не
      бесконечно.
- [ ] `execute_values_on(conn, sql, rows)` — одна пакетная вставка.
- [ ] `set_job_class_config` / `job_class_config` с проверкой полноты и
      типов, по образцу `set_pool_config`.
- [ ] `get_stats()`: по каждой аудитории — в очереди, выполняется,
      отклонено, взято, сумма и максимум ожидания.
- [ ] Проверки на старте: `reserved_workers <= max_conn - 1`;
      при `reserved_workers >= 1` — `min_conn >= 2`.
- [ ] Ни одного значения пула в коде (существующий страж это проверяет).

### B — сервис данных

- [ ] `registry.py`: `OPERATION_AUDIENCE` покрывает **каждый** метод
      `DataService`, вызывающий `submit` или `submit_transaction`.
- [ ] Сигнатура каждого такого метода объявляет `audience: str =
      AUDIENCE_*` строковым литералом-константой.
- [ ] `submit` / `submit_transaction` без дефолта; все ~25 мест вызова
      обновлены, включая `_write_events`, который сегодня аудиторию не
      передаёт.
- [ ] `_guarded` берёт потолок класса; `data.statement_timeout_ms`
      остаётся дефолтом для класса без собственного объявления; сброс в
      `finally` сохранён.
- [ ] `_write_events` — одна `execute_values_on` вместо цикла
      `cur.execute`.
- [ ] Сброс журнала неблокирующий: `PoolBusyError` → батч возвращается в
      буфер, счётчик отказов растёт, потери не происходит.
- [ ] `EventBuffer.stats()` получает счётчик отказов, отличный от счётчика
      потерь.
- [ ] `DataService.stats()` отдаёт счётчики пула по аудиториям.
- [ ] Аудитория каждой операции согласована с правами: операция, доступная
      только рантайму, имеет аудиторию `runtime`.

### C — конфигурация и страж

- [ ] `POOL_SETTING_KEYS` получает `ENTERPRISE_POOL_RESERVED_WORKERS`;
      `set(_POOL_SPEC) == set(POOL_SETTING_KEYS)` продолжает выполняться.
- [ ] Реестр для секции `job_classes` в `settings.py`; значения нет в
      коде, объявление — `FROM_FILE`.
- [ ] `platform.json`: `reserved_workers` в `pool`, полная секция
      `job_classes` с пояснением в `_about`.
- [ ] `server.py::_apply_pool_settings` применяет обе секции; баннер
      показывает значения **и источник** (файл/окружение).
- [ ] Плохое значение (не целое, `reserved_workers == max_conn`,
      `min_conn == 1` при резерве, неполная секция) останавливает сервер
      на старте с называнием ключа.
- [ ] `tests/test_db_job_classes.py`:
      - [ ] покрытие реестра: каждый `self.submit*` внутри
            `DataService` присутствует в `OPERATION_AUDIENCE`;
      - [ ] нет мёртвых записей реестра;
      - [ ] класс в сигнатуре совпадает с реестром;
      - [ ] `submit`/`submit_transaction` без дефолта аудитории;
      - [ ] приватный API пула не упоминается вне
            `libs/enterprise_data` (кроме `set_pool_config` в
            `servers/enterprise/server.py`);
      - [ ] сам страж срабатывает на заведомо плохом коде — по образцу
            `test_service_owners_guard_detects_violation`;
      - [ ] конфигурация: резервы невозможны → сервер падает.
- [ ] `test_pool_settings_seam.py`: `POOL_BASE` дополнен новым ключом,
      `test_pool_contract_matches_the_settings` проходит.
- [ ] `test_enterprise_data_db.py`: не сломать существующие проверки пула;
      добавить проверки нового поведения (воркер не берёт чужой класс,
      `try_submit` отказывает, счётчики есть).

## Известные падения среды, не связанные с change

В чистом worktree нет `.secrets.env` (он под gitignore), поэтому часть
наборов падает по существу окружения, а не кода:

- агентский набор — `tests/test_config.py` (2) и
  `tests/test_profile_lifecycle.py` (1): `${DATABASE_URL}` не разрешён;
- платформенный набор — весь `tests/test_live_stdio_contract.py` (8):
  поднимается настоящий сервер-процесс и гибнет на незаданных
  `DB_HOST`/`DB_NAME`/`DB_PASSWORD`/`DB_PORT`.

Секреты в worktree **не копируются** — падения отличаются по тексту
ошибки.
