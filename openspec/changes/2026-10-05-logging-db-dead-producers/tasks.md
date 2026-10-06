# Задачи

- [ ] 1.1 `## REMOVED Requirements` → `Requirement: Sync-события через
      DbLoggingService`: снять требование и все три его сценария.
- [ ] 1.2 `## MODIFIED Requirements` → `Requirement: Uniform logging behavior
      при недоступности сервиса`: заменить перечень producer'ов на
      `ContextCompactionService`, `MirrorPoller`,
      `FallbackTurnDeliveryFactory`, `RepeatGuardHook`, `PostgresChannel`.
- [ ] 1.3 То же требование: сценарий «Producer при недоступности сервиса» —
      заменить примеры вызовов, и добавить сценарий «Примеры в требовании —
      существующие классы», иначе правка примеров не защищена от следующего
      переезда.
- [ ] 1.4 `## MODIFIED Requirements` → `Requirement: Producers не читают
      logging-DB конфиг`: тот же перечень.
- [ ] 2.1 Гард: в `tests/` убедиться, что ни одна capability-спека не называет
      producer'ом класс, которого нет в дереве агента. Перечень снятых
      (`PgDuckDbSyncService`, `DuckDbCacheStore`, `PreloadService`,
      `ApplicationContext._record_sync_skipped`) — курируемый, по образцу
      `_REMOVED_AGENT_SYMBOLS` в `tests/test_docs_consistency.py`.
      **Гард начнёт проходить только после архивации change:** до неё канон
      `openspec/specs/observability/logging-db/spec.md` по праву ещё называет этих
      producer'ов, потому что дельта туда ещё не слита. Падение до архивации —
      ожидаемое состояние, а не регресс.
- [ ] 2.2 Гард: `logging-db` не содержит `event_type` из бывшего набора
      sync-событий, и `log_sync_event` не остался в каноне как действующий API.
- [ ] 3.1 Проверить, что архивация этого change не закрывает пункт **5.1**
      change `2026-10-05-vector-indexes-canon-gap`: адресат публикации сводки
      здоровья индексов остаётся открытым решением владельца.
- [ ] 3.2 Вне объёма, зафиксировано: `DbLoggingService.log_sync_event`
      (`lib/services/db_logging_service.py:1457`) — публичный метод без
      вызывающих. Решение о сносе принимает владелец отдельно от канона.
