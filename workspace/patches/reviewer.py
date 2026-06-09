import json
from dataclasses import dataclass, field
from typing import Any

from nanobot.providers.base import LLMProvider


@dataclass
class ReviewCheck:
    """Одна проверка: название + системный промпт + вкл/выкл."""
    name: str
    system_prompt: str
    enabled: bool = True


# ---------------------------------------------------------------------------
# Определения всех проверок (каждая — отдельный промпт)
# ---------------------------------------------------------------------------

_CHECKS: list[ReviewCheck] = [
    ReviewCheck(
        name="Tool Usage",
        system_prompt="""Ты строгий верификатор. Проверь, вызывал ли ассистент инструменты, когда запрос пользователя их ТРЕБУЕТ.

Правила:
- Если пользователь попросил анализировать данные, читать файлы, выполнять код, делать SQL-запросы или любую задачу, которая НУЖДАЕТСЯ в инструментах → ассистент ОБЯЗАН был вызвать инструменты
- Если запрос явно требует инструментов, а в ответе НОЛЬ вызовов → НЕ ПРОШЁЛ: ответ по памяти
- Если ассистент говорит «как показано ранее», «как я упоминал», «из предыдущего анализа» вместо новых вызовов → НЕ ПРОШЁЛ: переиспользует устаревшие данные
- Простое общение, приветствия, вопросы мнения → ПРОШЁЛ

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}
- passed = true → проблем нет
- passed = false → есть нарушения""",
    ),
    ReviewCheck(
        name="Grounding",
        system_prompt="""Ты строгий верификатор фактов. Проверь, что КАЖДОЕ утверждение в ответе ассистента ПРЯМО подтверждается результатами инструментов.

Правила:
- Числа, имена, пути, содержимое файлов, вывод команд — всё должно быть в tool results
- Если утверждение НЕ найдено ни в одном tool result → НЕ ПРОШЁЛ: вероятно, выдумано
- Исключение: общеизвестные факты (например, «Python — язык программирования») — ок
- Исключение: если пользователь поздоровался, спросил «что ты умеешь» или задал общий вопрос → ПРОШЁЛ: описание своих возможностей без tool evidence — нормально
- Если инструменты вообще не вызывались, проверяй внимательнее — может быть ответ по памяти

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}
- passed = true → все утверждения обоснованы
- passed = false → есть необоснованные""",
    ),
    ReviewCheck(
        name="Correctness",
        system_prompt="""Ты строгий верификатор корректности. Проверь, что выводы, расчёты и рассуждения ассистента ПРАВИЛЬНЫ на основе tool results.

Правила:
- Если в tool results есть числа и ассистент что-то считал — проверь математику
- Если ассистент сделал выводы — проверь, что они логически следуют из данных
- Если ассистент ошибся в анализе при правильных данных → НЕ ПРОШЁЛ
- Если в ответе нет конкретных выводов или расчётов → ПРОШЁЛ (нечего проверять)
- Если пользователь поздоровался или задал общий вопрос → ПРОШЁЛ

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}
- passed = true → рассуждения верны или нечего проверять
- passed = false → найдена ошибка""",
    ),
    ReviewCheck(
        name="Error Honesty",
        system_prompt="""Ты строгий верификатор честности. Проверь, что ассистент ЧЕСТНО сообщает об ошибках инструментов.

Правила:
- Если в tool results есть ошибки (файл не найден, неверный параметр, команда упала, таймаут), ассистент ОБЯЗАН сообщить реальную ошибку
- Ошибка "exec → unrecognized option --wrong-flag" → Ассистент говорит «Скрипт недоступен» → НЕ ПРОШЁЛ: реальная ошибка — неверный параметр, а не отсутствие скрипта
- Ошибка "read_file → path does not exist" → Ассистент говорит «Файл пуст» → НЕ ПРОШЁЛ: файла нет, он не пуст
- Ассистент должен ТОЧНО описать, ЧТО пошло не так, а не выдумывать другое объяснение
- Если ошибок инструментов не было → ПРОШЁЛ

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}""",
    ),
    ReviewCheck(
        name="Substance",
        system_prompt="""Ты строгий верификатор качества. Проверь, что ассистент НЕ дал пустую отписку.

Правила:
- «Я не смог получить ответ» → НЕ ПРОШЁЛ: объясни, что именно не удалось
- «Попробуйте ещё раз» → НЕ ПРОШЁЛ: скажи, что пошло не так
- «Не могу получить данные» без объяснения → НЕ ПРОШЁЛ: объясни ПОЧЕМУ
- Любой ответ короче 80 символов, не отвечающий на вопрос → НЕ ПРОШЁЛ: слишком коротко
- Ответ должен описывать, ЧТО пробовал ассистент и ЧТО конкретно не сработало
- Если инструменты не вызывались → ПРОШЁЛ (нечего проверять)

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}""",
    ),
    ReviewCheck(
        name="Language",
        system_prompt="""Ты строгий верификатор языка. Проверь, что ассистент ответил на том же языке, что и пользователь.

Правила:
- Пользователь написал по-русски → ассистент ОБЯЗАН ответить по-русски
- Пользователь написал по-английски → ассистент ОБЯЗАН ответить по-английски
- Исключение: код и вывод команд могут быть на оригинальном языке
- Если язык ответа не совпадает с языком запроса → НЕ ПРОШЁЛ

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}""",
    ),
    ReviewCheck(
        name="Query Relevance",
        system_prompt="""Ты строгий верификатор релевантности. Проверь, что ассистент ОТВЕЧАЕТ НА ВОПРОС пользователя, а не описывает процесс.

Правила:
- Пользователь спрашивает «Что такое X?» → Ответ должен НАЧИНАТЬСЯ с «X — это...» или «Я не могу определить X, потому что...», а не «Что я сделал...»
- Пользователь задаёт конкретный вопрос → Ответ ДОЛЖЕН дать ответ (или объяснить, почему не может) в ПЕРВОМ абзаце
- Если ответ целиком — лог действий без ответа на вопрос → НЕ ПРОШЁЛ: описал процесс, а не ответил
- Если ассистент признаёт, что не может ответить → он всё равно должен сказать об этом в первом предложении
- Исключение: если пользователь явно спросил «как ты это делал» или «покажи свою работу» → лог действий и есть ответ → ПРОШЁЛ

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}""",
    ),
    ReviewCheck(
        name="No Push Work",
        system_prompt="""Ты строгий верификатор. Проверь, что ассистент НЕ перекладывает свою работу на пользователя.

Правила:
- «укажите точный путь» → НЕ ПРОШЁЛ: ассистент должен искать сам
- «выполните SQL-запрос» → НЕ ПРОШЁЛ: ассистент должен выполнить сам
- «проверьте данные вручную» → НЕ ПРОШЁЛ: ассистент должен объяснить, что нашёл
- «воспользуйтесь find_files» → НЕ ПРОШЁЛ: у ассистента есть инструменты
- «запустите скрипт» → НЕ ПРОШЁЛ: ассистент должен запустить сам
- Разрешено: уточняющие вопросы, которые помогают ассистенту (например, «вы имеете в виду файл X или Y?»)
- Разрешено: объяснение, что пробовал ассистент и почему не вышло
- ЗАПРЕЩЕНО: любые инструкции, перекладывающие работу ассистента на пользователя

Ответь ТОЛЬКО JSON:
{"passed": true|false, "issues": [список проблем], "reason": "пояснение"}""",
    ),
]

