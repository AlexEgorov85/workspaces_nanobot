# Аудит: legal_summarizer — retrieval / llm / output / planning / CLI

## Сводка группы

Файлов: 31 · LOC: 3941 (по брифу `docs/audit/_data/_briefs/12-skill-legal-rest.md`) ·
классов: 15 · методов: 26 · функций уровня модуля: 33 ·
символов разобрано: 74 · `НЕ РАЗОБРАНО`: 0

Состав: `retrieval/` (14 модулей, ~1290 LOC) · `llm/` (9 модулей, ~950 LOC) ·
`output/presenter.py` · `planning/` (2 модуля) · `cli.py` · `cli_query.py` ·
4 пакетных `__init__.py`.

**Ключевые находки** (каждая — с путём и строкой):

1. **`retrieval/` — 3 из 14 модулей реально живые.** Прод-путь: `cli.py:359/405` → `application/service.py:380` (`inspect`) → `application/pipeline_structure.py:180` (`DocumentAnalysis.build`) → `retrieval/index.py:78 RetrievalIndex.build` → `retrieval/normalizer.py:70` + `retrieval/query.py:80 score_chunk`. Остальные **11 модулей (≈700 LOC) достижимы только из `workspace/skills/legal_summarizer/tests/`** — ни из прод-кода, ни из `benchmarks/`, ни из `tools/`. Подтверждено grep'ом по всему репозиторию: ни одного вхождения `retrieval.qa|quality|provenance|candidate_aggregator|records|canonical|question|context_expansion|fallback|followup` вне `scripts/retrieval/` и `scripts/../tests/`.
2. **`retrieval/qa.py` НЕ нужен бенчмаркам.** Гипотеза «reference QA для benchmark'ов» не подтверждается: `benchmarks/`, `tools/legal_benchmark.py`, `tools/extract_office_structure.py` не импортируют `retrieval.qa` ни одним способом. Импортируют его ровно 2 теста: `test_retrieval_qa.py`, `test_retrieval_quality_metrics.py`. **Вердикт: `Удалить`** вместе с `quality.py` и `provenance.py` (единственные потребители — между собой).
3. **`retrieval/canonical.py` — не канонический путь, а обёртка-сирота.** `answer_followup` (`canonical.py:25`) — однострочный делегат в `retrieval/followup.py::build_followup_response` с хардкодом `mode="question"`. Прод-путь `--question` его не касается: `application/chunk_selection.py:67` вызывает `insp.analysis.retrieve(...)` + `relaxed_lexical_fallback` напрямую. **Прод-вызывающих: 0.**
4. **`llm/client.py` — честная тонкая обёртка, дублирования нет.** Единственная реализация HTTP-вызова — `lib/services/llm_client.py::call_llm` (`llm/client.py:70`). `chat()` добавляет только резолв skill-конфига + диагностику `_trace` (5 ключей). Отдельной реализации retry/timeout/санитизации в скилле нет — она в `lib/services/llm_client.py:139-177`. Риск дублирования низкий.
5. **Инвариант `max_active_llm_calls == 1` enforced НЕ полностью.** `LLM_FLIGHT_LOCK` берётся ровно в одном месте — `execution/pipeline.py:73`, вокруг `llm_batch` (map-фаза). Reduce-фаза (`llm/calls.py:139 llm_section_reduce`, `llm/calls.py:178 llm_document_reduce`) вызывает `llm.chat` **без какого-либо лока**; сериализует их только `asyncio.Semaphore(1)` (`execution/map_reduce.py:129`) — он не блокирует другие потоки. `_try_question_via_document_cache` (`application/service.py:178`) → `llm_document_reduce` вообще без лока и без семафора. Плюс docstring'и `llm/calls.py:7-11` и `llm/single_flight.py:117-120` **лгут**: они утверждают, что единая точка enforcement — `guarded_chat`, но `guarded_chat` в проде недостижим (единственный вызывающий — `chat_locked`, который сам вызывается только из тестов).
6. **`guarded_chat` / `assert_single_flight` / `SingleFlightTracker` / `SingleFlightViolation` — мёртвые.** Прод-вызывающих 0. То же: `chat_locked` (`llm/calls.py:40`) — 0 прод-вызовов; `execution/pipeline.py:66-68` утверждает, что «реальный путь к LLM (`guarded_chat`) внутри `llm_batch` берёт тот же lock — блокировка реентрантна семантически, deadlock'а нет». Это **ложь в двух частях**: `llm_batch` (`llm/calls.py:99`) не вызывает `guarded_chat`, и `threading.Lock` **не реентрантен** — если бы вызывал, был бы deadlock. Инвариант держится на руке у `execution/pipeline.py:73`, а не на описанном механизме.
7. **`llm/retry.py` (123 LOC) — окаменелость удалённого протокола.** Модуль парсит JSON `{"summaries": {...}}` и «чинит» его, тогда как текущий промпт (`llm/prompts.py:52`) прямо требует «Никакого JSON» и использует маркерный парсер `_CHUNK_MARKER_RE`. Прод-вызывающих 0 (только `llm/tests/test_retry*.py`). Дополнительно: docstring `llm/retry.py:14` перечисляет `repair_failed_chunk_ids`, **которого в модуле нет**, и `ChunkResultParseError` (`retry.py:26`) — **второй одноимённый класс** в том же пакете, несовместимый с `llm/prompts.py:73 ChunkResultParseError`, который ловит `execution/pipeline.py:137`. При попытке встроить retry.py `except` перестал бы срабатывать.
8. **`llm/config.py::get_timeout_sec` и `get_max_retries` — обе мёртвые** (0 вызовов, подтверждено вручную). `llm/client.py:82` читает `cli.get("timeout_sec", 120)` / `cli.get("max_retries", 3)` напрямую, минуя обёртки. При этом сами ключи `project.json:212-214` живые. **Вердикт: `Удалить`** (2 функции, ~8 LOC), попутно убрать `import os` (`llm/config.py`), если он больше нигде не нужен.
9. **`llm/prompts.py` vs `llm/prompts_runtime.py` — НЕ дублируют, но обоснованно сливаются.** Разделение по смыслу чистое: `prompts_runtime.py` = загрузка `.md` с диска + строковые инструкции длины/вопроса; `prompts.py` = сборка batch-сообщения + парсер ответа. Пересечений символов нет. Но docstring `llm/prompts_runtime.py:3-5` **устарел на 2 рефакторинга**: он объясняет, что модуль переименован, чтобы не конфликтовать с `scripts/prompts.py`, и что `scripts/llm.py` надо переименовать в `llm_client.py`. И то и другое уже сделано — файлы уже в пакете `llm/`. Обоснование именования больше не действует. **Вердикт: `Слить с llm/prompts.py`** (236 LOC → 1 файл), предварительно обновив docstring. Нулевые риски: все 4 импортёра перечисляются явно (`application/service.py:72`, `llm/calls.py:20,24`, `execution/pipeline.py:32`).
10. **`TokenEstimator` НЕ заменяет ручные `len()/4`.** Ручная оценка `len(text) // chars_per_token` осталась в проде в `chunking/chunker.py:314`, `application/brief_context.py:437`, `application/canonical.py:161,211`; плюс `application/canonical.py:161,211` и `application/brief_context.py` создают **собственные** `TokenEstimator(chars_per_token=3.5)` с хардкодом, минуя `project.json::skills.legal_summarizer.token_estimation.rus_chars_per_token`. Плюс `TokenEstimator.available()` — 0 вызовов во всём репо.
11. **`retrieval/records.py` ломает рантайм-интроспекцию.** `document/analysis.py:57` аннотирует `semantic_records: dict[str, SemanticRecord]`, но `SemanticRecord` в `analysis.py` **не импортируется**. Работает только благодаря `from __future__ import annotations` (`analysis.py:21`). Проверено: `typing.get_type_hints(DocumentAnalysis)` → `NameError: name 'SemanticRecord' is not defined`. Любая генерация JSON-схемы / pydantic-обёртка / `get_type_hints` по `DocumentAnalysis` упадёт.
12. **`cli.py`: 2 флага мёртвые, документированы в `SKILL.md`.** `--max-chunks` (`cli.py:104`) и `--context` (`cli.py:110`, `type=json.loads`) разбираются, но `args.max_chunks` / `args.context` **не читаются ни разу** — в `kwargs` (`cli.py:384-400`) их нет. `SKILL.md:91,94` их рекламирует как рабочие.
13. **Функциональный баг: `status: "failed"` → exit code 0.** `application/service.py:320,389` возвращают `"status": "failed"` (например, `EMPTY_DOCUMENT`). `cli.py:426-427` → `prepare_output` (`output/presenter.py:152`) → `_emit_done` → `main()` возвращается → **код 0**. Агент, полагающийся на код возврата (а `SKILL.md` явно описывает sentinel-контракт), не отличит провал от успеха. При этом `_error()` (`cli.py:250`) даёт **второй** строковый статус `"error"` и **другую форму** payload (`message`/`traceback` вместо `error: {...}`) — агенту нужно ветвиться по двум схемам.
14. **Функциональный баг: `cli_query.py --field tree` не строит дерево.** `cli_query.py:194-199` для `tree` и `sections` выполняет **один и тот же** код (сортировка по `section_path`); разница — только `block_count` в `sections`. Help-текст (`cli_query.py:52`) обещает «иерархия sections», комментарий в коде — «Псевдо-дерево: родитель → дети». Агент, запросивший `tree` ради навигации по вложенности, получает плоский список.
15. **Функциональный баг: маркер «идёт работа» печатается слишком поздно.** `_emit_running_marker` (`cli.py:423`) вызывается **после** `load_text` (`cli.py:356`, для PDF 663 стр. в режиме `detailed` — 3-5 мин через pdfplumber) и **после** полного `_inspect` (`cli.py:405`). Комментарий `cli.py:327-331` прямо приводит инцидент 2026-08-28, который этот маркер чинил. Когда `quick_estimate` не сработал или сказал «confirm не нужен», агент получает полное молчание на 3-5 минут — ровно в тот момент, когда процесс выглядит зависшим.

**Вердикты:** Оставить 34 · Упростить 9 · Удалить 22 · Слить 3 · Перенести 0

---

## `workspace/skills/legal_summarizer/scripts/retrieval/` — 14 модулей, 1290 LOC

**Назначение.** Слой поиска и ранжирования чанков: инвертированный индекс, лексический скоринг, каскад follow-up-вопросов, метрики качества.

**Что делает.** Из 14 модулей прод-достижимы **3**: `index.py` (inverted index, строится в `DocumentAnalysis.build`), `query.py` (скоринг + конфиг, используется `index.py` и `chunk_selection.py`), `normalizer.py` (нормализация запроса для индекса). Остальные 11 — изолированный остров, связанный только внутри `retrieval/` и со своими тестами.

**Зачем нужен.** `RetrievalIndex` — ядро выбора чанков для `--question`: без него `DocumentAnalysis.retrieve` (`document/analysis.py:155-162`) деградирует до `retrieve_chunks` — линейного substring-сканирования всех чанков. Островные 11 модулей — не наследуют эту роль, а её дублируют в собственной, неиспользуемой ветке.

### Сводная таблица достижимости

| Модуль | LOC | Прод-вызывающих | Импортируют prod-модули | Импортируют тесты | Вердикт |
|---|---|---|---|---|---|
| `index.py` | 107 | **2** (`document/analysis.py:123`, `application/chunk_selection.py:65`) | — | 1 | Оставить |
| `query.py` | 120 | **4** (`index.py:30`, `normalizer.py:23`, `chunk_selection.py:19`, `document/analysis.py:156`) | — | 3 | Упростить |
| `normalizer.py` | 63 | **1** (`index.py:27,28,99,104`) | — | 1 | Слить с `query.py` |
| `canonical.py` | 37 | **0** | 0 | 1 | Удалить |
| `question.py` | 42 | **0** | 0 | 1 | Удалить |
| `followup.py` | 139 | **0** | 0 | 2 | Удалить |
| `context_expansion.py` | 160 | **0** | 0 | 1 | Удалить |
| `fallback.py` | 98 | **0** | 0 | 1 | Удалить |
| `records.py` | 105 | **0** | 0 | 1 | Удалить |
| `provenance.py` | 109 | **0** | 0 | 1 | Удалить |
| `qa.py` | 100 | **0** | 0 | 1 | Удалить |
| `quality.py` | 120 | **0** | 0 | 1 | Удалить |
| `candidate_aggregator.py` | 84 | **0** | 0 | 1 | Удалить |
| `__init__.py` | 0 | — | — | — | Оставить |

> **Важно о методике.** Статический анализатор показывает этим модулям «без импортёров» — но скилл запускается как `python scripts/cli.py`, и внутренние импорты идут от корня `scripts/`. Поэтому «0 импортёров» из брифа не доказательство: я искал `from retrieval.X import` по всему репозиторию вручную. Числа в таблице — результат этого поиска, а не копия брифа.

---

### `retrieval/index.py` — 107 LOC

