# Аудит: Каналы + `lib/utils`

## Сводка группы

Файлов: 14 · LOC: 4273 · классов: 5 · методов: 64 · функций: 46 · констант модуля: 12

Подсистемы: `lib/channels/*.py` (2819 LOC) и `lib/utils/*.py` (1454 LOC).

**Ключевые находки**

- `lib/channels/postgres_channel.py:1590` — обращение к несуществующему модульному `logger` (в модуле есть только `self.logger` из `BaseChannel`). Любой сбой в `subscriber.feed(msg)` превращается из «предупреждение в лог» в `NameError`, который вылетает из `send()` в поток обработки outbound. Проверено AST-скриптом: имя не связано ни на уровне модуля, ни в классе. **Функциональный баг.**
- `lib/channels/postgres_channel.py:1187-1201` (`_claim_one_single`) — мёртвая ветка повтора `error`: подзапрос выбирает `status='error' AND updated_at + 1s*error_retry_delay < NOW()`, но внешний `UPDATE … WHERE id = (подзапрос) AND status = 'pending'` отбрасывает такую строку. `0 rows` → задача с `error` **никогда** не возвращается в пул в single-режиме (дефолтном). При этом `_mark_failed` (`:1394-1400`) ставит `error` и инкрементит `retry_count`, `_report_queue` (`:651-676`) считает `error`, а `streamlit.error_window_sec` показывает окно повтора — UI обещает повтор, которого нет. **Функциональный баг.**
- `lib/channels/postgres_channel.py:1137-1141` (`_claim_one`, worker_pool) — `UPDATE … SET status='processing' WHERE id = %s` без `AND status != 'cancelled'` и без `RETURNING`-проверки. Единственный арбитр (`UNIQUE PK` на `agent_worker_claims`) защищает от двойной аренды, но не от переворачивания `cancelled` → `processing` в окне между `SELECT` (`:1120-1130`) и `UPDATE`. В отличие от single-режима (`:1201` имеет `AND status != 'cancelled'`). **Функциональный баг, узкое окно.**
- `lib/channels/postgres_channel.py:1616` — мёртвый конфиг: `_msg_ctx_max_size` читается в `__init__` (`:148`) и никогда не используется; лимит `_msg_ctx` захардкожен числом `100`. **Функциональный баг** (конфиг `channels.postgres.msg_ctx_max_size` не влияет ни на что).
- `lib/channels/postgres_channel.py:938-1023` (`_unstick_processing`) + `:148-149` — `updated_at` пользовательской строки обновляется только в момент claim; heartbeat-а во время работы агента нет. Любой turn длиннее `processing_timeout` (дефолт **120 с** в коде, `default_config` обещает `600`) переводится в `pending` под живым агентом. **Функциональный баг.** Плюс рассинхрон дефолтов: `_processing_timeout` = 120 в коде, 600 в `default_config` (`:2171-2190`).
- `lib/channels/postgres_channel.py:490-496` (heal-шаг `_reclaim_and_heal`) — ставит `status='error'` **без** инкремента `retry_count`. Так как worker_pool-ветка `_claim_one` (`:1123`) подхватывает `error` снова, путь «processing → heal → error → pending → …» **никогда** не достигает `failed` и нарушает документированный терминальный инвариант (`_max_stuck_retries`). **Функциональный баг.**
- `lib/utils/sql_safety.py:239-252` + `:178-192` — **обход SQL-guard'а через `EXPLAIN ANALYZE`**: подтверждено прогоном на sqlglot 30.17.0 — `validate_sql("EXPLAIN ANALYZE DELETE FROM t")` возвращает `None` (разрешено), так же `EXPLAIN ANALYZE UPDATE …` и `EXPLAIN ANALYZE COPY t FROM '/etc/passwd'`. Механика: `exp.Command(this="EXPLAIN", expression=Literal("ANALYZE DELETE FROM t"))`; `_parse_ast` внутреннего текста возвращает `[]` (ошибка парсинга), а `if sub_roots:` в ветке `Command` трактует пустой список как «политика не нарушена». В PostgreSQL `EXPLAIN ANALYZE` **выполняет** оператор. **Security-дефект** (сегодня не эксплуатируемый — единственный продакшн-вызов ведёт в DuckDB `read_only=True`, см. ниже, но это дыра в заявленной границе).
- `lib/utils/sql_safety.py:178-192` + `:346-352` — общий fail-open: `sqlglot.parse` поднимает `ParseError` на любой неподдерживаемой синтаксисе → `_parse_ast` возвращает `[]` → `validate_sql_report` считает, что нарушений нет, и разрешает запрос. Единственный оставшийся барьер — проверка первого слова. **Security-дефект (fail-open по дизайну).**
- `lib/utils/sql_safety.py:227-232` — правило `INTO` проверяется только у корневого узла `root.args.get("into")`. Для set-операций `into` живёт на дочернем `Select`: `validate_sql("SELECT 1 UNION ALL SELECT 2 INTO foo")` → `None`. Сегодня PostgreSQL такой синтаксис отвергает, поэтому это латентный обход, а не эксплуатируемый. **Security-замечание.**
- `lib/utils/sql_safety.py` — `validate_sql_report` (audit trail), `ValidationReport.to_dict` (`:150-157`), `SqlPolicy` (`:129-134`), `allow_catalog_access` не имеют **ни одного** продуктового потребителя: `validate_sql` вызывается только из `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py:239` и из тестов. Ничего не пишет `query_hash` в `agent_gateway_logs`. Заявленный в `docs/DATABASE.md` audit trail существует только как возвращаемое значение, которое никто не сохраняет. **Упростить / доработать.**
- `lib/utils/outbound_meta.py:48-57` — `is_stream_delta`: 0 ссылок во всём репозитории (проверено AST-скриптом по всем `*.py`). Обоснование «оставлена для обратной совместимости» не подтверждается: в установленном `nanobot` (`site-packages/nanobot`) ключа `_stream_delta` нет вообще. **Удалить.**
- `lib/channels/postgres_channel.py:67-80` / `lib/channels/redis_channel.py:104-110` — `_resolve_sfs_base` в двух каналах содержит **идентичные 4 строки** (`_resolve_sfs_base / _SFS_ENV`) внутри 14- и 7-строчных обёртках. Хэш `4e2206fc9ed84e8b` в `duplicates.md` — подтверждённое дублирование (в отличие от `1d65ca8c2e15184f`, см. ниже). **Слить в один модуль.**
- `lib/channels/postgres_channel.py:60`, `lib/utils/outbound_meta.py:60-115`, `lib/channels/redis_channel.py:320` — «единый источник истины» нарушен: `OUTBOUND_DROPPED_KEYS` (`:18-25`, 6 ключей) продублирован инлайном в `postgres_channel.send()` (`:1614-1630` три из них) плюс `FINAL_TURN_KEY or meta.get("_turn_end")` (`:1627`). **Упростить.**
- `lib/utils/table_utils.py:8-33` — `normalize_table_names` не импортируется **ни одним** продуктовым модулем: только `tests/test_table_utils.py`. Упоминания в `AGENTS.md:48` («используется в `_make_sync_services` и `tools/build_vectors.py`») устарели — `_make_sync_services` удалён вместе с `cache_ownership` (остались только упоминания в комментариях двух тестов). **Удалить** (или переиспользовать — см. рекомендацию).
- `lib/channels/postgres_channel.py:432-487` — `_reclaim_and_heal` делает N+1: на каждую возвращённую `DELETE … RETURNING` строку — отдельный `SELECT metadata` + 2–3 `UPDATE`/`DELETE`, **всё в одной транзакции**, каждые `lease_ttl*0.5` с. После массового падения воркеров один тик порождает тысячи round-trip'ов и держит транзакцию открытой, конкурируя с heartbeat. **Упростить** (batch-CTE на `RETURNING`).
- `lib/channels/priority_commands.py:34-80` — `get_priority_commands` создаёт **новый** `CommandRouter()`, чей `_priority` всегда пуст → шаги 2–3 (динамические и групповые priority-команды) мертвы, функция всегда возвращает `_DEFAULT_PRIORITY_COMMANDS`. Docstring утверждает обратное. **Упростить** или починить (передать реальный router).

**Вердикты:** Оставить 74 · Упростить 12 · Удалить 5 · Слить с 1 (из них 1 — «Слить с <модуль>», 3 пункта — с рекомендацией на перенос/правку) · `НЕ РАЗОБРАНО` 0

---

## `lib/channels/postgres_channel.py` — 2190 LOC

**Назначение.** Единственный канал, который обслуживает продовый путь «пользователь → агент → ответ»: аренда задач из таблицы сообщений, слоты параллельности, стриминг и финализация оборота, воркерский пул для нескольких машин.

