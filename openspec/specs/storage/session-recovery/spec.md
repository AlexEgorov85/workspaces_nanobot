# storage/session-recovery Specification

## Purpose
Lets the runtime safely recover sessions whose JSONL source is older than the PostgreSQL cold mirror (volume restore, host migration, multi-instance misconfiguration). Provides explicit, opt-in recovery modes so the gateway never silently overwrites authoritative cold-storage data.

## Scope

`agent` — подсистема **частично реализована**: из семи требований закрыто одно.

Реализовано (как инфраструктура `SessionMirror`,
`lib/gateway/mirror/`):
- «Reverse sync lag produces a logged event» — порог
  `sync_lag_threshold_seconds` (дефолт 3600), событие
  `sync_lag_exceeded`, счётчик `sync_lag_exceeded_total`;
- смежный stale-detection: `stale_tolerance_seconds` (дефолт 120), событие
  `session_stale_detected`, счётчики `stale_detected_total` и
  `stale_sync_skipped_total`. В самой спеке это требование не выделено, но
  детект — фундамент всех остальных.

Не реализовано ни в одном дереве:
- три режима восстановления (`detect-only`, `read-only-fallback`,
  `backup-and-restore`) — в `config.json` нет ни одного из них;
- read-only маркер сессии и запрет `save()` по нему;
- backup-and-restore с атомарным swap и copy-and-unlink на разных ФС;
- ограничение размера восстановления (100 МБ) и ротация бэкапов;
- admin tool восстановления — путь `tools/recover_stale_sessions.py` **в дереве
  не найден** и не существует ни в одной ветке.

То есть спека описывает несуществующий **слой действий** поверх уже
работающего слоя **детектов**.

**Как это произошло.** Change `2026-09-27-session-recovery` лежит в
`openspec/changes/archive/` со всеми задачами, отмеченными `[x]`, включая
«Написать admin tool `tools/recover_stale_sessions.py`» — **путь снят, в дереве
не найден** — и «`SessionRecoveryService`
создан в `ApplicationContext.create()`». Ни того, ни другого в репозитории **не
существует**: `git log --all -- tools/recover_stale_sessions.py` пуст — файл не был
закоммичен ни разу, то есть change заархивирован как выполненный без кода.

Следствие для читателя спеки: не считать её требования действующими. До
появления реализации (отдельный change-каталог) это нормативное описание
намерения, у которого нет ни одной точки исполнения, кроме перечисленного
выше детекта.

## Requirements

### Requirement: Session recovery is opt-in via explicit mode
The system SHALL support exactly three recovery modes selected by configuration: `detect-only`, `read-only-fallback`, `backup-and-restore`. The system SHALL NOT mutate any session JSONL file while mode is `detect-only` or `read-only-fallback`. In mode `read-only-fallback` the system SHALL return a session object sourced from PostgreSQL without writing to the JSONL slot, and SHALL mark the returned object as read-only.

#### Scenario: Default mode is detect-only
- **WHEN** the runtime starts without an explicit recovery mode override
- **THEN** stale sessions are detected and logged but no JSONL file is written or replaced
- **AND** no session object is returned from the recovery service

#### Scenario: Read-only-fallback returns PG materialisation
- **WHEN** the runtime starts with mode `read-only-fallback` and a stale session is observed
- **THEN** the recovery service returns a session object reconstructed from PostgreSQL
- **AND** the JSONL slot for that session is not written
- **AND** the returned object is marked so subsequent save attempts are rejected

#### Scenario: Mode change requires restart
- **WHEN** the operator changes the recovery mode in configuration
- **THEN** the new mode takes effect only after the next gateway restart
- **AND** no in-process mode switch occurs

### Requirement: Read-only mode forbids any write to the stale session
The system SHALL refuse any save attempt against a session marked as recovered read-only. The system SHALL raise an error that names the session key and the required mode to escape read-only state. The read-only marker SHALL survive metadata mutations performed by upstream session helpers (timestamp updates, system field additions) until the marker is explicitly cleared.

#### Scenario: Save attempt against read-only session is rejected
- **WHEN** upstream code calls `save()` on a session whose metadata marks it read-only
- **THEN** the call is rejected with a clear error
- **AND** no JSONL or cold-storage write is performed