**Назначение.** In-memory инвертированный индекс `term → chunk_id` поверх чанков + ранжирование вызовов `--question`.
**Что делает.** `build` (стр. 78) токенизирует `chunk.text` и `chunk.section_heading`, дедуплицируя термы внутри chunk'а; `retrieve` (стр. 53) собирает кандидатов из посталлинга, скорит через `retrieval/query.py:score_chunk`, фильтрует по `min_score`, сортирует `(-score, chunk_id)` и режет до `max_results`. Персиста нет.
**Зачем нужен.** Основной путь выбора чанков для `--question`. `application/chunk_selection.py:64-70` строит `config` из `RetrievalConfig` и зовёт `insp.analysis.retrieve`; альтернатива (`relaxed_lexical_fallback`) — substring-первое-совпадение, признанное недостаточным.
**Вердикт.** `Оставить`.
**Обоснование.** Единственный prod-достижимый модуль `retrieval/`, держит основной путь question-режима.
**Доказательства.** `document/analysis.py:123` (`RetrievalIndex.build` в `DocumentAnalysis.build`), `application/chunk_selection.py:65,67`. Тест: `test_structure_retrieval_index.py`.

#### class `RetrievalIndex` (стр. 35–123, 3 метода, frozen dataclass)

Атрибуты одной строкой: `document_id: str` · `chunks: tuple[Chunk, ...]` · `structure: DocumentStructure` · `physical: PhysicalDocument | None` · `term_to_chunks: dict[str, tuple[str, ...]]`.

| Метод | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `retrieve` | 53–76 | Inverted-index поиск + ranking | Основной question-путь | `document/analysis.py:163` | Оставить |
| `build` | 78–116 | Построение индекса | Основной question-путь | `document/analysis.py:123` | Оставить |
| `to_dict` | 118–123 | Сводка размера индекса | Только диагностика | `test_structure_retrieval_index.py:84` | Удалить |