**Что делает.** Три фоновых loop'а (`_lease_loop:335`, `_flush_reasoning_loop:682`, `_unstick_loop:1025`) плюс воркерская задача `MessageExchange._poll_loop`. Два режима аренды, переключаемые конфигом `channels.postgres.claim_strategy`: `single` (дефолт) и `worker_pool` (таблица `agent_worker_claims`, lease с `lease_until`, heartbeat, reclaim). Пишет в БД: статусы строк сообщений, `agent_worker_claims`, assistant-placeholder'ы, reasoning-буферы, `agent_gateway_logs` (через `DbLoggingService`). Декаодит data-URL медиа в файлы сессии (`_decode_media_from_db:254`) — побочный эффект записи на диск.

**Зачем нужен.** Единственный носитель диалога с Streamlit UI и единственный канал, который умеет воркерский пул с lease-реклеймом. Без него теряется и ворк-пул, и медиа, и `/stop`-приоритет.

**Вердикт.** `Упростить`
**Обоснование.** Функциональность востребована, но 2190 LOC содержат: 1 рантайм-баг (`NameError` на `logger`), 3 багa состояния (мёртвый `error`-retry в single, мёртвый `msg_ctx_max_size`, heal без `retry_count`), один длинный N+1 в reclaim-тике, инлайновую дубликацию `OUTBOUND_DROPPED_KEYS`, устаревший `# noqa`-импорт и рассинхрон дефолтов конфига с кодом.
**Доказательства.** Импортируется `lib/services/channel_factory.py`, точка входа `gateway.py`. Тесты: `tests/test_postgres_channel.py`, `tests/test_postgres_channel_static_audit.py`, `tests/test_redis_channel.py` (совместно), `tests/integration/test_postgres_channel_lifecycle_stress.py`, `tests/test_smoke_postgres_channel_media.py`, `tests/test_user_stop_signal_priority.py`.

