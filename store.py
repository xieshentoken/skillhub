"""中央库 (Canonical Store) — 管理 ~/.skillhub/store 下的唯一权威副本。

skill_id 命名: <name>--<md5前8>  (同名不同内容不冲突)
内容目录:     store/<skill_id>/        (skill 全部文件, 含 SKILL.md)
索引:         index.json               (skill_id -> manifest)
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import INDEX_FILE, MANIFEST_NAME, STORE_DIR

# 导入时排除的目录(大/易变/无关)
EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", ".cache"}


def ensure_home() -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)


def load_index() -> Dict[str, dict]:
    ensure_home()
    if not INDEX_FILE.exists():
        return {}
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_index(index: Dict[str, dict]) -> None:
    ensure_home()
    tmp = INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_FILE)


def skill_id_for(name: str, md5: str) -> str:
    return f"{name}--{md5[:8]}"


def _copytree(src: Path, dst: Path) -> int:
    """复制目录, 跳过排除项, 返回复制文件数。"""
    count = 0
    for item in src.rglob("*"):
        if item.is_dir():
            continue
        if any(part in EXCLUDED_DIRS for part in item.relative_to(src).parts):
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _merge_source(index: dict, sid: str, agent: str, path: str) -> None:
    """把第二个来源 agent 合并进既有 manifest。"""
    index[sid].setdefault("sources", [])
    if agent not in index[sid].get("agents", []):
        index[sid]["agents"].append(agent)
    if path not in [s.get("path") for s in index[sid]["sources"]]:
        index[sid]["sources"].append({"agent": agent, "path": path})


def import_skill(agent: str, record: dict, apply: bool = False) -> Optional[str]:
    """把 agent 的一个 skill 导入中央库, 返回 skill_id。

    apply=False 时只计算并返回 skill_id (不实际复制)。
    重复导入(skill_id 已存在)返回既有 skill_id。
    """
    sid = skill_id_for(record["name"], record["md5"])
    src_dir = Path(record["path"]).parent
    dst_dir = STORE_DIR / sid

    if dst_dir.exists():
        # 已有权威副本: 仍合并来源 agent 元数据
        if apply:
            index = load_index()
            if sid in index:
                _merge_source(index, sid, agent, record["path"])
                save_index(index)
        return sid
    if not apply:
        return sid

    dst_dir.mkdir(parents=True, exist_ok=True)
    count = _copytree(src_dir, dst_dir)
    if count == 0:
        # 目录里没有可复制文件(如全是排除项), 只放 SKILL.md 原文
        shutil.copy2(record["path"], dst_dir / "SKILL.md")

    manifest = {
        "id": sid,
        "name": record["name"],
        "category": record.get("category", ""),
        "source_agent": agent,
        "source_path": record["path"],
        "md5": record["md5"],
        "size": record.get("size", 0),
        "fm_name": record.get("fm_name", ""),
        "fm_desc": record.get("fm_desc", ""),
        "risks": record.get("risks", []),
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "agents": [agent],  # 已知可用的 agent
        "sources": [{"agent": agent, "path": record["path"]}],
    }
    index = load_index()
    if sid in index:
        _merge_source(index, sid, agent, record["path"])
    else:
        index[sid] = manifest
    save_index(index)
    return sid


def list_skills(only_risky: bool = False) -> List[dict]:
    index = load_index()
    skills = sorted(index.values(), key=lambda m: m.get("name", ""))
    if only_risky:
        skills = [s for s in skills if s.get("risks")]
    return skills


def get_skill(sid: str) -> Optional[dict]:
    return load_index().get(sid)


def remove_skill(sid: str) -> bool:
    """从中央库删除副本与索引(不动 agent 目录的投影)。"""
    index = load_index()
    if sid not in index:
        return False
    del index[sid]
    save_index(index)
    dst = STORE_DIR / sid
    if dst.exists():
        shutil.rmtree(dst)
    return True
