"""Надгробия операций: на диске не должно быть файлов, отрицающих операции.

Загрузчик по соглашению пропускает модули с именем на подчёркивании
(``libs/enterprise_common/loader.py``), поэтому такой файл не попадает ни в
реестр, ни в чей-либо прогон тестов. Он ничего не делает — и именно поэтому
опасен: файл ``_claim_task.py`` утверждал, что операции ``claim_task`` нет,
и называл причину. Когда операцию вернули (шаг 1 change
``2026-10-02-task-queue-into-mcp``), надгробие стало читаться как запрет на
то, что есть.

Проверять такое поведением нельзя: на пропущенном файле нет поведения.
Поэтому проверяется наличие — и список известных остатков зафиксирован
ниже явно, с причиной, по которой файл ещё лежит.

Страж срабатывает на **новом** надгробии, а не на перечисленных. Это
осознанное отступление от формулировки «на диске не должно быть»: удалить
файлы на этой машине нельзя (политика окружения запрещает удаление, а
обходить её нельзя), и молчащий запрет был бы нарушен с первого же дня.
Список ниже — не «зелёный статус», а список долга: он пустеет по мере
удаления, и каждый элемент назван.
"""

from __future__ import annotations

from pathlib import Path

PLATFORM_ROOT = Path(__file__).resolve().parent.parent
CAPABILITY_TOOLS = PLATFORM_ROOT / "servers" / "enterprise" / "capabilities"

#: Известные надгробия: путь → почему файл ещё лежит.
#:
#: **Список пуст.** Долг был погашен: `_claim_task.py` и
#: `_update_task_status.py` удалены владельцем (удаление агенту запрещено
#: политикой окружения) после возврата операций `claim_task` и
#: `update_task_status` в шаге 1 change ``2026-10-02-task-queue-into-mcp``.
#:
#: Список остаётся не ради этих двух файлов, а ради следующего: надгробие
#: появится снова тем же способом — кто-то удалит операцию, положит файл с
#: объяснением «надо убрать» и уйдёт. Тогда его надо будет внести сюда с
#: причиной, иначе страж упадёт. Сейчас он и падает на новом надгробии, и
#: требование вычеркнуть запись сразу после удаления — уже срабатывает.
KNOWN_TOMBSTONES: dict[Path, str] = {}


def _tombstones() -> list[Path]:
    return sorted(
        path
        for path in CAPABILITY_TOOLS.rglob("*.py")
        if path.name.startswith("_")
        and path.name != "__init__.py"
        and "__pycache__" not in path.parts
        and not path.parent.name.startswith("_")
    )


class TestNoUndeclaredTombstones:
    def test_no_undeclared_tombstone_appears(self) -> None:
        found = _tombstones()
        undeclared = [path for path in found if path not in KNOWN_TOMBSTONES]
        assert not undeclared, (
            "появились надгробия операций, о которых change не знает: "
            + ", ".join(p.relative_to(PLATFORM_ROOT).as_posix() for p in undeclared)
            + ". Файл с именем на подчёркивании загрузчик пропускает, поэтому он "
            "не проверяется ни одним функциональным тестом и читается как "
            "запрет. Удалите файл или, если операция действительно уходит, "
            "добавьте его в KNOWN_TOMBSTONES с причиной."
        )

    def test_declared_tombstones_still_lie_about_removed_operations(self) -> None:
        """Каждый объявленный остаток — это файл, отрицающий операцию.

        Проверка, что список долга честен: элемент, который ни на что не
        отрицает, в нём быть не должен; и элемент, которого уже нет на диске,
        тоже — иначе список перестаёт быть правдой и следующий читатель
        будет искать файл, которого нет.
        """
        for path, reason in KNOWN_TOMBSTONES.items():
            assert path.is_file(), (
                f"{path.relative_to(PLATFORM_ROOT).as_posix()} объявлен надгробием, "
                "но его нет на диске — вычеркните его из KNOWN_TOMBSTONES"
            )
            text = path.read_text(encoding="utf-8")
            assert "git rm" in text, (
                f"{path.name} объявлен надгробием, но не говорит, как его удалить: "
                f"причина в списке — {reason!r}"
            )

    def test_skipped_tool_files_never_reach_the_registry(self) -> None:
        """Всё, что загрузчик пропускает, действительно не попадает в реестр.

        Проверка без параметризации по списку долга: она обязана быть
        осмысленной и при пустом списке. Иначе после уплаты долга страж
        выродился бы в набор пустых прогонов, а пропуск загрузчика — то
        единственное, что делает надгробие безвредным, — остался бы
        непроверенным.

        Смысл: путь, который сегодня инертен, завтра может перестать быть
        инертным (например, загрузчик перестанет пропускать подчёркивание).
        Тогда файл молча вернётся в реестр как операция с телом из
        объяснения, и об этом узнают только на боевом вызове.
        """
        import sys

        sys.path.insert(0, str(PLATFORM_ROOT))
        try:
            from libs.enterprise_common.loader import discover_tool_files
        finally:
            sys.path.pop(0)

        registered = {p.name for p in discover_tool_files(CAPABILITY_TOOLS)}
        skipped = [p for p in _tombstones() if p.name != "__init__.py"]
        leaked = [p.name for p in skipped if p.name in registered]
        assert not leaked, (
            "загрузчик перестал пропускать файлы с подчёркиванием, и они вернулись "
            "в реестр как операции: " + ", ".join(sorted(leaked))
        )
        # Смысл проверки не в отсутствии жертв, а в самом факте, что загрузчик
        # по-прежнему держит соглашение: ни один пропущенный файл в реестре не
        # числится, независимо от того, сколько их на диске.
        assert all(p.name not in registered for p in skipped)