### Модульные символы

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_WORKSPACE_DIR` | 62 | `Path(...)/workspace` | база для `SessionFileStore` (медиа) | `__init__:120` | Оставить |
| `console` | 64 | `rich.Console` | human-readable лог в stdout | `_activity_print:566` | Оставить |
| `_resolve_sfs_base` | 67–80 | 4 строки: имя env + override из конфига | единая точка выбора каталога `SessionFileStore` | `__init__:120` | **Слить с `redis_channel._resolve_sfs_base`** — байт-в-байт дубль `:104-110`. Объединить в `lib/utils/` (напр. `session_store_base.py`) и импортировать оба канала; побочный эффект — исчезнет риск расхождения при добавлении третьего канала |

### `class PostgresChannel` (строки 83–2190, 42 метода)

**Назначение.** Stateful-адаптер `BaseChannel` поверх PostgreSQL: аренда → диспатч → стриминг → финализация.
**Зачем нужен.** Основной канал платформы; без него gateway не обслуживает диалог.
**Вердикт.** `Упростить` (см. файл).
**Атрибуты/константы класса.** `_processing_timeout`, `_error_retry_delay` (тип `int`, а `default_config` обещает float), `_max_stuck_retries`, `_msg_ctx_max_size` (**мёртв**), `_flush_interval`, `_max_stuck_retries`, `_claim_strategy`, `_worker_id`, `_lease_ttl`, `_heartbeat_interval`, `_inflight: dict`, `_msg_ctx: dict`, `_msg_chat: dict`, `_chat_inflight: set`, `_reasoning_buffers: dict`, `_leases: set`, `_last_reclaim: float`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 108–240 | конфиг, `MessageExchange`, `SessionFileStore`, DB-пул | DI-конструктор | `channel_factory.py` | **Упростить** — читает `msg_ctx_max_size` (не используется); добавить проверку диапазонов `lease_ttl`/`heartbeat_interval` |
| `_embed_media_for_db` | 246–252 | data-URL для БД | Streamlit отдаёт вложения без локальных путей | `_finalize_turn:1840-1850` | Оставить |
| `_decode_media_from_db` | 254–264 | data-URL → файлы в `data_store/cache/sessions/<key>/` | агент получает пути, а не base64 | `_poll_once:1288` | Оставить |
| `_resolve_media_paths_and_hints` | 267–272 | делегирует `media_resolve_paths_and_hints` | единый resolver путей | `_poll_once:1293` | Оставить (тонкая обёртка; кандидат на inline при рефакторинге `_poll_once`) |
| `file_store` | 279–281 | `@property` → `SessionFileStore` | нативный интерфейс `BaseChannel` | `BaseChannel` | Оставить |
| `start` | 283–305 | поднимает 3 loop'а + activity-таймер | старт воркинга | `ChannelManager` | Оставить |
| `stop` | 307–327 | гасит задачи, `exchange.stop()`, `_release_all_leases` | graceful shutdown | `ChannelManager` | Оставить |
| `_lease_loop` | 335–377 | тик lease: продление своих, `reclaim_needed` | не даёт своему lease истёкнуть | `start:297` | Оставить |
| `_reclaim_needed` | 379–404 | эвристика «пора ли reclaim» | не гонять reclaim вхолостую | `_lease_loop:352` | Оставить |
| `_reclaim_and_heal` | 406–512 | 4 шага: reclaim по lease, heal orphan, `failed`-терминал, чистка висячих claim | восстановление после падения воркера | `_lease_loop:355` | **Упростить** — (1) N+1 запросов в одной транзакции (`:446-487`); (2) heal (`:490-496`) не инкрементит `retry_count` → `failed`-терминал недостижим для этого пути; (3) `status='error'` на heal идёт в бесконечный повтор |
| `_delete_claim` | 514–531 | снять claim по `task_id`/`assistant_msg_id` | освобождение аренды | 8 мест (`:846, 898, 1257, 1273, 1312, 1803, …`) | Оставить |
| `_release_all_leases` | 533–555 | `DELETE … WHERE worker_id = %s` на shutdown | не оставлять мёртвые claim после рестарта | `stop:320` | Оставить |
| `_activity_print` | 566–573 | однострочник активности | операторский лог | 7 мест | Оставить |
| `_lifecycle_log` | 575–603 | `agent_gateway_logs` через `DbLoggingService` | трассировка жизненного цикла задачи | 9 мест | Оставить |
| `_journal_event` | 605–641 | `try_log_event` для рантайм-событий канала | аудит отмен/восстановлений | 4 места | Оставить |
| `_preview` | 644–649 | усечение текста для лога | читаемость логов | `_activity_print` | Оставить |
| `_report_queue` | 651–676 | счётчики pending/processing/error | диагностика застоя | лог в `_lease_loop` | Оставить (но включает `error`, которые в single-режиме не перерабатываются — см. находку №2) |
| `_flush_reasoning_loop` | 682–694 | тик flush | периодический сброс буферов | `start:296` | Оставить |
| `_flush_reasoning` | 696–727 | reasoning → assistant-placeholder в БД | пользователь видит рассуждение в UI | `_flush_reasoning_loop` | Оставить |
| `_flush_live_context` | 729–768 | контекст в реальном времени | UX длинных turn'ов | `_flush_reasoning_loop:692` | Оставить |
| `poll_priority_inbound` | 774–808 | `/stop`, `/restart`, `/status` вне слотов | команда прерывания не должна ждать | `MessageExchange._poll_loop:153` | Оставить |
| `_poll_priority_once` | 810–906 | выбор + re-check + диспатч priority | защита от гонки с AW-отменой | `poll_priority_inbound:805` | Оставить |
| `poll_inbound` | 908–936 | вход в `_poll_once` / heartbeat | интерфейс `BaseChannel` | `MessageExchange._poll_loop:162` | Оставить |
| `_unstick_processing` | 938–1023 | снятие «залипших» `processing` | восстановление после падения | `_unstick_loop:1042` | **Упростить** — нет heartbeat по `updated_at` user-строки; дефолт `processing_timeout` 120 с (код) против 600 (`default_config:2171-2190`). Добавить heartbeat-таблицу/обновление `updated_at` при flush'ах |
| `_unstick_loop` | 1025–1063 | тик unstick + чистка локального состояния | периодическое восстановление | `start:301` | Оставить |
| `_claim_one` | 1065–1146 | worker_pool-аренда: `INSERT claim` (UNIQUE PK как арбитр) + `UPDATE status` | эксклюзивность воркера | `_poll_once:1240` | **Упростить** — (1) `SELECT` без `FOR UPDATE SKIP LOCKED` → N−1 воркеров крутят цикл `IntegrityError` без backoff (`:1141-1146`); (2) `UPDATE … WHERE id = %s` (`:1137-1141`) без `AND status != 'cancelled'` в отличие от single-режима `:1201` |
| `_claim_one_single` | 1148–1207 | single-аренда: `UPDATE … WHERE id=(subq)` | эксклюзивность в одном процессе | `_poll_once:1241` | **Упростить** — мёртвая ветка `error` (внешний `AND status='pending'`); без неё подзапрос `:1187-1189` можно выкинуть, а `error_retry_delay` в single станет честно нерабочим (и перестать обещать его в `_mark_failed`/`_report_queue`) |
| `_poll_once` | 1209–1342 | claim → проверка busy-chat → media → placeholder → диспатч | основной конвейер | `poll_inbound:929` | Оставить (134 LOC — кандидат на разбиение на `_prepare_turn`/`_dispatch_turn`; функциональных дефектов нет) |
| `_insert_assistant_message` | 1344–1372 | placeholder для UI | UI начинает опрос сразу | `_poll_once:1298` | Оставить |
| `_mark_failed` | 1374–1460 | `status='error'` + `retry_count++` + запись в лог | обработка падения | `_poll_once:1341`, `_finalize_turn` | Оставить (но docstring обещает возврат в пул — в single это неверно, см. №2) |
| `_drop_context_bridge` | 1462–1476 | снятие context-bridge сессии | корректный разрыв turn'а | `_mark_failed:1456`, `:1808`, `:1905`, `:2051` | Оставить |
| `send_reasoning_delta` | 1492–1523 | буферизация чанка рассуждения | стриминг CoT в UI | `ChannelManager` (upstream) | Оставить (интерфейс `BaseChannel`) |
| `send_reasoning_end` | 1525–1537 | маркер конца рассуждения | UI закрывает блок | `ChannelManager` (upstream) | Оставить (интерфейс) |
| `send` | 1566–1652 | маршрутизация outbound: reasoning/progress/final/merge/drop | единственная точка финализации оборота | `ChannelManager` | **Упростить** — (1) `logger.opt(exception=True)` на `:1590` → `NameError` (**баг**); (2) инлайновый дубль `OUTBOUND_DROPPED_KEYS` (`:1614-1630`); (3) `is_dropped(meta)` на `:1639` после `FINAL_TURN_KEY`-ветки — порядок обязателен и нигде не enforced |
| `_merge_tool_delivery` | 1654–1716 | склейка `message(...)` в один assistant-текст | не плодить строки на каждый tool-вывод | `send:1631-1637` | Оставить |
| `_finalize_turn` | 1718–1909 | транзакция «assistant completed + user completed + delete claim» | атомарное закрытие оборота | `send:1628`, `send_delta` | Оставить (192 LOC, но транзакционные границы и порядок cleanup (`:1726-1732`) задокументированы и корректны) |
| `send_delta` | 1911–1971 | стрим-чанк в БД | «печатает» ответ | `ChannelManager` | Оставить |
| `_release_slot` | 1988–2005 | слот + `_leases` + `_msg_chat` + `_chat_inflight` | идемпотентное освобождение | 5 мест | Оставить (идемпотентность через `MessageExchange.release_slot:104-113`; docstring `:1991` содержит «多次» — опечатка, китайский иероглиф в русском тексте) |
| `_cleanup_unresolvable_turn` | 2007–2051 | снятие «осиротевших» assistant-строк | чистка после `_resolve_turn_context` | `send_delta:1948` | Оставить |
| `_resolve_assistant_msg_id` | 2057–2075 | поиск placeholder'а по `reply_to`/session | восстановление контекста стрима | `_resolve_turn_context:2085` | Оставить |
| `_resolve_turn_context` | 2077–2164 | полный resolve (ctx, chat, claim, placeholder) | единая точка привязки outbound к строке БД | `send_delta:1938` | Оставить (88 LOC, много ранних `return None` — читаемо) |
| `default_config` | 2171–2190 | шаблон секции `channels.postgres` для `nanobot onboard` | конфиг по умолчанию | `nanobot` (upstream) | **Упростить** — `processing_timeout: 600` расходится с кодом (`:149`, дефолт 120); `lease_ttl: 120` против кода 30 |

### Ответы на ключевые вопросы по этому файлу

- **(а) SQL аренды.** `single`: `UPDATE … WHERE id = (SELECT … LIMIT 1) AND status='pending' AND status!='cancelled'` — гонок нет: в READ COMMITTED второй транзакционный `UPDATE` блокируется на строке и перечитывает предикат по обновлённой версии → `0 rows`. `FOR UPDATE SKIP LOCKED` здесь не нужен, но блокировка вместо пропуска — при N инстансов это лишнее ожидание. `worker_pool`: `SELECT` без блокировки, эксклюзивность обеспечивается только `UNIQUE PK` на `agent_worker_claims`; обработчик `IntegrityError` (`:1141-1146`) **без backoff и без счётчика попыток** — при высокой конкуренции N−1 воркеров делают rollback+retry вхолостую. Рекомендация: `SELECT … FOR UPDATE SKIP LOCKED` в `_claim_one` + ограниченный retry.
- **(б) Падение между claim и завершением.** Утечки lease нет: `lease_until < NOW()` + `_reclaim_and_heal`. Покрыты 4 сценария (`:406-427`). Слабые места: (i) heal-шаг не инкрементит `retry_count` → нет терминала; (ii) `_reclaim_needed` (`:379-404`) — эвристика, при низкой активности reclaim может откладываться; (iii) `_leases` исключаются по `worker_id` (`:437`) — если воркер сменил `_worker_id` (рестарт с новым id) при незакрытых lease, старые claim уйдут в reclaim по таймауту, что корректно.
- **(в) `error`/`failed`.** `failed` терминален и корректен для пути lease-reclaim (`:455-467`). `error` в single-режиме — мёртв (находка №2); в worker_pool — работает, но с двумя дефектами: heal-шаг без `retry_count` и отсутствие терминала.
- **(г) Дублирование.** `_claim_one`/`_claim_one_single` — два почти одинаковых пути (82 + 60 LOC) с расхождениями в guard'ах; `_release_all_leases` vs `_delete_claim`; `_msg_ctx.pop`/`_leases.discard` дублируются в 4 местах поверх `_release_slot`.
- **(д) Outbound-фильтры.** `is_dropped` вызывается (`:1639`), `FINAL_TURN_KEY` — (`:1627`); `redis_channel` `FINAL_TURN_KEY` намеренно не использует (задокументировано в `outbound_meta.py:27-33`). Проблема не в отсутствии, а в дублировании правил (находка №13).

---

## `lib/channels/redis_channel.py` — 378 LOC

**Назначение.** Потоковый канал: забирает сообщения из Redis-списка, кладёт ответы в outbox; используется как low-latency транспорт, а не как источник истины по воркерам.

**Что делает.** Один `poll_inbound` (BRPOPLPUSH в inflight + подтверждение LREM), `send`/`send_delta` в outbox. Медиа не трогает. Фильтрует служебные outbound через `is_dropped` (`:320`).

**Зачем нужен.** Альтернатива Postgres-каналу для сценариев, где нужен стриминг без транзакций БД.

**Вердикт.** `Оставить`
**Обоснование.** Маленький, самодостаточный, покрыт `tests/test_redis_channel.py`. Единственная правка — общий `_resolve_sfs_base` (см. выше); функциональных дефектов не найдено.
**Доказательства.** `lib/services/channel_factory.py`; тест `tests/test_redis_channel.py`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_WORKSPACE_DIR` | 101 | база для `SessionFileStore` | тот же контракт, что у PG-канала | `__init__:148` | Оставить |
| `_resolve_sfs_base` | 104–110 | дубль `postgres_channel._resolve_sfs_base` | — | `__init__:148` | **Слить с `lib/channels/postgres_channel.py:67-80`** |
| `class RedisChannel` | 113–378 | канал Redis | стриминг-транспорт | `channel_factory` | Оставить |
| `__init__` | 128–175 | конфиг, redis-клиент, `MessageExchange` | DI | `channel_factory` | Оставить |
| `file_store` | 182–184 | `@property` → `SessionFileStore` | интерфейс `BaseChannel` | `BaseChannel` | Оставить |
| `start` | 186–194 | `exchange.start()` | старт | `ChannelManager` | Оставить |
| `stop` | 196–202 | `exchange.stop()` | shutdown | `ChannelManager` | Оставить |
| `_connect` | 208–216 | ленивое подключение к Redis | не подниматься, если канал выключен | `poll_inbound:224` | Оставить |
| `poll_inbound` | 222–299 | BRPOPLPUSH → dispatch → LREM | доставка входящих | `MessageExchange._poll_loop:162` | Оставить |
| `send` | 305–349 | фильтрация + запись в outbox | доставка ответа | `ChannelManager` | Оставить (единый `is_dropped(meta)` на `:320`, без инлайн-дублей — эталон дисциплины) |
| `send_delta` | 351–358 | стрим-чанк в outbox | «печатает» ответ | `ChannelManager` | Оставить |
| `default_config` | 365–378 | шаблон секции `channels.redis` | конфиг по умолчанию | `nanobot` (upstream) | Оставить |