#### Scenario: Upstream metadata mutation does not clear the marker
- **WHEN** upstream code mutates the session metadata while leaving the read-only marker intact
- **THEN** the next `save()` attempt is still rejected

### Requirement: Backup-and-restore performs atomic swap
The system SHALL preserve the original JSONL file before overwriting it from PostgreSQL. The system SHALL ensure that a crash, rollback, or filesystem boundary issue never leaves the JSONL slot empty. The system SHALL store backup artifacts in a directory that is not the same directory as the JSONL slots.

#### Scenario: Successful restore
- **WHEN** the operator runs `backup-and-restore` against a stale session
- **THEN** the original JSONL is moved to a backup location that is not the JSONL directory
- **AND** the JSONL slot is rewritten from PostgreSQL
- **AND** a backup artifact exists on disk

#### Scenario: Rollback on failure
- **WHEN** any step of the restore cannot be completed
- **THEN** the original JSONL is restored from the backup location
- **AND** no partial or empty JSONL is observed by upstream code

#### Scenario: Cross-filesystem rename handled
- **WHEN** the source volume and the backup volume are on different filesystems
- **THEN** the system falls back to a copy-and-unlink strategy
- **AND** the original slot is never empty between steps

#### Scenario: Concurrent cold-sync during restore
- **WHEN** the cold-sync background loop observes the JSONL slot while restore is in progress
- **THEN** the cold-sync loop does not raise an unhandled error and does not write to the cold mirror
- **AND** on the next iteration after the restore completes the JSONL and cold mirror are consistent

### Requirement: Restore size is bounded
The system SHALL refuse to materialize a session whose total message content exceeds 100 MB.

#### Scenario: Oversized session is rejected
- **WHEN** the cold mirror contains a session whose combined message payload exceeds 100 MB
- **THEN** the restore is aborted with a clear error that names the session key and the measured size
- **AND** no JSONL file is written

### Requirement: Backup rotation is multi-instance safe
The system SHALL prune old backup files using age and per-session count limits, and SHALL tolerate a backup file being concurrently removed by another instance.

#### Scenario: Concurrent removal does not raise
- **WHEN** another gateway instance removes a backup file between listing and deletion
- **THEN** the rotation step records the absence and continues
- **AND** no unhandled error is raised

### Requirement: Reverse sync lag produces a logged event
The system SHALL observe when the JSONL source is significantly newer than the cold mirror (configured threshold) and SHALL emit a logged event so the operator can investigate broken cold-sync.

#### Scenario: Reverse lag is logged
- **WHEN** the JSONL updated-at exceeds the cold-mirror updated-at by more than the configured reverse-lag threshold
- **THEN** the system emits a logged event that names the session key and both timestamps

### Requirement: Admin dry-run lists stale candidates without mutation
The admin tool SHALL list every session that would be recovered in the configured mode and SHALL NOT mutate any file when invoked with `--dry-run`. The admin tool SHALL obtain its candidate list from the same staleness detector used by the runtime.

#### Scenario: Dry-run reports candidates only
- **WHEN** оператор запускает admin tool восстановления с `--dry-run` — **путь снят, в дереве не найден** (`tools/recover_stale_sessions.py`): сценарий описывает намерение, а не существующий исполняемый файл
- **THEN** инструмент печатает ключи сессий, метки времени JSONL и PG, и режим, который применился бы
- **AND** ни один JSONL- или backup-файл не создаётся, не перемещается и не удаляется

## Responsibility

**Спека описывает предмет, которого в коде нет.** Это её определяющее
свойство, и оно установлено проверкой дерева, а не предположением: поиск
по `lib/`, `workspace/`, `tools/`, `mcp-platform/` на
`SessionRecoveryService`, `detect-only`, `read-only-fallback`,
`backup-and-restore`, `recover_stale_sessions` даёт **ноль** совпадений в
коде.

Поэтому различать надо три вещи:

1. **Реализовано** — один требование из семи: «Reverse sync lag produces a
   logged event» (порог `sync_lag_threshold_seconds`, дефолт 3600, событие
   `sync_lag_exceeded`), и смежный stale-detection
   (`stale_tolerance_seconds`, дефолт 120, `session_stale_detected`);
