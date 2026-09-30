"""Навык ``follow_up`` (D5) — отдельный MCP-процесс, описание в ``docs/D5.md``.

Что здесь закреплено:

* SKILL.md разбирается фронтматтером и виден агенту всегда (``always: true``);
* инструменты агента — ``workspace/tools/follow_up.py``: загрузчик проекта
  регистрирует ровно семь ``mcp_follow_up_*`` со схемами из ``tools.json``,
  а мост действительно поднимает MCP-сервер, передаёт вызовы и переживает
  его падение; записи ``tools.mcpServers.follow_up`` в ``config.json`` нет;
* код сервера лежит в папке навыка, и лаунчер находит его без настройки;
* лаунчер — только стандартная библиотека; если кода нет, выходит с кодом 3
  и **ничего не пишет в stdout** (это канал JSON-RPC gateway'я);
* код навыка изолирован: не импортирует проект и не импортируется им,
  его ``requirements.txt`` не спорит с корневым, линтер проекта его не
  проверяет (сопровождается в репозитории Follow Up).

Поведение самого навыка здесь не тестируется — у него свой набор тестов.
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.conftest import REPO_ROOT

SKILL_DIR = REPO_ROOT / "workspace" / "skills" / "follow_up"
LAUNCHER_REL = "workspace/skills/follow_up/scripts/follow_up_mcp"
LAUNCHER = REPO_ROOT / LAUNCHER_REL


def _frontmatter() -> dict:
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    assert m, "SKILL.md без фронтматтера"
    return yaml.safe_load(m.group(1))


# ── SKILL.md ───────────────────────────────────────────────────────

def test_skill_md_frontmatter_matches_the_directory_name():
    meta = _frontmatter()
    assert meta["name"] == "follow_up"
    assert meta["description"]
    nanobot_meta = meta["metadata"]
    if isinstance(nanobot_meta, str):
        nanobot_meta = json.loads(nanobot_meta)
    assert nanobot_meta["nanobot"]["always"] is True


def test_skill_md_names_only_tools_the_server_exposes():
    """Имена в SKILL.md — ``mcp_<сервер>_<инструмент>``; набор фиксирован
    контрактом навыка. Лишнее имя — обещание агенту, которое не сдержим."""
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    named = set(re.findall(r"mcp_follow_up_([a-z_]+)", text))
    assert named == {"ask", "hypotheses", "deviations", "status",
                     "card_start", "card_status", "forget"}


def test_skill_md_draws_the_line_with_audit_analyzer():
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "audit_analyzer" in text


# ── реестры ────────────────────────────────────────────────────────

def test_config_json_has_no_mcp_block():
    """Сервер держат инструменты агента. Запись в config.json подняла бы
    второй экземпляр там, где фреймворк её читает, — над той же базой."""
    cfg = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    assert "follow_up" not in (cfg.get("tools", {}).get("mcpServers") or {})


# ── лаунчер ────────────────────────────────────────────────────────

def test_launcher_uses_only_the_standard_library():
    tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)


def test_launcher_has_a_windows_wrapper():
    assert (LAUNCHER.parent / "follow_up_mcp.cmd").read_bytes().startswith(b"@echo off")


@pytest.fixture
def launcher_copy(tmp_path: Path) -> Path:
    """Копия лаунчера в чистом дереве: на машине разработчика рядом с
    настоящим может лежать follow_up.env.local."""
    skill = tmp_path / "nb" / "workspace" / "skills" / "follow_up" / "scripts"
    skill.mkdir(parents=True)
    dst = skill / "follow_up_mcp"
    dst.write_bytes(LAUNCHER.read_bytes())
    return dst


def _clean_env() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith("FOLLOW_UP_") and k != "MODELS_DEVICE"}


def test_unconfigured_launcher_exits_3_and_keeps_stdout_clean(launcher_copy):
    r = subprocess.run([sys.executable, str(launcher_copy)], capture_output=True,
                       text=True, env=_clean_env())
    assert r.returncode == 3
    assert r.stdout == ""
    assert "follow_up.env.local" in r.stderr


def test_where_explains_what_is_missing(launcher_copy):
    r = subprocess.run([sys.executable, str(launcher_copy), "--where"],
                       capture_output=True, text=True, env=_clean_env())
    info = json.loads(r.stdout)
    assert r.returncode == 3 and info["ok"] is False and info["problems"]
    # NANOBOT_HOME — корень нанобота, вычислен от расположения лаунчера.
    assert info["nanobot_home"] == str(launcher_copy.parents[4])


def test_local_file_configures_the_launcher(launcher_copy, tmp_path):
    root = tmp_path / "follow_up"
    (root / "backend" / "skill").mkdir(parents=True)
    (root / "backend" / "skill" / "mcp_server.py").write_text("", encoding="utf-8")
    (launcher_copy.parent.parent / "follow_up.env.local").write_text(
        f"FOLLOW_UP_ROOT={root}\nFOLLOW_UP_PYTHON={sys.executable}\n", encoding="utf-8")
    r = subprocess.run([sys.executable, str(launcher_copy), "--where"],
                       capture_output=True, text=True, env=_clean_env())
    info = json.loads(r.stdout)
    assert r.returncode == 0, info
    assert info["mode"] == "отдельный клон"
    assert info["root"] == str(root) and info["python"] == sys.executable
    assert info["models_device"] == "cpu"


# ── код навыка в папке навыка ──────────────────────────────────────

def test_bundled_server_is_found_without_configuration():
    """Ни follow_up.env.local, ни переменных: код рядом — лаунчер готов."""
    assert (SKILL_DIR / "backend" / "skill" / "mcp_server.py").is_file()
    r = subprocess.run([sys.executable, str(LAUNCHER), "--where"],
                       capture_output=True, text=True, env=_clean_env())
    info = json.loads(r.stdout)
    if info["local_file_exists"]:
        pytest.skip("на этой машине есть follow_up.env.local — он важнее")
    assert r.returncode == 0, info
    assert info["mode"] == "встроенный код"
    assert Path(info["root"]) == SKILL_DIR.resolve()
    assert info["python"]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module)
    return out


def test_skill_code_does_not_import_the_project():
    project = ("lib", "workspace", "gateway", "config", "cli_agent", "streamlit_app")
    offenders = {
        str(p.relative_to(REPO_ROOT)): sorted(m for m in _imports(p)
                                              if m.split(".")[0] in project)
        for p in (SKILL_DIR / "backend").rglob("*.py")
    }
    assert not {k: v for k, v in offenders.items() if v}


def test_project_does_not_import_the_skill_code():
    offenders = []
    for d in ("lib", "workspace/tools", "workspace/utils"):
        for p in (REPO_ROOT / d).rglob("*.py"):
            if any(m.split(".")[0] == "backend" or "skills.follow_up" in m
                   for m in _imports(p)):
                offenders.append(str(p.relative_to(REPO_ROOT)))
    assert not offenders


def _requirement_names(path: Path) -> set[str]:
    names = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and not line.startswith("-"):
            names.add(re.split(r"[\[<>=!~ ;]", line, maxsplit=1)[0].lower().replace("_", "-"))
    return names


def test_skill_requirements_do_not_repin_root_packages():
    """Версии общих пакетов задаёт корневой requirements.txt, а не навык."""
    shared = (_requirement_names(SKILL_DIR / "requirements.txt")
              & _requirement_names(REPO_ROOT / "requirements.txt"))
    assert not shared


def test_project_linter_leaves_the_skill_code_to_its_repository():
    text = (SKILL_DIR / "ruff.toml").read_text(encoding="utf-8")
    assert 'extend = "../../../pyproject.toml"' in text
    assert re.search(r'extend-exclude\s*=\s*\["backend"\]', text)
    assert "scripts/follow_up_mcp" in text          # лаунчер без .py — тоже под линтером


def _git_ignored(paths: list[str]) -> set[str]:
    """Что из ``paths`` git не возьмёт в коммит — по правилам всех .gitignore."""
    r = subprocess.run(["git", "-C", str(REPO_ROOT), "check-ignore", "--no-index", "--stdin"],
                       input="\n".join(paths), capture_output=True, text=True)
    if r.returncode not in (0, 1):
        pytest.skip(f"git check-ignore недоступен: {r.stderr.strip()[:200]}")
    return set(r.stdout.split())


_RUNTIME = {"data", "logs", "models", ".ruff_cache", "__pycache__"}


def test_skill_runtime_data_is_ignored():
    rel = "workspace/skills/follow_up"
    runtime = [f"{rel}/data/followup.db", f"{rel}/logs/server.log",
               f"{rel}/models/m/config.json", f"{rel}/follow_up.env.local", f"{rel}/.env"]
    assert _git_ignored(runtime) == set(runtime)


def test_every_skill_file_reaches_git():
    """Корневой .gitignore режет каталоги по имени (``memory/`` — для
    workspace/memory), а в backend/ есть пакет core/memory: без защиты в
    .gitignore навыка он не попал бы в коммит, и навык приехал бы сломанным."""
    files = [p.relative_to(REPO_ROOT).as_posix() for p in SKILL_DIR.rglob("*")
             if p.is_file() and not _RUNTIME & set(p.relative_to(SKILL_DIR).parts)
             and p.name not in {"follow_up.env.local", ".env"}]
    assert any(f.endswith("backend/core/memory/state.py") for f in files)
    assert not _git_ignored(files)


# ── инструменты агента: workspace/tools/follow_up.py ───────────────

TOOL_MODULE = REPO_ROOT / "workspace" / "tools" / "follow_up.py"


def _tool_module():
    """Модуль так, как его грузит загрузчик проекта (``workspace.tools.<имя>``)."""
    import importlib.util

    name = "workspace.tools.follow_up"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL_MODULE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _server_specs() -> list[dict]:
    return json.loads((SKILL_DIR / "tools.json").read_text(encoding="utf-8"))


def test_agent_tools_mirror_the_server_and_skill_md():
    from nanobot.agent.tools.base import Tool

    mod = _tool_module()
    concrete = {c for c in vars(mod).values()
                if isinstance(c, type) and issubclass(c, Tool)
                and not getattr(c, "__abstractmethods__", None)}
    by_name = {c().name: c() for c in concrete}
    specs = {"mcp_follow_up_" + s["name"]: s for s in _server_specs()}
    assert set(by_name) == set(specs)
    text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert set(by_name) == {"mcp_follow_up_" + n for n in re.findall(r"mcp_follow_up_([a-z_]+)", text)}
    for name, tool in by_name.items():
        assert tool.parameters == specs[name]["schema"]
        assert tool.description == specs[name]["description"]
    assert getattr(mod.FollowUpTool, "__abstractmethods__", None)   # база не регистрируется


def test_project_loader_registers_the_seven_tools(monkeypatch):
    """Настоящий загрузчик проекта; сервер при этом не поднимается."""
    from types import SimpleNamespace

    from nanobot.agent.tools.registry import ToolRegistry

    from lib.services.project_tool_loader import register_project_tools

    mod = _tool_module()
    warmed = []
    monkeypatch.setattr(mod._Bridge, "warm_up", lambda self: warmed.append(1))
    agent = SimpleNamespace(tools=ToolRegistry(), workspace=REPO_ROOT / "workspace")
    result = register_project_tools(agent, REPO_ROOT / "workspace")
    ours = {n for n in agent.tools.tool_names if n.startswith("mcp_follow_up_")}
    assert ours == {"mcp_follow_up_" + s["name"] for s in _server_specs()}, result.detail
    # Тесты проекта (ApplicationContext) не должны поднимать сервер навыка.
    assert not warmed


def test_warm_up_only_in_gateway_and_once(monkeypatch):
    from types import SimpleNamespace

    mod = _tool_module()
    monkeypatch.delenv("FOLLOW_UP_WARM_UP", raising=False)
    assert mod.should_warm_up() is False                 # под pytest — никогда
    assert mod.should_warm_up(["/srv/nb/gateway.py", "--profile=prod"], {}) is True
    assert mod.should_warm_up(["gateway.py", "--smoke"], {}) is False
    assert mod.should_warm_up(["cli_agent.py"], {}) is False
    monkeypatch.setenv("FOLLOW_UP_WARM_UP", "1")
    assert mod.should_warm_up() is True
    b = mod._Bridge(sys.executable, ["-c", "pass"], REPO_ROOT)
    submitted = []
    monkeypatch.setattr(b, "submit", lambda coro: (coro.close(), submitted.append(1),
                                                   SimpleNamespace(add_done_callback=lambda f: None))[2])
    monkeypatch.setattr(mod, "_BRIDGE", b)
    for cls in {c for c in vars(mod).values() if isinstance(c, type)
                and issubclass(c, mod.FollowUpTool) and c is not mod.FollowUpTool}:
        cls.create(SimpleNamespace())
    assert submitted == [1]                              # семь инструментов — один подъём


def test_agent_tools_can_be_switched_off():
    from types import SimpleNamespace

    mod = _tool_module()
    ctx = SimpleNamespace(_settings_ref=SimpleNamespace(
        tools=SimpleNamespace(follow_up={"enable": False})))
    assert mod.FollowUpAskTool.enabled(ctx) is False
    assert mod.FollowUpAskTool.enabled(SimpleNamespace()) is True


_STUB_SERVER = """
import json, os, sys
import anyio
import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