---

## `lib/channels/message_exchange.py` — 171 LOC

**Назначение.** Слой конкуренции между каналом и `AgentLoop`: слоты параллельности, учёт inflight, цикл поллинга.

**Что делает.** `asyncio.Semaphore(max_concurrent)` + множество `_inflight`. `_poll_loop` (`:135-171`) сначала всегда дёргает `poll_priority_inbound` (через `getattr`, `:149` — duck typing, чтобы каналы без priority-метода работали), потом обычный `poll_inbound`, если есть свободный слот. На исключении — backoff (`_error_backoff`, `:171`).

**Зачем нужен.** Без него каждый канал дублировал бы семафор, inflight и цикл. Это правильно выделенный общий слой.

**Вердикт.** `Оставить`
**Обоснование.** Компактен и корректен; `release_slot` идемпотентен (`:104-113`); дефектов не найдено. Одно наблюдение ниже.
**Доказательства.** `postgres_channel.py:59`, `redis_channel.py` (через `channel_factory`); тесты `tests/test_user_stop_signal_priority.py` (10 совпадений), `tests/contract/test_channels_base.py`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `__init__` | 50–66 | семафор, счётчики, `_poll_interval`, `_error_backoff` | состояние конкуренции | оба канала | Оставить |
| `embed` | 72–74 | ctx → `msg.metadata` | передача контекста агенту | `_poll_once:1329` | Оставить |
| `decode` | 76–78 | reverse | симметрия API | `postgres_channel._resolve_turn_context` | Оставить |
| `resolve` | 80–82 | reverse | симметрия API | `postgres_channel` | Оставить |
| `inflight` | 89–90 | `@property` → `set[str]` | проверки дублей (`:1046, 1617, 2045`) | `postgres_channel` | Оставить |
| `is_slot_free` | 92–93 | есть ли слот | гейт в `_poll_loop:161` | `_poll_loop` | Оставить |
| `acquire_slot` | 95–96 | `semaphore.acquire()` | лимит параллельности | `postgres_channel._poll_once:1316` | Оставить |
| `add_inflight` | 98–99 | пометить задачу | дедупликация | `_poll_once:1317` | Оставить |
| `discard_inflight` | 101–102 | снять метку | — | прямых вызовов нет | **Упростить** — публичный метод без вызывающих; оставить только если планируется использование, иначе удалить (проверено grep по всему репо) |
| `release_slot` | 104–113 | идемпотентное освобождение | защита от двойного release | `_release_slot:2001` | Оставить (см. риск ниже) |
| `start` | 119–125 | `create_task(_poll_loop)` | старт | оба канала | Оставить |
| `stop` | 127–133 | cancel + `suppress(CancelledError)` | graceful shutdown | оба канала | Оставить |
| `_poll_loop` | 135–171 | цикл: priority → обычный поллинг → backoff | единый ритм для каналов | `start:121` | Оставить (комментарий про 100% CPU и `asyncio.sleep(0)` на `:158` — хорошая защита) |

**Наблюдение (не дефект).** `release_slot(key)` (`:104-113`) при `key not in self._inflight` выходит **до** `semaphore.release()` — то есть рассинхронизация `semaphore` и `_inflight` приведёт к永久 утечке слота. Сейчас инвариант держится (`:1316-1317` — `acquire_slot` и `add_inflight` подряд, между ними исключение невозможно). Стоит зафиксировать инвариант комментарием или объединить пару в один метод `acquire_slot(key)`.

---

## `lib/channels/priority_commands.py` — 80 LOC

**Назначение.** Единственный список текстов, которые канал считает «командой, требующей priority-доставки» (`/stop`, `/restart`, `/status`).

**Что делает.** `get_priority_commands()` (`:34-80`) возвращает `set`: сначала жёсткий `_DEFAULT_PRIORITY_COMMANDS` (`:24`), затем (1) команды с приоритетом из **нового** `CommandRouter()` (`:36-44`), (2) групповые priority-команды из того же нового router'а (`:46-53`), (3) имена из конфига `channels.postgres.priority_commands` (`:55-79`).

**Зачем нужен.** `postgres_channel._poll_priority_once:824` вызывает его, чтобы одним `content = ANY(%s)` найти priority-кандидата; без него `/stop` ждал бы свободного слота и не смог бы прервать активный turn.

**Вердикт.** `Упростить`
**Обоснование.** Шаги (1) и (2) **всегда** пусты: `CommandRouter()` создаётся заново, его `_priority` никем не заполняется, потому что регистрация команд идёт на инстансе приложения. Функция фактически возвращает только `_DEFAULT_PRIORITY_COMMANDS` ∪ конфиг. Либо передавать реальный router (нужна зависимость от `AgentLoop`), либо убрать шаги (1)–(2) и скорректировать docstring. Docstring `:9-13` сейчас обещает поведение, которого нет.
**Доказательства.** `postgres_channel.py:824` (единственный вызывающий, локальный импорт); `tests/test_priority_commands.py` (тестирует текущее поведение, т.е. зафиксирует именно конфиг-путь).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `get_priority_commands` | 34–80 | собрать множество priority-команд | фильтр `ANY(%s)` в `_poll_priority_once` | `postgres_channel.py:824` | **Упростить** — убрать мёртвые шаги (1)–(2) (`:36-53`) либо внедрить реальный router; обновить docstring |

---

## `lib/utils/outbound_meta.py` — 126 LOC

**Назначение.** Единый контракт «что такое служебный outbound»: какие metadata-ключи означают шум, какой маркер означает финал оборота.

**Что делает.** Чистые функции без побочных эффектов. `is_outbound_final`/`is_outbound_noise` лениво импортируют `nanobot.bus.outbound_events` внутри тела (`:60-70`) под `try` — на каждый вызов.

**Зачем нужен.** Каналы, CLI-цикл и `DbLoggingBus` должны решать «показывать ли это пользователю» одним правилом; иначе разъезжается между `postgres_channel`, `redis_channel` и логгером.

