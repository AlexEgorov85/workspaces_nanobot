# TASK: применить 19 правок Категории 1 (nanobot 0.3.5 upgrade)

## Контекст

- **Источник правок:** `backup/master-pre-future-work-2026-10-02` (== master HEAD `32d61a7c`) — 105 коммитов, из них 19 относятся к апгрейду `nanobot-ai` 0.3.0 → 0.3.5.
- **Цель:** создать новую ветку от `v2.5.3` (== `0c474a38`, `nanobot-ai==0.3.0`) с 19 правками Категории 1, чтобы агент в этой ветке работал на nanobot 0.3.5.
- **Имена ветки:** предлагаю `release/v2.5.4-nanobot-035` (владелец может переименовать).
- **Все SHA сверены с `git show`**, хронологический порядок — по дате коммита.

## 19 коммитов Категории 1 (в хронологическом порядке)

| # | SHA | Дата | Тип | Что делает |
|---|---|---|---|---|
| 1 | `b7f73c9` | 26.09 21:36 | **код** | **Главный upgrade**: `runtime_patcher.py` (635 строк), `base_tool_tracking_hook.py` и `compact_command.py` удалены, `agent_factory.py` добавлен `ToolRegistry`, hooks (`database_logging_hook.py`, `tool_audit_hook.py`, `terminal_tool_print_hook.py`) переписаны. **28 файлов.** Включает правку `requirements.txt` (0.3.0→0.3.5) и `pyproject.toml` |
| 2 | `9c94609` | 28.09 09:45 | спек | OpenSpec спека `post-0.3.5-patches-cleanup` (pub-sub расширения, удаление ActiveFilesHook и fallback _last_usage) |
| 3 | `c0fe1e4` | 28.09 10:24 | код | `RuntimeEventsSubscriber` на `TurnRuntimeAdmitted`/`TurnCompleted`/`SubagentTurnCompleted` через `bus.subscribe` |
| 4 | `9509012` | 28.09 10:34 | код | `_SubagentLoggingHook` + `_SubagentLoggingSubscriber` для `SubagentTurnCompleted` |
| 5 | `79d5810` | 28.09 10:41 | код | Флаг `_subscriber_registered` устраняет дубль `subagent_run_finished` |
| 6 | `22cbafe` | 28.09 10:44 | код | `event_type=turn_completed` в `history_search` |
| 7 | `7a56440` | 28.09 10:46 | код | Удалён fallback на `_last_usage` (атрибут удалён в 0.3.5) |
| 8 | `60e7b8f` | 28.09 10:55 | код | Удалён `ActiveFilesHook` (0.3.5 имеет свой) |
| 9 | `2163f05` | 28.09 11:03 | код | Удалён `patch_context_bridge_seed` как no-op (`_state_build` удалён в 0.3.5) |
| 10 | `efe147e` | 28.09 11:04 | код | Удалён устаревший комментарий в `runtime_patcher.py` |
| 11 | `1733621` | 28.09 11:12 | спек | ADR о breaking changes post-0.3.5 для unit-тестов |
| 12 | `b27020e` | 28.09 11:17 | спек | Документация `turn_completed` + правка `runtime-patcher-inventory` |
| 13 | `c20c3a6` | 28.09 11:19 | спек | Mark [x] в `tasks.md` (post-0.3.5-patches-cleanup) |
| 14 | `7dae3a8` | 28.09 14:17 | тест | Unit-тесты под `RuntimeEventsSubscriber` |
| 15 | `011b6b4` | 28.09 14:18 | код | `_attach_context_window` берёт `limit/model` из bridge с fallback на `agent.model` (property в 0.3.5) |
| 16 | `6615c51` | 28.09 14:19 | спек | Mark [x] для 5.2 и 8.2 в `tasks.md` |
| 17 | `3500d48` | 28.09 15:16 | **код** | **Post-upgrade фиксы**: `priority_commands.py` (порядок), `database_logging_hook.py` (`get_model` callable — замена удалённого `LLMResponse.model`), `tool_audit_hook.py` (`ToolCallRequest.arguments` теперь dict), `outbound_meta.py` (убран `outbound_event_from_message`, заменён `outbound_message_for_event`). **+ новый файл `tools/audit_nanobot_contracts.py` (194 строки)** |
| 18 | `43414e5` | 28.09 15:15 | спек | Архивация `post-0.3.5-patches-cleanup` |
| 19 | `f73be3a` | 28.09 16:16 | **код** | **CLI-адаптер для приватных REPL-хелперов nanobot 0.3.5**. Новый файл `lib/cli/nanobot_cli_compat.py` (132 строки), +114 строк тестов |

