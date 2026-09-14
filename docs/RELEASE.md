# Release Process

Полная процедура выпуска нового релиза. Минимум ручных шагов, всё через `tools/release_vX_Y_Z.py` + git-flow.

## 0. Семантика версий

Проект следует [Semantic Versioning](https://semver.org/lang/ru/) (см. шапку `CHANGELOG.md`):

- **MAJOR** (X.0.0) — ломающие изменения API/конфига/настроек.
- **MINOR** (X.Y.0) — новые возможности, backward-compatible.
- **PATCH** (X.Y.Z) — баг-фиксы и hardening, не меняющие интерфейс.

Текущая версия — в `project.json::project.version` (без префикса `v`).
git-теги имеют префикс `v` (`v2.5.2`).
**Из-за release-веток git-теги могут отставать от актуальной версии в `project.json`** — см. секцию «Branching» ниже.

## 1. Подготовка артефактов (на ветке `master`)

```bash
# Чистый tree, без "WIP" / "wip:*" stash
git status

# Прогнать тесты — всё зелёное перед подготовкой релиза
.venv/Scripts/python.exe -m pytest -q   # Windows
# или
python -m pytest -q                    # Linux/macOS

# Проверить, что нет неразрешённых merge-конфликтов
git diff --check
```

### 1.1 `project.json::project.version`

```diff
- "version": "2.5.1"   // Версия проекта (актуальный релизный тег vX.Y.Z без префикса v)
+ "version": "2.5.2"   // Версия проекта (актуальный релизный тег vX.Y.Z без префикса v)
```

### 1.2 `CHANGELOG.md`

Заменить пустой блок `## [Unreleased]` на блок `## [<version>] — <YYYY-MM-DD>`. Контент:

- эпиграф PATCH/MINOR/MAJOR в формате v2.5.2;
- по подсистемам секции `### Fixed`, `### Added`, `### Changed`, `### Removed`;
- в конце упоминание про полный changelog и ссылки на issues/коммиты `(commit_hash)`.

Сверху добавить **новый пустой блок** `## [Unreleased]` для следующих изменений:

```diff
+ ## [Unreleased]
+
- ## [Unreleased]
+ ## [2.5.2] — 2026-09-14
+ ...
```

### 1.3 `README.md`

Вставить блок «Что нового в v<version>» сразу над блоком предыдущего релиза. Формат — как у `## 🆕 Что нового в v2.5.2` (см. `README.md:160`). Минимум — короткий эпиграф + ссылка на `CHANGELOG.md`.

### 1.4 `docs/*.md`

**Полный аудит** документации на ссылки, которые могли устареть:

- пути к файлам (`workspace/data_store/duckdb/cache.duckdb`, `~/.cache/...`, `data_store/...`);
- имена секций в `project.json` (`gateway.cache.local_path`, `gateway.sync.*`);
- имена CLI-флагов, опций конфига, переменных окружения;
- ссылки на коммиты (`605660b`) и PR.

Проверять можно так:

```bash
# Все вхождения старого пути кеша:
grep -rn 'workspace/data_store/duckdb/cache.duckdb' docs/ CHANGELOG.md README.md
```

Устаревшие ссылки — обновить или пометить как исторические (например,
прежние пути `workspace/data_store/duckdb/cache.duckdb`, ныне удалённые
опции вроде `gateway.cache.use_workspace_path`).

### 1.5 Артефакт `tools/release_v<X>_<Y>_<Z>.py`

**Не редактировать старый** (`tools/release_v251.py` уже сделал свою работу — оставь как исторический маркер). Создать новый по образцу:

```bash
cp tools/release_v251.py tools/release_v252.py
# edit:
#   TAG = "v2.5.2"
#   TITLE = "v2.5.2"
#   CHANGELOG_BLOCK_HEADER = "## [2.5.2] — 2026-09-14"
#   NEXT_BLOCK_HEADER     = "## [2.5.1] — 2026-09-13"
#   BODY = "..."   # эпиграф + Fixed/Added/Changed/Removed по CHANGELOG.md
#   def main():
#       # --dry-run по умолчанию; явный --run для боевого запуска
```

`--dry-run` должен печатать полный payload в **stdout** и **не трогать диск**. Только `--run` пишет временный payload.json для `gh release create`.

### 1.6 (опционально) `.gitignore`

Если release-script пишет payload в файл — добавить паттерн в `.gitignore`:

```gitignore
# --- Release-script payload артефакты (tools/release_v*.py --dry-run) ---
release_v*_payload.json
```

Сейчас артефакты идут в `tempfile.gettempdir()` и `.gitignore`-фильтр не нужен, но на всякий случай оставлен как страховка.

## 2. Локальная проверка

```bash
# Сухой прогон: выгрузить payload, ничего не публикуя
.venv/Scripts/python.exe tools/release_v252.py --dry-run | head -40

# Сухой прогон curl-формой (если gh не установлен)
.venv/Scripts/python.exe tools/release_v252.py --curl
```

Если что-то не так — правь `tools/release_v252.py` (epigraph, секции), повторяй.

## 3. Коммит в master

Один atomic-коммит с шаблонным сообщением:

```bash
git add \
  CHANGELOG.md \
  README.md \
  docs/ \
  project.json \
  tools/release_v252.py \
  .gitignore

git commit -m "chore(release): v2.5.2 — <короткий эпиграф PATCH/MINOR/MAJOR>

- bumped project.version 2.5.1 → 2.5.2
- CHANGELOG: блок v2.5.2 (N коммитов с v2.5.1)
- README: «Что нового в v2.5.2»
- docs/*: аудит устаревших ссылок
- tools/release_v252.py: по образцу v2.5.1, --dry-run по умолчанию
"
```

## 4. Branching

Для каждого релиза (включая patch) создаётся **release branch** `release/vX.Y[.Z]` от текущего `master`:

```bash
git checkout -b release/v2.5.2
git push -u origin release/v2.5.2
```

Назначение `release/vX.Y` — нести regression-фиксы для уже опубликованной версии, пока разработка ушла вперёд на `master`. Аналогично `release/v2.3` живёт пока `master` уже на `v2.5.x` — это «стабильная ветка для тех, кто застрял на v2.3».

Если ты не планируешь поддерживать старые ветки — всё равно создай `release/v2.5.2` для маркировки «эта версия опубликована тут». Это дешёво и упрощает historical navigation.

## 5. Публикация

### Вариант A: gh CLI (рекомендуемый)

```bash
# Убедись, что gh авторизован
gh auth status

# Боевой запуск (явный флаг — защита от случайного запуска)
.venv/Scripts/python.exe tools/release_v252.py --run
```

Под капотом:

```
gh release create v2.5.2 --repo AlexEgorov85/workspaces_nanobot \
  --title v2.5.2 --notes-file <tempfile>
```

### Вариант B: curl (если gh недоступен)

```bash
# Печатает готовую curl-команду с путём к body
.venv/Scripts/python.exe tools/release_v252.py --curl

# Дальше руками:
$env:GITHUB_TOKEN = "<PAT с правами repo>"
curl -X POST -H "Authorization: Bearer $env:GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/AlexEgorov85/workspaces_nanobot/releases \
  --data-binary @<body.json> --fail-with-body -o response.json
```

## 6. Push и анонс

```bash
# 1) master с release-артефактами
git push origin master

# 2) release/v2.5.2
git push origin release/v2.5.2

# 3) Аннонс (опционально — README.md → "Что нового" уже сделано выше)
#    Slack / Telegram / Notion — по принятому в команде каналу
```

## 7. Чек-лист «что не забыл»

- [ ] `project.json::project.version` бампнут
- [ ] `CHANGELOG.md` блок `## [<version>]` + новый пустой `## [Unreleased]`
- [ ] `README.md` блок «Что нового в v<version>»
- [ ] `docs/*.md` — аудит устаревших ссылок (пути, опции)
- [ ] `tools/release_v<X>_<Y>_<Z>.py` создан по образцу v2.5.1
- [ ] `pytest -q` зелёный
- [ ] `git commit` один atomic-коммит с шаблонным сообщением
- [ ] `release/v<X>.<Y>` ветка создана и запушена
- [ ] `gh release create --run` (или curl-эквивалент)
- [ ] `git push origin master` + `git push origin release/v<X>.<Y>`
- [ ] Анонс в командный чат (если есть)

## 8. Что **не** делать

- ❌ Не редактировать старые блоки в `CHANGELOG.md` (это история; правки только в новом блоке).
- ❌ Не править `project.json::version` задним числом для уже опубликованного тега.
- ❌ Не создавать несколько release-скриптов под один тег — один файл = одна версия.
- ❌ Не писать секреты в payload-скрипт (он уйдёт в git и GitHub Release).