**Вердикт.** `Упростить`
**Обоснование.** Один символ мёртв (`is_stream_delta`), два «логических» правила дублируются в `postgres_channel.send()`, ленивый импорт внутри hot-функции дублируется дважды.
**Доказательства.** `postgres_channel.py:60, 1627, 1639`; `redis_channel.py:320`; `lib/services/db_logging_bus.py` (`is_outbound_noise`, `is_outbound_final`, `msg_session_key`); `lib/services/context_compaction.py` (`FINAL_TURN_KEY`); `lib/services/runtime_patcher.py:1485`; тест `tests/test_outbound_meta.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `is_dropped` | 36–45 | есть ли ключ из `OUTBOUND_DROPPED_KEYS` | единый фильтр служебного | `postgres_channel:1639`, `redis_channel:320`, `is_outbound_noise:103` | **Оставить.** Хэш-коллизия `1d65ca8c2e15184f` с `tools/architecture_guard.py::is_factory_pattern:43-47` — **ложное срабатывание**: обе функции имеют форму `any(x in S for x in C)`, но сравнивают разные домены (metadata-ключи vs подстрока имени класса). Дублирования правил нет |
| `is_stream_delta` | 48–57 | `metadata["_stream_delta"]` | — | **0 ссылок** во всём репозитории (AST-скрипт по всем `*.py`, включая `tests/`) | **Удалить.** Обоснование «оставлена для обратной совместимости» опровергается: в установленном `nanobot` (30.17-совместимая ветка, `site-packages/nanobot`) ключ `_stream_delta` не встречается ни разу (grep по пакету); стриминг идёт через типизированный `StreamDeltaEvent` в `msg.event`. Ключа нет и в `OUTBOUND_DROPPED_KEYS` (`:18-25`), так что удаление ничего не меняет в фильтрации |
| `_typed_event` | 60–70 | достать типизированный event из msg | единый способ проверки типов | `is_outbound_final:86`, `is_outbound_noise:105` | **Упростить** — поднять ленивый импорт на уровень модуля (`try: … except ImportError` один раз); сейчас `sys.modules`-lookup выполняется на каждый вызов в hot-пути логирования |
| `is_outbound_final` | 73–91 | финальный ли outbound | разделение финал/дельта в БД-логе | `db_logging_bus.py` | Оставить |
| `is_outbound_noise` | 94–115 | шум ли outbound | не раздувать `agent_gateway_logs` | `db_logging_bus.py` | **Упростить** — первые 3 строки (`:102-104`) дублируют `is_dropped`, а с `is_outbound_final` дублируется `getattr(msg, "metadata", None) or {}` + `_typed_event`. Вынести `_meta_of(msg)` |
| `msg_session_key` | 118–126 | достать `session_key` из msg | корреляция событий | `db_logging_bus.py` | Оставить |

---

## `lib/utils/sql_safety.py` — 425 LOC

**Назначение.** Security-граница перед выполнением LLM-сгенерированного SQL: AST-политика «только чтение» на sqlglot для диалекта PostgreSQL.

**Что делает.** `validate_sql(sql) -> str | None` возвращает текст причины при нарушении и `None` при разрешении. Политика: (1) первый токен (после снятия комментариев) не из blacklist; (2) если sqlglot недоступен — regex-fallback; (3) AST-обход: `INTO` у корня, `exp.Func` в blocklist, `exp.Table` с системной схемой, ровно один statement; (4) рекурсивный разбор `EXPLAIN` с ограничением глубины 3. Плюс `normalize_sql`/`query_hash` (нормализация для отпечатка) и `format_schema` (сборка описания схемы для промпта).

**Зачем нужен.** Единственная защита от того, что агент выполнит `DROP`/`pg_sleep`/`information_schema` через `generated_sql_mode`. Второй (независимый) барьер — `_classify_sql` в `lib/services/duckdb_cache_store.py:216-250` плюс соединение DuckDB в `read_only=True`.

**Вердикт.** `Упростить` (правки обязательны — security-код)
**Обоснование.** Политика в целом написана грамотно (комментарии-строки, `INTO` через `-> "into"`, фильтрация `None`-корней, отдельный `Command`-кейс), но имеет один **реальный обход** (`EXPLAIN ANALYZE`), общий **fail-open** на ошибке парсинга и незакрытый audit trail. Это security-код — «оставить как есть» нельзя, но и переписывать не нужно: точечные правки в трёх местах.

**Доказательства.** Единственный продакшн-вызов — `workspace/skills/audit_analyzer/scripts/generated_sql_mode.py:239` (далее `db.query_sql` / `db.explain`, оба в DuckDB `read_only`). `SqlPolicy` и `allow_catalog_access` — только в тестах. Тест `tests/test_sql_safety.py` (6 совпадений по `validate_sql_report`/`SqlPolicy`).

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `SqlPolicy` | 129–134 | dataclass: blocklist функций, системные схемы, `allow_catalog_access` | расширяемость политики | `DEFAULT_POLICY:137`; `SqlPolicy(...)` — **только тесты** | **Упростить** — кастомные политики в проде не создаются; `allow_catalog_access` не выставляется нигде. Либо удалить, либо оставить как явную точку расширения с примером |
| `DEFAULT_POLICY` | 137 | продакшн-политика | единственная активная | `validate_sql_report:312` | Оставить |
| `ValidationReport` | 141–157 | результат: `allowed`, `reason`, `query_hash`, `normalized`, `used_sqlglot` | audit trail | `validate_sql_report` | Оставить как тип |
| `to_dict` | 150–157 | сериализация отчёта | запись в лог | **0 вызывающих** | **Упростить** — вместе с реальным audit trail; сейчас мёртв |
| `_first_word` | 160–161 | первый токен верхнего регистра | blacklist первых слов | `validate_sql_report:319` | Оставить |
| `_get_exp` | 164–175 | доступ к `sqlglot` + индикатор доступности | fail-soft без зависимости | `_parse_ast`, `_walk_policy_issues`, `validate_sql_report` | Оставить (проверено: `sqlglot 30.17.0` установлен, fallback в проде не срабатывает) |
| `_parse_ast` | 178–192 | `sqlglot.parse(..., read="postgres")` + `_COMMENT_RE` | AST или `[]` | `_walk_policy_issues:217` | **Упростить** — `return []` на `ParseError` (`:188-191`) превращает любую неподдерживаемую синтаксис в «нарушений нет». Минимум — различать «пусто как «валидно»» и «не разобрали» (см. замечания ниже) |
| `_func_name` | 195–212 | имя функции с учётом `sql_names()`/`Anonymous` | проверка blocklist | `_walk_policy_issues:239` | Оставить (проверено: `pg_catalog.pg_sleep(1)` и `"pg_sleep"(1)` **блокируются** — sqlglot нормализует имя) |
| `_walk_policy_issues` | 215–281 | обход AST: `INTO`, `Func`, `Table`, число statement'ов, рекурсия `EXPLAIN` | ядро политики | `validate_sql_report:352` | **Упростить** — три конкретных дефекта (см. ниже) |
| `_regex_fallback_checks` | 284–295 | запасной multi-statement контроль без sqlglot | деградация | `validate_sql_report:356` | Оставить. Замечание: `rstrip(";")` срезает **все** хвостовые `;`, поэтому `SELECT 1;;` проходит; для single-statement это безопасно, но логика «считаем `;` после последнего `;`» неочевидна — стоит комментарий |
| `validate_sql_report` | 298–361 | полный отчёт | единая точка проверки | `validate_sql:372`; `SqlPolicy`/тесты | Оставить; **переключить на `SqlPolicy`/структурировать так, чтобы вызывающий мог писать отчёт** (сейчас нет) |
| `validate_sql` | 364–376 | тонкая обёртка, контракт `None \| str` сохранён | стабильный API для скилла | `generated_sql_mode.py:239` | **Оставить** — контракт `(sql) -> str | None` соблюдён, `tests/test_sql_safety.py` его фиксирует |
| `normalize_sql` | 379–386 | схлопывание пробелов, нижний регистр | стабильный отпечаток | `validate_sql_report:352` | Оставить (внутри) |
| `query_hash` | 389–391 | sha256 от нормализованного SQL | идентификатор запроса в audit trail | `validate_sql_report:352` | **Упростить** — значение вычисляется, но **никуда не пишется**; либо включить в `agent_gateway_logs`, либо удалить |
| `format_schema` | 394–425 | текстовое описание схемы для промпта | LLM пишет SQL по схеме | `generated_sql_mode.py` (по схеме) | Оставить; замечание — имена таблиц/колонок и `comment` вставляются без экранирования, так что описание схемы (в т.ч. из данных) может содержать prompt-injection. Низкая серьёзность, но стоит экранировать/обрезать |

### Security-замечания по `sql_safety.py` (предметно)

1. **Обход через `EXPLAIN ANALYZE` — воспроизводимо.** Проверено на `sqlglot 30.17.0`:
   - `validate_sql("EXPLAIN ANALYZE DELETE FROM t")` → `None` (разрешено)
   - `validate_sql("EXPLAIN ANALYZE UPDATE t SET a=1")` → `None`
   - `validate_sql("EXPLAIN (ANALYZE, BUFFERS) DELETE FROM t")` → `None`
   - `validate_sql("EXPLAIN ANALYZE COPY t FROM '/etc/passwd'")` → `None`

   Механика: `sqlglot` парсит это как `exp.Command(this="EXPLAIN", expression=Literal(this="ANALYZE DELETE FROM t", is_string=True))`. Ветка `Command` (`:239-252`) берёт внутренний текст, `_parse_ast` на нём **возвращает `[]`** (ошибка парсинга), а условие `if sub_roots:` трактует пустой список как «поддерево чистое» → `continue` → statement разрешён. В PostgreSQL `EXPLAIN ANALYZE` выполняет оператор, а `EXPLAIN ANALYZE COPY … FROM` читает файл с сервера.

   Почему это ещё не эксплуатируется сегодня: единственный вызов `generated_sql_mode.py:239` передаёт результат в `db.query_sql`/`db.explain` — DuckDB-коннект открыт `read_only=True`, а `duckdb_cache_store._classify_sql:246-247` классифицирует `EXPLAIN` как `SELECT`. То есть эксплуатируемость Today = 0, но оба защитных слоя имеют одинаковую дыру по первому слову, и фактической защитой остаётся только режим соединения DuckDB. При подключении `validate_sql` к PostgreSQL-пути (например, для `generated_sql_mode` в режиме PG) это станет немедленной уязвимостью.

   Минимальная правка: в ветке `Command` различать `sub_roots == []` (не разобрали → **блокировать** с текстом «вложенный оператор не разобран») и `sub_roots` непустой; плюс явно запрещать `EXPLAIN ANALYZE`/`EXPLAIN (ANALYZE …)` до разбора внутреннего текста.

2. **Fail-open на неподдерживаемой синтаксисе.** `_parse_ast` (`:188-191`) глотает любое исключение и возвращает `[]`; `validate_sql_report` (`:346-352`) при пустом `real_roots` считает, что нарушений нет, и разрешает. Значит любой SQL, который парсер sqlglot 30.17.0 не понимает (а это pinned-зависимость, версия будет меняться при апгрейде), проходит мимо проверки `Func`/`Table`/`INTO`. Остаётся только проверка первого слова. Рекомендация: `_parse_ast` возвращает `None` при ошибке парсинга, а `validate_sql_report` трактует `None` как отказ (`"SQL не удалось разобрать — отказ по политике"`). Fail-closed здесь дешевле, чем fail-open.

3. **`INTO` проверяется только у корневого узла** (`:227-232`, `root.args.get("into")`). Для set-операций `into` живёт на дочернем `Select`: `validate_sql("SELECT 1 UNION ALL SELECT 2 INTO foo")` → `None`. PostgreSQL такой синтаксис отвергает, поэтому это латентно; но правило стоит ужесточить до `any(n.args.get("into") for n in root.walk())` — цена нулевая, а зависимость от поведения PG-грамматики исчезает.

4. **Проверка системных схем ограничена `exp.Table`** (`:252-267`) и по `db`/`catalog` узла. Обходы, которых в blocklist нет: функции, возвращающие системные данные, и параметры конфигурации. Например `validate_sql("SELECT current_setting('is_superuser')")` → `None`. Не критично, но blocklist функций (`DEFAULT_POLICY`) короткий: `lo_get`/`lowrite` (есть только `lo_import`/`lo_export`/`lo_unlink`), `pg_read_binary_file`, `pg_ls_dir` — стоит проверить фактический состав.

5. **Комментарии и кодировка имён — ОК.** Проверено: `/* ; DROP TABLE t */` (комментарий внутри запроса) → `SELECT 1 /* ; DROP TABLE t */` разрешён безопасно, т.к. парсер видит один statement; первый токен вычисляется по строке со снятыми `COMMENT_RE`, поэтому `/* */DROP TABLE t` блокируется. Экранирование имён через кавычки тоже проверено: `"information_schema".tables` блокируется. Обходов через комментарии и quoted-идентификаторы не найдено.

6. **Дублирование с `duckdb_cache_store._classify_sql` (`:216-250`).** Два независимых AST-классификатора, оба по первому слову, оба с одинаковой дырой `EXPLAIN`. Сейчас это «два слоя», но они не независимы по классу ошибок. Минимально — синхронизировать правило `EXPLAIN`; в идеале переиспользовать `validate_sql` в `query_sql` и оставить `_classify_sql` как дешёвый fast-path.

7. **Audit trail отсутствует фактически.** `validate_sql_report` (`:298-361`) не вызывается ни одним продуктовым модулем; `query_hash` (`:389`) и `to_dict` (`:150`) не персистятся. Утверждение в `docs/DATABASE.md` про `validate_sql_report` для audit trail не соответствует коду.

---

## `lib/utils/text_utils.py` — 96 LOC

**Назначение.** Общая подготовка текста для tool'ов и скиллов (back-compat re-export `_sanitize_value` из `audit_analyzer`).

**Что делает.** `sanitize_value` рекурсивно чистит структуры (str/int/float/bool/None/list/dict), обрезает строки по `_MAX_STR`, экранирует управляющие символы. `truncate_middle` — обрезка с многоточием в середине.

**Зачем нужен.** Единый формат вывода для `history_search` и скиллов; раньше копипаста в `audit_analyzer/scripts/output.py`.

**Вердикт.** `Оставить`
**Обоснование.** Два потребителя, покрыт `tests/test_text_utils.py`. Одна неточность в docstring (ниже).
**Доказательства.** `workspace/skills/audit_analyzer/scripts/output.py:21, 88` (`sanitize_value`); `workspace/skills/legal_summarizer/…/presenter.py:7` (импорт — проверено, используется ли символ в теле файла, требует запуска); `workspace/tools/history_search_tool.py` (`truncate_middle`).

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `sanitize_value` | 18–69 | рекурсивная очистка структуры | единый формат вывода | `output.py:88` (audit_analyzer), `presenter.py:7` (legal_summarizer) | **Оставить.** Замечание: `isinstance(obj, (int, str, bool))` (`:38`) — `bool` ⊂ `int`, третья ветка избыточна; безвредно |
| `truncate_middle` | 72–96 | обрезка «начало … середина … конец» | длинные SQL/ответы в выдаче | `history_search_tool.py` | **Оставить, поправить docstring.** `:74` обещает «жёсткий потолок длины результата (>= 4)», но функция возвращает `head + _MIDDLE_MARKER + tail`, т.е. строку **длиннее** `max_chars` примерно на `len(_MIDDLE_MARKER)`. Потолок не жёсткий — либо в docstring, либо в коде |

---

## `lib/utils/table_utils.py` — 36 LOC

**Назначение.** Канонизация списков таблиц из `project.json` в плоский `["schema.table", …]`.

**Что делает.** `normalize_table_names` (`:8-33`) принимает `[["schema","table"], …]`, `[{"schema":…,"table":…}, …]`, `["schema.table", …]`, пропускает пустые/некорректные, возвращает строки.

**Зачем нужен.** Исторически — для `db_additional_tables` и `_make_sync_services`.

**Вердикт.** `Удалить**
**Обоснование.** Продуктовых импортёров нет: проверено grep по `lib/`, `tools/`, `workspace/` — совпадений нет, единственный импортёр `tests/test_table_utils.py` (9 совпадений). Канонизация фактически выполняется в `lib/core/skill_config.py:53` (`get_db_tables`) и `lib/services/table_registry.py`, которые принимают уже плоские строки `schema.table`. Упоминания в `AGENTS.md:48` («используется в `_make_sync_services` и `tools/build_vectors.py`») **устарели**: `_make_sync_services` удалён (остались только упоминания в комментариях `tests/test_application_context_cache_lifecycle.py:82` и `tests/test_application_context_logging.py:230`), в `tools/build_vectors.py` импорта нет. Формат `[[schema, table]]` из docstring — тоже пережиток `db_additional_tables`. Удаление: файл + `tests/test_table_utils.py`; проверить `AGENTS.md:48` и `docs/table-registry.md` на упоминание. Если формат `[[schema, table]]` всё ещё поддерживается в `project.json` — сначала перенести вызов в точку чтения конфига (иначе сломается канонический формат конфига), и только потом удалять.

