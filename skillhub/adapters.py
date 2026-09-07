"""适配投影 (Adapter Engine) — 把中央库 skill 投影到各 agent 目录。

- symlink 模式: agent_dir/<name> -> store/<skill_id>  (pi/codex/opencode/claude/hermes)
- copy 模式:    agent_dir/<name>/ 复制一份          (workbuddy)
- 冲突: 目标已存在且非本库投影 -> 报告冲突, 需 --force 才备份后替换
- 每次 apply 前整体备份到 backups/<timestamp>/, 可 rollback
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import AGENTS, BACKUP_DIR, INDEX_FILE, STORE_DIR
from .store import get_skill, load_index


def target_path(agent: str, manifest: dict) -> Path:
    """计算投影目标路径。"""
    cfg = AGENTS[agent]
    root = cfg["skill_dir"]
    if cfg.get("nested"):
        category = manifest.get("category") or "uncategorized"
        return root / category / manifest["name"]
    return root / manifest["name"]


def _is_our_projection(target: Path, sid: str) -> bool:
    if target.is_symlink():
        return str(target.resolve()) == str((STORE_DIR / sid).resolve())
    return False


def _backup_all(ts: str) -> None:
    """备份 store + index 当前状态 (用于回滚)。同一 ts 已备份过则跳过, 避免秒级并发/批量操作撞车。"""
    dest = BACKUP_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    store_dest = dest / "store"
    if STORE_DIR.exists() and not store_dest.exists():
        shutil.copytree(STORE_DIR, store_dest)
    idx_dest = dest / "index.json"
    if INDEX_FILE.exists() and not idx_dest.exists():
        shutil.copy2(INDEX_FILE, idx_dest)


def plan_link(sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    """生成投影计划(不执行)。返回 {agent: [action,...]}。
    action: {type: link|conflict|skip, target, detail}
    """
    manifest = get_skill(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    plan: Dict[str, List[dict]] = {}
    for agent in agents:
        if agent not in AGENTS:
            plan[agent] = [{"type": "error", "target": str(agent), "detail": "未知 agent"}]
            continue
        cfg = AGENTS[agent]
        root = cfg["skill_dir"]
        target = target_path(agent, manifest)
        root.mkdir(parents=True, exist_ok=True)
        if _is_our_projection(target, sid):
            plan[agent] = [{"type": "skip", "target": str(target), "detail": "已链接"}]
        elif target.exists():
            plan[agent] = [{"type": "conflict", "target": str(target),
                            "detail": "已存在非本库投影(真实目录或其它链接)"}]
        else:
            mode = cfg.get("mode", "symlink")
            plan[agent] = [{"type": "link", "target": str(target), "detail": f"{mode} -> store/{sid}"}]
    return plan


def apply_link(sid: str, agents: List[str], force: bool = False, backup: bool = True,
               ts: Optional[str] = None) -> Dict[str, List[dict]]:
    """执行投影。force=True 时把冲突目标先备份再替换。返回执行结果。

    ts: 备份时间戳。批量调用时传入同一个 ts, 让冲突备份落在同一备份点,
    便于 rollback 时一次性找回。
    """
    manifest = get_skill(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    ts = ts or time.strftime("%Y%m%d-%H%M%S")
    if backup:
        _backup_all(ts)
    src = STORE_DIR / sid
    results: Dict[str, List[dict]] = {}
    for agent in agents:
        cfg = AGENTS[agent]
        root = cfg["skill_dir"]
        root.mkdir(parents=True, exist_ok=True)
        target = target_path(agent, manifest)
        if _is_our_projection(target, sid):
            results[agent] = [{"type": "skip", "target": str(target), "detail": "已链接"}]
            continue
        if target.exists():
            if not force:
                results[agent] = [{"type": "conflict", "target": str(target),
                                   "detail": "需 --force 才替换(替换前会备份)"}]
                continue
            # 冲突目标备份到 backups/<ts>/conflicts/
            bk = BACKUP_DIR / ts / "conflicts"
            bk.mkdir(parents=True, exist_ok=True)
            shutil.move(str(target), str(bk / target.name))
            results[agent] = [{"type": "backup", "target": str(target),
                               "detail": f"原目录已备份到 {bk / target.name}"}]
        mode = cfg.get("mode", "symlink")
        try:
            if mode == "symlink":
                target.symlink_to(src, target_is_directory=True)
            else:  # copy
                shutil.copytree(src, target)
            results[agent] = [{"type": "link", "target": str(target),
                               "detail": f"{mode} -> store/{sid}"}]
        except Exception as e:  # pragma: no cover
            results[agent] = [{"type": "error", "target": str(target), "detail": str(e)}]
    return results


def apply_link_batch(
    sids: List[str], agents: List[str], force: bool = False
) -> Dict[str, List[dict]]:
    """批量执行投影, 整批只做一次全量备份。

    逐个调用 apply_link(backup=True) 会让每个 skill 都触发一次 _backup_all:
    同一秒内靠幂等跳过, 跨秒则重复复制整个 store。批量场景(几百个 skill)
    下必须显式只备份一次, 再让每个 skill 跳过自己的备份。
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    _backup_all(ts)
    merged: Dict[str, List[dict]] = {}
    for sid in sids:
        results = apply_link(sid, agents, force=force, backup=False, ts=ts)
        for agent, actions in results.items():
            merged.setdefault(agent, []).extend(actions)
    return merged