> **Принцип группировки:** коммиты 1, 17, 19 — **большие кодовые** (много файлов, требуют аккуратности). Остальные — точечные, один-два файла, низкий риск конфликтов.

## Предусловия (агент ОБЯЗАН проверить перед стартом)

```bash
# 1. v2.5.3 чистая, без незакоммиченных изменений
git checkout v2.5.3
git status --porcelain   # должен быть пустым

# 2. backup/master-pre-future-work-2026-10-02 доступен
git rev-parse backup/master-pre-future-work-2026-10-02
# ожидаемо: 32d61a7c006f925618c987654df39537ab36f162

# 3. текущая ветка НЕ v2.5.3 (мы делаем НОВУЮ ветку)
git rev-parse --abbrev-ref HEAD   # не должна быть v2.5.3 / release/v2.5.3
```

Если `git status` показывает изменения — **СТОП**. Не продолжать, пока владелец не закоммитит/откатит. Это разрушит cherry-pick.

## Шаг 1. Создать рабочую ветку

```bash
git checkout v2.5.3
git pull   # если есть remote
git checkout -b release/v2.5.4-nanobot-035
```

**Не делать** `git pull` если работаем локально без remote — будет ошибка, и это нормально.

## Шаг 2. Обновить requirements и pyproject.toml (предварительно, до cherry-pick)

Cherry-pick `b7f73c9` тоже это делает, но **для подстраховки** выполним вручную первыми — на случай, если cherry-pick упадёт на этом:

```bash
# Проверить текущее
grep -n "nanobot-ai" requirements.txt pyproject.toml 2>/dev/null

# Заменить 0.3.0 → 0.3.5
sed -i 's/^nanobot-ai==0\.3\.0$/nanobot-ai==0.3.5/' requirements.txt
# В pyproject.toml — то же самое, если есть
```

## Шаг 3. Cherry-pick 19 коммитов в указанном порядке

```bash
# Все SHA одной командой (порядок критичен!)
git cherry-pick -x \
    b7f73c9 \
    9c94609 \
    c0fe1e4 \
    9509012 \
    79d5810 \
    22cbafe \
    7a56440 \
    60e7b8f \
    2163f05 \
    efe147e \
    1733621 \
    b27020e \
    c20c3a6 \
    7dae3a8 \
    011b6b4 \
    6615c51 \
    3500d48 \
    43414e5 \
    f73be3a
```

### Что делать, если cherry-pick конфликтует

**Вероятные точки конфликтов** (по убыванию вероятности):
1. **`3500d48`** — модифицирует `outbound_meta.py` (возможно пересечение с `b7f73c9`).
2. **`f73be3a`** — модифицирует `lib/cli/console_loop.py` (CLI-конфигурация изменилась).
3. **`3500d48`** + **`b7f73c9`** — оба трогают `database_logging_hook.py` и `tool_audit_hook.py` (вероятно конфликт в строках документации).