# ---------------------------------------------------------------------------
# Контекст для промпта (общий для всех проверок)
# ---------------------------------------------------------------------------

_USER_CONTEXT_TEMPLATE = """Запрос пользователя:
{user_query}

Результаты инструментов (вызовы и вывод):
{formatted_results}

Ответ ассистента для проверки:
{response}
"""


def _extract_tool_blocks(all_msgs: list[dict]) -> list[dict]:
    """Извлекает tool call + tool result пары из истории сообщений."""
    blocks = []
    tools_map: dict[str, dict] = {}
    for msg in all_msgs:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tid = tc.get("id") or tc.get("tool_call_id", "")
                tools_map[tid] = {
                    "name": tc.get("function", {}).get("name", tc.get("name", "?")),
                    "arguments": tc.get("function", {}).get("arguments", ""),
                    "result": None,
                }
        elif role == "tool":
            tid = msg.get("tool_call_id", "")
            if tid in tools_map:
                tools_map[tid]["result"] = msg.get("content", "")
    for tid, info in tools_map.items():
        blocks.append(info)
    return blocks


async def _run_single_check(
    provider: LLMProvider,
    model: str,
    check: ReviewCheck,
    user_context: str,
) -> dict[str, Any]:
    """Запускает одну проверку и возвращает её результат."""
    try:
        llm_response = await provider.chat_with_retry(
            messages=[
                {"role": "system", "content": check.system_prompt},
                {"role": "user", "content": user_context},
            ],
            model=model,
            tools=None,
            temperature=0.0,
            max_tokens=500,
        )
        raw = llm_response.content or "{}"
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()
        result = json.loads(raw)
        if not isinstance(result, dict):
            return {"passed": False, "issues": [f"{check.name}: невалидный ответ"], "reason": "ошибка парсинга"}
        return {
            "passed": result.get("passed", False),
            "issues": [f"{check.name}: {i}" for i in result.get("issues", [])],
            "reason": result.get("reason", ""),
        }
    except Exception:
        return {"passed": True, "issues": [], "reason": f"{check.name}: ошибка ревьюера (пропущено)"}


