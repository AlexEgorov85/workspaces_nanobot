"""Навык ``follow_up`` — внешний MCP-процесс (см. openspec/changes/add-follow-up-skill).

Что здесь закреплено:

* SKILL.md разбирается фронтматтером и виден агенту всегда (``always: true``);
* в ``config.json`` статичный блок ``tools.mcpServers.follow_up`` — без путей
  конкретной машины, команда — лаунчер внутри папки навыка;
* навык объявлен в единственном реестре ``project.json::skills``;
* лаунчер — только стандартная библиотека, без настройки выходит с кодом 3
  и **ничего не пишет в stdout** (это канал JSON-RPC gateway'я).
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


def _strip_jsonc(text: str) -> str:
    no_line = re.sub(r"(?<!:)//.*$", "", text, flags=re.MULTILINE)
    return re.sub(r"/\*.*?\*/", "", no_line, flags=re.DOTALL)


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

def test_config_json_block_is_static():
    cfg = json.loads((REPO_ROOT / "config.json").read_text(encoding="utf-8"))
    block = cfg["tools"]["mcpServers"]["follow_up"]
    assert block == {"type": "stdio", "command": LAUNCHER_REL,
                     "args": [], "tool_timeout": 120}
    # Ни абсолютных путей, ни ${VAR}: неизвестная переменная валит
    # resolve_config_env_vars у nanobot-ai на старте.
    assert "${" not in json.dumps(block) and not Path(block["command"]).is_absolute()


def test_project_json_declares_the_skill_once():
    data = json.loads(_strip_jsonc((REPO_ROOT / "project.json").read_text(encoding="utf-8")))
    assert data["skills"]["follow_up"] == {"enabled": True}


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


def test_check_explains_what_is_missing(launcher_copy):
    r = subprocess.run([sys.executable, str(launcher_copy), "--check"],
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
    r = subprocess.run([sys.executable, str(launcher_copy), "--check"],
                       capture_output=True, text=True, env=_clean_env())
    info = json.loads(r.stdout)
    assert r.returncode == 0, info
    assert info["root"] == str(root) and info["python"] == sys.executable
    assert info["models_device"] == "cpu"
