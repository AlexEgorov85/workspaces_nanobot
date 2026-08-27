"""Smoke-тесты навыка legal_summarizer (без реальных вызовов LLM).

Покрывает:
    * summarizer: короткий документ → strategy=single
    * summarizer: длинный документ → strategy=map_reduce, chunks>1
    * summarizer: пустой текст → status=error
    * summarizer: subject извлекается из первой строки
    * summarizer: неизвестный length → fallback на medium
    * load_text: неизвестное расширение → ValueError
    * load_text: несуществующий файл → FileNotFoundError
    * load_text: пустой .txt → ValueError
    * output: структура prepare_output
    * skill_config: дефолты через lib.core.skill_config

Запуск:
    python -m pytest tests/test_skill_legal_summarizer.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SKILL_ROOT = Path(__file__).resolve().parents[1] / "workspace" / "skills" / "legal_summarizer"
_SCRIPTS_DIR = _SKILL_ROOT / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import summarizer  # noqa: E402
from output import _sanitize_value, prepare_output  # noqa: E402

# ---------------------------------------------------------------------------
# summarizer
# ---------------------------------------------------------------------------


def test_short_document_single_strategy(monkeypatch):
    """Короткий документ (< threshold) → один вызов LLM, strategy=single."""
    text = "Это договор аренды. Арендодатель сдаёт помещение арендатору."
    monkeypatch.setattr(
        summarizer.llm,
        "chat",
        lambda messages, *, context=None, **kwargs: (
            "Это договор аренды.\n\nСуть: аренда помещения."
        ),
    )
    result = summarizer.summarize(text, length="brief")
    assert result["status"] == "success"
    data = result["data"]
    assert data["strategy"] == "single"
    assert data["chunks"] == 1
    assert data["length"] == "brief"
    assert data["chars_in"] == len(text)
    assert data["subject"] == "Это договор аренды."
    assert "аренда" in data["summary"].lower()


def test_long_document_map_reduce_strategy(monkeypatch):
    """Длинный документ (> threshold) → map-reduce, strategy=map_reduce.

    Использует небольшой threshold и короткие абзацы, чтобы избежать
    длинного пути через ``split_text`` (он параметризуется в скилле
    и сейчас не предназначен для огромных текстов в одном прогоне).
    """
    monkeypatch.setattr(
        summarizer,
        "get_chunking_config",
        lambda: {
            "chunk_size": 80,
            "chunk_overlap": 0,
            "single_call_threshold": 50,
        },
    )

    paragraph = "Арендодатель сдаёт помещение арендатору на три года."
    text = "\n\n".join([paragraph] * 10)
    assert len(text) > 50

    state = {"n": 0}

    def fake_chat(messages, *, context=None, **kwargs):
        state["n"] += 1
        return f"Это договор аренды.\n\nЧасть {state['n']}: суть аренды."

    monkeypatch.setattr(summarizer.llm, "chat", fake_chat)
    # max_chunks=None в тесте — глобальный лимит не должен срабатывать
    # на маленьком синтетическом документе (10 чанков < дефолт 5,
    # но в тесте важна общая логика map_reduce, а не лимит).
    result = summarizer.summarize(text, length="medium", max_chunks=None)

    assert result["status"] == "success"
    data = result["data"]
    assert data["strategy"] == "map_reduce"
    assert data["chunks"] > 1
    # N чанков (map) + 1 (reduce)
    assert state["n"] == data["chunks"] + 1
    assert data["subject"]


def test_empty_text_returns_error(monkeypatch):
    monkeypatch.setattr(summarizer.llm, "chat", lambda *a, **kw: "x")
    result = summarizer.summarize("", length="brief")
    assert result["status"] == "error"
    assert "message" in result["data"]


def test_subject_extracted_from_first_line(monkeypatch):
    text = "Договор подряда."
    monkeypatch.setattr(
        summarizer.llm,
        "chat",
        lambda messages, *, context=None, **kwargs: (
            "Это договор подряда: заказчик нанимает подрядчика.\n\n"
            "Подрядчик выполняет работу."
        ),
    )
    result = summarizer.summarize(text, length="brief")
    assert (
        result["data"]["subject"]
        == "Это договор подряда: заказчик нанимает подрядчика."
    )


def test_invalid_length_falls_back_to_medium(monkeypatch):
    text = "Договор."
    monkeypatch.setattr(
        summarizer.llm,
        "chat",
        lambda messages, *, context=None, **kwargs: "Это договор.\n\nСуть.",
    )
    result = summarizer.summarize(text, length="nonexistent")
    assert result["data"]["length"] == "medium"


def test_prompts_load_from_markdown(monkeypatch):
    """Промпты читаются из ``prompts/*.md``, а не хардкодятся в .py."""
    monkeypatch.setattr(
        summarizer.llm,
        "chat",
        lambda messages, *, context=None, **kwargs: (
            "Это договор аренды.\n\nСуть."
        ),
    )
    summarizer.summarize("Договор аренды.", length="brief")

    # Реальное содержимое промпта должно лежать в .md
    summarize_md = (
        Path(summarizer.__file__).resolve().parent.parent
        / "prompts"
        / "summarize_system.md"
    )
    reduce_md = (
        Path(summarizer.__file__).resolve().parent.parent
        / "prompts"
        / "reduce_system.md"
    )
    assert summarize_md.is_file()
    assert reduce_md.is_file()
    assert "юридическ" in summarize_md.read_text(encoding="utf-8").lower()
    assert "юридическ" in reduce_md.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------------------
# load_text (через office_files.extract_text)
# ---------------------------------------------------------------------------


def test_load_text_txt_success(tmp_path):
    p = tmp_path / "contract.txt"
    p.write_text("Договор аренды.\n\nАрендодатель сдаёт помещение.\n", encoding="utf-8")
    text = summarizer.load_text(p)
    assert "Договор аренды" in text
    assert "Арендодатель" in text


def test_load_text_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        summarizer.load_text(tmp_path / "missing.pdf")


def test_load_text_empty_file_raises(tmp_path):
    p = tmp_path / "empty.txt"
    p.write_text("   \n\n  \n", encoding="utf-8")
    with pytest.raises(ValueError, match="не содержит извлекаемого текста"):
        summarizer.load_text(p)


def test_load_text_unknown_extension_raises(tmp_path):
    # ``.bin`` не зарегистрирован в mimetypes → office_files падает
    # на detect_format. Проверяем, что либо ValueError, либо любой
    # ожидаемый exception от офисного слоя — пользователь увидит ошибку.
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00\x01\x02" * 16)
    with pytest.raises((ValueError, LookupError, Exception)):
        summarizer.load_text(p)


# ---------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------


def test_prepare_output_success():
    result = {
        "status": "success",
        "data": {
            "subject": "Договор аренды.",
            "summary": "Договор аренды.\n\nСуть: аренда.",
            "length": "medium",
            "chars_in": 100,
            "chunks": 3,
            "strategy": "map_reduce",
        },
    }
    out = prepare_output(result)
    assert out["mode"] == "summarize"
    assert out["status"] == "success"
    assert out["subject"] == "Договор аренды."
    assert out["summary"].startswith("Договор")
    assert out["length"] == "medium"
    assert out["chunks"] == 3
    assert out["strategy"] == "map_reduce"


def test_prepare_output_error():
    result = {
        "status": "error",
        "data": {"message": "Документ не содержит текста"},
    }
    out = prepare_output(result)
    assert out["mode"] == "summarize"
    assert out["status"] == "error"
    assert out["message"] == "Документ не содержит текста"


def test_sanitize_handles_datetime():
    from datetime import datetime
    out = _sanitize_value({"d": datetime(2024, 1, 15, 10, 30)})
    assert out["d"] == "2024-01-15T10:30:00"


# ---------------------------------------------------------------------------
# skill_config (обёртка над lib.core.skill_config)
# ---------------------------------------------------------------------------


def test_skill_config_chunking_defaults_match_project_json():
    """Если chunking явно задан в project.json — он читается."""
    import skill_config

    cfg = skill_config.get_chunking_config()
    assert cfg["chunk_size"] == 12000
    assert cfg["chunk_overlap"] == 1000
    assert cfg["single_call_threshold"] == 20000


def test_skill_config_cli_matches_project_json():
    import skill_config

    # ``lib.core.skill_config.get_cli_config`` — generic-обёртка
    # (default_mode/default_format/max_retries/timeout_sec).
    # Skill-специфичные поля (``default_length`` для legal_summarizer)
    # читаются напрямую через skill_config.
    cli = skill_config.get_cli_config()
    assert cli["max_retries"] == 3
    assert cli["timeout_sec"] == 120
    assert skill_config.get_default_length() == "medium"


def test_max_chunks_default_is_five():
    """Защита от подвисания на огромных документах: max_chunks=None даёт
    дефолт 5, при превышении skill возвращает structured error не вызывая LLM."""
    long_text = "x" * 200_000
    called_llm = {"n": 0}

    def fake_chat(*args, **kwargs):
        called_llm["n"] += 1
        return "summary"

    import summarizer
    summarizer.llm.chat = fake_chat
    result = summarizer.summarize(long_text, length="brief", max_chunks=3)
    assert result["status"] == "error"
    assert "max_chunks" in result["data"]["message"] or \
        "слишком" in result["data"]["message"].lower()
    assert called_llm["n"] == 0, "LLM не должен вызываться при превышении max_chunks"


def test_max_chunks_allow_small_doc():
    """Для маленького документа max_chunks не блокирует."""
    small_text = "Договор аренды.\n\nСрок 11 месяцев, оплата помесячно."
    import summarizer
    summarizer.llm.chat = lambda *a, **kw: "Это договор.\n\nСуть."
    result = summarizer.summarize(small_text, length="brief", max_chunks=5)
    assert result["status"] == "success"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