def plan_unlink(sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    """生成解除投影计划。只处理指向本库的投影。"""
    manifest = get_skill(sid)
    plan: Dict[str, List[dict]] = {}
    for agent in agents:
        target = target_path(agent, manifest)
        if _is_our_projection(target, sid):
            plan[agent] = [{"type": "unlink", "target": str(target), "detail": "移除本库投影"}]
        elif target.exists():
            plan[agent] = [{"type": "conflict", "target": str(target), "detail": "非本库投影, 不动"}]
        else:
            plan[agent] = [{"type": "skip", "target": str(target), "detail": "不存在"}]
    return plan


def apply_unlink(sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    manifest = get_skill(sid)
    results: Dict[str, List[dict]] = {}
    for agent in agents:
        target = target_path(agent, manifest)
        if _is_our_projection(target, sid):
            if target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(target)
            results[agent] = [{"type": "unlink", "target": str(target), "detail": "已移除"}]
        else:
            results[agent] = [{"type": "skip", "target": str(target), "detail": "非本库投影或不存在"}]
    return results


def list_backups() -> List[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted([p for p in BACKUP_DIR.iterdir() if p.is_dir()], reverse=True)


def plan_cleanup(keep: int = 3) -> List[dict]:
    """清理计划: 保留最近 keep 份完整快照 (store+index+conflicts)。
    更老的备份: 含 conflicts 的只删 store/index 保留原目录; 无 conflicts 的整体删除。
    action: {type: keep|prune_store|remove, ts, size, detail}
    """
    bks = list_backups()
    plan: List[dict] = []
    for i, b in enumerate(bks):
        has_conflicts = (b / "conflicts").exists() and any((b / "conflicts").iterdir())
        size = sum(f.stat().st_size for f in b.rglob("*") if f.is_file())
        if i < keep:
            plan.append({"type": "keep", "ts": b.name, "size": size,
                         "detail": "保留完整快照 (可 rollback)"})
        elif has_conflicts:
            store_size = (b / "store").exists() and sum(
                f.stat().st_size for f in (b / "store").rglob("*") if f.is_file()) or 0
            idx_size = (b / "index.json").stat().st_size if (b / "index.json").exists() else 0
            plan.append({"type": "prune_store", "ts": b.name, "size": store_size + idx_size,
                         "detail": "删 store/index, 保留 conflicts/ 原目录"})
        else:
            plan.append({"type": "remove", "ts": b.name, "size": size,
                         "detail": "无 conflicts, 整体删除"})
    return plan


def apply_cleanup(keep: int = 3) -> List[dict]:
    """执行清理, 返回实际动作。"""
    plan = plan_cleanup(keep)
    for a in plan:
        b = BACKUP_DIR / a["ts"]
        if a["type"] == "prune_store":
            shutil.rmtree(b / "store", ignore_errors=True)
            (b / "index.json").unlink(missing_ok=True)
        elif a["type"] == "remove":
            shutil.rmtree(b, ignore_errors=True)
    return plan


def status() -> Dict[str, List[dict]]:
    """每个 agent 的投影状态: 已链接 / 未链接 / 冲突。"""
    index = load_index()
    out: Dict[str, List[dict]] = {a: [] for a in AGENTS}
    for sid, manifest in index.items():
        for agent in AGENTS:
            target = target_path(agent, manifest)
            if _is_our_projection(target, sid):
                out[agent].append({"sid": sid, "name": manifest["name"], "state": "linked"})
            elif target.exists():
                out[agent].append({"sid": sid, "name": manifest["name"], "state": "conflict"})
            else:
                out[agent].append({"sid": sid, "name": manifest["name"], "state": "not_linked"})
    return out