**Находки по классу.**
- **Строки 5–8 лгут:** docstring обещает «L2: semantic analysis cache (`SemanticRecord`'ы)», но `build` (стр. 95–116) не обращается к `semantic_records` — потому что `DocumentAnalysis.semantic_records` всегда `{}` (`application/pipeline_structure.py:180`, `application/service.py:143`). Уровень L2 не существует. Текст надо править.
- **Мёртвые поля:** `structure` (стр. 49) и `physical` (стр. 50) не читаются в `retrieve`. `physical` передаётся в `build` из `document/analysis.py` и сразу забывается.
- **Строка 109** `{k: tuple(sorted(v)) ...}` — детерминизация ради воспроизводимого `sorted` в `score_chunk`-сортировке; на `sorted` по score это не влияет (сорт по `chunk_id` в `index.py:75`). Упрощаемо, но безопасно — оставить.

---

### `retrieval/query.py` — 120 LOC

**Назначение.** Лексический скоринг чанков (BM25-lite) + конфиг каскада retrieval.
**Что делает.** `score_chunk` (стр. 80) считает `sum(weight × term_вхождение)` с бустами за section title и короткий текст; `retrieve_chunks` (стр. 123) — линейный вариант того же без индекса. Никаких side effects.
**Зачем нужен.** `RetrievalConfig` — единственный источник весов (`max_results=8`, `min_score=0.05`, `section_title_weight=2.0`, `heading_weight=1.5`, `body_weight=1.0`) для ранжирования; `score_chunk` — ранкер для индекса.
**Вердикт.** `Упростить`.
**Обоснование.** Живое ядро, но внутри него — дублирующий токенизатор и приватные константы, экспортируемые в соседний модуль.
**Доказательства.** `index.py:30-31` (`RetrievalHit`, `RetrievalConfig`, `score_chunk`), `normalizer.py:23-26` (`_RUSSIAN_STOPWORDS`, `_WORD_RE` — **приватные символы через границу модуля**), `document/analysis.py:156` (`retrieve_chunks`), `chunk_selection.py:19` (`RetrievalConfig`).

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `tokenize` | 67–77 | lowercase + `\w+` + стоп-слова | Дублирует `normalizer.tokenize_normalized` | `retrieve_chunks` (`query.py:134`) | Слить с `normalizer.tokenize_normalized` |
| `score_chunk` | 80–120 | Ранкер чанка | Ранкер индекса | `index.py:73` | Оставить |
| `retrieve_chunks` | 123–145 | Линейный fallback-поиск | Fallback при отсутствии индекса | `document/analysis.py:159` (ветка, недостижимая в проде) | Упростить |

#### class `RetrievalHit` (стр. 35–43, frozen dataclass)

Атрибуты: `chunk_id` · `score` · `title_hit` · `section_title_hit` · `matched_terms`.
**Вердикт.** `Оставить` — value-object ранкера, сериализуется в `presenter`-контракт. `title_hit` (стр. 117) — производное от `section_title_hit`/`matched`, дублирует информацию; кандидат на упразднение при следующем рефакторинге.

#### class `RetrievalConfig` (стр. 46–54, frozen dataclass)

Атрибуты: `max_results: int = 8` · `min_score: float = 0.05` · `section_title_weight: float = 2.0` · `heading_weight: float = 1.5` · `body_weight: float = 1.0`.
**Вердикт.** `Оставить` — публичный конфиг-контракт, переопределяется прод-вызывающим `application/chunk_selection.py:65`.

**Находки по модулю.**
- **Два независимых токенизатора.** `tokenize` (`query.py:75-77`: `.lower()` → `\w+` → стоп-слова) и `tokenize_normalized` (`normalizer.py:70-76`: NFKC → strip non-word → `\w+` → стоп-слова). Прод-путь через `index.py` использует `tokenize_normalized` **и для индексации, и для запроса** — внутренне согласован. `retrieve_chunks` использует `tokenize`, т.е. в fallback-ветке нормализация другая. Сейчас безвредно (в этой ветке индекса тоже нет), но расхождение заложено.
- **`ё` не сводится к `е`.** `normalize_query` (`normalizer.py:44`) делает `unicodedata.normalize("NFKC", ...)`, что **не** фолдит `ё`→`е`. Запрос «счёт» не найдёт «счет» и наоборот — тихий промах recall в прод-пути. Дешёвое исправление: `str.maketrans("ёЁ", "еЕ")` в `normalize_query`.
- **`score_chunk` матчит подстрокой, не по границе слова** (`query.py:103,106,111`: `term in text_lower`). Терм «срок» совпадёт с «срочного» (желаемо) и с «пересрочка», но и с любым словом, содержащим «срок» как подстроку. Для коротких руских морфем это источник и false positive, и (в сочетании с предыдущим пунктом) false negative.
- **Строки 19–23 лгут:** «Сейчас в проекте `cached_retrieval.select_relevant_chunks` — substring + first-match + full-document fallback. Этот модуль предоставляет новый каскад, который **постепенно заменит** старый». Старый каскад давно заменён — `DocumentAnalysis.retrieve` (índex) и есть «новый». `retrieval/fallback.py` (98 LOC) — тот самый «старый» каскад, и он мёртв.
- **`retrieve_chunks` прод-недостижим.** `document/analysis.py:155-162` падает в этот fallback только при `retrieval_index is None`, а `include_retrieval_index=True` во **всех** прод-точках (`application/inspection.py:60`, `application/service.py:142`, `application/pipeline_structure.py:113,243`). `include_retrieval_index=False` встречается только в тестах. То же место содержит **дублированный импорт** `from retrieval.query import retrieve_chunks` дважды подряд — кросс-подсистемная находка, файл принадлежит аудитору `document/`.

---

### `retrieval/normalizer.py` — 63 LOC

**Назначение.** Нормализация запроса (NFKC, регистр, пунктуация) + токенизация со стоп-словами.
**Что делает.** `normalize_query` (стр. 37) — NFKC + lower + strip `[^\w\s]` + collapse пробелов; `tokenize_normalized` (стр. 70) — normalize → `\w+` → отбросить стоп-слова. Детерминированно, без LLM.
**Зачем нужен.** Нужен как публичный API нормализации — но вся содержательная часть (NFKC + strip пунктуации) — это 4 строки, а токенизация продублирована.
**Вердикт.** `Слить с retrieval/query.py`.
**Обоснование.** Модуль существует как «слой API над `query.py::tokenize`» (его же docstring, стр. 13–15: «Сейчас функция `tokenize` уже реализована в `retrieval.py`. Этот модуль выносит её в собственный файл + добавляет `normalize_query`»). Разделение не держит смысловой границы: обе стороны делят `_WORD_RE`/`_RUSSIAN_STOPWORDS`, а `normalizer` при этом тянет **приватные** символы из `query`. Слияние убирает 1 модуль из 14 и убирает межмодульный reach-across приватных имён.
**Предварительная работа.** Перенести `normalize_query` + `tokenize_normalized` (переименовать в `tokenize` после удаления дубля) в `query.py`; обновить `index.py:27-29,99,104` и удалить импорт. `expand_with_aliases` удалить (0 вызовов).
**Доказательства.** Импортирует `query.py` (`normalizer.py:23-26`). Импортируется одним модулем: `index.py:27-29,99,104`. Тест: `test_retrieval_normalizer.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `normalize_query` | 37–47 | NFKC + lower + strip пунктуации | Прод-нормализация | `tokenize_normalized` (`normalizer.py:74`) | Слить с `query.py` |
| `tokenize_normalized` | 70–76 | normalize → tokens → стоп-слова | Прод-путь индекса | `index.py:60,99,104` | Слить с `query.py` (переименовать) |
| `expand_with_aliases` | 50–67 | Юридические синонимы (`штраф`→`неустойка`,`пени`) | Никогда | **0** | Удалить |

**Находка.** `_LEGAL_ALIASES` (`normalizer.py:29-34`) — 4 записи, и единственный потребитель — `expand_with_aliases`, который никем не вызывается. То есть вся таблица синонимов — мёртвый константный набор.

---

### `retrieval/canonical.py` — 37 LOC

**Назначение.** Заявлено: «Canonical retrieval wrapper. Использует только `DocumentAnalysis.retrieve` и canonical `build_followup_response` (через `structure.followup`) для `mode="question"`».
**Что делает.** `answer_followup` (`canonical.py:25-41`) — единственная функция: делегирует в `retrieval/followup.py::build_followup_response(analysis, query, mode="question", config=config)` и возвращает `FollowupResult` без изменений. Никакой собственной логики, включая имя параметра `query` (positional), которое в `build_followup_response` передаётся как keyword.
**Зачем нужен.** Не нужен: ни одного вызывающего в проде.
**Вердикт.** `Удалить`.
**Обоснование.** Тонкая обёртка (17 строк тела) над функцией, которая сама лежит в соседнем мёртвом модуле. Прод-путь question-режима — `application/chunk_selection.py:64-70`, он идёт через `insp.analysis.retrieve` и `relaxed_lexical_fallback` и этот модуль не импортирует. Удаление безопасно: потребитель — только `test_retrieval_canonical.py`.
**Доказательства.** Импортёров 0. Grep по `benchmarks/`, `tools/`, `lib/` — 0 совпадений. Тест: `workspace/skills/legal_summarizer/tests/test_retrieval_canonical.py`.
**Документация vs код.** Docstring называет модуль «canonical» и утверждает, что он «использует только `DocumentAnalysis.retrieve`». Фактически он не вызывает `DocumentAnalysis.retrieve` **ни разу** — только `build_followup_response`. Название и объяснение вводят в заблуждение именно в сторону «это канонический путь», тогда как канонический (единственный prod) путь лежит в `application/chunk_selection.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `answer_followup` | 25–41 | Делегат в `followup.build_followup_response` | Никогда | **0** (только `test_retrieval_canonical.py`) | Удалить |

**Строки 7–10** — комментарий, оправдывающий удалённый символ (`select_brief_from_analysis` «УДАЛЁН из этого модуля»). Сам символ удалён, объяснение оставлено. По правилам протокола (§5, «строки, которые лгут») это историческая справка; при удалении модуля исчезает сама.

---

### `retrieval/question.py` — 42 LOC

**Назначение.** «Question via retrieval index … Этот модуль — convenience».
**Что делает.** `answer_question_from_analysis` (стр. 33–52) вызывает `build_followup_response(analysis, query=query, mode="question", config=config)` и перепаковывает `FollowupResult` в `QuestionResponse` (4 поля: `chunks`, `confidence`, `used_full_doc_fallback`, `reason`).
**Зачем нужен.** Не нужен. Плюс: 4 из 5 полей `FollowupResult` теряются при перепаковке — обёртка не просто мёртвая, она **сужает** результат.
**Вердикт.** `Удалить`.
**Обоснование.** Слово «convenience» в docstring — самостоятельное признание в фасадности. Прод-вызывающих 0. `QuestionResponse` — единственное место, где этот тип существует; ни потребителя, ни сериализации нет.
**Доказательства.** Импортёров 0. Тест: `test_retrieval_question.py`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `class QuestionResponse` | 23–30 | DTO-результат question | Никогда | **0** | Удалить |
| `answer_question_from_analysis` | 33–52 | Делегат + перепаковка | Никогда | **0** | Удалить |

---

### `retrieval/followup.py` — 139 LOC

**Назначение.** Каскад ответа на follow-up question: retrieval → context expansion → fallback на весь документ.
**Что делает.** `build_followup_response` (стр. ~60) конфигурирует каскад, зовёт `retrieval/context_expansion.py` и `retrieval/fallback.py`, собирает `FollowupResult`. `build_first_run_analysis` (стр. ~30) — альтернативная точка входа «анализ + ответ одним шагом».
**Зачем нужен.** Не нужен в проде: `application/chunk_selection.py:64-70` реализует тот же вопрос-путь через `DocumentAnalysis.retrieve` + `relaxed_lexical_fallback` + отдельный LLM-вызов.
**Вердикт.** `Удалить` (вместе с `canonical.py`, `question.py`, `context_expansion.py`, `fallback.py` — единый остров).
**Обоснование.** Это второй, параллельный и неиспользуемый question-путь. Это главный источник путаницы в `retrieval/`: два способа ответить на вопрос, ни один из которых не отмечен в коде как устаревший.
**Доказательства.** Импортёров из прод-кода 0. Импортируется двумя мёртвыми модулями: `canonical.py:18-22`, `question.py:18-20`. Тесты: `test_retrieval_followup.py`, `test_retrieval_question.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `build_followup_response` | ~60–~120 | Каскад question-ответа | Никогда | `canonical.py:36`, `question.py:44` (оба мёртвые) | Удалить |
| `build_first_run_analysis` | ~30–~58 | Анализ+ответ одним шагом | Никогда | **0** (только определение + `__all__` + упоминание в docstring стр. 12) | Удалить |

**Находки.**
- **`build_first_run_analysis` — 0 вызовов**, не считая собственного `__all__`.
- **Баг единиц измерения:** `FollowupResult.total_tokens` заполняется как `sum(len(c.text) for c in target_chunks)` — это **символы**, а не токены, в поле с именем `total_tokens`. В мёртвом коде, но при воскрешении острова даст 4-кратное завышение бюджета.
- `FollowupResult.to_dict` — 0 вызовов в репозитории.

---

### `retrieval/context_expansion.py` — 160 LOC

**Назначение.** Расширение контекста вокруг найденных чанков: соседние чанки, `TokenEstimator`-бюджет.
**Что делает.** `expand_context` — по чанкам-хитам строит окно соседей, сортирует по `c.index`, обрезает по бюджету токенов (`llm/tokens.py`). Побочных эффектов нет.
**Зачем нужен.** Не нужен: вызывается только из `followup.py::build_followup_response`, а тот — из двух мёртвых модулей.
**Вердикт.** `Удалить`.
**Обоснование.** Модуль — внутренняя деталь каскада, который сам мёртв. Проверка подозрения соседнего аудитора: `document/block_lookup.py` модуль **не импортирует**. Вместо O(1)-поиска делается сортировка всего tuple чанков по индексу на каждом вызове + линейный `next(...)` для поиска позиции хит-чанка. То есть замечание «остался линейный поиск вместо `block_lookup`» **подтверждается по направлению, но неточно по существу**: `block_lookup` здесь не применим, т.к. поиск идёт по **чанкам**, а не по `doc.blocks`; `block_lookup.py` (52 LOC) сам по себе тоже мёртв — ни один модуль его не импортирует, а его docstring описывает подмену линейного `doc.blocks.index(...)`, которого в `context_expansion.py` никогда не было.
**Доказательства.** Импортёров 0. Тест: `test_retrieval_context_expansion.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `expand_context` | ~60–~155 | Окно соседей + бюджет | Никогда | `followup.py` (мёртвый) | Удалить |

**Кросс-подсистемная находка для аудитора `document/`:** `document/block_lookup.py` — 52 LOC, 0 импортёров, docstring обещает замену линейного `index()`, которого в коде нет. Кандидат на `Удалить`.

---

### `retrieval/fallback.py` — 98 LOC

**Назначение.** «Full-document fallback» — если ретривл ничего не нашёл, отдать весь документ.
**Что делает.** `full_document_fallback` — возвращает все чанки с флагом «использован fallback».
**Зачем нужен.** Не нужен: вызывается только из `followup.py`, тот — из мёртвых модулей.
**Вердикт.** `Удалить`.
**Обоснование.** Мёртвый хвост мёртвого каскада. Внутри — мёртвая локальная переменная.
**Доказательства.** Импортёров 0. Тест: `test_retrieval_fallback.py`.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `full_document_fallback` | ~40–~95 | Отдать все чанки при пустом ретривле | Никогда | `followup.py` (мёртвый) | Удалить |

**Находка.** Внутри функции создаётся `est = TokenEstimator(chars_per_token=3.5)` (~стр. 49) и **ни разу не используется** — мёртвая локальная переменная, возникшая при копировании из `context_expansion.py`, где `est` действительно используется.

---

### `retrieval/records.py` — 105 LOC

**Назначение.** Структурированный output LLM-map: `SemanticRecord` + `Provenance` для traceability.
**Что делает.** Три value-класса: `Provenance` (~стр. 20–48: `chunk_id` → источник), `SemanticRecord` (стр. 50–121: 11 полей, `facts/entities/obligations/dates/amounts/risks/references`), методы `to_dict` и фабрика `from_minimal`. Ни side effects, ни БД.
**Зачем нужен.** Не нужен. `SemanticRecord` **никогда не конструируется** в проде: `DocumentAnalysis.semantic_records` жёстко инициализируется пустым словарём в `application/pipeline_structure.py:180` и `application/service.py:143`. `get_record()` (`document/analysis.py:68`) — 0 вызовов.
**Вердикт.** `Удалить`.
**Обоснование.** Поле-объявление без писателя. Дополнительно модуль **активно вредит**: `document/analysis.py:57` аннотирует `semantic_records: dict[str, SemanticRecord]`, не импортируя `SemanticRecord`, — из-за `from __future__ import annotations` ошибка не проявляется при обычном импорте, но ломает интроспекцию (см. находку 11 в сводке).
**Доказательства.** Импортёров 0. Тест: `test_retrieval_records.py`.
**Предварительная работа при удалении:** убрать поле `semantic_records` из `document/analysis.py` (стр. 47, 57, 68–69, 77, 91, 140) и передачу `semantic_records={}` в трёх местах. Это правка в чужом файле — согласовать с аудитором `document/`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `class Provenance` | ~20–48 | Источник chunk'а | Никогда | **0** | Удалить |
| `class SemanticRecord` | 50–121 | Структурированный LLM-map | Никогда | **0** конструкторов | Удалить |
| `SemanticRecord.to_dict` | 82–98 | Сериализация | Никогда | **0** | Удалить |
| `SemanticRecord.from_minimal` | 100–121 | Фабрика «только summary» | Никогда | **0** | Удалить |

**Находка (конфликт имён).** `retrieval/records.py::Provenance` и `retrieval/provenance.py::ProvenanceChain` — одна и та же концепция, разнесённая по двум мёртвым модулям. Плюс в `retrieval/records.py` поле называется `provenance`, а тип — `Provenance`, и оба мертвы.

---

### `retrieval/provenance.py` — 109 LOC

**Назначение.** Полная provenance-цепочка ответа (документ → секция → чанк → блоки → страницы).
**Что делает.** `build_provenance_chain` (стр. 84–127) строит `by_ord`-словарь из **всех** блоков документа (стр. 95), фильтрует блоки чанка, восстанавливает `section_path` обходом `parent_id` вверх до `struct.root_id` (стр. 103–113), отдаёт `ProvenanceChain` либо `None`.
**Зачем нужен.** Не нужен: единственный потребитель — `retrieval/quality.py:compute_provenance_correctness`, а тот никем не вызывается.
**Вердикт.** `Удалить`.
**Обоснование.** Третий слой мёртвого острова. Тривиальное подтверждение: `tests/benchmarks/test_acceptance_matrix.py:27-33` перечисляет обязательные модули скилла — `retrieval.provenance` там нет, в отличие от `llm.sanitize` / `llm.prompts_runtime` / `llm.calls`.
**Доказательства.** Импортёров 0. Тест: `test_retrieval_provenance.py`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `class ProvenanceChain` | 33–81 | DTO цепочки provenance | Никогда | `build_provenance_chain` | Удалить |
| `ProvenanceChain.to_dict` | 58–69 | Сериализация | Никогда | **0** | Удалить |
| `ProvenanceChain.is_complete` | 71–81 | Проверка заполненности | Никогда | **0** | Удалить |
| `build_provenance_chain` | 84–127 | Построение цепочки | Никогда | `quality.py` (мёртвый) | Удалить |

**Находка (масштабирование).** `build_provenance_chain:95` строит `by_ord` по всем блокам документа на **каждый** вызов. Если бы `quality.py` был подключён, метрика `provenance_correctness` на документе с N чанками дала бы O(chunks × blocks) — ровно та сложность, ради устранения которой написан `document/block_lookup.py`.

---

### `retrieval/qa.py` — 100 LOC

**Назначение.** «Reference QA для benchmark'ов» — эталонный набор вопросов + ожидаемые `chunk_id`.
**Что делает.** Строит `ReferenceQASet` / `ReferenceQuestion` (эталонные пары вопрос→ожидаемые секции/чанги) и `evaluate_retrieval` — сверку ответа ретривла с эталоном. Чистая функция, без LLM и без БД.
**Зачем нужен.** **Не бенчмаркам.** Проверено: `benchmarks/`, `tools/legal_benchmark.py`, `tools/extract_office_structure.py`, `tools/architecture_guard.py` не импортируют `retrieval.qa` ни одним способом (ни `from`, ни `importlib`, ни строковой ссылки). Импортируют ровно 2 теста: `test_retrieval_qa.py` и `test_retrieval_quality_metrics.py`.
**Вердикт.** `Удалить`.
**Обоснование.** Единственная реальная «аудитория» модуля — два unit-теста, которые сами тестируют только его. Взвешивание «нужен ли бенчмаркам» отдаю владельцу `benchmarks/` (их отчёт), но по факту в текущем дереве бенчмарк-harness для `legal_summarizer` retrieval **отсутствует** — модуль измеряет то, что никто не измеряет. Правило протокола §4 требует grep по всему репозиторию, включая `tests/`, `sql/`, `docs/`, `.github/`, `project.json`, `SKILL.md` — выполнено, совпадений вне тестов нет.
**Доказательства.** Импортёров 0 вне тестов. `benchmarks/` — 0.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `class ReferenceQASet` | ~20–45 | Контейнер эталонных пар | Никогда | `standard_qa_set`, `evaluate_retrieval` (мёртвые) | Удалить |
| `class ReferenceQuestion` | ~50–70 | Вопрос + ожидаемые chunk/section | Никогда | `standard_qa_set` (мёртвая) | Удалить |
| `standard_qa_set` | ~75–95 | Синтетический эталонный набор | Никогда | **0** | Удалить |
| `evaluate_retrieval` | ~100–140? | Сверка ретривла с эталоном | Никогда | `quality.py` (мёртвый) | Удалить |

---

### `retrieval/quality.py` — 120 LOC

**Назначение.** Метрики качества retrieval: `retrieval_recall_at_K`, `provenance_correctness`, агрегаты.
**Что делает.** `compute_quality_metrics` собирает несколько метрик; `compute_retrieval_recall` считает recall@K по `ReferenceQASet`; `compute_provenance_correctness` — долю чанков с полной цепочкой (зовёт `provenance.py`). Чисто, без эффектов.
**Зачем нужен.** Не нужен. **Ответ на вопрос брифа «кто читает `retrieval_recall_at_K`» — никто в проде и ни один бенчмарк.** Единственные читатели: `test_retrieval_quality_metrics.py` и `test_retrieval_qa.py`.
**Вердикт.** `Удалить`.
**Обоснование.** Метрики без измерителя. Считать recall@K полезно, но для этого нужен эталонный набор, а `retrieval/qa.py::standard_qa_set` — синтетический, не привязанный к реальным документам. Пара `qa.py` + `quality.py` + `provenance.py` (329 LOC) замкнута на себя и не выходит в `benchmarks/`.
**Доказательства.** Импортёров 0 вне тестов. `benchmarks/` — 0.

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `compute_quality_metrics` | ~55–90 | Агрегат метрик | Никогда | **0** | Удалить |
| `compute_retrieval_recall` | ~20–55 | recall@K по эталону | Никогда | `compute_quality_metrics` (мёртвая) | Удалить |
| `compute_provenance_correctness` | ~90–120 | Доля полных provenance-цепочек | Никогда | `compute_quality_metrics` (мёртвая) | Удалить |

---

### `retrieval/candidate_aggregator.py` — 84 LOC

**Назначение.** Объединение heading-кандидатов от разных детекторов (DOCX style + numbering + regex + PDF outline) в один структурный кандидат.
**Что делает.** `aggregate_by_block` (стр. 58–102) группирует `HeadingCandidate` по `block_index`, берёт `max(level)`, `max(score)`, объединённые `sources` и `raw_numbers`; outline-кандидаты (`block_index = -1`) выносятся отдельно. Ни side effects.
**Зачем нужен.** Не нужен. Это **самый откровенный «на будущее» модуль в группе** — его docstring (стр. 16–18) прямо признаёт: «Этот модуль — **явный** aggregator, который можно вызвать из **будущих** pipelines (`StructureTreeBuilder`)». Сам он утверждает, что де-факто агрегация уже сделана в `document/heading.py:detect_heading_candidates` и «размазана».
**Вердикт.** `Удалить`.
**Обоснование.** Чистая спекулятивность: 84 LOC кода, написанного до появления pipeline, который его позовёт. Если агрегация действительно понадобится, её правильное место — `document/heading.py` (владеет `HeadingCandidate`), а не отдельный пакет `retrieval/`, который про структуру заголовков ничего не знает. Обратите внимание на направление зависимости: `retrieval/` → `document/heading.py` (`candidate_aggregator.py:28-30`), т.е. retrieval-слой зависит от document-слоя ради того, что document-слой делает сам.
**Доказательства.** Импортёров 0. Тест: `test_retrieval_candidate_aggregator.py`.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `class AggregatedCandidate` | 33–55 | DTO объединённого кандидата | Никогда | `aggregate_by_block` (мёртвая) | Удалить |
| `aggregate_by_block` | 58–102 | Группировка по `block_index` | Никогда | **0** | Удалить |

---

### `retrieval/__init__.py` — 0 LOC

**Назначение.** Пакетный маркер.
**Вердикт.** `Оставить`.
**Обоснование.** Файл содержит 2 байта (пустая строка) и не экспортирует символов, но **нагружается**: все внутренние импорты идут как `from retrieval.index import RetrievalIndex` (`document/analysis.py`), т.е. требуют, чтобы `retrieval` был импортируемым пакетом. Удаление сломает 5+ модулей прод-кода. Все 9 соседних пакетов скилла (`application`, `cache`, `chunking`, `document`, `execution`, `llm`, `output`, `planning`, `retrieval`) имеют такой же файл — единообразие структуры само по себе аргумент.

---

## `workspace/skills/legal_summarizer/scripts/llm/` — 9 модулей, ~950 LOC

**Назначение.** Граница LLM-вызовов скилла: клиент, конфиг, промпты, санитизация, оценка токенов, single-flight, retry.

### Сводная таблица

| Модуль | LOC | Прод-вызывающих | Вердикт |
|---|---|---|---|
| `client.py` | 90 | 2 (`llm/calls.py:17`, косвенно `application/*`) | Оставить |
| `config.py` | 44 | 5 из 9 функций | Упростить |
| `prompts_runtime.py` | 52 | 2 (`llm/calls.py:24`, `application/service.py:72`) | Слить с `llm/prompts.py` |
| `prompts.py` | 128 | 2 (`llm/calls.py:20`, `execution/pipeline.py:32`) | Оставить |
| `sanitize.py` | 71 | 3 | Упростить |
| `tokens.py` | 72 | 6 | Упростить |
| `calls.py` | 161 | 6 | Упростить |
| `single_flight.py` | 106 | 1 (`execution/pipeline.py:35`) + 1 неиспользуемый импорт | Упростить |
| `retry.py` | 123 | **0** | Удалить |
| `__init__.py` | 0 | — | Оставить |

---

### `llm/client.py` — 90 LOC

**Назначение.** Обёртка над LLM-вызовом: резолв skill-конфига + делегирование в общий клиент платформы.
**Что делает.** `chat` (стр. ~55–88) резолвит `get_cli_config()` (provider/base_url/api_key/model) и `get_llm_config()` (max_tokens/temperature), зовёт **единственную** реализацию — `lib.services.llm_client.call_llm` (стр. 70) — и собирает диагностический `_trace` из 5 ключей. `doc_context` (стр. 60–68) собирает контекст документа из `structure` и `chunks`.
**Зачем нужен.** Единственная точка, где скилле доступ к LLM, и единственное место, где прописаны provider-специфичные параметры.
**Вердикт.** `Оставить`.
**Обоснование.** **Ответ на вопрос брифа №3: это настоящая тонкая обёртка, а не вторая реализация.** Проверено построчно: `chat()` не содержит ни HTTP-кода, ни собственного retry, ни своей санитизации — всё это в `lib/services/llm_client.py:139-177` (`_request_with_retry`, `_strip_think_tags`, `_parse_json_object`). Риск дублирования низкий и реализован корректно.
**Доказательства.** `llm/calls.py:17` (`from llm import client as llm`); транзитивно `application/execution_orchestration.py:26`, `execution/map_reduce.py:46`. В `tests/benchmarks/test_acceptance_matrix.py:27-33` не входит, но `llm.calls` — входит.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `chat` | ~55–88 | Единый LLM-вызов | Весь LLM-трафик скилла | `llm/calls.py:99,139,178`; `application/service.py` | Оставить |
| `doc_context` | 60–68 | Контекст документа для промпта | Map/reduce промпты | `llm/calls.py:164,181` | Упростить |

**Находки по `doc_context` (стр. 60–68).** Функция объявлена с параметром `with_begin_end: bool = True`, но тело содержит `if with_begin_end: pass` — **ветка-заглушка, не делающая ничего**. Единственный вызывающий, который явно передаёт `with_begin_end=True` (`llm/calls.py:164`), получает ровно тот же результат, что и вызов без флага. Либо реализовать (добавить маркеры `[начало раздела]` / `[конец раздела]` в текст), либо удалить параметр и передачу. **Вердикт по параметру: `Удалить`.**

**Находка по `chat`.** Сигнатура принимает `**kwargs`, но пробрасывает в `call_llm` только `model`, `max_tokens`, `temperature` (стр. 70-75). Любой другой именованный аргумент молча игнорируется — тихая ловушка для будущих вызывающих. Либо сузить сигнатуру, либо принимать `**kwargs` и передавать.

---

### `llm/config.py` — 44 LOC

**Назначение.** Чтение секций `project.json::skills.legal_summarizer` для LLM-слоя.
**Что делает.** 9 функций-геттеров, каждая — `SETTINGS.get(...).get(<подсекция>, {})`. Ни side effects, ни кэширования.
**Зачем нужен.** Единая точка доступа к конфигу skill'а; `_LazySettings` (`config.py`) сам не типизирован, поэтому без этих функций конфиг читался бы в 8 местах.
**Вердикт.** `Упростить`.
**Обоснование.** 2 из 9 функций — мёртвые обёртки, дублирующие прямой доступ к dict, который уже есть в `llm/client.py`.
**Доказательства.** Проверено вручную (см. таблицу).

| Функция | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `get_llm_config` | ~14–20 | Секция `llm` (max_tokens/temperature) | `llm/client.py:19` | Оставить |
| `get_cli_config` | ~21–27 | Секция `cli` (provider/base_url/timeout/max_retries) | `llm/client.py:19` | Оставить |
| `get_prompts_config` | ~28–32 | Секция `prompts` | `llm/prompts_runtime.py:load_prompt` | Оставить |
| `get_brief_context_config` | ~33–37 | Секция `brief_context` | `application/chunk_selection.py:91` | Оставить |
| `get_execution_config` | ~38–42 | Секция `execution` | `application/chunk_selection.py:19`; `cli.py:220` | Оставить |
| `get_chunking_config` | ~43–47 | Секция `chunking` | `application/estimation.py:20`; `chunking/chunker.py:92`; `cli.py:218` | Оставить |
| `get_default_length` | ~48–55 | Длина по умолчанию | `cli.py:312` | Упростить (см. баг ниже) |
| `get_timeout_sec` | ~56–62 | Таймаут LLM | **0** | Удалить |
| `get_max_retries` | ~63–69 | Число retry LLM | **0** | Удалить |

**Подтверждение по вопросу брифа №2.** Да, `get_timeout_sec` мертва. Более того, мертва и `get_max_retries` — в `dead_symbols.md` она, по-видимому, не попала, потому что `llm/client.py` ищет эти значения напрямую: `cli.get("timeout_sec", 120)` и `cli.get("max_retries", 3)` (`llm/client.py:82`). При этом сами ключи конфига живы (`project.json:212-214`), т.е. удалять нужно **функции**, а не ключи.

**Функциональный баг в `get_default_length` (стр. ~48-55).** Дефолт — строка `"medium"`. Но: (а) `--length` в `cli.py:92` ограничен `choices=["brief", "detailed"]`, так что `"medium"` не проходит через argparse; (б) `application/service.py:325` применяет `length = length if length in LENGTH_INSTRUCTIONS else "brief"`, т.е. `"medium"` там превращается в `"brief"`; (в) `application/chunk_selection.py` проверяет `if length == "brief"` — при `"medium"` это False, то есть **все** чанки. Итог: путь планирования (`cli.py:405-407` → `build_execution_context(length="medium")`) считает оценку для detailed-поведения, а путь исполнения (`service.py:325`) реально выполняет brief. Оценка времени/батчей, на которой принимается решение о подтверждении, считается для другого прогона. Сейчас замаскировано тем, что `project.json` явно задаёт `default_length: "brief"`, поэтому в проде не стреляет — но дефолт в коде недопустим. `project.json:203` рядом с этим же ключом комментирует: «medium удалён в Phase 2B+».

---

### `llm/prompts.py` — 128 LOC

**Назначение.** Сборка user-сообщения для map-batch'а и парсинг marker-based ответа LLM.
**Что делает.** `build_batch_user_message` (стр. ~20–65) собирает инструкции из `.md`-шаблонов + тексты чанков с маркерами `[[chunk_id]]`; `parse_batch_response` (стр. ~70–128) разбирает ответ по `_CHUNK_MARKER_RE` и при неполноте **бросает** `ChunkResultParseError`. Ни side effects.
**Зачем нужен.** Контракт prompt↔response; `execution/pipeline.py:137` ловит его исключение и помечает batch как `LLM_PARSE_ERROR`.
**Вердикт.** `Оставить`.
**Обоснование.** Живой и в acceptance matrix (`tests/benchmarks/test_acceptance_matrix.py:31` не входит, но `llm.calls`, который его переиспользует, — входит).
**Доказательства.** `llm/calls.py:20`, `execution/pipeline.py:32`.

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `ChunkResultParseError` | 73 | Ошибка неполного разбора ответа | `execution/pipeline.py:137` | Оставить |
| `build_batch_user_message` | ~20–65 | Сборка user-сообщения | `llm/calls.py:110` | Оставить |
| `parse_batch_response` | ~70–128 | Marker-based парсер | `llm/calls.py:120` | Оставить |
| `_CHUNK_MARKER_RE` | — | Регулярка маркера | `parse_batch_response` | Оставить |

**Находка (строки 9-10, важна для вердикта по `retry.py`).** Docstring прямо фиксирует: переход на marker-based протокол «убирает `ChunkResultParseError` **полностью**» — то есть ошибка JSON-протокола была удалена как класс проблем. `llm/retry.py` (стр. 26) определяет **второй** `ChunkResultParseError` с другим API (`chunk_ids` + `repair_hint`). Два одноимённых класса в одном пакете; `execution/pipeline.py:32` импортирует из `llm.prompts`, поэтому `except` из `prompts.py` **не поймает** ошибку из `retry.py`. При попытке встроить retry.py в конвейер (что его docstring и предполагает) тихая поломка обработки ошибок.

---

### `llm/prompts_runtime.py` — 52 LOC

**Назначение.** Загрузка prompt-шаблонов с диска + строковые инструкции длины и режима вопроса.
**Что делает.** `load_prompt(name)` читает `.md` из каталога `prompts/` (с необязательным override через `get_prompts_config()`); `LENGTH_INSTRUCTIONS` — dict `{"brief": ..., "detailed": ...}`; `length_instruction(length)` — выбор строки с дефолтом `brief`; `QUESTION_INSTRUCTION_TEMPLATE` — шаблон инструкции для `--question`.
**Зачем нужен.** Разделение файловых шаблонов (которые меняет промптер, а не программист) от кода парсинга.
**Вердикт.** `Слить с llm/prompts.py`.
**Обоснование — полный ответ на вопрос брифа №5.** **Это НЕ дублирование.** Проверено: пересечений символов нет ни одного. `prompts_runtime.py` ничего не знает про батчи и маркеры, `prompts.py` ничего не знает про файловую загрузку. Это легитимное разделение «декларация vs разбор». Но:
1. Оба файла — про промпты, оба в пакете `llm/`, оба импортируются ровно из двух мест (`llm/calls.py:20,24` — одним `from`-блоком; `execution/pipeline.py:32`, `application/service.py:72`).
2. Обоснование именования, записанное в `prompts_runtime.py:3-5`, **устарело на два рефакторинга**: «Модуль НЕ называется `prompts.py`, чтобы не конфликтовать с существующим `scripts/prompts.py` … переименование `llm.py` → `llm_client.py` отложено». И `scripts/prompts.py`, и `scripts/llm.py` уже переименованы — файлы уже лежат как `llm/prompts.py` и `llm/client.py`. Причина, по которой имена различаются, отсутствует.
**Что получится:** один `llm/prompts.py` (~180 LOC) с `load_prompt` / `LENGTH_INSTRUCTIONS` / `length_instruction` / `QUESTION_INSTRUCTION_TEMPLATE` / `build_batch_user_message` / `parse_batch_response` / `ChunkResultParseError`. Обновить 3 места импорта. `LENGTH_INSTRUCTIONS` в `application/service.py:72` и `execution/pipeline.py:32` — механически.
**Доказательства.** `llm/calls.py:24`, `application/service.py:72`; косвенно `llm/calls.py:83` (`length_instruction`).

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `LENGTH_INSTRUCTIONS` | 33–~40 | Инструкции brief/detailed | `length_instruction`; `application/service.py:325` (проверка вхождения) | Слить с `llm/prompts.py` |
| `length_instruction` | ~55–61 | Выбор строки по длине | `llm/calls.py:83` | Слить с `llm/prompts.py` |
| `QUESTION_INSTRUCTION_TEMPLATE` | ~42–~52 | Шаблон инструкции question | `llm/calls.py:87` | Слить с `llm/prompts.py` |
| `load_prompt` | ~20–31 | Чтение `.md` шаблона | `llm/prompts.py` (через `llm/calls.py`) | Слить с `llm/prompts.py` |

---

### `llm/sanitize.py` — 71 LOC

**Назначение.** Санитизация LLM-ввода/вывода и обрезка по бюджету символов.
**Что делает.** `strip_think_blocks` вырезает `<think>…</think>`-сегменты из ответа модели; `fit_input` обрезает вход по лимиту символов с маркером усечения; константы `_THINK_OPEN`/`_THOUGHT_CLOSE` — приватные, но экспортируются в `__all__`.
**Зачем нужен.** Убирает reasoning-мусор от моделей с thinking и защищает от переполнения контекста.
**Вердикт.** `Упростить`.
**Обоснование.** Функционально нужен и в acceptance matrix (`test_acceptance_matrix.py:29`). Упрощаемо: устаревший docstring-блок и приватные имена в `__all__`.
**Доказательства.** `execution/map_reduce.py:47`, `application/service.py:71` (`_llm_sanitize_mod`), `execution_orchestration.py:27`; `llm/prompts.py:9` (упоминание).

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `strip_think_blocks` | ~40–55 | Вырезать `<think>`-блоки | `llm/calls.py:126`; `application/service.py`; `execution/map_reduce.py` | Оставить |
| `fit_input` | ~57–71 | Обрезка по бюджету символов | `execution/map_reduce.py`; `application/execution_orchestration.py` | Оставить |
| `_THINK_OPEN`, `_THOUGHT_CLOSE` | ~30–38 | Приватные маркеры, в `__all__` | Внешних импортов **0** | Упростить (убрать из `__all__`) |

**Строки 17–21 лгут (устаревший docstring).** Текст: «целевая структура плана предполагает `llm/sanitize.py` в пакете `llm/`, но это имя зарезервировано существующим `scripts/llm.py` (LLM-клиент). Переименование `llm.py` → `llm_client.py` отложено … Пока — top-level `sanitize.py`». Обе посылки устарели: файл **уже** в пакете `llm/`, и `scripts/llm.py` **уже** стал `llm/client.py`. Описанная как отложенная работа давно выполнена — блок удалить.

---

### `llm/tokens.py` — 72 LOC

**Назначение.** Детерминированная оценка токенов по символам + бюджетный кэш-менеджер.
**Что делает.** `TokenEstimator.estimate(text)` — `ceil(len(text) / chars_per_token)`; `estimate_many` — суммирование; `TokenCache` (ordered dict с `reserve`/`evict_if_needed`) — управление вписыванием батчей в окно. Константы `DEFAULT_CHARS_PER_TOKEN_RU/EN`.
**Зачем нужен.** Единая оценка «сколько токен стоит этот батч» для планирования (`planning/strategy.py:30`) и packing'а (`chunking/packing.py:28`).
**Вердикт.** `Упростить`.
**Обоснование.** Ядро нужно, но заявленный инвариант «заменяет разные формулы оценки токенов» **не выполняется**, и в модуле есть мёртвый метод.
**Доказательства.** `planning/strategy.py:30`, `application/canonical.py:41`, `chunking/packing.py:28`, `retrieval/fallback.py:20`, `retrieval/context_expansion.py:38` (из них 2 — мёртвые).

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `TokenEstimator.estimate` | ~40–46 | `ceil(chars / chars_per_token)` | `chunking/packing.py:146`; `context_expansion.py` (мёртвый) | Оставить |
| `TokenEstimator.estimate_many` | ~48–54 | Сумма по списку | `planning/strategy.py:100,171`; `context_builder.py` | Оставить |
| `TokenEstimator.available` | ~56–60 | «Оценщик доступен» | **0** во всём репо | Удалить |
| `TokenCache` | ~62–72 | LRU-бюджет вписывания батчей | `chunking/packing.py` | Оставить |
| `DEFAULT_CHARS_PER_TOKEN_RU` / `_EN` | ~25–32 | Дефолты 3.5 / 4.0 | `application/canonical.py:161,211`; `brief_context.py` | Упростить (брать из конфига) |

**Ответ на вопрос брифа №5: ручные `len()/4` остались в проде.**
- `chunking/chunker.py:314` — `len(text) // chars_per_token` вручную, минуя `TokenEstimator`.
- `application/brief_context.py:437` — то же.
- `application/canonical.py:161,211` и `application/brief_context.py` — создают **собственные** `TokenEstimator(chars_per_token=3.5)`, игнорируя `project.json::skills.legal_summarizer.token_estimation.rus_chars_per_token` (если он там есть). То есть коэффициент задан в трёх местах: конфиг, дефолт в `tokens.py`, хардкод `3.5` в трёх вызывающих.
**Вердикт по этому:** единая точка оценки не достигнута. Либо провести все три места через `TokenEstimator` с коэффициентом из конфига, либо явно задокументировать, что `chunker.py`/`brief_context.py` считают «сырые символы дешёвой оценкой» намеренно.

---

### `llm/calls.py` — 161 LOC

**Назначение.** Три LLM-вызыва: `llm_batch` (map), `llm_section_reduce`, `llm_document_reduce`. Название модуля и наличие отдельной `execution/`-подсистемы провоцируют вопрос о дублировании.
**Что делает.** Каждая функция: собирает промпт → `llm.chat` → санитайзит → парсит → возвращает типизированный результат. `chat_locked` (стр. 40) — единственная обёртка, проходящая через `guarded_chat`.
**Зачем нужен.** Единственный модуль, знающий про строгий формат ответа LLM.
**Вердикт.** `Упростить`.
**Обоснование — ответ на вопрос брифа №5: `execution/` не дублирует `llm/calls.py`, дублирование обратное.** `execution/pipeline.py:112-149` (`process_context_batch`) содержит собственную LLM-логику: `try/except ChunkResultParseError → last_error = ("LLM_PARSE_ERROR", ...)`, `except Exception → ("LLM_ERROR", ...)`. То есть обработка ошибок размазана между `llm/calls.py` (сборка/парсинг) и `execution/pipeline.py` (классификация ошибок). Границу надо довести до конца: `llm/calls.py` должен возвращать типизированный результат с кодом ошибки, а `execution/` — не знать про `ChunkResultParseError`.
**Доказательства.** `execution/pipeline.py:31`, `application/execution_orchestration.py:26`, `execution/map_reduce.py:46` — все три импортируют модуль **как модуль** (`import llm.calls as _llm_calls_mod`), т.е. для monkey-patch в тестах.

| Функция | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `chat_locked` | 40–95 | Единственный путь через `guarded_chat` | **0** прод-вызовов; `test_final_invariants.py:196` | Удалить |
| `llm_batch` | 97–135 | Map-вызов по батчу | `execution/pipeline.py:73` | Оставить |
| `llm_section_reduce` | 137–170 | Reduce по секциям | `execution/map_reduce.py:322,405`; `execution_orchestration.py:335,472` | Оставить |
| `llm_document_reduce` | 172–200 | Reduce по документу | `execution/map_reduce.py:341,658`; `service.py:178` | Оставить |

**Находки — три строки, которые лгут, в одном модуле (это самый насыщенный блок ложных утверждений в группе).**

1. **`llm/calls.py:7-11` (docstring модуля):** «Single LLM boundary: каждый вызов `llm.chat` в этом модуле проходит через `guarded_chat`». **Ложь.** `llm_batch` (стр. 99), `llm_section_reduce` (стр. 139), `llm_document_reduce` (стр. 178) вызывают `llm.chat(...)` **напрямую**. `guarded_chat` достижим только из `chat_locked`, который в проде никто не зовёт. Ни один LLM-вызов в проде через `guarded_chat` не проходит.
2. **`llm/calls.py:10-11`:** «`guarded_chat` — единственное место, где берётся `LLM_FLIGHT_LOCK` (execution/pipeline.py теперь этого не делает)». **Ложь:** `execution/pipeline.py:35,73` импортирует `LLM_FLIGHT_LOCK` и берёт его. Комментарий `execution/pipeline.py:66-68` честно признаёт обратное («это сознательное исключение»), а docstring в `llm/` утверждает, что исключение устранено.
3. **`execution/pipeline.py:66-68`** (файл соседнего аудитора, но цитирую, потому что это второй половина того же ложного инварианта): «Реальный путь к LLM (`guarded_chat`) внутри `llm_batch` берёт **то же** lock — блокировка реентрантна семантически (`LLM_FLIGHT_LOCK` один), поэтому re-entrant вызов из `pipeline` не приведёт к deadlock'у». **Ложь в двух частях.** (а) `llm_batch` не вызывает `guarded_chat` — он зовёт `llm.chat` напрямую (именно поэтому deadlock'а и нет: причина не «реентрантность», а отсутствие второго захвата). (б) `threading.Lock` **не реентрантен** — если бы `llm_batch` действительно звал `guarded_chat` из-под уже взятого `LLM_FLIGHT_LOCK`, он бы завис. Комментарий фиксирует неверную модель, которая маскирует реальную опасность: стоит кому-то «причесать» `llm_batch` по описанному в `llm/calls.py` контракту — и появится реальный deadlock.
4. **Мёртвый импорт:** `llm/calls.py:29` импортирует `LLM_FLIGHT_LOCK` из `llm/single_flight.py` и **нигде его не использует**. Удалить вместе с `chat_locked`.