---

## `lib/utils/project_version.py` — 60 LOC

**Назначение.** Единое чтение версии релиза для баннера в `gateway.py`.

**Что делает.** `project_version()` (`:24-30`) → `_config_version()` (читает `project.json::project.version`), при отсутствии → `_git_version()` (`git describe --tags --abbrev=0` через subprocess с таймаутом), иначе `"unknown"`.

**Зачем нужен.** Баннер должен показывать релизный тег, а не git-состояние ветки release-*.

**Вердикт.** `Упростить`
**Обоснование.** Контракт («версия из `project.json`, git только как фолбэк») корректен и решает реальную задачу, но весь модуль существует ради 6-строчной функции и одного вызывающего; fallback через `subprocess` на каждом старте gateway добавляет зависимость от git в рантайм, который иначе git не требует.
**Доказательства.** `gateway.py` (баннер при старте). Прямого теста на `project_version` нет (`tests/test_docs_consistency.py` и `test_config_keys.py` упоминают версию косвенно).

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_PROJECT_ROOT` | 21 | корень репозитория | резолв `project.json` | `_config_version:36` | Оставить |
| `project_version` | 24–30 | публичный API | баннер | `gateway.py` | **Оставить** (единая точка — правильно) |
| `_config_version` | 33–45 | `project.json::project.version` | основной источник | `project_version:26` | Оставить |
| `_git_version` | 48–60 | `git describe` как фолбэк | подсказка при отсутствии тега | `project_version:28` | **Упростить** — кандидат на удаление: `project.version` объявлен обязательным в `AGENTS.md`, ветка `_git_version` при живом конфиге недостижима, а при мёртвом — лучше показать `"unknown"`, чем вскрывать зависимость рантайма от git. Удаление безопасно после проверки `tests/test_config_keys.py` (если `project.version` в `REQUIRED_KEYS` — фолбэк точно не нужен) |

---

## `lib/utils/duckdb_query.py` — 338 LOC

**Назначение.** Низкоуровневые утилиты выполнения SQL и сборки FAISS-индексов поверх уже открытого DuckDB-коннекта.

**Что делает.** `run_query`/`explain_query` — thin-обёртки над `conn.execute`; `build_schema` (72L) — описание таблиц кэша; `build_raw_items` (75L) — вычитка векторов/документов; `group_vector_hits` (31L) — группировка top-k по документу; `build_faiss_index` (47L) — сборка индекса в памяти; `_as_vector` (34L) — разбор строки в `np.ndarray`; `rewrite_duck_sql` — `to_char` → `strftime`.

**Зачем нужен.** Отделяет примитивы DuckDB от `CacheProvider`: чтение файла кэша наружу не торчит, персисты у индексов нет.

**Вердикт.** `Оставить`
**Обоснование.** Правильное разделение: модуль **не** открывает соединений (принимает `conn` аргументом) — значит инвариант «read-метод открывает соединение на время вызова» и «читать может только проверенное хранилище (`_is_ready`)» не нарушается: обе ответственности лежат на вызывающем (`DuckDbCacheStore._read_conn`). Дублирования `_read_conn` нет. Замечания — только по гигиене.
**Доказательства.** `lib/services/duckdb_cache_store.py` (`build_schema`, `run_query`, `explain_query`, `build_faiss_index`, `build_raw_items`, `group_vector_hits`); `rewrite_duck_sql` — только внутри модуля; `_as_vector` — `duckdb_cache_store` + `tests/test_duckdb_cache_store.py`. Тесты косвенные (через `test_duckdb_cache_store.py`); прямого `tests/test_duckdb_query.py` нет.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `REWRITE_TO_CHAR` | 23 | карта `to_char` → `strftime` | переносимость SQL PG→DuckDB | `rewrite_duck_sql:28` | Оставить |
| `rewrite_duck_sql` | 26–29 | текстовая замена по карте | порт SQL со скилла | внутри модуля | Оставить |
| `run_query` | 32–56 | `conn.execute` + `fetchall` → dict | базовый read-примитив | `duckdb_cache_store`, `build_raw_items` | Оставить |
| `explain_query` | 59–68 | `EXPLAIN <sql>` | проверка плана | `generated_sql_mode` → `db.explain` | Оставить. **Замечание:** оборачивает пользовательский SQL в `EXPLAIN {sql}` без собственной проверки. Для `sql = "EXPLAIN ANALYZE DELETE …"` получится `EXPLAIN EXPLAIN ANALYZE …` (ошибка), т.е. безопасность держится на ошибке парсинга, а не на проверке. Стоит либо отклонять вход, начинающийся с `EXPLAIN`, либо валидировать через `validate_sql` (см. § по `sql_safety`) |
| `build_schema` | 71–142 | описание таблиц для LLM | контекст схемы в промпте | `duckdb_cache_store.get_schema` | Оставить (72 LOC, но это DataFrame-агрегация, упрощать нечего) |
| `build_raw_items` | 145–219 | вычитка id/vector/doc | вход для FAISS | `duckdb_cache_store.preload_indexes` | Оставить |
| `group_vector_hits` | 222–252 | top-k по документу | формат выдачи `vector_search` | `duckdb_cache_store.search_vector` | Оставить |
| `build_faiss_index` | 255–301 | FAISS в памяти | поиск без персиста | `vector_index_service` / `duckdb_cache_store.preload_indexes` | Оставить |
| `_as_vector` | 304–337 | строка → `np.ndarray` | десериализация эмбеддингов | `build_faiss_index`, `duckdb_cache_store`, тесты | Оставить |

**Замечание по модулю.** Docstring (`:1-5`) ссылается на `PostgresDuckDbProvider` — класс удалён вместе с `pg_duckdb_sync_service` (change `drop-local-cache-read-from-pg`), ссылка битая. Обновить docstring.

---

## `lib/utils/retry.py` — 70 LOC

**Назначение.** Единый retry с exponential backoff вместо копипаст в модулях.

**Что делает.** `retry_on_exception(fn, *, exceptions, max_retries=3, base_delay=0.5, max_delay=15.0, on_retry=None, label="")` — синхронный повтор через `time.sleep`, «retry-и-пере-raise»: после исчерпания попыток пробрасывает последнее исключение.

**Зачем нужен.** Два реальных потребителя: `lib/services/cache_provider_impl.py:300-310` (эмбеддинги) и `lib/services/llm_client.py:151-168` (HTTP). Оба — сетевые вызовы, где backoff уместен.

**Вердикт.** `Упростить`
**Обоснование.** Смысл нужен, но реализация содержит неточности в контракте и docstring'ах, а модуль смешивается с остальной кодовой базой по системе логирования.
**Доказательства.** `cache_provider_impl.py:300,303`; `llm_client.py:28,168`. Прямого теста нет.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `retry_on_exception` | 25–70 | повтор `fn` при исключениях из списка | сетевые вызовы | `cache_provider_impl.py:303`, `llm_client.py:168` | **Упростить** (см. замечания) |

**Замечания (по вопросу 7 брифа).**

1. **Исключения.** Ретраит **только** переданный кортеж. `KeyboardInterrupt`/`SystemExit` не глотаются — они `BaseException`, а не подклассы `Exception` (это верно). Но `cache_provider_impl.py:305` передаёт `exceptions=(Exception,)` → **ретраятся в том числе ошибки конфигурации и программные баги** (`KeyError`, `TypeError`, `ConfigurationError`), что растягивает падение в 3–5 раз (до 16 с на попытку при `max_delay=16.0`) и маскирует реальную причину. Рекомендация: сузить до сетевых (`httpx.HTTPError`, `ConnectionError`, `TimeoutError`).
2. **Потолок задержки.** `max_delay` применяется **после** `time.sleep(delay)` (`:60-64` в теле цикла), т.е. первый sleep всегда равен `base_delay` и **не ограничен** `max_delay`. При `base_delay > max_delay` контракт «потолок задержки» нарушается на первой же попытке. Исправить: `delay = min(delay, max_delay)` перед первым sleep.
3. **Имя `max_retries` ≠ семантика.** `range(1, max_retries + 1)` → `max_retries` — это **число попыток**, а не повторов. Docstring (`:27-29`) формулирует верно («максимум попыток»), имя — нет. Переименовать в `max_attempts` (2 вызывающих, обновить).
4. **Комментарий «Недостижимо» врёт.** (`:65`) — ветка `return fn()` достижима при `max_retries <= 0` (цикл пуст). Правда, поведение при этом корректное (одна попытка без retry), но пометка «недостижимо» дезинформирует.
5. **Побочный эффект `on_retry`.** Если `on_retry` вернул кастомную задержку, она всё равно удваивается на следующей итерации (`:60`). Не документировано.
6. **Логирование.** Модуль использует stdlib `logging` (`:15, 20`), тогда как весь проект — loguru (`configure_loguru` в `gateway.py:310`). Retry-предупреждения не попадают в loguru-лог/файл и уходят в handler-of-last-resort. Заменить на loguru для консистентности.
7. **Блокирующий sleep.** `time.sleep` в async-процессе. `get_embedding` (`cache_provider_impl.py:303`) вызывается из путей, которые могут исполняться в event loop — блокировка цикла до 16 с. Не проверено, оборачивается ли вызов в `to_thread`; требует проверки в подсистеме services.

---

## `lib/utils/node_access.py` — 49 LOC

**Назначение.** Типобезопасный доступ к вложенным узлам конфигурации (`SETTINGS`) без цепочек `.get()`.

**Что делает.** `get_path(obj, *keys, default=None)` (`:13-33`) идёт по цепочке, поддерживая `Mapping`, объекты с атрибутами и `_LazySettings`-прокси. `get_settings_section(node, key, default=None)` (`:36-49`) возвращает секцию, если это dict, иначе default (или `{}`).

**Зачем нужен.** Убирает повторяющуюся защиту «а есть ли секция и это ли dict» в трёх местах.

**Вердикт.** `Оставить`
**Обоснование.** Обоснованно, покрыт `tests/test_utils_*`, мал. Замечание: `get_settings_section` **молча** возвращает `{}` для не-dict секции — если `SETTINGS` когда-нибудь перестанет быть `AttrDict` (а `config.py:498` показывает, что это прокси), три вызывающих молча получают пустую секцию и канал не включится без ошибки. Стоит логировать предупреждение.
**Доказательства.** `lib/services/channel_factory.py:18`, `lib/services/config_service.py`, `lib/services/runtime_patcher.py`; тесты `tests/test_utils_db.py`, `tests/test_project_settings.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `get_path` | 13–33 | безопасный обход вложенной структуры | убирает повторяющиеся guard'ы | `get_settings_section:44`, 2 вызывающих | Оставить |
| `get_settings_section` | 36–49 | секция конфига или `{}` | типизация `channels.*` | `channel_factory.py:18`, `config_service`, `runtime_patcher` | **Оставить** с замечанием выше (молчаливое `{}`) |

