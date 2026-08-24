"""Кастомные tool'ы проекта (auto-discover).

Модули в этой директории сканируются ``RuntimePatcher.patch_project_tools``
после старта ``AgentLoop``. Каждый модуль может экспортировать tool-классы
— наследники ``nanobot.agent.tools.base.Tool``.

Конвенции (см. ``workspace/tools/example.py`` как reference и
``nanobot/agent/tools/filesystem.py::_FsTool`` как оригинал из nanobot):

* ``config_key: ClassVar[str] = "<name>"`` → секция
  ``tools.<name>.*`` в ``config.json`` (или ``gateway.<name>.*``, если
  так сложилась история — см. ``compact_context``);
* ``config_cls()`` возвращает pydantic-модель секции;
* ``enabled(ctx)`` и ``create(ctx)`` читают настройки через
  ``ctx._settings_ref`` (полный ``Settings``), а НЕ через
  ``ctx.config`` (это ``ToolsConfig`` pydantic-схемы nanobot, которая
  знает только встроенные подсекции и отбрасывает неизвестные —
  отсюда падение ``AttributeError: 'ToolsConfig' object has no
  attribute 'example'``);
* ``name`` / ``description`` / ``parameters`` — стандартные;
* ``async execute(...)`` возвращает ``str`` или ``ToolResult.error(...)``.

Пример см. ``workspace/tools/example.py``.
"""