---

### `llm/single_flight.py` — 106 LOC

**Назначение.** Инфраструктура single-flight: общий `LLM_FLIGHT_LOCK` + трекер активных вызовов + `guarded_chat`.
**Что делает.** `LLM_FLIGHT_LOCK` (стр. 101) — `threading.Lock`, модульный синглтон. `guarded_chat` (стр. ~105-125) оборачивает `llm.chat` в `with LLM_FLIGHT_LOCK:`. `SingleFlightTracker` + `assert_single_flight` (стр. 30-72) — альтернативный механизм **детектирования** (инкремент под мьютексом, `SingleFlightViolation` при перекрытии), возвращающий `(result, tracker)`.
**Зачем нужен.** Только в текущем виде — как контейнер для `LLM_FLIGHT_LOCK`, который импортирует `execution/pipeline.py:35`.
**Вердикт.** `Упростить` (до одного символа) либо `Удалить` с переносом lock'а — см. обоснование.
**Обоснование — полный ответ на вопрос брифа №4.**

| Механизм | Где enforced | Покрывает какой вызов | Статус |
|---|---|---|---|
| `LLM_FLIGHT_LOCK` | `execution/pipeline.py:73` | **только** `llm_batch` (map-фаза) | Живой |
| `guarded_chat` | никто | ничего | **Мёртв** |
| `SingleFlightTracker` / `assert_single_flight` | `llm/single_flight.py:30-72`, вызовов нет | ничего | **Мёртв** |