2. **Не реализовано** — три режима восстановления, read-only маркер,
   атомарный swap, ограничение размера, ротация бэкапов, admin tool;
3. **Назначение** — спека описывает слой **действий** поверх уже
   работающего слоя **детектов**.

Владелец по `## Scope` — `agent`. Ответственность спеки на текущий момент
нормативно-описательная: её требования читаются как замысел, а не как
действующий контракт. Это прямо оговорено в `## Scope` самой спеки («не
считать её требования действующими») и подтверждено проверкой кода.

## Boundary

**Граница — слой действий, которого не существует.** Существующая граница
проходит по линии «детект — в зеркале, действие — нигде».

Внутри границы (то, что реально есть):

- детект расхождения — `lib/gateway/mirror/session_mirror.py` и общий
  механизм `lib/gateway/mirror/mirror_poller.py`;
- публикация события расхождения — `_log_lag`
  (`session_mirror.py:273-285`) и `_log_stale_once`
  (`session_mirror.py:246-271`);
- пороги — `gateway.session_cold_sync.sync_lag_threshold_seconds` и
  `stale_tolerance_seconds`.

Вне границы:

- **всё, что требования называют «режимом восстановления»** — три режима
  не объявлены ни в `config.json`, ни в
  `lib/core/project_settings.py`; их негде выбрать;
- **admin tool восстановления** — **путь снят, в дереве не найден**:
  `tools/recover_stale_sessions.py` (`Test-Path: False`, `git log --all` пуст);
  требование «Admin dry-run» ссылается на несуществующий инструмент;
- **атомарный swap, бэкапы, ротация** — нет ни файла, ни функции;
- **ограничение 100 МБ** — не объявлено ни в одном файле;
- **read-only маркер сессии** — понятия нет: upstream-`SessionManager` его
  не знает, а `SanitizingSessionStore` (`lib/session/pg_session_manager.py:71`)
  не имеет флага, который запрещал бы `save()`.

Практическая граница для читателя: спека описывает **будущую** подсистему
поверх существующей, и любая её реализация обязана начаться с объявления
конфигурации режима — иначе `detect-only` останется единственным
неявным поведением.

## Public Contract

**Публичного контракта у предмета нет** — и это установлено проверкой
дерева, а не суждением по прозе.

Конкретно: в коде отсутствуют

- класс `SessionRecoveryService` (упомянутый в `## Scope` спеки как
  «создан в `ApplicationContext.create()`», чего не произошло);
- перечисление режимов `detect-only` / `read-only-fallback` /
  `backup-and-restore` — ни как констант, ни как значений конфигурации;
- admin tool восстановления и его точка входа `--dry-run` — **путь снят, в
  дереве не найден** (`tools/recover_stale_sessions.py`), функции нет.

Единственная наблюдаемая поверхность, которая относится к предмету по
существу, — **детект**, и он принадлежит другой спеке
(`storage/session-hybridization`). Здесь он цитируется как
единственная реализованная точка:

| Элемент | Где | Что это |
|---|---|---|
| порог расхождения | `sync_lag_threshold_seconds`, дефолт 3600 | `lib/core/application_context.py:1728-1730` |
| порог устаревания | `stale_tolerance_seconds`, дефолт 120 | `lib/core/application_context.py:1727` |
| событие расхождения | `sync_lag_exceeded` | `session_mirror.py:274-276` |
| событие устаревания | `session_stale_detected` | `session_mirror.py:258-261` |

Уточнение к формулировке сценария «Reverse lag is logged»: он требует, чтобы
событие «называло ключ сессии и обе метки времени». По коду `payload`
содержит `session_key`, `replica_id`, `sync_lag_seconds` и
`threshold_seconds` (`session_mirror.py:279-284`) — то есть **обе метки
времени не передаются**, только вычисленное расхождение. Ключ сессии
передаётся дважды: в тексте события и в `payload`. Это расхождение между
требованием и реализацией, и оно зафиксировано в `## Error Behavior` и
`## Invariants`.

## Inputs

Входов у предмета нет: он не читает файлы и не принимает аргументов,
потому что точки исполнения нет.

Входы, которые требования предполагают (перечислены, чтобы было видно, что
именно предстоит реализовать):