---

## `lib/utils/logging_utils.py` — 44 LOC

**Назначение.** Конфигурация loguru на старте gateway (уровень, формат, файл, ротация).

**Что делает.** `configure_loguru(...)` (`:14-44`) — вызывает `logger.remove()`, добавляет stderr-синк и (опционально) файловый sink с `rotation="50 MB"`, `retention="7 days"`. Побочный эффект: **глобальная перенастройка** loguru (затрагивает всё приложение и любые импортировавшие модули).

**Зачем нужен.** Единый формат логов gateway; без него дефолтный loguru-вывод не ротируется и не пишется в файл.

**Вердикт.** `Оставить`
**Обоснование.** Вызывается один раз из `gateway.py:310`, функция компактна и соответствует своей задаче. Глобальный побочный эффект — намеренный и уместный в точке входа. Прямого теста нет.
**Доказательства.** `gateway.py:310`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `configure_loguru` | 14–44 | настроить sinks loguru | логирование в файл + ротация | `gateway.py:310` | Оставить |

---

## `lib/utils/windows_terminal.py` — 210 LOC

**Назначение.** Включение ANSI/VT-последовательностей в Windows-консоли (PowerShell/cmd), где по умолчанию вывод plain.

**Что делает.** `enable_vt` (`:74-102`) включает `ENABLE_VIRTUAL_TERMINAL_PROCESSING` через ctypes на `STDOUT`/`STDERR`; `install_ansi_stripper` (`:119-165`) вешает `sys.stdout = AnsiStripper(...)`, который вырезает escape-последовательности; `ensure_console_colors` (`:171-210`) — «умная» точка входа: сначала пробует VT, при неудаче ставит stripper. Модуль импортируется только на Windows и деградирует в no-op на других ОС. Побочный эффект — подмена `sys.stdout`.