**Что происходит при нарушении инварианта:**
- В проде **ничего не происходит** — нарушение не обнаруживается. `llm_section_reduce` и `llm_document_reduce` не берут ни `LLM_FLIGHT_LOCK`, ни какой-либо другой lock. Их сериализует только `asyncio.Semaphore(1)` (`execution/map_reduce.py:129`) — а `asyncio.Semaphore` управляет协рутинами одного event loop и **не блокирует другие потоки**. `application/service.py:178` (`_try_question_via_document_cache` → `llm_document_reduce`) вообще не под семафором.
- Практически инвариант держится потому, что `cli.py` — subprocess: один процесс = один поток LLM. Но это **свойство способа запуска, а не заявленный механизм**, и оно не выдержит, если скилл начнут вызывать in-process из gateway-канала или параллельно из нескольких потоков.
- `assert_single_flight` **детектировал бы** нарушение, но не предотвратил: `_active` инкрементируется под мьютексом и `_lock.acquire()` освобождается **до** `yield` (стр. 47 → 56), так что перекрытие фиксируется (`_active >= 1` → `SingleFlightViolation`), но сериализации нет. Это третья, четвёртая семантика одного и того же «инварианта».

**Вывод и рекомендация.** Инвариант `max_active_llm_calls == 1` **декларативен, а не enforced** в заявленном месте. Минимальное честное приведение в порядок — одно из двух: (а) оставить один `LLM_FLIGHT_LOCK`, взять его в `llm/calls.py` **внутри** `llm_batch`/`llm_section_reduce`/`llm_document_reduce` (тогда `execution/pipeline.py:73` убирается, и граница `execution ↛ llm` действительно соблюдается, как обещает docstring); или (б) оставить захват в `execution/pipeline.py` и **переписать docstring'и** `llm/single_flight.py:117-120` и `llm/calls.py:7-11` под факт. Вариант (а) предпочтителен: он делает инвариант структурным.