**Алгоритм разрешения:**
```bash
# Получили конфликт — смотрим, в чём дело
git status

# Открываем файл, ищем маркеры конфликта
# <<<<<<< HEAD
# ... наша версия (из v2.5.3) ...
# =======
# ... их версия (из cherry-pick) ...
# >>>>>>> ...

# Правило: для Категории 1 ВСЕГДА берём ВЕРСИЮ ИЗ CHERRY-PICK (theirs),
# потому что код v2.5.3 написан под 0.3.0, а cherry-pick — под 0.3.5.
# Исключение: см. ниже.

# Если файл с конфликтом — это runtime_patcher.py и конфликт в строках,
# которые КАСАЮТСЯ _DEFAULT_INTERNAL_ERROR_TEXT / patch_turn_delivery_fail
# (нашего error-fallback фичи из TASK.md) — НЕ ПРИМЕНЯТЬ ту часть, где
# есть "Старая фича error-fallback"! На 0.3.5 фича переехала на
# turn_delivery_factory (см. AGENTS.md раздел TurnDelivery). Сохранить
# обновления runtime_patcher.py из cherry-pick, но УДАЛИТЬ из них блок
# patch_turn_delivery_fail, если он там появится.
```

**ВАЖНО:** во всех 19 cherry-pick'ах, кроме случая выше, **принимаем theirs**:
```bash
git checkout --theirs <конфликтный_файл>
git add <конфликтный_файл>
```

После разрешения всех конфликтов:
```bash
git cherry-pick --continue
# или, если прерывание:
GIT_EDITOR=true git cherry-pick --continue
```

Если cherry-pick не нужен (например, обнаружили, что фича уже в v2.5.3):
```bash
git cherry-pick --skip
```

### Какие файлы НЕ конфликтуют (почти наверняка)

Эти файлы в v2.5.3 либо отсутствуют, либо отличаются минимально, поэтому cherry-pick пройдёт чисто:
- `lib/services/runtime_events_subscriber.py` — **новый файл** (создаётся в `c0fe1e4`).
- `lib/cli/nanobot_cli_compat.py` — **новый файл** (создаётся в `f73be3a`).
- `tools/audit_nanobot_contracts.py` — **новый файл** (создаётся в `3500d48`).
- `lib/hooks/base_tool_tracking_hook.py` — **удаление** (в v2.5.3 файл есть, в `b7f73c9` удаляется; cherry-pick удалит).
- `lib/commands/compact_command.py` — **удаление** (аналогично).
- Все `.md` файлы (спека) — разные, не конфликтуют.

## Шаг 4. Проверить результат

```bash
# 4.1. Все 19 коммитов на новой ветке
git log --oneline v2.5.3..HEAD | wc -l
# ожидаемо: 19

# 4.2. requirements.txt — 0.3.5
grep "nanobot-ai" requirements.txt
# ожидаемо: nanobot-ai==0.3.5

# 4.3. pyproject.toml — 0.3.5
grep "nanobot-ai" pyproject.toml
# ожидаемо: nanobot-ai==0.3.5 (если присутствует)

# 4.4. Удалённые файлы действительно удалены
test ! -f lib/hooks/base_tool_tracking_hook.py && echo "OK: base_tool_tracking_hook.py удалён"
test ! -f lib/commands/compact_command.py && echo "OK: compact_command.py удалён"

# 4.5. Новые файлы на месте
test -f lib/services/runtime_events_subscriber.py && echo "OK: runtime_events_subscriber.py"
test -f lib/cli/nanobot_cli_compat.py && echo "OK: nanobot_cli_compat.py"
test -f tools/audit_nanobot_contracts.py && echo "OK: audit_nanobot_contracts.py"
```

## Шаг 5. Установить nanobot 0.3.5 и проверить API

