"""Точка входа: CLI с разбором аргументов и запуском суммаризации.

Phase 2B API: ``summarizer.run(text, ...)`` возвращает dict со статусом
``completed`` / ``confirmation_required`` / ``requires_continuation`` /
``failed``. CLI делает estimate первым проходом (без LLM-вызовов),
показывает его пользователю и либо сразу выполняет (если короткая
операция), либо запрашивает `--confirm` (как в ARCHITECTURE.md
invariant #16).

Примеры запуска::

    # Оценка + исполнение короткого документа
    legal_summarize --file contract.pdf --length medium

    # Длинный документ: estimate + явное подтверждение
    legal_summarize --file gkodeksrf.pdf --length medium --confirm

    # С фокусом
    legal_summarize --file contract.pdf --focus "сроки и штрафы"

    # С контекстом чата (история для LLM)
    legal_summarize --file contract.pdf --length medium \\
        --context '[{"role":"user","content":"сфокусируйся на рисках"}]'
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[4]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="legal_summarizer — суммаризация юридических документов (Phase 2B)",
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Путь к документу (.pdf, .docx, .txt, …) — см. office_files.detect_format",
    )
    parser.add_argument(
        "--length",
        default=None,
        choices=["brief", "detailed"],
        help="Объём саммари: brief (150–250 слов) — первые 8 chunks; "
             "detailed (800–1200) — весь документ. По умолчанию — из project.json.",
    )
    parser.add_argument(
        "--question",
        default=None,
        help="Конкретный вопрос по документу (взаимоисключающе с --length). "
             "Skill найдёт релевантные chunks (≤8) и ответит кратко. "
             "Если ничего не найдено — fallback на чтение всего документа.",
    )
    parser.add_argument(
        "--focus",
        default=None,
        help="Тема, на которой фокусироваться при суммаризации (например, "
             "\"сроки и штрафы\"). Передаётся в LLM как instruction.",
    )
    parser.add_argument(
        "--context",
        default=None,
        type=json.loads,
        help='Контекст чата в формате JSON. Например: '
             '\'[{"role":"user","content":"сфокусируйся на рисках"}]\'',
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Подтвердить выполнение длинной операции (по умолчанию для "
             "коротких — авто). Без флага при confirmation_required CLI "
             "выводит estimate и завершает работу.",
    )
    parser.add_argument(
        "--max-chunks",
        default=None,
        type=int,
        help="Override ``max_chunks_for_execution`` из конфига. По "
             "умолчанию берётся из project.json::skills.legal_summarizer."
             "execution.max_chunks_for_execution.",
    )
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="Только оценить документ (без LLM-вызовов). Печатает "
             "Inspection (chunks, batches, sections) + Estimate "
             "(duration, llm_calls). Полезно для предварительной проверки.",
    )
    parser.add_argument(
        "--operation-id",
        default=None,
        help="Передать явный operation_id (иначе — автогенерация из текста).",
    )
    return parser


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# Sentinel, который навык печатает в stdout СТРОГО в самом конце любого
# завершённого прогона (успех / partial / confirmation / error). Агент ловит
# его через write_stdin(wait_for=...) и дожидается ОДНИМ блокирующим вызовом
# реального конца вместо опроса каждые 30 сек (каждый опрос = лишний
# LLM-вызов агента). Progress-строки уходят в stderr и этот sentinel не
# содержат, поэтому ложных срабатываний нет.
_DONE_SENTINEL = "__LEGAL_SUMMARIZER_DONE__"


def _emit_done(payload: dict) -> None:
    """Напечатать финальный результат, затем sentinel завершения в stdout."""
    _emit(payload)
    print(_DONE_SENTINEL)


def _emit_running_marker(text: str) -> None:
    """Маркер старта длинного прогона — печатается ПЕРВЫМ в stdout.

    Агент при ``exec`` (или первом ``write_stdin``) видит этот JSON и
    понимает: (1) скилл реально работает, не висит; (2) сколько примерно
    ждать; (3) с каким интервалом опрашивать. Без этого маркера агент
    вслепую вызывал write_stdin каждые 30 сек — ~14 LLM-вызовов на
    прогон 7 мин (инцидент 2026-08-28).

    Оценки грубые (без структурного парсинга): только по длине текста.
    ``done_marker`` — sentinel, который навык печатает в stdout в самом
    конце прогона. Агент должен ждать ЕГО одним блокирующим вызовом
    ``write_stdin`` (``wait_for=done_marker``, ``wait_timeout_ms=120000``),
    а НЕ опрашивать по таймеру. Каждый опрос = лишний LLM-вызов агента,
    поэтому блокирующее ожидание радикально их сокращает.

    ``poll_interval_hint_sec`` оставлен для обратной совместимости (и тестов)
    как грубая оценка ожидания; семантики «частота опроса» больше не несёт.
    Лимит nanobot ``wait_timeout_ms ≤ 120000`` (инцидент 2026-08-28) — поэтому
    один блокирующий вызов покрывает прогон ≤120 сек; для более длинных
    агент делает повторный write_stdin с тем же ``wait_for`` (минимум вызовов).
    """
    from summarizer import get_chunking_config, get_execution_config
    chunk_size = int(get_chunking_config().get("chunk_size", 100000))
    chunk_dur = float(get_execution_config().get("estimated_chunk_duration_sec", 20))
    rough_chunks = max(1, -(-len(text) // max(1, chunk_size)))
    estimated_total_sec = max(1.0, rough_chunks * chunk_dur)
    # Интервал polling жёстко задан: 60-90 сек. Меньше нельзя (хуже
    # пользовательского опыта — слишком частые polls), больше нельзя
    # потому что nanobot лимит wait_timeout_ms = 120000 (120 сек) —
    # агент не сможет ждать дольше одним write_stdin и вынужден будет
    # опрашивать несколько раз (= больше LLM-вызовов). Прикидка:
    # estimated_total_sec/5, кламп в [60, 90]. Для прогона 7 мин это
    # 5-7 polls вместо 14 (вслепые 30-сек).
    poll_interval_hint_sec = max(60, min(90, int(estimated_total_sec / 5)))
    marker = {
        "mode": "summarize",
        "status": "running",
        "estimated_total_sec": int(estimated_total_sec),
        "poll_interval_hint_sec": poll_interval_hint_sec,
        "hint": (
            f"Обработка займёт примерно {int(estimated_total_sec)} сек. "
            f"НЕ опрашивайте по таймеру — это лишние LLM-вызовы. Дождитесь "
            f"конца ОДНИМ блокирующим write_stdin: wait_for=<финальный маркер "
            f"завершения, см. SKILL.md>, wait_timeout_ms=120000 (максимум "
            f"nanobot). Навык напечатает этот маркер в stdout строго в самом "
            f"конце (успех/ошибка/confirmation). Если вернулось «Wait target "
            f"not observed» (прогон >120 сек), вызовите write_stdin ещё раз с "
            f"тем же wait_for — вызовов будет минимум."
        ),
    }
    _emit(marker)


def _error(status_message: str, exc: Exception | None = None) -> dict:
    out: dict = {
        "mode": "summarize",
        "status": "error",
        "message": status_message,
    }
    if exc is not None and not isinstance(exc, (FileNotFoundError, ValueError, argparse.ArgumentTypeError)):
        out["traceback"] = traceback.format_exc()
    return out


def _ensure_registered() -> None:
    """Зарегистрировать legal_summarizer и runtime-инфраструктуру в ``table_registry``.

    Standalone CLI не имеет ``ApplicationContext`` (его поднимает gateway),
    поэтому skill сам себя регистрирует из ``project.json::skills.legal_summarizer``
    через ``lib.core.skill_registration.register_skill_from_config``. Идемпотентно:
    если skill уже зарегистрирован (например, gateway-populated реестр),
    повторная регистрация игнорируется.

    Для skill'а без vector-инфраструктуры и без PG-таблиц вызовы
    ``register_vector_storage`` и ``register_embedding_config`` будут no-op,
    но вызываются для единообразия с полными skill'ами (audit_analyzer).
    """
    from lib.core.infra_registration import register_vector_storage
    from lib.core.skill_registration import (
        register_embedding_config,
        register_skill_from_config,
    )
    from config import SETTINGS

    legal_cfg = SETTINGS.get("skills", {}).get("legal_summarizer", {})
    register_skill_from_config("legal_summarizer", legal_cfg)
    register_vector_storage()
    register_embedding_config()


def main() -> None:
    try:
        _ensure_registered()
        parser = _build_parser()
        args = parser.parse_args()

        from output import prepare_output
        from summarizer import (
            estimate as _estimate,
            inspect as _inspect,
            load_text,
            needs_confirmation,
            quick_estimate,
            run,
        )
        from summarizer import _progress
        from skill_config import get_default_length

        length = args.length or get_default_length()
        if args.question and args.length:
            _emit_done(_error(
                "--question и --length взаимно исключают друг друга. "
                "Укажите либо --question, либо --length."
            ))
            sys.exit(2)
        file_path = Path(args.file)

        # Fast pre-confirm gate: БЕЗ полной экстракции текста. Для PDF
        # (ГК РФ, 663 стр.) полная экстракция через pdfplumber занимает
        # 3–5 минут — пользователь ждал только чтобы узнать «документ
        # большой» (инцидент 2026-08-28). quick_estimate (pypdf page_count
        # + сэмпл 10 стр.) → секунды.
        # Payload для агента: меню из двух вариантов (brief / detailed) +
        # подсказка про --question. Технические числа (chunks, batches,
        # llm_calls) НЕ отдаём — агент их зеркалит в ответ, что раздражает
        # (инцидент 2026-08-28).
        if not args.confirm and not args.estimate_only:
            try:
                qe = quick_estimate(file_path)
                qest = qe["estimate"]
                if needs_confirmation(qest):
                    from output import build_confirmation_options
                    payload = build_confirmation_options(
                        chars_in=qe["chars_in"],
                        min_seconds=qest.estimated_duration_min_sec,
                        max_seconds=qest.estimated_duration_max_sec,
                    )
                    _emit_done(payload)
                    return
            except Exception as exc:
                # quick_estimate упал (битый PDF и т.п.) → fallback к полному
                # пути: полная экстракция + inspect + safety-net confirm.
                _progress(f"quick_estimate failed ({exc}); falling back to full inspect")

        # Всегда полная экстракция. Для brief mode ускорение — через
        # выборку первых 8 chunks (summarizer.select_brief_chunks) +
        # параллельные map-вызовы (concurrency=4).
        text = load_text(file_path)

        if args.estimate_only:
            insp = _inspect(text, document_path=str(args.file))
            est = _estimate(insp)
            _emit_done({
                "mode": "estimate_only",
                "status": "ok",
                "chars_in": len(text),
                "chunks_total": len(insp.chunks),
                "context_batches_total": len(insp.context_batches),
                "sections_total": len(insp.tree.sections) if insp.tree else 0,
                # estimated_llm_calls намеренно не отдаём — пользователю
                # важно только время; агенты склонны зеркалить числа.
                "strategy": insp.strategy,
                "estimated_duration_min_sec": est.estimated_duration_min_sec,
                "estimated_duration_max_sec": est.estimated_duration_max_sec,
                "confirmation_threshold_sec": est.confirmation_threshold_sec,
                "needs_confirmation": needs_confirmation(est),
            })
            return

        kwargs: dict = {
            "length": length,
            "focus": args.focus,
            "question": args.question,
            "operation_id": args.operation_id,
            "document_path": str(args.file),
            # Корень РЕПО (не workspace dir) — стабильный абсолютный путь,
            # выведенный из __file__. Раньше передавали None → скилл брал
            # относительный путь и при cwd=<workspace> создавал дубль
            # workspace/workspace/data_store/... (см. инцидент 2026-08-28).
            "workspace_root": Path(__file__).resolve().parents[4],
        }

        # Safety-net: если quick_estimate сказал «не нужно confirm» (или
        # упал и упал на fallback), полный inspect может пересмотреть.
        if not args.confirm:
            insp = _inspect(text, document_path=str(args.file))
            est = _estimate(insp)
            if needs_confirmation(est):
                from output import build_confirmation_options
                payload = build_confirmation_options(
                    chars_in=len(text),
                    min_seconds=est.estimated_duration_min_sec,
                    max_seconds=est.estimated_duration_max_sec,
                )
                _emit_done(payload)
                return

        # Маркер старта длинного прогона: печатаем в stdout ДО вызова run(),
        # чтобы агент увидел его в первом exec/write_stdin результате и
        # перестал опрашивать каждые 30 сек вслепую. Содержит
        # estimated_total_sec (грубая оценка) и poll_interval_hint_sec
        # (рекомендация для write_stdin yield_time_ms). Это решает
        # «инцидент 2026-08-28»: ~14 LLM-вызовов на polling вместо ~3-4.
        _emit_running_marker(text)

        result = run(text, confirmed=args.confirm, **kwargs)
        out = prepare_output(result)
        _emit_done(out)
    except argparse.ArgumentTypeError as e:
        _emit_done(_error(str(e)))
        sys.exit(1)
    except FileNotFoundError as e:
        _emit_done(_error(str(e)))
        sys.exit(1)
    except ValueError as e:
        _emit_done(_error(str(e)))
        sys.exit(1)
    except Exception as e:
        _emit_done(_error(f"Внутренняя ошибка: {e}", e))
        sys.exit(1)


if __name__ == "__main__":
    main()