- режим восстановления из конфигурации — **не читается ниоткуда**; в
  `config.json` (`gateway.session_cold_sync`, строки 707-713) такого ключа
  нет, и в `SessionColdSyncSettings`
  (`lib/core/project_settings.py:157-173`) его тоже нет;
- флаги админ-инструмента `--dry-run` — инструмента не существует;
- пороги терпимости — читаются зеркалом, а не подсистемой восстановления.

Вход, существующий по факту, — **детект зеркала**: JSONL-файл сессии и
строка холодного зеркала, расхождение которых измеряется платформой и
возвращается вердиктом операции `data.mirror_session`. Именно этот вердикт
— единственный вход, который сегодня есть у предмета, и он приходит
**событием**, а не вызовом.

## Outputs

Выход реализованной части — **одно опубликованное событие журнала**
(`_publish`, `mirror_poller.py:485`), тип `agent.degraded`, уровень
`WARN`:

- `sync_lag_exceeded` — `session_mirror.py:273-285`;
- `session_stale_detected` — `session_mirror.py:246-271`.

Оба события уходят в журнал через `_publish` и доступны оператору;
побочно они читаются моделью через `data.history_search` — это тот случай,
когда детект зеркала наблюдаем из оборота, хотя сам зеркалом не является.

Выходов нереализованной части нет. Отдельно стоит зафиксировать
**дедупликацию**: `session_stale_detected` публикуется не на каждый
пропуск, а не чаще раза в `_STALE_LOG_DEDUP_TTL`
(`session_mirror.py:254-257`) — иначе один устаревший файл заполнил бы
журнал. У `sync_lag_exceeded` такого дедупа нет: событие публикуется
каждый раз, когда вердикт вернул `sync_lag_exceeded`
(`session_mirror.py:227-228`).

## State

**Неприменимо: слой действий не имеет состояния, потому что не
существует.**

Обоснование, а не умолчание: поиск по дереву не находит ни класса, ни
экземпляра, ни ключа конфигурации, который хранил бы выбранный режим;
следовательно, нечего и читать. Утверждать «состояние пустое» было бы
заглушкой, а утверждать «режим по умолчанию `detect-only`» — неправдой,
поскольку нигде не записано, что режим где-либо проверяется.

Что есть и относится к предмету лишь косвенно:

- `_stale_logged_at: dict[str, datetime]` (`session_mirror.py:164`) —
  состояние дедупликации событий, принадлежащее зеркалу, а не предмету;
- строка зеркала с `missing_cycles`, `synced_at`, `source_digest`
  (см. `sql/migrations/V010__agent_session_mirror_replica_key.sql`) —
  это то, что детект читает; **восстановление по ней не выполняется**.

Существенно для будущей реализации: если требования будут реализованы,
состоянием предмета станут выбранный режим, read-only маркер на сессии и
набор бэкапов с их ротацией. Ни одного из этих трёх элементов в дереве
нет.

## Dependencies

Прямых зависимостей у предмета нет, поскольку нет кода.

Косвенные — те, что потребуются реализации, и все они уже присутствуют в
дереве:

- `lib/gateway/mirror/` — детект, чей вывод предмет будет потреблять;
- `nanobot.session.manager.SessionManager` /
  `JsonlSessionStore` — хот-путь, к которому применялось бы восстановление;
- `lib/core/project_settings.py::SessionColdSyncSettings` — место, где
  объявился бы ключ режима;
- пул PostgreSQL — если восстановление будет читать холодное зеркало
  напрямую; сейчас его читает платформа.

Зависимость, которой **нет** и которая обнаружилась бы при первой же
реализации: admin tool требует способа перечислить кандидатов «из того же
детектора, что и рантайм» (требование «Admin dry-run»). Сегодня детект
живёт внутри фоновой задачи, а не в вызываемой функции, поэтому переиспользовать
его из CLI нельзя — потребуется выделение функции.

## Configuration

**Неприменимо: конфигурационных ключей у предмета нет.**

Проверено: в `config.json` секция `gateway.session_cold_sync`
(строки 707-713) содержит только `enabled`, `sync_interval_sec`,
`batch_size`, `stale_tolerance_seconds`, `sync_lag_threshold_seconds`;
в `SessionColdSyncSettings` (`lib/core/project_settings.py:157-173`) —
`enabled`, `sync_interval_sec`, `batch_size`, `stale_tolerance_seconds`,
`sync_lag_threshold_seconds`. Ключа режима восстановления нет ни там, ни
там.