```bash
# Создать чистый venv с 0.3.5
python -m venv .venv-035
.venv-035\Scripts\activate  # Windows
# или: source .venv-035/bin/activate  # Linux/macOS
pip install -U pip
pip install "nanobot-ai==0.3.5"
pip install -r requirements.txt

# Проверить критичные API, на которые опираются патчи
python -c "
from nanobot.agent.turn_delivery import TurnDelivery
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.loop import AgentLoop
import inspect

# 1. TurnDelivery.fail — должен быть с сигнатурой (self, *, publish_completion: bool)
sig = inspect.signature(TurnDelivery.fail)
assert list(sig.parameters.keys()) == ['self', 'publish_completion'], sig
print('OK: TurnDelivery.fail signature')

# 2. TurnDelivery._failure_error_kind — должен быть атрибутом
print('OK: _failure_error_kind is in dataclass fields' if '_failure_error_kind' in TurnDelivery.__dataclass_fields__ else 'WARN: no _failure_error_kind in TurnDelivery')

# 3. AgentLoop.from_config — должен принимать tool_registry
sig = inspect.signature(AgentLoop.from_config)
assert 'tool_registry' in sig.parameters, 'from_config must accept tool_registry'
print('OK: AgentLoop.from_config accepts tool_registry')

# 4. ToolRegistry — должен существовать
ToolRegistry()
print('OK: ToolRegistry is instantiable')
"
```

## Шаг 6. Прогнать тесты

```bash
# 6.1. Контрактные тесты (НЕ должны падать)
pytest tests/contract/ -x --tb=short
# Ожидаемо: 0 failed

# 6.2. RuntimePatcher тесты (были переписаны в b7f73c9)
pytest tests/test_runtime_patcher.py -x --tb=short
# Ожидаемо: 0 failed

# 6.3. Хуки (были переписаны)
pytest tests/test_tool_audit_hook.py tests/test_database_logging_hook.py tests/test_terminal_tool_print_hook.py -x --tb=short
# Ожидаемо: 0 failed

# 6.4. Compaction (новый subscriber)
pytest tests/test_compaction_event_subscriber.py -x --tb=short
# Ожидаемо: 0 failed

# 6.5. CLI compat (новый)
pytest tests/contract/test_nanobot_cli_compat.py -x --tb=short
# Ожидаемо: 0 failed

# 6.6. Audit tool
pytest tests/contract/test_database_logging_get_model.py -x --tb=short
# Ожидаемо: 0 failed

# 6.7. Полный прогон (должен быть зелёным, ~3600 тестов)
pytest tests/ -q --tb=line
# Ожидаемо: ~3600 passed, 0 failed (или допустимый xfail/skip)
```

Если **6.1-6.6** падают — **СТОП**, искать root cause (скорее всего не разрешён конфликт). Частично зелёный прогон в 6.7 — **СТОП**, разбираться.

## Шаг 7. Audit-tool для проверки контрактов

```bash
python tools/audit_nanobot_contracts.py
# Ожидаемо: 0 MISSING (раньше было 23 MISSING на старом 0.3.0 коде)
```

Если > 0 MISSING — значит какие-то из 19 cherry-pick'ов не применились корректно. Идти в вывод, смотреть какой символ MISSING, восстанавливать.

## Шаг 8. Smoke-test CLI и gateway

```bash
# CLI smoke (--smoke режим)
python cli_agent.py --smoke
# Ожидаемо: exit 0, "OK_SMOKE_COMPLETE"

# Gateway smoke (если есть)
python gateway.py --smoke 2>&1 | head -50
# Ожидаемо: exit 0 (или dry-run, без падений)
```

## Шаг 9. Проверка error-fallback (для полноты)

`release/v2.5.4-nanobot-035` отличается от `release/v2.5.3` тем, что там теперь 0.3.5. Фича error-fallback (из `openspec/changes/v2.5.3-error-fallback/TASK.md`) в этой ветке **уже не нужна** в том виде, как она описана. В 0.3.5 есть **публичная точка расширения** `AgentLoop(turn_delivery_factory=...)` (см. `lib/services/turn_delivery_factory.py` в master), и фича реализуется через `lib/services/turn_delivery_factory.py`, а не через monkey-patch.