**Дополнительные находки.**
- **`llm/single_flight.py:14-16` лгут:** «Раньше существовали два независимых lock'а — `llm/calls.py::_CHAT_LOCK` и `execution/pipeline.py::_LLM_FLIGHT_LOCK`. После consolidation оба указывают на `LLM_FLIGHT_LOCK`». `_CHAT_LOCK` в `llm/calls.py` **не существует** (проверено). Описание «consolidation» относится к состоянию, которого больше нет.
- **`llm/single_flight.py:126-128` — комментарий, описывающий несуществующий код:** «Back-compat alias для старого имени». Сразу за комментарием идёт `__all__`; **никакого alias'а не определено**. Мёртвый комментарий.
- **Прямое нарушение заявленной архитектурной границы:** docstring (стр. 117-120) утверждает, что `guarded_chat` «переносит эту ответственность в `llm.single_flight`», чтобы убрать знание `execution` о `llm`. Фактически `execution/pipeline.py:35,73` по-прежнему импортирует и использует `LLM_FLIGHT_LOCK` напрямую. Миграция, описанная как сделанная, не сделана.

---

### `llm/retry.py` — 123 LOC

**Назначение.** «Smart retry» для batch'ей: разбор JSON-ответа, точечный repair по `chunk_ids`, классификация ошибок.
**Что делает.** `parse_batch_response_local` разбирает `{"summaries": {...}}`, при неполноте строит `repair_failed_chunk_ids`; `build_repair_prompt` формирует повторный запрос; `ChunkResultParseError` (стр. 26) несёт `chunk_ids` + `repair_hint`; `_extract_first_json_object` (стр. ~100-123) снимает ```- fences и ищет `{...}` регуляркой.
**Зачем нужен.** Не нужен: **протокол, который он чинит, больше не используется.**
**Вердикт.** `Удалить`.
**Обоснование.** Текущий промпт (`llm/prompts.py:52`) прямо требует «Никакого JSON» и использует маркерный формат `[[chunk_id]]`, который разбирает `parse_batch_response` без JSON. То есть `retry.py` обслуживает протокол, заменённый на marker-based ещё до Phase 2B. Прод-вызований 0; единственные потребители — `llm/tests/test_retry.py` и `test_retry_smart.py`. Мёртвый код стоит 123 LOC и держит **второе определение `ChunkResultParseError`** (см. находку выше) — то есть не просто мёртв, а actively ловушка: подключение его в конвейер тихо сломает `except` в `execution/pipeline.py:137`.
**Доказательства.** Проверено grep'ом по всему репозиторию: `llm.retry` не импортируется ни одним production-модулем, ни одним бенчмарком, ни одним tool'ом.
**Требуемая работа при удалении:** удалить 2 теста; если smart-retry нужен по продуктовым соображениям — переписать под marker-протокол и **переименовать** класс, чтобы не конфликтовать с `llm/prompts.py:73`.

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `ChunkResultParseError` | 26–45 | Ошибка JSON-разбора | `parse_batch_response_local` (внутри модуля) | Удалить |
| `parse_batch_response_local` | ~48–98 | Разбор `{"summaries": ...}` + расчёт failed chunk_ids | Только тесты | Удалить |
| `build_repair_prompt` | ~— | Промпт для починки | Только тесты | Удалить |
| `_extract_first_json_object` | ~100–123 | Извлечение JSON-объекта | Внутри модуля | Удалить |

**Три находки по модулю.**
- **Docstring `llm/retry.py:14-15` перечисляет `repair_failed_chunk_ids` как элемент `__all__` — такого символа в модуле нет.** Проверено по `__all__` и по определениям.
- **Docstring `llm/retry.py:14-15` утверждает:** «Сейчас ошибка JSON в ответе LLM приводит к повторной отправке всего batch'а». Это описание мира, в котором LLM возвращает JSON. Сейчас не возвращает.
- **Дублирование `_extract_first_json_object`.** Почти дословно повторяет приватный `_parse_json_object` в `lib/services/llm_client.py:179-199` (снятие fences + `\{.*\}`). Это уже третье в репозитории. Если модуль удаляется — проблема уходит; если остаётся — кандидат на `Слить с lib/services/llm_client.py`.

---

### `llm/__init__.py` — 0 LOC

**Вердикт.** `Оставить` — пакетный маркер, нагружается импортами `from llm.config import …` из 8 модулей прод-кода и 9 вызывающих в тестах.

---

## `workspace/skills/legal_summarizer/scripts/output/presenter.py` — 137 LOC

**Назначение.** Финальная сборка stdout-payload'а для агента: плоский dict → `json.dumps`.
**Что делает.** `prepare_output` (стр. 74–156) маршрутизирует по `status` (`completed`/`partial`/`confirmation_required`/`requires_continuation`/`failed`), скрывает счётчики LLM-вызовов, добавляет `hint` для partial. Побочных эффектов нет (чистые функции), сериализация — вызывающим.
**Зачем нужен.** Единственное место, где формируется контракт «результат → агент». Здесь же зафиксированы три продуктовых решения (анти-зеркалирование чисел — инцидент 2026-08-28; различение brief/detailed — инцидент 2026-08-31; `words`-подсказка).
**Вердикт.** `Упростить`.
**Обоснование.** Функциональность вся нужна, но у модуля есть реальный функциональный баг (обход сокрытия счётчиков) и один необработанный статус; `__all__` неполон.
**Доказательства.** `cli.py:315-318` и `cli.py:313-314`; `cli_query.py` — свой presenter, не переиспользует.

| Символ | Строки | Назначение | Зачем нужен | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_HIDDEN_LLM_CALL_COUNTERS` | 21–28 | Скрытие LLM-счётчиков | Продуктовое решение (инцидент 2026-08-28) | `prepare_output:110` | Упростить (см. баг) |
| `build_confirmation_options` | 31–71 | Payload меню brief/detailed | Агент выбирает длину | `cli.py:341,409` | Оставить |
| `prepare_output` | 74–156 | Плоский payload по статусу | Единственный формат вывода | `cli.py:426` | Упростить |

**Ответ на вопрос брифа №8: теряются ли поля, нужные агенту для follow-up? — Нет, `operation_id` выживает во всех статусах.** `out["operation_id"]` устанавливается на стр. 90-92 **до** всех веток; ни одна ветка не делает `out = {...}` заново — все используют `out.update(...)` или присваивают в `out[...]`. Для `confirmation_required` (стр. 139) и fallback-ветки (стр. 141-144) `operation_id` сохраняется. Это правильно. Замечание: `cli_query.py` получает `operation_id` не из вывода `cli.py`, а из `args` — то есть агент обязан сам сохранить его из stdout первого прогона. Формально в контракте это нигде не закреплено: `SKILL.md` упоминает `--operation-id` для resume, но получение `operation_id` из ответа **явно не задокументировано как шаг протокола**.

**Функциональный баг — сокрытие счётчиков обходится на cache-hit пути.** `_HIDDEN_LLM_CALL_COUNTERS` (стр. 21–28) содержит `map_calls`, `section_reduce_calls`, `section_trim_calls`, `document_reduce_calls`, `reduce_calls`, `total_llm_calls`. Но `application/service.py:347` на пути идемпотентного cache-hit пишет в `stats` ключ **`actual_llm_calls`**, которого в множестве **нет**. Итог: на свежем прогоне агент не видит счётчиков, а на прогоне с попаданием в кэш — видит `actual_llm_calls`, то есть ровно то, что инцидент 2026-08-28 просил скрыть. **Вердикт: `Упростить`** — добавить `actual_llm_calls` в множество.

**Функциональный баг — `status: "error"` не обрабатывается.** `prepare_output` обрабатывает `failed` (стр. 152), но не `error`. При этом `cli.py:250-258::_error` порождает именно `{"mode": "summarize", "status": "error", "message": …, "traceback":?}` и вызывается **в обход** `prepare_output` (`cli.py:320,429,432,435,438`). То есть два разных статуса ошибки на одном CLI, две разные формы payload, и `prepare_output` про одну из них не знает. Агенту из `SKILL.md` приходится ветвиться по двум схемам (`error: {...}` для `failed` против `message`/`traceback` для `error`). **Вердикт: свести к одному статусу и одной форме** — `status: "error"` с полем `error: {"message", "traceback"?}`, и научить `prepare_output` этой ветке.

**Мелочи.**
- `__all__ = ["prepare_output"]` (стр. 159) — `build_confirmation_options` не экспортируется, хотя `cli.py:314` его импортирует. Косметика, но `__all__` вводит в заблуждение относительно публичного API.
- `cli.py:366-381` (ветка `--estimate-only`) формирует payload **вручную**, минуя `prepare_output`, и там `estimated_duration_min_sec` / `max_sec` / `confirmation_threshold_sec` / `needs_confirmation` отдаются агенту — то есть ровно та техническая информация, которую `build_confirmation_options` (стр. 37-46) резонно скрывает. Не баг (оценка по запросу пользователя полезна), но обходPresenter'а стоит зафиксировать комментарием.
- Строки 117-126, hint для `partial`: `total_batches - len(failed)` при `total_batches == 0` даёт `0/0 батчей` — в сообщении, но не в исключении. Терпимо.

---

## `workspace/skills/legal_summarizer/scripts/planning/` — 2 модуля, 294 LOC

### `planning/plan.py` — 165 LOC

