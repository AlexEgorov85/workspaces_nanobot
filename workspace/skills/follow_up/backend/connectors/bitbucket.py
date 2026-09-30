"""
Follow Up 2.0 — коннектор BitBucket (репозитории проверок, проект cfg.bitbucket_project).

Факты среды (разведка 13.07.2026, scripts/fu_bitbucket_test.ipynb):
  - Аутентификация: Bearer HTTP access token (анонимно — 401).
  - Слаг репо проверки = km_id напрямую (`99-12347`); display-имена
    вида «Fork0000» — старый стиль, слаг у них голый номер.
  - Web-путь /projects/.../raw/ НЕ принимает Bearer — файлы читаем
    ТОЛЬКО через REST: /rest/api/1.0/.../raw/{path} (проверено).
  - README генерируется шаблоном, бывает с плейсхолдерами; таблица
    «Описание работы с данными» есть не всегда.

Индексация ленивая: первый запрос по КМ тянет репо и кладёт разбор
в кэш (GP t_fu_repo_index / локальный JSON), дальше все читают кэш.
Актуальность — по head_commit.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional

import requests

from backend.config import get_settings

logger = logging.getLogger(__name__)

try:
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    pass

_TIMEOUT = 15

# Технические файлы/каталоги — не показываем аудитору (подтверждено заказчиком)
_EXCLUDE_RE = re.compile(
    r"^Documentation_CK_SPK/|/\.img/|^\.|pylintrc|tox\.ini|"
    r"init_repository\.py|Requirements.*\.txt|\.gitignore",
    re.IGNORECASE)

_KIND_BY_EXT = {
    ".sql": "sql", ".py": "py", ".ipynb": "ipynb", ".sh": "sh",
    ".md": "md", ".json": "cfg", ".yml": "cfg", ".yaml": "cfg", ".ini": "cfg",
}


def _cfg():
    return get_settings()


def _headers() -> Dict[str, str]:
    token = _cfg().bitbucket_token
    return {"Authorization": f"Bearer {token}"} if token else {}


def available() -> bool:
    return bool(_cfg().bitbucket_token)


def _rest(path: str, params: Optional[Dict] = None) -> requests.Response:
    cfg = _cfg()
    url = (f"{cfg.bitbucket_base_url}/rest/api/1.0/projects/"
           f"{cfg.bitbucket_project}{path}")
    return requests.get(url, params=params or {}, headers=_headers(),
                        verify=False, timeout=_TIMEOUT)


def file_url(repo_slug: str, path: str, branch: str = "master") -> str:
    """Кликабельная ссылка в web-UI BitBucket."""
    cfg = _cfg()
    return (f"{cfg.bitbucket_base_url}/projects/{cfg.bitbucket_project}"
            f"/repos/{repo_slug}/browse/{path}?at=refs%2Fheads%2F{branch}")


def resolve_repo(km_id: str) -> Optional[Dict]:
    """Слаг = km_id; фолбэк Fork{km_id}. Возвращает {slug, exists}."""
    for slug in (km_id, f"Fork{km_id}"):
        try:
            r = _rest(f"/repos/{slug}")
            if r.status_code == 200:
                return {"slug": slug, "meta": r.json()}
            if r.status_code == 401:
                logger.warning("[BitBucket] 401 — токен невалиден/просрочен")
                return None
        except requests.RequestException as e:
            logger.warning(f"[BitBucket] resolve {slug}: {e}")
            return None
    return None


def get_head_commit(slug: str, branch: str = "master") -> Optional[str]:
    try:
        r = _rest(f"/repos/{slug}/commits", {"limit": 1, "until": branch})
        if r.status_code == 200:
            vals = r.json().get("values", [])
            return vals[0]["id"] if vals else None
    except requests.RequestException:
        pass
    return None


def get_default_branch(slug: str) -> str:
    for ep in (f"/repos/{slug}/default-branch", f"/repos/{slug}/branches/default"):
        try:
            r = _rest(ep)
            if r.status_code == 200:
                return r.json().get("displayId") or "master"
        except requests.RequestException:
            pass
    return "master"


def get_readme(slug: str, branch: str) -> Optional[str]:
    """README через REST raw (web-путь не принимает Bearer!) с фолбэком browse."""
    try:
        r = _rest(f"/repos/{slug}/raw/README.md",
                  {"at": f"refs/heads/{branch}"})
        if r.status_code == 200 and r.text.strip():
            return r.text
        # фолбэк: REST browse (JSON построчно)
        lines, start = [], 0
        for _ in range(20):
            r2 = _rest(f"/repos/{slug}/browse/README.md",
                       {"at": f"refs/heads/{branch}", "start": start, "limit": 500})
            if r2.status_code != 200:
                return None
            data = r2.json()
            lines += [l.get("text", "") for l in data.get("lines", [])]
            if data.get("isLastPage", True):
                break
            start = data.get("nextPageStart", start + 500)
        text = "\n".join(lines)
        return text if text.strip() else None
    except requests.RequestException as e:
        logger.warning(f"[BitBucket] README {slug}: {e}")
        return None


def get_file_content(slug: str, path: str, branch: Optional[str] = None,
                     max_chars: int = 60_000) -> Optional[str]:
    """Содержимое файла через REST raw (web-путь не принимает Bearer)."""
    try:
        br = branch or get_default_branch(slug)
        r = _rest(f"/repos/{slug}/raw/{path}", {"at": f"refs/heads/{br}"})
        if r.status_code == 200:
            return r.text[:max_chars]
        # фолбэк: browse построчно
        lines, start = [], 0
        for _ in range(40):
            r2 = _rest(f"/repos/{slug}/browse/{path}",
                       {"at": f"refs/heads/{br}", "start": start, "limit": 500})
            if r2.status_code != 200:
                return None
            data = r2.json()
            lines += [l.get("text", "") for l in data.get("lines", [])]
            if data.get("isLastPage", True) or sum(map(len, lines)) > max_chars:
                break
            start = data.get("nextPageStart", start + 500)
        text = "\n".join(lines)
        return text[:max_chars] if text.strip() else None
    except requests.RequestException as e:
        logger.warning(f"[BitBucket] файл {slug}/{path}: {e}")
        return None


def get_files(slug: str, branch: str) -> List[str]:
    """Полный листинг путей файлов."""
    files, start = [], 0
    try:
        for _ in range(10):
            r = _rest(f"/repos/{slug}/files",
                      {"at": f"refs/heads/{branch}", "limit": 1000, "start": start})
            if r.status_code != 200:
                break
            data = r.json()
            files += data.get("values", [])
            if data.get("isLastPage", True):
                break
            start = data.get("nextPageStart", 0)
    except requests.RequestException as e:
        logger.warning(f"[BitBucket] files {slug}: {e}")
    return files


def useful_files(all_files: List[str]) -> List[Dict]:
    """Фильтр технического мусора + классификация по типу."""
    out = []
    for f in all_files:
        if _EXCLUDE_RE.search(f):
            continue
        ext = "." + f.rsplit(".", 1)[-1].lower() if "." in f.rsplit("/", 1)[-1] else ""
        kind = _KIND_BY_EXT.get(ext)
        if kind is None and ext not in ("",):
            continue  # png и прочее — мимо
        if kind is None:
            kind = "other"
        out.append({"file_path": f, "file_kind": kind})
    return out


async def parse_readme_llm(readme: str, slug: str, check_id: str,
                           model: Optional[str] = None) -> Optional[Dict]:
    """LLM-структурирование README: устойчиво к кривым таблицам и
    вольному формату (regex-парсер здесь ломался бы на каждом втором репо)."""
    from backend.llm.client import generate_async
    from backend.llm.prompts.execution_control import (
        REPO_README_SYSTEM, REPO_README_USER_TEMPLATE)
    try:
        raw = await generate_async(
            [{"role": "system", "content": REPO_README_SYSTEM},
             {"role": "user", "content": REPO_README_USER_TEMPLATE.format(
                 repo_slug=slug, check_id=check_id,
                 readme_text=readme[:12000])}],
            model=model, max_tokens=3500, temperature=0.0)
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        logger.warning(f"[BitBucket] LLM-парсинг README {slug}: {e}")
        return None


async def index_repo(km_id: str, model: Optional[str] = None) -> Optional[Dict]:
    """
    Полная ленивая индексация репозитория одного КМ.

    Возвращает {index: {...}, files: [...], parsed: {...}} или None,
    если репо не найдено/недоступно.
    """
    check_id = f"КМ-{km_id}"
    resolved = resolve_repo(km_id)
    if not resolved:
        return None
    slug = resolved["slug"]
    branch = get_default_branch(slug)
    head = get_head_commit(slug, branch)

    readme = get_readme(slug, branch)
    listing = useful_files(get_files(slug, branch))
    for f in listing:
        f["repo_slug"] = slug
        f["file_url"] = file_url(slug, f["file_path"], branch)

    parsed = None
    if readme:
        parsed = await parse_readme_llm(readme, slug, check_id, model)

    # Уровень качества (деградация из архитектуры §13.7-bis)
    if parsed and parsed.get("files_mapping"):
        tier = "A"
        # обогащаем листинг маппингом из README
        by_path = {f["file_path"]: f for f in listing}
        for m in parsed["files_mapping"]:
            fpath = (m.get("file") or "").strip()
            target = by_path.get(fpath)
            if target is None:  # README может указывать имя без пути
                cand = [f for f in listing
                        if f["file_path"].endswith("/" + fpath) or
                        f["file_path"] == fpath]
                target = cand[0] if cand else None
            if target:
                target.update({
                    "punkt_akta": m.get("punkt"),
                    "authors": m.get("authors"),
                    "descr": m.get("descr"),
                    "data_sources": m.get("sources"),
                    "tech": m.get("tech"),
                })
    elif parsed and parsed.get("readme_quality") in ("full", "partial"):
        tier = "B"
    else:
        tier = "C"

    cfg = _cfg()
    return {
        "index": {
            "check_id": check_id,
            "repo_slug": slug,
            "repo_url": (f"{cfg.bitbucket_base_url}/projects/"
                         f"{cfg.bitbucket_project}/repos/{slug}/browse"),
            "readme_ok": bool(parsed and parsed.get("readme_quality") != "template"),
            "quality_tier": tier,
            "head_commit": head,
        },
        "files": listing,
        "parsed": parsed or {},
    }