Если нужна error-fallback фича в этой ветке:
1. **НЕ применять** `openspec/changes/v2.5.3-error-fallback/TASK.md` как есть.
2. Вместо этого: после Шага 8 проверить, что в `lib/services/turn_delivery_factory.py` уже есть фича (если в cherry-pick'ах b7f73c9 / последующих она не была удалена).
3. Если её нет — взять `lib/services/turn_delivery_factory.py` из `backup/master-pre-future-work-2026-10-02` напрямую (один файл, не cherry-pick).

Проверка:
```bash
test -f lib/services/turn_delivery_factory.py && echo "turn_delivery_factory.py уже есть" || echo "нужно взять из backup"
```

## Что НЕ делать (явные запреты)

1. **НЕ мержить** `backup/master-pre-future-work-2026-10-02` в новую ветку — принесёт 86 несвязанных коммитов.
2. **НЕ cherry-pick'ать** SHA, которых нет в списке 19 — это коммиты Категории 2-4 (спек, error-fallback, профили, кэш и т.п.).
3. **НЕ устанавливать** `nanobot-ai==0.3.0` после cherry-pick'а — `b7f73c9` уже обновил requirements на 0.3.5, и весь код написан под 0.3.5.
4. **НЕ редактировать** `runtime_patcher.py` «вручную» — все 19 коммитов приходят как патчи, любые ручные правки порождают merge-конфликты с будущими cherry-pick'ами.
5. **НЕ пушить** новую ветку до прохождения всех 8 шагов верификации.
6. **НЕ удалять** `CHANGELOG.md` блоки cherry-pick'ов — они нужны для CHANGELOG релиза `v2.5.4`.

## Сводка для быстрого старта (копировать в буфер обмена)

```bash
# === pre-flight ===
git checkout v2.5.3
git status --porcelain
git rev-parse backup/master-pre-future-work-2026-10-02

# === step 1 ===
git checkout -b release/v2.5.4-nanobot-035

# === step 2 ===
sed -i 's/^nanobot-ai==0\.3\.0$/nanobot-ai==0.3.5/' requirements.txt

# === step 3 ===
git cherry-pick -x b7f73c9 9c94609 c0fe1e4 9509012 79d5810 22cbafe 7a56440 60e7b8f 2163f05 efe147e 1733621 b27020e c20c3a6 7dae3a8 011b6b4 6615c51 3500d48 43414e5 f73be3a

# === step 4 ===
git log --oneline v2.5.3..HEAD | wc -l    # должно быть 19
grep nanobot-ai requirements.txt

# === step 5-6 ===
python -m venv .venv-035
.venv-035\Scripts\activate
pip install -U "nanobot-ai==0.3.5" -r requirements.txt
pytest tests/contract/ tests/test_runtime_patcher.py tests/test_tool_audit_hook.py tests/test_database_logging_hook.py tests/test_terminal_tool_print_hook.py tests/test_compaction_event_subscriber.py -x

# === step 7 ===
python tools/audit_nanobot_contracts.py    # должно быть 0 MISSING

# === step 8 ===
python cli_agent.py --smoke

# === done ===
git log --oneline v2.5.3..HEAD   # показать владельцу
```

## Что вернуть владельцу после выполнения

1. `git log --oneline v2.5.3..HEAD` — список из 19 коммитов на новой ветке.
2. `pytest tests/ -q` — вывод прогона (или `lastfailed` если есть падения).
3. `python tools/audit_nanobot_contracts.py` — вывод (должно быть «0 MISSING»).
4. `python cli_agent.py --smoke` — exit code (должен быть 0).
5. `git status` — должен быть чистым.
6. `grep "nanobot-ai" requirements.txt` — строка с `==0.3.5`.
7. **Не** пушить, не открывать PR, не ставить тег — это владелец сделает сам после review.