Требование «Session recovery is opt-in via explicit mode» требует, чтобы
система поддерживала «ровно три режима, выбираемых конфигурацией»
(`Requirements`, строка 46). Сегодня это требование **невыполнимо по
определению**: выбирать нечего. Сценарий «Default mode is detect-only»
описывает поведение, которое случайно совпадает с реальностью (детект без
действий), но не закреплено ни одним ключом — то есть совпадение
негарантировано и может быть нарушено добавлением любого ключа.

Оговорка, чтобы правило не читалось шире: пороги
`stale_tolerance_seconds` и `sync_lag_threshold_seconds` **существуют** и
применяются (`lib/core/application_context.py:1727-1730`), и валидация
`≥` между ними выполняется на старте
(`lib/gateway/mirror/session_mirror.py:166-170`). Это конфигурация
детекта, а не восстановления.

## Lifecycle

**Неприменимо: у предмета нет жизненного цикла.** Он не создаётся, не
запускается и не останавливается — ни класса, ни функции, ни задачи.

Что существует рядом и имеет жизненный цикл (принадлежит зеркалу, а не
предмету):

- фоновая задача `session_mirror-poller` — `MirrorPoller.start()`
  (`mirror_poller.py:272-279`), имя задачи по умолчанию
  `f"{resource_name}-poller"` (`mirror_poller.py:176`);
- финальный проход при остановке (`mirror_poller.py:281-304`);
- запуск в `gateway.py:607-616` — строго после рукопожатия с платформой,
  потому что клиент нужен и зеркалу.

Требования предмета описывают lifecycle, которого быть не может: режим
выбирается **при старте**, а не в цикле; `detect-only` обязан «детектировать
и логировать, но не писать и не заменять файл» (сценарий «Default mode is
detect-only»). Сегодня это поведение получается не режимом, а тем, что
режима нет вообще, — и это разные вещи, хотя наблюдаемо совпадают.

## Data Ownership

**Неприменимо: предмет не владеет данными.**

Ни один из слоёв, которыми требования предполагают оперировать
(JSONL-файл сессии, бэкап восстановления, каталог бэкапов), предмету не
принадлежит:

- JSONL — hot path, единственный writer upstream `SessionManager`;
- холодное зеркало в PostgreSQL — платформа, через
  `data.mirror_session` и `data.cleanup_session_mirror`;
- бэкапов, каталога бэкапов и файлов копий в дереве нет.

Особое место в требованиях занимает требование «Read-only mode forbids any
write to the stale session»: оно подразумевает, что подсистема
восстановления **имеет** право записи в сессию. Сегодня права записи в
сессии нет ни у кого, кроме upstream-библиотеки, — поэтому запрет
соблюдается тривиально и не потому, что режим его обеспечивает.

Проверяемое следствие, которое запрещено требованиями и подтверждено
кодом: **ни одна сессия сейчас не помечается read-only.**
`SanitizingSessionStore` (`lib/session/pg_session_manager.py:71-82`) не
имеет такого флага, а `SessionManager` его не знает.

## Error Behavior

У предмета нет обработки ошибок: нет кода, который мог бы что-то
обработать или не обработать.

Что существует и относится к предмету лишь как к источнику событий:

- отказ цикла зеркала ловится и логируется, задача не умирает
  (`mirror_poller.py:312-315`);
- публикация `sync_lag_exceeded` не дедуплицируется — при каждом цикле с
  превышением порога в журнал пишется новая строка
  (`session_mirror.py:227-228`). Для оператора это означает, что объём
  журнала растёт пропорционально длительности расхождения, а не единиц;
- `session_stale_detected` дедуплицируется по
  `_STALE_LOG_DEDUP_TTL` (`session_mirror.py:254-257`).

Требования предмета при этом описывают отказоустойчивость, которой нет:
`backup-and-restore` с атомарным swap и copy-and-unlink на разных ФС
(`Requirements`, строки 76-99) — самая рискованная часть замысла, потому
что атомарность на разных файловых системах недостижима в принципе, и
требование это учитывает (отдельный путь через копирование), но реализации
нет. Отдельно: ограничение размера восстановления (100 МБ) и ротация
бэкапов также не реализованы, то есть при появлении слоя действий первым
делом должен быть именно отказ, а не happy path.

