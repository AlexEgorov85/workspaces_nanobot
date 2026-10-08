#!/usr/bin/env python3
"""Классификация активных change'ов OpenSpec: что действительно в работе,
что готово к архивированию, а что застряло.

Зачем отдельный инструмент. Три состояния, которые нельзя различить по
``tasks.md``:

1. **Дельта нацелена на несуществующую спеку.** Путь спеки в каноне
   (``openspec/specs/<категория>/<компонент>/spec.md``) ещё не создан.
   ``openspec archive`` такой change не отвергает — он молча создаёт заготовку
   без обязательных разделов, без записи в ``COMPONENTS.md`` и без записи в
   ``OWNERSHIP.md``, и валидатор падает уже после того, как дельта применена.
2. **Несколько change'ов претендуют на один и тот же путь спеки.** Канон
   пишет создатель, поэтому порядок архивирования решает, чьё требование
   выживет; очередь тут не работает.
3. **Часть задач закрыта другими.** Дельта change'а уже применена архивным
   change'ом или требование уже лежит в каноне, но ``tasks.md`` об этом не
   знает и продолжает числить задачу открытой.

Что из этого препятствие, а что нет. Прежняя версия складывала в один список
и препятствия, и признаки состояния, из-за чего отчёт показывал «требуют
решения 10 из 14» там, где непроходимы были меньше. Разделение:

* **Препятствие** — архив в таком состоянии сделает не то, что ожидают:
  дельта заводит несуществующую спеку; дельта с операцией ``ADDED`` содержит
  требование, уже лежащее в каноне (архив добавит второй такой же заголовок);
  состояние недоказуемо (нет ``tasks.md``).
* **Признак состояния** — архив ничего не испортит: требования уже применены
  архивом; требования уже лежат в каноне под операцией ``MODIFIED`` (архив их
  обновит, дублирования не будет); дельт нет вовсе.

Операция дельты — не украшение: именно она решает, обновит архив канон или
продублирует его. Поэтому она и разбирается, а не только имя требования.

Инструмент ничего не меняет: он только сообщает. Решение принимает владелец.

Использование::

    python tools/change_status.py            # отчёт по всем активным change'ам
    python tools/change_status.py --blocked  # только те, что нельзя архивировать

Код возврата 0, если блокирующих состояний нет, 1 — есть. Применим к CI:
джоба ``spec-validation`` может вызывать его рядом с валидатором структуры.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGES_DIR = ROOT / "openspec" / "changes"
ARCHIVE_DIR = CHANGES_DIR / "archive"
SPECS_DIR = ROOT / "openspec" / "specs"
COMPONENTS = SPECS_DIR / "COMPONENTS.md"
OWNERSHIP = SPECS_DIR / "OWNERSHIP.md"

REQ_RE = re.compile(r"^### Requirement: (.+?)\s*$")
OPS_RE = re.compile(r"^## (ADDED|REMOVED|MODIFIED|RENAMED) Requirements\s*$")
DONE_RE = re.compile(r"^\s*-\s*\[x\]", re.I)
TODO_RE = re.compile(r"^\s*-\s*\[ \]", re.I)


def _read(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _norm(text: str) -> str:
    """Сравнение текстов требований не должно зависеть от пустых строк."""
    return "\n".join(ln.rstrip() for ln in text.strip().splitlines() if ln.strip())


def requirement_bodies(text: str) -> dict[str, str]:
    """{имя требования: тело} — тело строго до следующей строки-заголовка."""
    lines = text.splitlines()
    out: dict[str, str] = {}
    i = 0
    while i < len(lines):
        m = REQ_RE.match(lines[i])
        if not m:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and not lines[j].lstrip().startswith("#"):
            j += 1
        out[m.group(1).strip()] = "\n".join(lines[i + 1:j])
        i = j
    return out


def canon_specs() -> dict[str, dict[str, str]]:
    """{'<категория>/<компонент>': {имя требования: текст}} по канону.

    Нужен именно текст, а не только имя: `MODIFIED` поверх требования с
    другим текстом не добавляет дубль, а молча переписывает канон версией
    change'а. Различить это по именам нельзя.
    """
    out: dict[str, dict[str, str]] = {}
    for cat in sorted(p for p in SPECS_DIR.iterdir() if p.is_dir()):
        for comp in sorted(p for p in cat.iterdir() if p.is_dir()):
            spec = comp / "spec.md"
            if not spec.exists():
                continue
            out[f"{cat.name}/{comp.name}"] = requirement_bodies(
                spec.read_text(encoding="utf-8")
            )
    return out


def archived_specs() -> dict[str, set[str]]:
    """То же по архиву: какие имена требований уже применены."""
    out: dict[str, set[str]] = {}
    if not ARCHIVE_DIR.is_dir():
        return out
    for change in sorted(ARCHIVE_DIR.iterdir()):
        if not change.is_dir():
            continue
        sd = change / "specs"
        if not sd.is_dir():
            continue
        for delta in sorted(sd.rglob("*.md")):
            key = delta.parent.relative_to(sd).as_posix()
            for ln in _read(delta):
                m = REQ_RE.match(ln)
                if m:
                    out.setdefault(key, set()).add(m.group(1).strip())
    return out


def delta_targets(change: Path) -> dict[str, dict[str, tuple[str, str]]]:
    """{'<категория>/<компонент>': {имя требования: (операция, текст)}}.

    Операция и текст нужны не для красоты. Одно и то же требование, уже
    лежащее в каноне, дельта может ``ADDED``-ить (второй заголовок с тем же
    именем), ``MODIFIED``-ить (канон переписывается версией change'а, если
    тексты разошлись) или ``REMOVED``-ить (живое требование исчезает). По
    одному имени все три случая неразличимы, и по отчёту они выглядят
    одинаково — а стоят по-разному.
    """
    sd = change / "specs"
    out: dict[str, dict[str, tuple[str, str]]] = {}
    if not sd.is_dir():
        return out
    for delta in sorted(sd.rglob("*.md")):
        key = delta.parent.relative_to(sd).as_posix()
        text = delta.read_text(encoding="utf-8")
        bodies = requirement_bodies(text)
        op = "?"
        for ln in text.splitlines():
            m = OPS_RE.match(ln)
            if m:
                op = m.group(1)
                continue
            m = REQ_RE.match(ln)
            if m:
                name = m.group(1).strip()
                out.setdefault(key, {})[name] = (op, bodies.get(name, ""))
    return out


def task_counts(change: Path) -> tuple[int, int, bool]:
    """(выполнено, осталось, есть ли tasks.md)."""
    tasks = change / "tasks.md"
    if not tasks.exists():
        return 0, 0, False
    done = sum(1 for ln in _read(tasks) if DONE_RE.match(ln))
    todo = sum(1 for ln in _read(tasks) if TODO_RE.match(ln))
    return done, todo, True


def registered_paths() -> tuple[set[str], set[str]]:
    """Пути спек, упомянутые в COMPONENTS.md и OWNERSHIP.md."""
    def collect(path: Path) -> set[str]:
        if not path.exists():
            return set()
        found: set[str] = set()
        for ln in _read(path):
            for m in re.finditer(r"([a-z][\w-]*)/([\w][\w-]*)", ln):
                found.add(f"{m.group(1)}/{m.group(2)}")
        return found
    return collect(COMPONENTS), collect(OWNERSHIP)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--blocked", action="store_true",
                    help="показать только те, что архивировать нельзя")
    args = ap.parse_args(argv)

    canon = canon_specs()
    archived = archived_specs()
    in_components, in_ownership = registered_paths()

    rows: list[tuple[str, str, list[str], list[str]]] = []
    creators: dict[str, list[str]] = {}

    for change in sorted(p for p in CHANGES_DIR.iterdir() if p.is_dir()):
        if change.name == "archive":
            continue
        done, todo, has_tasks = task_counts(change)
        targets = delta_targets(change)
        blockers: list[str] = []
        notes: list[str] = []

        new_paths = sorted(k for k in targets if k not in canon)
        for key in new_paths:
            creators.setdefault(key, []).append(change.name)

        already = [
            f"{key.split('/')[-1]}/{name}"
            for key, names in targets.items()
            for name in sorted(names)
            if key in archived and name in archived[key]
        ]
        in_canon = [
            f"{key.split('/')[-1]}/{name}"
            for key, names in targets.items()
            for name in sorted(names)
            if key in canon and name in canon[key]
        ]
        # Что архив сделает с каноном, если требование в нём уже живёт.
        # Три исхода различаются, и по одному имени они неразличимы:
        #   ADDED    — второй заголовок с тем же именем (канон спорит сам с собой)
        #   REMOVED  — живое требование исчезает
        #   MODIFIED — канон переписывается версией change'а, если тексты разошлись
        # Пока задачи открыты, любое из этого означает, что архив применит
        # частичную работу, — поэтому это препятствие, а не примечание.
        duplicate: list[str] = []
        delete: list[str] = []
        overwrite: list[str] = []
        for key, names in targets.items():
            if key not in canon:
                continue
            for name, (op, body) in sorted(names.items()):
                if name not in canon[key]:
                    continue
                label = f"{key.split('/')[-1]}/{name}"
                if op == "ADDED":
                    duplicate.append(label)
                elif op == "REMOVED":
                    delete.append(label)
                elif op == "MODIFIED" and _norm(canon[key][name]) != _norm(body):
                    overwrite.append(label)
        done_work = has_tasks and todo == 0

        if not has_tasks:
            blockers.append("нет tasks.md — состояние недоказуемо")
        if new_paths:
            # Поднимается ВСЕГДА, а не только при закрытых задачах. Прежнее
            # условие ``todo == 0 and done > 0`` молчало ровно там, где
            # опаснее всего: у change’а с открытыми задачами и несуществующей
            # целью флаг не поднимался, и он выглядел как обычная работа в
            # процессе — неотличим от здорового. Из-за этого в отчёт попадало
            # 8 из фактических 13 change’ов с препятствиями.
            blockers.append(
                "дельта заводит спеки, которых нет в каноне — архив создаст "
                "заготовку без обязательных разделов и без записи в реестрах: "
                + ", ".join(new_paths)
            )
        # Опасные операции — препятствие, пока работа change'а не сделана.
        # Сделана — примечание: снятие требования и переписывание его текста
        # могут быть именно тем, ради чего change заведён.
        danger = (("удалит из канона", delete, "REMOVED"),
                  ("перезапишет текст в каноне", overwrite, "MODIFIED, тексты разошлись"),
                  ("продублирует", duplicate, "ADDED"))
        for what, items, why in danger:
            if not items:
                continue
            msg = (f"архив {what} требований: {len(items)} — {why}: "
                   + ", ".join(items[:4]))
            if done_work:
                notes.append(msg)
            else:
                blockers.append(msg + " (работа change'а не сделана)")
        if not targets and not has_tasks:
            blockers.append("нет ни дельт, ни tasks.md — нечего архивировать и нечего проверять")
        for key in new_paths:
            if key in in_ownership and key not in canon:
                blockers.append(f"путь {key} упомянут в OWNERSHIP.md, но спеки нет")

        if already:
            notes.append(f"требований уже применено архивом: {len(already)}")
        if in_canon:
            notes.append(f"требований уже лежит в каноне: {len(in_canon)}")
        if not targets and has_tasks:
            notes.append("дельт нет — архив перенесёт только proposal.md и tasks.md, "
                         "канон не изменится")

        if not has_tasks:
            state = "БЕЗ tasks.md"
        elif todo == 0:
            state = "задачи закрыты"
        else:
            state = f"в работе {done}/{done + todo}"
        rows.append((change.name, state, blockers, notes))

    multi = {k: v for k, v in creators.items() if len(v) > 1}

    print("=" * 96)
    print(f"Активных change'ов: {len(rows)}   спеек в каноне: {len(canon)}")
    print("=" * 96)
    for name, state, blockers, notes in rows:
        if args.blocked and not blockers:
            continue
        print(f"{name:<50} {state}")
        for b in blockers:
            print(f"    ! {b}")
        for n in notes:
            print(f"    ~ {n}")

    if multi:
        print()
        print("=" * 96)
        print("ПУТЬ СПЕКИ ЗАЯВЛЕН НЕСКОЛЬКИМИ CHANGE'АМИ — порядок архивирования решает,")
        print("чьё требование выживет. Очередь не подходит: канон пишет создатель.")
        print("=" * 96)
        for key, owners in sorted(multi.items()):
            print(f"{key}:")
            for o in owners:
                print(f"    - {o}")

    blocked = [r for r in rows if r[2]]
    ready = [r for r in rows if not r[2] and r[1] == "задачи закрыты"]
    print()
    print(f"Требуют решения: {len(blocked)} из {len(rows)}; "
          f"готовы к архивированию: {len(ready)}; "
          f"конфликтов за путь спеки: {len(multi)}")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
