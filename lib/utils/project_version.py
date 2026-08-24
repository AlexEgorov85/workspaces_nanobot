"""project_version — версия текущего проекта.

Версия проекта (в отличие от версии библиотеки nanobot ``__version__``)
канонически хранится в ``project.json`` в секции ``project.version``
(актуальный релизный тег ``vX.Y.Z`` без префикса ``v``). Ключ закоммичен
в ``master`` и распространяется во все релизные ветки, поэтому показывает
актуальный релиз независимо от ветки.

Git-теги и CHANGELOG для этого ненадёжны: релизные ветки ``release/vX.Y``
ответвляются от ``master`` и не мержатся обратно, поэтому и ``git describe``,
и первый релизный блок ``CHANGELOG.md`` на ``master`` отстают от актуального тега.

Fallback при отсутствии ключа — ``git describe --tags``, затем ``"dev"``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def project_version(root: str | Path | None = None) -> str:
    """Версия проекта: ``project.version`` из ``project.json``, иначе git-тег,
    иначе ``"dev"``."""
    cfg_version = _config_version(root)
    if cfg_version:
        return cfg_version
    return _git_version(root or _PROJECT_ROOT)


def _config_version(root: str | Path | None) -> str | None:
    try:
        import json as _json

        from config import _strip_jsonc_comments

        cfg_file = Path(root) / "project.json" if root else _PROJECT_ROOT / "project.json"
        data = _json.loads(_strip_jsonc_comments(cfg_file.read_text(encoding="utf-8")))
        project = data.get("project") if isinstance(data, dict) else None
        ver = str(project.get("version", "")).strip() if isinstance(project, dict) else ""
        return ver or None
    except Exception:
        return None


def _git_version(repo: str | Path) -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=3,
        )
        tag = out.stdout.strip()
        return tag.lstrip("v") if tag else "dev"
    except Exception:
        return "dev"