## Invariants

Реализованная часть — одна, и её инварианты проверяемы:

1. **Расхождение только обнаруживается, а не устраняется.** Зеркало
   публикует событие и продолжает работу; ни один файл сессии не
   перезаписывается детектором. Это совпадает с требованием
   `detect-only`, но совпадает **по следствию, а не по механизму**: режима
   нет.
2. **Детект является предпосылкой, а не частью действия.** Пороги
   вычисляются платформой, вердикт возвращается в `after_write`, и по
   нему принимается решение только о журналировании
   (`session_mirror.py:218-228`).
3. **`sync_lag_exceeded` и `session_stale_detected` — разные события**
   с разными порогами и разной дедупликацией.

Нормативная часть (требования без реализации) — их следует считать
целями, а не наблюдаемыми свойствами:

4. Восстановление не мутирует JSONL ни в `detect-only`, ни в
   `read-only-fallback`.
5. В режиме `read-only-fallback` возвращается объект сессии из
   PostgreSQL **без** записи в JSONL-слот, помеченный read-only.
6. `backup-and-restore` выполняет атомарный swap; на разных ФС — через
   копирование с последующим unlink.
7. Размер восстановления ограничен; превышение отвергается.
8. Ротация бэкапов безопасна при нескольких репликах.
9. `--dry-run` перечисляет кандидатов и не создаёт, не перемещает и не
   удаляет ни одного файла.

Инвариант, который важнее остальных и который предмет обязан будет
удерживать при реализации: **восстановление не должно выглядеть как
успех.** Требование «Read-only mode forbids any write» существует
именно потому, что молчаливая перезапись авторитетного холодного хранилища
необратима; поэтому отказ должен быть громче успеха.

## Forbidden Behavior

1. **Читать эту спеку как действующий контракт.** Её требования не имеют
   точки исполнения; это нормативное описание намерения (оговорено в
   `## Scope` и подтверждено проверкой дерева).
2. **Молча перезаписывать JSONL-файл сессии при расхождении.** Ни в одном
   режиме; в `detect-only` и `read-only-fallback` — прямо запрещено.
3. **Возвращать объект сессии из PostgreSQL, не помечая его read-only.**
   Непомеченный объект уедет обратно в запись.
4. **Выполнять swap наивным `rename` между разными ФС** — это не
   атомарно; требование требует пути копирования с unlink.
5. **Восстанавливать сессию без ограничения размера.** 100 МБ — граница
   замысла; превышение обязано отвергаться, а не уходить в память.
6. **Ротировать бэкаты без учёта реплик** — несколько реплик, пишущих в
   один каталог, завалят его взаимными удалениями.
7. **Выводить кандидатов в восстановление из отдельного детектора.**
   Требование прямо требует того же детектора, что и у рантайма; два
   детектора разойдутся, и `--dry-run` будет обещать не то, что сделает
   рантайм.
8. **Мутировать что-либо при `--dry-run`.** Не только JSONL: бэкапы,
   каталоги, метки.
9. **Объявлять режим по умолчанию дефолтом настроек.** Сценарий
   «Default mode is detect-only» описывает наблюдаемое поведение,
   но код его не гарантирует — совпадение держится на отсутствии режимов,
   а не на объявленном значении.
10. **Заводить `SessionRecoveryService` или admin tool, не покрыв их
    тестами и не описав в `## Implementation`.** Предмет, созданный
    после написания спеки, обязан быть в ней назван — иначе спека снова
    станет описанием намерения.

## Consumers

Потребителей реализованной части:

| Потребитель | Что использует | Где |
|---|---|---|
| Оператор | события `sync_lag_exceeded` / `session_stale_detected` в `agent_gateway_logs` | через журнал и `runtime_health` |
| Модель агента | те же события через `data.history_search` | косвенно, в пределах своей сессии |
| Спека `storage/session-hybridization` | тот же детект как часть своей предметной области | — |