server = Server("stub")

if os.environ.get("STUB_PIDFILE"):
    with open(os.environ["STUB_PIDFILE"], "a") as f:
        f.write(str(os.getpid()) + chr(10))

@server.list_tools()
async def list_tools():
    return [types.Tool(name=n, description=n, inputSchema={"type": "object", "properties": {}})
            for n in ("echo", "die", "slow")]

@server.call_tool()
async def call_tool(name, arguments):
    if name == "die":
        os._exit(3)
    if name == "slow":
        await anyio.sleep(float(arguments.get("sec", 1)))
    return [types.TextContent(type="text", text=json.dumps(
        {"args": arguments, "pid": os.getpid(), "home": os.environ.get("STUB_MARK")}))]

async def main():
    async with stdio_server() as (r, w):
        await server.run(r, w, server.create_initialization_options())

anyio.run(main)
"""


@pytest.fixture
def stub_bridge(tmp_path, monkeypatch):
    mod = _tool_module()
    stub = tmp_path / "stub_server.py"
    stub.write_text(_STUB_SERVER, encoding="utf-8")
    monkeypatch.setenv("STUB_MARK", "из окружения gateway")
    b = mod._Bridge(sys.executable, [str(stub)], tmp_path, tmp_path / "server.log")
    monkeypatch.setattr(mod, "_BRIDGE", b)
    yield mod, b
    b.close()


def test_bridge_talks_to_a_real_mcp_server(stub_bridge):
    import asyncio

    mod, b = stub_bridge
    is_error, text = asyncio.run(b.call("echo", {"q": "акт"}, 60))
    reply = json.loads(text)
    assert not is_error and reply["args"] == {"q": "акт"}
    assert reply["home"] == "из окружения gateway"      # окружение доходит до сервера
    _, again = asyncio.run(b.call("echo", {}, 60))      # тот же процесс, другой цикл
    assert json.loads(again)["pid"] == reply["pid"]


def test_bridge_restarts_a_server_that_died(stub_bridge):
    import asyncio

    mod, b = stub_bridge
    first = json.loads(asyncio.run(b.call("echo", {}, 60))[1])["pid"]
    with pytest.raises(Exception):  # noqa: B017 — любой отказ транспорта
        asyncio.run(b.call("die", {}, 60))
    second = json.loads(asyncio.run(b.call("echo", {}, 60))[1])["pid"]
    assert second != first


def test_unavailable_server_is_a_clear_tool_error(tmp_path, monkeypatch):
    import asyncio

    from nanobot.agent.tools.base import ToolResult

    mod = _tool_module()
    b = mod._Bridge(sys.executable, [str(tmp_path / "missing.py")], tmp_path,
                    tmp_path / "server.log")
    monkeypatch.setattr(mod, "_BRIDGE", b)
    monkeypatch.setattr(mod, "START_TIMEOUT_SEC", 30)
    try:
        out = asyncio.run(mod.FollowUpStatusTool().execute())
    finally:
        b.close()
    assert isinstance(out, ToolResult) and "недоступен" in str(out)


def test_unknown_arguments_are_not_forwarded(monkeypatch):
    import asyncio

    mod = _tool_module()
    seen = {}

    class FakeBridge:
        async def call(self, name, args, timeout):
            seen.update(name=name, args=args)
            return False, "ok"

    monkeypatch.setattr(mod, "bridge", lambda: FakeBridge())
    out = asyncio.run(mod.FollowUpAskTool().execute(question="что в акте", _internal=1))
    assert out == "ok" and seen == {"name": "ask", "args": {"question": "что в акте"}}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # Зомби (завершён, но не пожат) тоже считается мёртвым.
    if sys.platform.startswith("linux"):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"
        except OSError:
            return False
    r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
    return bool(r.stdout.strip()) and not r.stdout.strip().startswith("Z")


def _wait_dead(pid: int, sec: float = 10) -> bool:
    import time
    end = time.time() + sec
    while time.time() < end:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)
    return False


def test_hung_start_is_cancelled_and_the_process_killed(tmp_path, monkeypatch):
    """Сервер, который не отвечает на initialize, не должен вешать мост навсегда."""
    import asyncio

    mod = _tool_module()
    pidfile = tmp_path / "pid"
    hung = tmp_path / "hung.py"
    hung.write_text(f"import os, time\nopen({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
                    "while True:\n    time.sleep(1)\n", encoding="utf-8")
    monkeypatch.setattr(mod, "START_TIMEOUT_SEC", 2)
    b = mod._Bridge(sys.executable, [str(hung)], tmp_path, tmp_path / "server.log")
    try:
        with pytest.raises(RuntimeError, match="не поднялся"):
            asyncio.run(b.call("echo", {}, 30))
        assert b._gen is not None and b._gen.task.done()
        assert _wait_dead(int(pidfile.read_text()))       # процесс действительно погашен
    finally:
        b.close()


def test_first_call_after_idle_death_succeeds(stub_bridge):
    """Сервер умер между вопросами — первый же вопрос поднимает новый и отвечает."""
    import asyncio
    import signal

    mod, b = stub_bridge
    first = json.loads(asyncio.run(b.call("echo", {}, 60))[1])["pid"]
    os.kill(first, signal.SIGKILL)
    assert _wait_dead(first)
    is_error, text = asyncio.run(b.call("echo", {}, 60))
    assert not is_error and json.loads(text)["pid"] != first


def test_death_under_concurrent_calls_neither_hangs_nor_kills_the_replacement(stub_bridge):
    """Смерть сервера при трёх вызовах в полёте: они быстро получают отказ, а
    поздняя очистка старого запуска не гасит уже поднятый новый."""
    import asyncio
    import time

    mod, b = stub_bridge

    async def scenario():
        slow = [asyncio.create_task(b.call("slow", {"sec": 3}, 60)) for _ in range(3)]
        await asyncio.sleep(0.5)
        die = asyncio.create_task(b.call("die", {}, 60))
        await asyncio.sleep(1.0)
        echo = await b.call("echo", {}, 60)              # уже новый сервер
        later = await b.call("slow", {"sec": 6}, 60)     # живёт дольше пинга в 5 с
        old = await asyncio.gather(*slow, die, return_exceptions=True)
        return echo, later, old

    t0 = time.time()
    echo, later, old = asyncio.run(scenario())
    assert time.time() - t0 < 30
    assert all(isinstance(r, Exception) and not isinstance(r, TimeoutError) for r in old)
    new_pid = json.loads(echo[1])["pid"]
    assert json.loads(later[1])["pid"] == new_pid        # замену никто не погасил


def test_start_failure_reason_reaches_the_error_and_the_log_is_appended(tmp_path, monkeypatch):
    import asyncio

    mod = _tool_module()
    broken = tmp_path / "broken.py"
    broken.write_text("import sys\nsys.stderr.write('причина: кода навыка нет\\n')\n"
                      "sys.exit(3)\n", encoding="utf-8")
    log = tmp_path / "logs" / "server.log"
    b = mod._Bridge(sys.executable, [str(broken)], tmp_path, log)
    try:
        for _ in range(2):
            with pytest.raises(RuntimeError, match="причина: кода навыка нет"):
                asyncio.run(b.call("echo", {}, 30))
    finally:
        b.close()
    assert log.read_text(encoding="utf-8").count("старт сервера навыка") == 2


def test_unwritable_log_does_not_stop_the_server(tmp_path):
    import asyncio

    mod = _tool_module()
    stub = tmp_path / "stub_server.py"
    stub.write_text(_STUB_SERVER, encoding="utf-8")
    (tmp_path / "logs").write_text("это файл, а не каталог", encoding="utf-8")
    b = mod._Bridge(sys.executable, [str(stub)], tmp_path, tmp_path / "logs" / "server.log")
    try:
        is_error, text = asyncio.run(b.call("echo", {}, 60))
    finally:
        b.close()
    assert not is_error and json.loads(text)["pid"]


def test_session_key_and_user_come_from_the_request_context():
    """Модель свой ключ сессии не видит; придуманный ею совпал бы у двух
    аудиторов, и память диалога стала бы общей."""
    from nanobot.agent.tools.context import RequestContext, request_context

    mod = _tool_module()
    tool = mod.FollowUpAskTool()
    with request_context(RequestContext(channel="postgres", chat_id="101",
                                        session_key="postgres:101", sender_id="alice")):
        args = tool._arguments({"question": "а по второму пункту?", "session_key": "КМ-дата",
                                "user_id": "Иванов"})
    assert args == {"question": "а по второму пункту?", "session_key": "postgres:101",
                    "user_id": "alice"}
    # Вне хода (тесты, отладка) — что передала модель.
    assert tool._arguments({"question": "q", "session_key": "k"}) == {
        "question": "q", "session_key": "k"}