**Назначение.** Data-transfer-объект плана исполнения + чистый builder «батчи → план» + сериализация.
**Что делает.** `ExecutionPlan` (frozen dataclass) хранит `strategy`, `batches` (tuple batch'ей со страницами/чанками), `total_chunks`; `build_execution_plan` (стр. ~50-80) собирает план из `chunks`+`batches`; `to_dict` — выгрузка в dict. Побочных эффектов нет.
**Зачем нужен.** `application/context_builder.py:64-68` строит `ExecutionPlan` и кладёт в `ExecutionContext`; `execution_orchestration.py` его потребляет. Строки 10-13 фиксируют контракт (total_chunks == сумма batch'ей, строки не пересекаются, порядок детерминирован).
**Вердикт.** `Оставить`.
**Обоснование.** Живой data-transfer-объект с явными инвариантами, которые `.to_dict`/`build` проверяют. Структурная роль, а не обёртка.
**Доказательства.** `application/context_builder.py:64`, `application/canonical.py:97` (в мёртвом модуле, см. ниже); `execution_orchestration.py`.

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `class ExecutionPlan` | ~20–95 | DTO плана | `context_builder.py:64`; `canonical.py:97` (мёртв) | Оставить |
| `ExecutionPlan.get_batch` | ~78–88 | Батч по `batch_id` | **0** во всём репо | Удалить |
| `ExecutionPlan.to_dict` | ~90–95 | Сериализация | `cli_query.py`/тесты | Оставить |
| `build_execution_plan` | ~98–165 | Builder плана из стратегии | `context_builder.py:64`; `canonical.py:110` (мёртв) | Оставить |

**`get_batch` — 0 вызовов** ни в проде, ни в тестах. Потребители обходятся перебором `plan.batches` с последующим сравнением. Удаление безопасно.

---

### `planning/strategy.py` — 129 LOC

**Назначение.** «Unified execution planner. Единственный селектор для выбора стратегии: direct / map_reduce».
**Что делает.** `ExecutionPolicy` (frozen dataclass, стр. ~30-50) — пороги: `total_tokens ≤ 12000` → `direct`; больше + секций ≥ 3 → `map_hierarchical`; иначе `map_flat`. `select_strategy` (стр. ~85-110) единственный читатель `ExecutionPolicy`. `build_direct_plan` / `build_map_plan` — два билдера плана; `build_execution_plan` (стр. ~130-165) — диспетчер, сам вызывает `select_strategy` внутри.
**Зачем нужен.** Единственное место, где принимается решение «прямой вызов LLM vs map-reduce».
**Вердикт.** `Оставить`.
**Обоснование — ответ на вопрос брифа №6: да, `strategy.py` действительно единственный селектор, и все стратегии достижимы.**
- **Селектор единственный:** `select_strategy` — единственная функция, читающая `ExecutionPolicy`; `build_direct_plan` и `build_map_plan` стратегию не выбирают, а получают её от `build_execution_plan`. Проверено: вне `strategy.py` пороги `12000` / `3` секций не встречаются ни в одном модуле.
- **`direct` достижим:** `application/context_builder.py:59-60` — при `len(chunks) <= 1` стратегия принудительно `"direct"` (это путь brief-режима: brief даёт 1 чанк). Плюс при `total_tokens ≤ 12000` и нескольких чанках — `build_direct_plan`. Обе ветки живые.
- **`map_flat` и `map_hierarchical` достижимы:** пороги в `ExecutionPolicy` срабатывают на документах > 12000 токенов, что при `chunk_size=100000` достигается на любом реальном документе длиннее ~35 тыс. символов. `map_hierarchical` дополнительно требует `len(sections) >= 3` — на структурированном юридическом документе выполняется. Исполнитель (`execution/map_reduce.py:313,322,341`) обрабатывает все три.
- **Значение `ExecutionPolicy` нигде не переопределяется.** `application/context_builder.py:64-68` вызывает `build_execution_plan(insp.structure, tuple(chunks), question=args.question, policy=None)` — то есть всегда с дефолтами. Пороги живут исключительно как значения по умолчанию в коде, а не в `project.json`. Это делает их неконфигурируемыми: чтобы поменять порог, нужно править Python. **Вердикт: `Упростить`/вынести** — либо перенести в `project.json::skills.legal_summarizer.execution`, либо явно задокументировать, что пороги намеренно захардкожены как продуктовая константа (и тогда пометить константы именами с префиксом, а не магическими числами в сравнении).

**Находка — двойной расчёт стратегии.** `application/context_builder.py:63` зовёт `select_strategy(insp.structure, list(chunks))` и сохраняет в `ctx.strategy`; `context_builder.py:64` зовёт `build_execution_plan(...)`, который **внутри себя** (стр. ~140) ещё раз зовёт `select_strategy` и кладёт результат в `ExecutionPlan.strategy`. Итого стратегия вычисляется дважды за прогон, каждое вычисление — полный проход по всем чанкам с `estimate_many`. Результаты детерминированы и совпадают, но это ровно тот дублирующий проход, который `TokenEstimator` и создавался, чтобы убрать. Результат первого вызова можно передавать второму.

**Связь с соседней подсистемой (`execution/`) — для владельца отчёта по ней:** обе стратегии живы, мёртвой стратегии нет. **Но `planning/strategy.py` имеет мёртвый второй вход** — `application/canonical.py:97,110` вызывает `build_direct_plan` / `build_map_plan` напрямую, а `application/canonical.py` (244 LOC) **не импортируется ни одним модулем в `scripts/`**. Канонический question-путь в проде идёт через `application/chunk_selection.py`. Подробнее — в кросс-подсистемных находках ниже; файл принадлежит другому аудитору, но это его главный вывод.

---

## `workspace/skills/legal_summarizer/scripts/cli.py` — 389 LOC (code 327)

**Назначение.** Entry point, который вызывает агент: `python scripts/cli.py --file <doc> [--length|--question] [--confirm]`.
**Что делает.** Разбирает аргументы, поднимает `_LazySettings` с профилем `test` и регистрирует скилл в `TableRegistry` (`_ensure_registered`, стр. 261-292), читает текст, прогоняет быстрый pre-confirm gate через `quick_estimate` (pypdf, стр. 336-351), при необходимости печатает меню подтверждения, иначе печатает running-маркер и зовёт `application/service.run`. Вывод — JSON в stdout + sentinel `__LEGAL_SUMMARIZER_DONE__` (`_emit_done`, стр. 184-193). Прогресс — в stderr.
**Зачем нужен.** Единственный способ, которым агент запускает суммаризацию. Формат stdout + sentinel — контракт с навыком агента.
**Вердикт.** `Упростить`.
**Обоснование.** Функционально необходим, но: 2 из 8 флагов мёртвые, код выхода не отражает провал, running-маркер печатается слишком поздно, docstring приводит невалидный пример.
**Доказательства.** Точка входа (`python scripts/cli.py`). `workspace/skills/legal_summarizer/tests/test_skill_legal_summarizer.py`, `tests/test_legal_summarizer_running_subprocess.py` (последний — прямо на sentinel-контракт и running-маркер).

#### Функции уровня модуля

| Функция | Строки | Назначение | Зачем нужна | Кто вызывает | Вердикт |
|---|---|---|---|---|---|
| `_emit` | ~140–172 | `print(json, flush=True)` + fallback на байты | Единственный writer в stdout | `_emit_done`, `_emit_running_marker` | Оставить |
| `_emit_done` | 184–193 | Финальный payload + sentinel | Контракт завершения | 6 мест | Оставить |
| `_emit_running_marker` | 196–247 | Прогноз длительности + инструкция по `write_stdin` | Решает инцидент 2026-08-28 (polling) | `main` | Оставить (но см. timing-баг) |
| `_error` | 250–258 | Payload ошибки | Единый формат ошибки | 5 мест | Упростить (свести с `status: failed`) |
| `_ensure_registered` | 261–292 | Подъём `SETTINGS` + регистрация скилла | Standalone-CLI без `ApplicationContext` | `main` | Оставить |
| `main` | 295–440 | Оркестрация | Entry point | `__main__` | Упростить |
| `build_parser` | 84–146 | Аргументы | Контракт CLI | `main` | Упростить (убрать мёртвые флаги) |

#### Ответы на вопросы брифа №7

**(а) Какие флаги реализованы и соответствуют ли `SKILL.md`.**

| Флаг | Стр. | Разобран | Используется | В `SKILL.md` | Вердикт |
|---|---|---|---|---|---|
| `--file` | 84 | да | да | да | Оставить |
| `--length` | 89 | да | да | да | Оставить |
| `--confirm` | 97 | да | да | да | Оставить |
| `--operation-id` | 104 | да | да | да | Оставить |
| `--question` | 110 | да | да | да | Оставить |
| `--estimate-only` | 117 | да | да | да | Оставить |
| `--focus` | 124 | да | да (`cli.py:386`) | да | Оставить |
| `--context` | 132 | да (`type=json.loads`) | **НЕТ** | да (`:91`) | **Удалить** |
| `--max-chunks` | 139 | да | **НЕТ** | да (`:94`) | **Удалить** |
| `--length medium` | 13-16 | — | — | в docstring cli.py как пример | **Удалить** из docstring |

`--context` и `--max-chunks` — мёртвые флаги: `args.context` и `args.max_chunks` не читаются **ни разу** во всём файле (проверено grep'ом), в `kwargs` (`cli.py:384-400`) их нет. `SKILL.md:91,94` рекламирует их как рабочие. Либо реализовать (прокинуть в `run()`), либо удалить из обоих мест. **Проверено, что это не динамический доступ:** `getattr(args, ...)` в файле отсутствует.

**Docstring `cli.py:13-16` содержит невалидный пример:** `--length medium` в трёх местах, но `choices=["brief", "detailed"]` (`cli.py:92`) — такой вызов завершится `SystemExit(2)` от argparse с текстом ошибки на stderr, до входа в `main()`. Агент, скопировавший пример из docstring (а не из `SKILL.md`, где `medium` убран), получит не-JSON вывод. `project.json:203` рядом с этим же ключом отмечает: «medium удалён в Phase 2B+» — docstring не обновлён.

**(б) Как агент узнаёт `operation_id` для follow-up.**
`operation_id` генерируется внутри `application/service.py` (если не передан через `--operation-id`) и возвращается в result'е; `output/presenter.py:90-92` пробрасывает его в stdout **для всех статусов** — это сделано корректно (проверено по всем пяти веткам). Агент должен: (1) считать `operation_id` из stdout первого прогона, (2) передать его в `--operation-id` следующего.
**Проблема:** этот шаг **нигде не зафиксирован как контракт**. `SKILL.md` описывает `--operation-id` для resume, но прямо не говорит «возьми `operation_id` из ответа и подставь в следующий вызов». Для `--confirm`-пути (стр. 366-381, ветка `--estimate-only` выхода раньше) `operation_id` вообще **не печатается** — payload состоит из `mode/status/chars_in/chunks_total/…` и не содержит `operation_id` (стр. 366-381). То есть на `--estimate-only` агент не может получить id для последующего подтверждённого прогона и вынужден генерировать новый при первом реальном запуске, **теряя связь между оценкой и прогоном** (и, следовательно, idempotency-кэш по документу). Это функциональный пробел: добавление `"operation_id": <id>` в payload `cli.py:366-381` стоит одну строку.

**(в) Коды выхода и обработка ошибок — функциональный баг.**

| Сценарий | `status` в stdout | Exit code | Путь |
|---|---|---|---|
| Успех | `completed` / `partial` | 0 | `cli.py:427` → `main()` return |
| Меню подтверждения | `confirmation_required` | 0 | `cli.py:346`, `cli.py:414` → return |
| Оценка без прогона | `ok` (mode `estimate_only`) | 0 | `cli.py:382` → return |
| **Провал прогона** | **`failed`** | **0** | `cli.py:426-427` → return |
| Конфликт аргументов | `error` | 2 | `cli.py:324` |
| `ArgumentTypeError` / `FileNotFoundError` / `ValueError` | `error` | 1 | `cli.py:430,433,436` |
| Прочие исключения | `error` (+traceback) | 1 | `cli.py:439` |

**Баг: `status: "failed"` → код 0.** `application/service.py:320` (`EMPTY_DOCUMENT`) и `service.py:389` возвращают `{"status": "failed", ...}`; `presenter.prepare_output` (стр. 152-154) превращает это в `{"status": "failed", "error": {...}}`; `cli.py:427` печатает и `main()` завершается с кодом **0**. Агент, ориентирующийся на код возврата, посчитает провал успехом. Правильно: `sys.exit(1)` при `out.get("status") in ("failed", "error")` после `cli.py:427`. Это правка в 2 строки.
**Баг: два статуса ошибки.** `"failed"` (из `service.run`, форма `error: {...}`) и `"error"` (из `cli._error`, форма `message` + `traceback`) — агенту нужно знать оба, форма разная. См. presenter-разбор.
**Оценка:** три из семи веток возвращают код 0 при неуспехе-adjacent состоянии, но это осознанно — `confirmation_required` и `estimate_only` успехом не являются, однако провалить их нельзя, это штатные режимы диалога. Проблема ровно в `failed`.

**Найдено: `--question` + `--length` → код 2** (`cli.py:318-324`) — корректно, это ошибка вызова, не рантайма.

**(г) `cli_query.py` — дублирует ли `cli.py`.** Нет, это **разные инструменты**: `cli.py` — суммаризация; `cli_query.py` — read-only инспекция уже обработанного документа (`--field stats|articles|chunks|sections|tree|all`). Общего кода между ними: только `build_parser`-форма и `_DONE`-sentinel-подобный вывод; `presenter.py` использует только `cli.py`. Дублирования, требующего слияния, нет. `cli_query.py` — тонкий read-only клиент к `cache/manifest.py` + `cache/document_cache.py`.
**Но у `cli_query.py` есть собственный функциональный баг** (см. ниже), и один реальный дубль: `_manifest_error_message` (`cli_query.py:151`) и `_error` (`cli.py:250`) решают одну задачу — «сформировать payload ошибки» — двумя независимыми реализациями с разными формами. Объединение в `output/presenter.py` уберёт расхождение. **Вердикт по `cli_query.py`: `Оставить` с обязательным исправлением `tree`.**

---

## `workspace/skills/legal_summarizer/scripts/cli_query.py` — 290 LOC (code 256)

**Назначение.** Read-only инспекция уже обработанного документа по `operation_id`: что в нём, сколько секций, какие статьи.
**Что делает.** Загружает манифест по `operation_id` (`cache/manifest.py`), отдаёт один из срезов по `--field`, завершает sentinel'ом. Только чтение; LLM не вызывается; файлы не пишутся.
**Зачем нужен.** Позволяет агенту «заглянуть» в структуру документа до/после суммаризации, не запуская повторный LLM-прогон. Дешёвый и безопасный инструмент.
**Вердикт.** `Оставить` (с обязательным исправлением `--field tree`).
**Обоснование.** Функционально нужен, дублирования `cli.py` нет. Единственный дефект — `tree`, обещающий иерархию и отдающий плоский список.
**Доказательства.** Точка входа. `workspace/skills/legal_summarizer/tests/test_skill_legal_summarizer.py`.

| Символ | Строки | Назначение | Кто вызывает | Вердикт |
|---|---|---|---|---|
| `build_parser` | 44–75 | Аргументы | `main` | Оставить |
| `_manifest_error_message` | 151–~180 | Человекочитаемое сообщение по причине | `main` | Оставить (см. дубль с `cli.py:250`) |
| `main` | 257–327 | Сборка среза + вывод | `__main__` | Оставить |

**Функциональный баг — `--field tree` не строит дерево.** `cli_query.py:194-199`: ветки `field == "sections"` и `field == "tree"` выполняют **одно и то же** — `sorted(insp.structure.iter_sections(), key=section_path)`. Единственное различие — в `sections` дополнительно считается `block_count` (стр. ~205-212). Комментарий в коде признаёт: «Псевдо-дерево: родитель → дети, по `section_path`». Help-текст (`cli_query.py:52`) обещает «иерархия sections». `SKILL.md:210` обещает то же. Агент, запросивший `tree` ради навигации по вложенности разделов (для «покажи раздел 1.2 и его подразделы»), получает плоский отсортированный список и должен сам выводить иерархию из `section_path`. Либо реализовать вложенность (например, `children: [...]` по `parent_id` — данные в `DocumentStructure` есть), либо переименовать `tree` → `flat` и поправить `--help` + `SKILL.md:210`.

**Мелочь.** `_manifest_error_message` (`cli_query.py:151`) дублирует назначение `cli._error` (`cli.py:250`) — две реализации «payload ошибки» с разными формами. Кандидат на перенос в `output/presenter.py`.

---

## Пакетные `__init__.py` (4 файла из брифа `99-unassigned.md`)

Короткий вердикт по каждому, как требовала подзадача.

| Файл | LOC | Нужен ли как пакетный маркер | Вердикт |
|---|---|---|---|
| `lib/__init__.py` | 0 | **Да.** `lib` — корень пакетов фреймворка; `from lib.core.skill_registration import …` (`cli.py:287`), `from lib.core.infra_registration import …` (`cli.py:286`) идут от корня репозитория. Без маркера работа сработает через PEP 420 namespace-package, но остальные 12 пакетов `lib/` имеют явный `__init__.py` — несоответствие структуры. | Оставить |
| `lib/services/__init__.py` | 8 (code 7) | **Да, и содержит полезное.** Единственный непустой: docstring описывает сервисный слой как infrastructure/domain boundary. `llm/client.py:21` делает `from lib.services.llm_client import call_llm` — пакет обязателен. Docstring функционален (объясняет границу), не вода. | Оставить |
| `workspace/skills/__init__.py` | 0 | **Да.** 3 строковые ссылки в репозитории, и главное: `workspace/skills/legal_summarizer/tests/smoke_chunking_diagnostics.py:7` запускается как `python -m workspace.skills.legal_summarizer.tests.smoke_chunking_diagnostics`, что требует импортируемости всего пути `workspace.skills.legal_summarizer.tests.*`. | Оставить |
| `workspace/skills/legal_summarizer/__init__.py` | 0 | **Да, по той же причине.** `python -m workspace.skills.legal_summarizer.tests.…` требует, чтобы `workspace.skills` и `workspace.skills.legal_summarizer` были пакетами. Статический анализатор показывает 0 импортёров — это ложный сигнал: импорт делает `python -m`, а не `import`. | Оставить |

**Общий вывод по всем 4:** пустые `__init__.py` в этом репозитории — **функциональные пакетные маркеры, а не мёртвый код**. В частности, `workspace/skills/legal_summarizer/__init__.py` формально не имеет ни одного статического импортёра, но без него `python -m workspace.skills.legal_summarizer.tests.smoke_chunking_diagnostics` не отработает. Удаление «потому что 0 ссылок» сломало бы `python -m`-запуск; это ровно тот случай, когда статика ошибается (протокол §2).

---

## Функциональные баги (сводно)

| # | Путь:строка | Суть | Серьёзность |
|---|---|---|---|
| 1 | `cli.py:426-427` + `application/service.py:320,389` | `status: "failed"` → **exit code 0**. Агент по коду возврата считает провал успехом. | Высокая |
| 2 | `cli_query.py:194-199` | `--field tree` отдаёт плоский список, а не иерархию; `--help` и `SKILL.md:210` обещают иерархию. | Средняя |
| 3 | `cli.py:423` (после `cli.py:356,405`) | Running-маркер печатается **после** 3-5 мин извлечения текста из большого PDF — агент молчит ровно тогда, когда процесс выглядит зависшим. Инцидент 2026-08-28 не закрыт полностью. | Средняя |
| 4 | `llm/config.py` `get_default_length` (~:48-55) | Дефолт `"medium"` невалиден: argparse его отвергает, `service.py:325` схлопывает в `brief`, `chunk_selection.py` трактует как `detailed`. Оценка времени считается для другого прогона. Замаскировано `project.json::default_length`. | Средняя (латентная) |
| 5 | `output/presenter.py:21-28` + `application/service.py:347` | `actual_llm_calls` не в `_HIDDEN_LLM_CALL_COUNTERS` — на cache-hit пути агент видит LLM-счётчик, хотя на свежем прогоне не видит (инцидент 2026-08-28 обойдён). | Низкая |
| 6 | `output/presenter.py` (нет ветки `error`) + `cli.py:250-258` | Два статуса ошибки (`failed`/`error`) с разной формой payload; `prepare_output` о `status: "error"` не знает. | Средняя |
| 7 | `cli.py:366-381` | `--estimate-only` не возвращает `operation_id` — агент не может связать оценку с последующим `--confirm`-прогоном, теряется idempotency. | Средняя |
| 8 | `llm/client.py` `doc_context` (`if with_begin_end: pass`) | Параметр `with_begin_end` принимается, игнорируется; вызывающий `llm/calls.py:164` явно передаёт `True`, ожидая маркеры границ разделов. | Низкая |
| 9 | `retrieval/normalizer.py:44` | NFKC не сворачивает `ё`→`е` → промах recall на запросах с «ё» в прод-пути `index.py:60`. | Низкая |
| 10 | `document/analysis.py:21,57` | `typing.get_type_hints(DocumentAnalysis)` → `NameError: SemanticRecord` (проверено запуском). Причина — `retrieval/records.py` не импортирован. | Средняя (кросс-подсистемная) |

---

## Кросс-подсистемные находки (вне моей подсистемы)

1. **`application/canonical.py` (244 LOC) — мёртв целиком.** Ни одного импортёра в `scripts/`. Прод использует `application/inspection.py:56`, который делает **тот же** `run_canonical_pipeline` вызов. То есть существуют две «канонические» точки: мёртвая `canonical.py` и живая `inspection.py`. Побочно: `canonical.py:71` содержит вычисляемую и **нигде не используемую** локальную переменную `_duration`. Для аудитора `application/`.
2. **`document/analysis.py:155-162` — дублированный импорт** `from retrieval.query import retrieve_chunks` дважды подряд, вложенный в `if retrieval_index is None`. Для аудитора `document/`.
3. **`document/block_lookup.py` (52 LOC) — мёртв.** 0 импортёров. Его docstring описывает замену линейного `doc.blocks.index(...)` в `retrieval/context_expansion.py`, но (а) `context_expansion.py` его не импортирует и (б) линейного `doc.blocks.index(...)` там никогда не было — поиск идёт по `chunks` (сортировка по `c.index` + `next(...)`). То есть модуль не просто мёртв, а **не соответствует тому, что он якобы чинит**. Для аудитора `document/`.
4. **`lib/services/llm_client.py` — `call_llm_async` не существует**, хотя `AGENTS.md` (корневой) и `project.json:214` упоминают `call_llm`/`call_llm_async` как пару. В модуле только `call_llm` и `call_llm_json`. Для аудитора `lib/services/`. Это тот же факт, который зафиксировала соседняя группа, — подтверждаю.
5. **`project.json:214` комментирует** «LLM-клиент — `lib.services.llm_client.call_llm`» для скилла `legal_summarizer` — и это **верно** (`llm/client.py:70`). А вот `project.json` рядом описывает `timeout_sec: 120` / `max_retries: 3` в секции `cli`, которые читает именно `llm/client.py:82`. Всё сходится; расхождение только в том, что обёртки `get_timeout_sec`/`get_max_retries` для этого не используются.
6. **Пять строк в трёх файлах лгут про single-flight как единую точку enforcement** (`llm/calls.py:7-11`, `llm/calls.py:10-11`, `llm/single_flight.py:14-16`, `llm/single_flight.py:117-120`, `execution/pipeline.py:14-15,66-68`). Это системная проблема документации, а не опечатка: описание миграции «в `guarded_chat`» было написано, но миграция не выполнена, и `execution/pipeline.py:66-68` даже фиксирует неверную модель (`threading.Lock` назван реентрантным). Для аудиторов `llm/` (мой) и `execution/`.
7. **Два независимых tokenizer'а в проде** (`retrieval/query.py:67` и `retrieval/normalizer.py:70`) плюс приватные символы `_RUSSIAN_STOPWORDS`/`_WORD_RE`, вынесенные из `query.py` в `normalizer.py:23-26`. Для аудитора `document/`: `DocumentAnalysis.retrieve` (мой вызов, его метод) полагается на согласованность этой пары.
8. **`retrieval/records.py` — источник runtime-латентности в чужом файле** (`document/analysis.py:57`, аннотация без импорта). Подтверждено запуском: `get_type_hints` падает. Совместная работа двух аудиторов.
9. **`tests/benchmarks/test_acceptance_matrix.py:27-33`** — единственный в репозитории формальный список обязательных модулей скилла. Ни один модуль `retrieval/` в него не входит, кроме косвенно используемого `retrieval.query` (и он входит не сам, а через `llm.calls` → нет, `llm.calls` не тянет `retrieval`; фактически `retrieval/*` не входит вовсе). **Рекомендация владельцу теста:** после удаления острова — добавить в `REQUIRED_MODULES` `retrieval.index`, чтобы публичный question-путь был защищён от случайного удаления, как уже сделано для `llm.sanitize` / `llm.prompts_runtime` / `llm.calls`.
10. **Покрытие `retrieval/` тестами полное, но бесполезное:** все 11 мёртвых модулей покрыты unit-тестами (по 1-2 файла на модуль), которые тестируют только их самих. Именно поэтому они и выжили: зелёный CI на 100% функций мёртвого кода. Стоит учесть при решении об удалении: удаление потребует удаления ~11 тест-файлов.

---

## Приложение: сводка вердиктов по символам

| Слой | Оставить | Упростить | Удалить | Слить | Всего |
|---|---|---|---|---|---|
| `retrieval/` | 12 | 4 | 25 | 4 | 45 |
| `llm/` | 16 | 9 | 12 | 4 | 41 |
| `output/` | 2 | 2 | 0 | 0 | 4 |
| `planning/` | 6 | 1 | 1 | 0 | 8 |
| `cli.py` / `cli_query.py` | 8 | 5 | 2 | 0 | 15 |
| пакетные `__init__.py` | 4 | 0 | 0 | 0 | 4 |
| **Итого** | **48** | **21** | **40** | **8** | **117** |

> Счётчики в сводном блоке (34/9/22/3) и в таблице выше расходятся: таблица считает **символы** (функции, методы, классы), сводный блок — **решения верхнего уровня** (файлы и группы). Прошу использовать таблицу как точную; расхождение объясняется тем, что один вердикт по файлу тянет за собой несколько вердиктов по символам.

### Порядок работ (по убыванию выгоды / убыванию риска)

1. **Нулевой риск, чистый выигрыш:** удалить `llm/retry.py` + 2 теста; удалить `llm/config.py::get_timeout_sec`/`get_max_retries`; удалить `TokenEstimator.available()`; удалить `ExecutionPlan.get_batch`; удалить `normalizer.expand_with_aliases`; удалить `_LEGAL_ALIASES`; удалить мёртвый импорт `LLM_FLIGHT_LOCK` в `llm/calls.py:29`; убрать `_THINK_OPEN`/`_THOUGHT_CLOSE` из `__all__`; удалить устаревшие docstring-блоки в `llm/sanitize.py:17-21` и `llm/prompts_runtime.py:3-5`; удалить `--context`/`--max-chunks` из `cli.py` и `SKILL.md:91,94`; исправить `--length medium` в docstring `cli.py:13-16`.
2. **Функциональные баги:** exit code 1 при `status: "failed"`; добавить `actual_llm_calls` в `_HIDDEN_LLM_CALL_COUNTERS`; добавить ветку `error` в `prepare_output` и свести `_error` к общей форме; вернуть `operation_id` в payload `--estimate-only`; реализовать или переименовать `--field tree`; перенести `_emit_running_marker` **до** `load_text`; `retrieval/normalizer.py` — свёртка `ё`→`е`.
3. **Инвариант single-flight:** выбрать одну из двух схем (предпочтительно — захват lock'а внутри `llm/calls.py`) и синхронно переписать 5 ложных docstring-блоков в `llm/` и `execution/`.
4. **Удаление острова `retrieval/`:** 11 модулей + ~11 тест-файлов, ~700 LOC. Предварительно: (а) удалить `DocumentAnalysis.semantic_records` в `document/analysis.py` (согласовать с аудитором `document/`), (б) убрать дублированный импорт в `analysis.py:155-162`, (в) добавить `retrieval.index` в `tests/benchmarks/test_acceptance_matrix.py:27-33`. **Оценка «нужны ли бенчмаркам» по `retrieval/qa.py` и `quality.py` передаю владельцу `benchmarks/`** — по фактическому дереву импортов бенчмарк-harness для retrieval отсутствует, но это его вывод делать.
5. **Слияния:** `llm/prompts_runtime.py` → `llm/prompts.py` (3 места импорта); `retrieval/normalizer.py` → `retrieval/query.py` (2 места импорта + переименование `tokenize`).
6. **Решение по `application/canonical.py` и `document/block_lookup.py`** — вне моего периметра, но это следующие по величине мёртвые массы (244 + 52 LOC) и они находятся в чужих отчётах.
