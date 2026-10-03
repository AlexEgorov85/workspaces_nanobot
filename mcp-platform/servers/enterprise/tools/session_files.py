"""Операция ``session_files`` — сказать агенту, где лежат файлы его сессии.

Замысел. Файлы сессии пишут две стороны: платформа (снимки оборота, крупные
результаты, события, вложения) и агент (то, что создала модель). Корни у них
разошлись — агент писал в ``workspace/data_store/cache/sessions``, платформа в
``mcp-platform/.sessions``, — и «где мои файлы» приходилось угадывать по
содержимою. Операция отдаёт агенту каталог его сессии и ``files/`` оттуда, откуда
их заводит платформа, поэтому разными объявлениями они больше не станут.

Почему операция платформенная, а не capability. Файлами сессии владеет
платформа, и страж ``tests/test_tool_execution_boundaries.py`` запрещает
capability касаться ``SessionWorkspace``/``ArtifactStore``: иначе у capability
появился бы второй каталог файлов одной сессии, а в контейнере — второй путь к
ним. Здесь рабочая папка достаётся composition root'у от слоя исполнения, в
контейнер не кладётся вовсе, поэтому ни одна capability до неё не дотянется.

Почему сессия из контекста, а не из аргумента. Идентичность вызова приходит из
``_meta`` и подменять её аргументом нельзя (см. ``execution/context.py``): иначе
агент спросил бы папку чужой сессии и получил бы её. Вызов без идентичности до
этой функции не доходит вовсе — конвейер отвечает ``identity_missing`` раньше, и
каталог при этом не создаётся.

Почему тут нет кода создания каталогов. Раскладку объявлена и создаёт
``SessionWorkspace``; вторая копия ``mkdir`` здесь разошлась бы с ней при первой
же правке подкаталога. Операция только спрашивает, создавать ли каталог, — и
этим вопросом управляет вызывающая сторона, а не она сама.

Почему пути на машине платформы. В ответе операции, отдающей вложение, вместо
пути отдаётся ``session://``; здесь путь и есть результат, и спрятать его
нечем — агент пишет в ``files/`` своим же файловым инструментом. Абсолютным он
делается не здесь: корень приходит уже развёрнутым из ``${NANOBOT_WORKSPACE}``
(``libs/enterprise_common/execution/factory.py`` склеивает его с корнем
платформы), и ``resolve()`` был бы лишним — он разошёлся бы с тем путём, по
которому пишет сама платформа, стоит только появиться симлинку.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from libs.enterprise_common.execution.context import ToolExecutionContext
from libs.enterprise_common.registry import ToolDefinition, build_input_schema, validate_handler
from libs.enterprise_common.session.workspace import SESSION_SUBDIRS, SessionWorkspace

#: Подкаталог файлов агента, по имени из :data:`SESSION_SUBDIRS`. Имя повторено
#: намеренно: в ответе оно нужно строкой, а в раскладке — элементом кортежа, и
#: разойтись они могут только здесь. Держит их страж раскладки
#: (``tests/test_session_workspace.py::test_layout_is_pinned``).
FILES_SUBDIR = "files"


def create_tool(workspace: SessionWorkspace) -> ToolDefinition:
    """Определение операции, отдающей каталог файлов текущей сессии.

    ``workspace`` — рабочая папка сессий из слоя исполнения. В контейнер она не
    кладётся: держать её должен composition root, иначе путь к файлам сессии
    появляется в том месте, откуда его достанет кто угодно.
    """

    def handle_session_files(ctx: ToolExecutionContext, ensure: bool = True) -> str:
        """Каталог файлов этой сессии, его ``files/`` и вся раскладка.

        Аргументы:
            ensure: Создать ли каталог сессии с подкаталогами. ``false`` —
                спросить, где файлы окажутся, ничего не заводя на диске.

        Возвращает ``created`` — создал ли каталог именно этот вызов: при
        ``ensure=false`` он всегда ``false``, иначе по нему не отличить
        «каталога не было» от «каталог уже был».
        """
        directory = workspace.session_dir(ctx.session_id, create=False)
        created = ensure and not directory.is_dir()
        if ensure:
            directory = workspace.session_dir(ctx.session_id, create=True)
        answer: dict[str, Any] = {
            "session_id": ctx.session_id,
            "root": str(directory),
            "files_dir": str(directory / FILES_SUBDIR),
            "layout": list(SESSION_SUBDIRS),
            "created": created,
        }
        return json.dumps(answer, ensure_ascii=False)

    definition = ToolDefinition(
        name="session_files",
        description=(
            "Каталог файлов этой сессии: где лежат файлы, созданные агентом "
            "(files_dir), и как называются подкаталоги сессии (layout). "
            "Вызывается один раз на сессию — ответ кэширует вызывающая "
            "сторона, и повторный вызов при том же корне ничего не меняет."
        ),
        handler=handle_session_files,
        category="session",
        tags=("session", "files"),
    )
    # Те же проверки, что и для операций из каталога: подпись без аннотаций
    # превратилась бы в пустую схему, а сессия в аргументах обошла бы
    # подмену идентичности из _meta.
    validate_handler(definition.handler, name=definition.name)
    return replace(definition, input_schema=build_input_schema(definition.handler))