**Зачем нужен.** Без него CLI-агент на Windows печатает мусор от `rich`/цветных логов.

**Вердикт.** `Упростить`
**Обоснование.** Функциональность востребована, но две из шести функций — тонкие обёртки с одним вызывающим, а `ensure_console_colors` дублирует логику двух других (выбирает между «включить» и «вырезать»). Тест `tests/test_windows_terminal.py` есть.
**Доказательства.** `cli_agent.py` (`ensure_console_colors`), `gateway.py` (`ensure_console_colors`); остальные функции — только внутри модуля и в тестах.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `ENABLE_VIRTUAL_TERMINAL_PROCESSING` | 28 | Win32-флаг | включить VT | `enable_vt:81` | Оставить |
| `_STDOUT_HANDLE` / `_STDERR_HANDLE` | 30–31 | Win32 handle'ы | тот же | `enable_vt` | Оставить |
| `_ANSI_RE` | 33–37 | regex escape-последовательностей | stripper | `install_ansi_stripper` | Оставить |
| `_console_mode` | 40–58 | чтение/запись режима консоли | низкоуровневый доступ | `is_vt_enabled`, `enable_vt` | Оставить |
| `is_vt_enabled` | 61–71 | VT уже включён? | не вызывать `SetConsoleMode` дважды | `ensure_console_colors:180` | Оставить |
| `enable_vt` | 74–102 | включить VT | предпочтительный путь | `ensure_console_colors:189` | Оставить |
| `is_windows_console` | 105–116 | Windows + настоящая консоль | guard для CI/pipe | `install_ansi_stripper:126`, `ensure_console_colors:174` | Оставить |
| `install_ansi_stripper` | 119–165 | подмена `sys.stdout` на фильтр | fallback, когда VT недоступен | `ensure_console_colors:203` | Оставить. Побочный эффект (глобальная подмена `sys.stdout`) задокументирован в `:119-127` |
| `_WARNED` | 168 | флаг «уже предупреждали» | без повторов в лог | `ensure_console_colors:207` | Оставить |
| `ensure_console_colors` | 171–210 | публичная точка входа | вызывается из CLI/gateway | `cli_agent.py`, `gateway.py` | **Упростить** — 40 LOC диспетчеризации поверх трёх коротких функций; выиграет, если свести к 15–20 LOC без потери поведения |

---

## Кросс-подсистемные находки

1. **Два независимых SQL-guard'а с одинаковой дырой.** `lib/utils/sql_safety.py:239-252` (skill-side) и `lib/services/duckdb_cache_store.py:216-250` (store-side) классифицируют statement по первому слову и оба относят `EXPLAIN …` к read-only. Примерку `_classify_sql` в `query_sql` (вместо дублирования) — задача для подсистемы services; из этой подсистемы фиксируется, что правило должно быть одно.
2. **Два независимых «контракта версии» проверки outbox.** `lib/utils/outbound_meta.py:18-25` и инлайн-ветки `lib/channels/postgres_channel.py:1614-1630` — расхождение возможно при добавлении ключа. Канал — единственное место, где приоритет `FINAL_TURN_KEY` > `is_dropped` нигде не enforced.
3. **Мёртвый конфиг `channels.postgres.msg_ctx_max_size`.** Объявлен в `AGENTS.md`/коде (`:148`), не используется (`:1616` хардкод `100`). Проверить, не внесён ли он в `REQUIRED_KEYS` (`tests/test_config_keys.py`).
4. **Расхождение дефолтов `default_config` и кода** в `postgres_channel`: `processing_timeout` 600 vs 120, `lease_ttl` 120 vs 30. `default_config` — шаблон для `nanobot onboard`, т.е. расхождение реально дойдёт до новых инсталляций.
5. **`AGENTS.md:48` устарел** — `normalize_table_names` описан как используемый в `_make_sync_services` и `tools/build_vectors.py`; ни того, ни другого в коде нет. Обновить при удалении `table_utils.py`.
6. **`AGENTS.md:48`** (второе упоминание) утверждает про `db_logging_service`: `flush_interval_sec` — валидный контракт; расхождений с `lib/utils` нет.
7. **`lib/utils/retry.py` использует stdlib `logging`** — единственный такой модуль в `lib/`; остальное — loguru. Влияет на `lib/services/llm_client.py` и `cache_provider_impl.py` (retry-предупреждения не попадают в loguru-лог).
8. **`import json` / `fetch` в `postgres_channel.py:33, 42`** — `fetch` помечен `# noqa: F401` с комментарием «атрибут модуля патчится тестами», но тесты патчат `fetchone` (`tests/test_postgres_channel.py:1239`), а `fetch` в модуле не используется. Устаревший noqa; кандидат на удаление импорта.

## Проверено вручную (не нашёл обходов)

- SQL multi-statement: `SELECT 1; DROP TABLE t` — блокируется; `SELECT 1;` — разрешается корректно.
- SQL комментарии: `/* ; DROP TABLE t */` внутри запроса — безопасно; `/* */DROP TABLE t` — блокируется.
- SQL кодировка имён: `"pg_sleep"(1)`, `"information_schema".tables` — блокируются.
- SQL системные схемы: `information_schema`, `pg_catalog` — блокируются.
- SQL `INTO`: `SELECT 1 INTO foo` — блокируется; вариант через set-операцию — нет (см. § безопасность, п. 3).
- `OUTBOUND_DROPPED_KEYS`: все 6 ключей применяются в `postgres_channel.send()`; `redis_channel` фильтрует через `is_dropped` без дублей.
- `MessageExchange.release_slot` идемпотентна; инвариант «`acquire_slot` немедленно за `add_inflight`» соблюдён (`postgres_channel.py:1316-1317`).
- Все вызовы `is_dropped` передают `Mapping`, а не объект сообщения (`redis_channel.py:317-320`, `postgres_channel.py:1639`).
- `retry.py`: `KeyboardInterrupt`/`SystemExit` не глотаются (только `exceptions`-кортеж).
- `table_utils.normalize_table_names`: 0 продуктовых импортёров.
- `validate_sql_report`, `ValidationReport.to_dict`, `SqlPolicy`, `allow_catalog_access`: 0 продуктовых вызывающих.
- `is_stream_delta`: 0 вызывающих; ключа `_stream_delta` нет в установленном `nanobot`.

## Не проверено

- Фактическая нагрузка и поведение `_reclaim_and_heal` под массовым падением воркеров на Greenplum (оценка N+1 — по коду, без прогона).
- Транспорт Redis-канала в проде (тесты покрывают, но реального стенда нет).
- `presenter.py` (legal_summarizer) — импорт `sanitize_value` есть, использование символа в теле не проверено.
- Асинхронность `get_embedding` (блокирующий `time.sleep` в `retry.py`) — требует проверки в подсистеме services.