Потребителей нереализованной части нет: admin tool, режимы восстановления
и бэкапы не имеют ни одного потребителя, потому что не существуют. Это
важно понимать при планировании — «потребитель ожидаем» здесь означал бы
спеку, а не код.

Косвенный потребитель, о котором стоит сказать прямо: **никакого
восстановления не ожидает и сам gateway.** Ни один модуль не читает
`stale_tolerance_seconds` как повод к действию; порог используется только
д формирования события.

## Implementation

**Отдельной реализации у предмета нет.** Это установлено проверкой
дерева, а не предположением: поиск по `lib/`, `workspace/`, `tools/`,
`mcp-platform/`, `tests/` на `SessionRecoveryService`,
`recover_stale_sessions`, `detect-only`, `read-only-fallback`,
`backup-and-restore` не даёт ни одного совпадения в коде.

Что **есть** и относится к предмету (все пути проверены `Test-Path`, все
существуют):

- `lib/gateway/mirror/session_mirror.py` — детект: `_log_lag` (строка 273)
  и `_log_stale_once` (строка 246), пороги в конструкторе (строки
  160-161), валидация их соотношения (166-170);
- `lib/gateway/mirror/mirror_poller.py` — механизм цикла и `_publish`
  (строка 485);
- `lib/core/application_context.py` — чтение порогов (строки 1727-1730);
- `lib/core/project_settings.py` — `SessionColdSyncSettings` (строки
  157-173);
- `./config.json` — секция `gateway.session_cold_sync` (строки 707-713).

Чего в репозитории **не существует**, хотя требования на это ссылаются:

- `tools/recover_stale_sessions.py` — `Test-Path: False`;
- `SessionRecoveryService` — класса нет ни в одном дереве;
- конфигурационных ключей режимов восстановления — нет ни в
  `./config.json`, ни в `lib/core/project_settings.py`;
- read-only маркера сессии — нет ни в `SanitizingSessionStore`, ни в
  обвязке;
- каталогов и файлов бэкапов — не создаются и не задекларированы.

Причина появления спеки в таком виде установлена по её же `## Scope`:
change `2026-10-27-session-recovery` (точная дата в тексте спеки —
`2026-09-27-session-recovery`) заархивирован в
`openspec/changes/archive/` со всеми задачами `[x]`, включая «Написать
admin tool `tools/recover_stale_sessions.py`» и «`SessionRecoveryService`
создан в `ApplicationContext.create()`»; при этом `git log --all --
tools/recover_stale_sessions.py` пуст. То есть change закрыт как
выполненный без кода: в репозитории не существует ни файла
`tools/recover_stale_sessions.py`, ни класса `SessionRecoveryService`.

## Verification

**Проверок реализации у предмета нет — и это тоже установлено проверкой,
а не предположением.** Отдельного файла тестов, относящегося к слою
восстановления, в дереве нет.

Что покрывает **реализованную** часть (детект), все пути проверены
`Test-Path`, все существуют:

- `tests/test_session_cold_sync_service.py` — циклы зеркала, детект
  расхождения, публикация событий;
- `tests/test_session_mirror_wire.py` — сверка состава операций зеркала с
  реестром платформы;
- `tests/test_storage_hybridization.py` — инварианты слоёв хранения;
- `tests/test_storage_hybridization_lifecycle.py` — жизненный цикл;
- `tests/test_storage_hybridization_factory.py` — сборка и разбор
  параметров;
- `tests/test_config_keys.py` — разбор `gateway.session_cold_sync`,
  включая отсутствие ключа режима;
- `tests/test_runtime_health.py` — счётчики детекта в отчёте живости;
- `tests/test_service_identity.py` — служебная личность вызовов зеркала.

Существующие стражи **не** покрывают предмет: проверки порогов и событий
относятся к зеркалу (`storage/session-hybridization`), а не к слою
восстановления. Ни один из перечисленных тестов не может провалить
требование о трёх режимах, потому что код, который их реализует, не
существует.

Честная граница: единственный автоматический сигнал о том, что спека
опережает код, — это её собственный `## Scope` и валидатор структуры
(`tools/validate_component_specs.py`), который проверяет наличие разделов,
но не их исполнимость. Проверки «требование имеет реализацию» в проекте
нет, и до появления кода раздел `## Implementation` этой спеки остаётся
списком детекта, а не подсистемы.