async def run_review(
    provider: LLMProvider,
    model: str,
    response: str,
    all_msgs: list[dict],
    enabled_checks: set[str] | None = None,
) -> dict[str, Any]:
    """Запускает LLM-ревью — каждую проверку отдельным вызовом.

    Параметры:
        provider: LLM-провайдер
        model: модель для ревьюера
        response: ответ ассистента для проверки
        all_msgs: история сообщений (для извлечения tool results)
        enabled_checks: set названий проверок для запуска (None = все)

    Returns:
        {"quality": "good"|"bad", "issues": [...], "reason": "..."}
        При ошибке — quality="good" (не блокировать ответ).
    """
    if not response or not all_msgs:
        return {"quality": "good", "issues": [], "reason": "нечего проверять"}

    # Извлечь user query (последнее user-сообщение не от self-review)
    user_query = ""
    for msg in reversed(all_msgs):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and "[Self-review," not in content:
                user_query = content[:1000]
                break

    # Добавить предыдущий вопрос для контекста, если диалог многошаговый
    prev_questions = []
    found_current = False
    for msg in reversed(all_msgs):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str) and "[Self-review," not in content:
                if not found_current:
                    found_current = True
                    continue
                prev_questions.append(content[:300])
                if len(prev_questions) >= 2:
                    break
    if prev_questions:
        user_query = "Предыдущий контекст: " + " | ".join(reversed(prev_questions)) + "\n\nНовый вопрос: " + user_query

    # Fast-path: приветствия и общие вопросы пропускаем без проверок
    greeting_words = ["привет", "здравствуй", "что ты умеешь", "hello", "hi", "what can you do"]
    query_lower = user_query.lower().strip()
    if any(query_lower.startswith(w) or query_lower == w for w in greeting_words):
        return {"quality": "good", "issues": [], "reason": "приветствие — проверки пропущены"}

    tool_blocks = _extract_tool_blocks(all_msgs)
    has_tools = bool(tool_blocks)

    formatted = json.dumps(
        [
            {
                "tool": b["name"],
                "args": b["arguments"],
                "result": (b.get("result") or "")[:2000],
            }
            for b in tool_blocks
        ],
        ensure_ascii=False,
        indent=2,
    )

    user_context = _USER_CONTEXT_TEMPLATE.format(
        user_query=user_query or "(сообщение не найдено)",
        formatted_results=formatted if has_tools else "Инструменты не вызывались.",
        response=response,
    )

    # Отобрать включённые проверки
    checks_to_run = [
        c for c in _CHECKS
        if c.enabled and (enabled_checks is None or c.name in enabled_checks)
    ]

    if not checks_to_run:
        return {"quality": "good", "issues": [], "reason": "нет включённых проверок"}

    # Запустить все проверки последовательно
    results = []
    for c in checks_to_run:
        results.append(
            await _run_single_check(provider, model, c, user_context)
        )

    # Собрать результаты
    all_issues: list[str] = []
    all_reasons: list[str] = []
    all_passed = True

    for check, result in zip(checks_to_run, results):
        if not result.get("passed", True):
            all_passed = False
            all_issues.extend(result.get("issues", []))
            reason = result.get("reason", "")
            if reason:
                all_reasons.append(f"{check.name}: {reason}")

    if all_passed:
        return {
            "quality": "good",
            "issues": [],
            "reason": "Все проверки пройдены",
        }

    return {
        "quality": "bad",
        "issues": all_issues,
        "reason": "; ".join(all_reasons) or "Обнаружены проблемы",
    }
