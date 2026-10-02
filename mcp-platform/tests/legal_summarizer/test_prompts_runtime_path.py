"""Страж: промпты домена лежат там, где их ищет ``llm.prompts_runtime``.

Почему страж нужен именно после переноса
----------------------------------------
До переноса в платформу код жил в ``<skill>/scripts/llm/``, и промпты — в
``<skill>/prompts/``: якорь ``parents[2]`` от ``prompts_runtime.py`` указывал
на каталог скилла. После переноса код переехал в
``mcp-platform/libs/legal_summarizer/``, и payload с ``SKILL.md`` переехал
рядом с доменом, в ``libs/legal_summarizer/skill/``. Старый якорь стал
указывать в ``mcp-platform/libs/prompts/``, которого нет.

Payload намеренно лежит в домене, а не в
``servers/enterprise/capabilities/legal_summarizer/``: страж
``test_no_capability_without_operations`` требует у каждого каталога в
``capabilities/`` наличие ``tools/*.py``, а
``test_every_capability_on_disk_is_in_the_registry`` - запись в реестре
``CAPABILITIES``. Capability без операций и без записи в реестре - это
недоделанная работа, а не заготовка. Перенос payload в capability
(п. 11.4) делается единым куском вместе с ``tools/`` и реестром.

Симптом был тихим и далёким от причины: ``llm.calls`` падал на
``load_prompt`` ещё ДО LLM-вызова, а ``run_direct`` глотал исключение
через голый ``except Exception`` и отдавал
``status='failed', REDUCE_INPUT_EMPTY`` — то есть «LLM вернул пустое
саммари» при коде, который до LLM даже не дошёл.
"""
from __future__ import annotations

import libs.legal_summarizer.llm.prompts_runtime as prompts_runtime


def test_prompts_dir_is_the_domain_skill_payload():
    """Каталог промптов — payload домена, рядом с ``SKILL.md``."""
    prompts_dir = prompts_runtime._PROMPTS_DIR
    assert prompts_dir.is_dir(), (
        f"каталог промптов не найден: {prompts_dir}. "
        f"Промпты обязаны лежать в libs/legal_summarizer/skill/prompts/."
    )
    assert prompts_dir.name == "prompts"
    assert prompts_dir.parent.name == "skill"
    assert prompts_dir.parent.parent.name == "legal_summarizer"
    assert prompts_dir.parts[-4:] == ("libs", "legal_summarizer", "skill", "prompts")


def test_prompts_are_not_under_capabilities_dir():
    """Payload не лежит в ``capabilities/``, пока у capability нет операций.

    Появление каталога ``servers/enterprise/capabilities/legal_summarizer/``
    без ``tools/*.py`` и без записи в ``CAPABILITIES`` роняет два стража
    платформы. Страж стоит здесь, чтобы payload не вернулся туда молча.
    """
    parts = prompts_runtime._PROMPTS_DIR.parts
    assert "capabilities" not in parts, (
        "промпты вернулись в capabilities/: каталог capability обязан иметь "
        "tools/*.py и запись в реестре CAPABILITIES"
    )


def test_every_declared_prompt_file_exists():
    """Каждый объявленный в реестре промпт физически присутствует."""
    missing = [
        f"{name} -> {path}"
        for name, path in prompts_runtime._PROMPT_FILES.items()
        if not path.is_file()
    ]
    assert not missing, "объявлены, но отсутствуют промпты: " + "; ".join(missing)


def test_load_prompt_returns_non_empty_text_with_instruction_slot():
    """Промпты читаются и содержат подставляемый слот инструкции.

    Пустой файл прошёл бы проверку ``is_file()``, но сломал бы сборку
    сообщения: ``.replace('{length_instruction}', ...)`` оставил бы в
    system-промпте сырой плейсхолдер длины.
    """
    for name in ("summarize_system", "reduce_system", "section_reduce_system"):
        text = prompts_runtime.load_prompt(name)
        assert text.strip(), f"{name}: промпт пуст"
        assert "{length_instruction}" in text, (
            f"{name}: нет слота {{length_instruction}} — "
            f"system_instruction() не подставится"
        )
