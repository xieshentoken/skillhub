"""适配投影 (Adapter Engine) — 把中央库 skill 投影到各 agent 目录。

- symlink 模式: agent_dir/<name> -> store/<skill_id>  (pi/codex/opencode/claude/grok/hermes)
- copy 模式:    agent_dir/<name>/ 复制一份 + 投影标记 (workbuddy)
- 冲突: 目标已存在且非本库投影 -> 报告冲突, 需 --force 才备份后替换
- 断链: 指向本库但中央库副本已不存在 -> 状态 broken, 不再谎报 linked
- 每次 apply 前整体备份到 backups/<timestamp>/, 可 rollback (硬链接, 几乎零额外占用)
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import AGENTS, BACKUP_DIR, INDEX_FILE, RISK_GATE, STORE_DIR
from .store import get_skill, load_index

# copy 模式在产物目录里写的标记文件, 让复制产物也能被验证为本库投影
MARKER_NAME = ".skillhub-projection.json"
# 备份份数上限 (超出后自动清理最旧的无 conflicts 备份, 0 = 不限制)
MAX_BACKUPS = int(os.environ.get("SKILLHUB_MAX_BACKUPS", "10"))


def target_path(agent: str, manifest: dict) -> Path:
    """计算投影目标路径。"""
    cfg = AGENTS[agent]
    root = cfg["skill_dir"]
    if cfg.get("nested"):
        category = manifest.get("category") or "uncategorized"
        return root / category / manifest["name"]
    return root / manifest["name"]


def _read_marker(target: Path) -> Optional[dict]:
    """读取 copy 投影标记。"""
    p = target / MARKER_NAME
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_marker(target: Path, sid: str, md5: str) -> None:
    """在 copy 产物里写标记, 使复制结果也能被识别为本库投影。"""
    try:
        (target / MARKER_NAME).write_text(
            json.dumps({"skillhub": 1, "sid": sid, "md5": md5,
                        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")
    except OSError:
        pass


def projection_state(target: Path, sid: str) -> str:
    """判断目标路径的投影状态。

    linked     — symlink 指向本库且中央库副本存在
    broken     — symlink 指向本库, 但中央库副本已被删除 (悬空, 不能再算已投影)
    copy       — copy 模式产物且标记匹配
    conflict   — 已被别的内容占据
    not_linked — 不存在
    """
    if target.is_symlink():
        if str(target.resolve()) != str((STORE_DIR / sid).resolve()):
            return "conflict"
        return "linked" if target.exists() else "broken"
    if target.exists():
        marker = _read_marker(target)
        if marker and marker.get("sid") == sid:
            return "copy"
        return "conflict"
    return "not_linked"


def _is_our_projection(target: Path, sid: str) -> bool:
    """是否为本库投影 (含已断链的, 以便 unlink 仍能清理)。"""
    return projection_state(target, sid) in ("linked", "copy", "broken")


def _link_or_copy(src: str, dst: str) -> None:
    """硬链接优先 (store 文件不可变, 同卷几乎零成本), 跨设备/失败回落复制。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def prune_backups(max_keep: int = MAX_BACKUPS) -> List[str]:
    """按份数上限清理最旧备份。只删不含 conflicts/ 的, 保留被替换过的原始目录。"""
    if max_keep <= 0:
        return []
    removed = []
    for b in list_backups()[max_keep:]:
        if (b / "conflicts").exists() and any((b / "conflicts").iterdir()):
            continue
        shutil.rmtree(b, ignore_errors=True)
        removed.append(b.name)
    return removed


def _backup_all(ts: str) -> None:
    """备份 store + index 当前状态 (用于回滚)。同一 ts 已备份过则跳过, 避免秒级并发/批量操作撞车。"""
    dest = BACKUP_DIR / ts
    dest.mkdir(parents=True, exist_ok=True)
    store_dest = dest / "store"
    if STORE_DIR.exists() and not store_dest.exists():
        # 硬链接备份: store 文件不可变, 同卷几乎不占额外空间
        shutil.copytree(STORE_DIR, store_dest, copy_function=_link_or_copy)
    idx_dest = dest / "index.json"
    if INDEX_FILE.exists() and not idx_dest.exists():
        shutil.copy2(INDEX_FILE, idx_dest)
    # 超出上限时清理最旧备份 (保留含 conflicts 的)
    for name in prune_backups():
        print(f"  (备份超上限, 已清理旧备份 {name})")


def _risk_blocked(manifest: dict, agent: str) -> List[str]:
    """风险门禁: 命中 RISK_GATE 的 skill 默认禁止投影, 需 --force 放行。"""
    risks = set(manifest.get("risks") or [])
    return sorted(risks & set(RISK_GATE))


def plan_link(sid: str, agents: List[str], force: bool = False) -> Dict[str, List[dict]]:
    """生成投影计划(不执行)。返回 {agent: [action,...]}。
    action: {type: link|conflict|broken|blocked|skip, target, detail}
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
        state = projection_state(target, sid)
        if state in ("linked", "copy"):
            plan[agent] = [{"type": "skip", "target": str(target),
                            "detail": f"已投影({state})"}]
        elif state == "broken":
            plan[agent] = [{"type": "broken", "target": str(target),
                            "detail": "悬空链接: 中央库副本已缺失, 需先 import 或 rollback"}]
        elif state == "conflict":
            plan[agent] = [{"type": "conflict", "target": str(target),
                            "detail": "已存在非本库投影(真实目录或其它链接)"}]
        else:
            blocked = _risk_blocked(manifest, agent)
            if blocked and not force:
                plan[agent] = [{"type": "blocked", "target": str(target),
                                "detail": f"风险门禁: {','.join(blocked)} (需 --force 放行)"}]
                continue
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
        state = projection_state(target, sid)
        if state in ("linked", "copy"):
            results[agent] = [{"type": "skip", "target": str(target),
                               "detail": f"已投影({state})"}]
            continue
        if state == "broken":
            # 中央库副本已不在, 重建链接只会再造一个悬空链接, 必须拒绝
            results[agent] = [{"type": "broken", "target": str(target),
                               "detail": "中央库副本缺失, 请先 import 或 rollback"}]
            continue
        if state == "conflict":
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
        blocked = _risk_blocked(manifest, agent)
        if blocked and not force:
            results[agent] = [{"type": "blocked", "target": str(target),
                               "detail": f"风险门禁: {','.join(blocked)} (需 --force 放行)"}]
            continue
        mode = cfg.get("mode", "symlink")
        try:
            if mode == "symlink":
                target.symlink_to(src, target_is_directory=True)
            else:  # copy
                shutil.copytree(src, target)
                _write_marker(target, sid, manifest.get("md5", ""))
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
    """每个 agent 的投影状态: linked / broken / conflict / not_linked。

    copy 模式的正常产物也归为 linked (靠投影标记验证)。
    """
    index = load_index()
    out: Dict[str, List[dict]] = {a: [] for a in AGENTS}
    for sid, manifest in index.items():
        for agent in AGENTS:
            target = target_path(agent, manifest)
            state = projection_state(target, sid)
            if state == "copy":
                state = "linked"
            out[agent].append({"sid": sid, "name": manifest["name"], "state": state})
    return out


def doctor() -> dict:
    """一次性体检: 断链 / 孤儿 store 目录 / copy 漂移 / 冲突 / 同名多版本 / 缺失目录。

    返回 {summary, broken, orphan_store, copy_drift, conflict, duplicate_names, missing_dirs}
    """
    index = load_index()
    st = status()
    findings: dict = {
        "broken": [], "orphan_store": [], "copy_drift": [],
        "conflict": [], "duplicate_names": {}, "missing_dirs": [],
    }

    for agent, items in st.items():
        for it in items:
            if it["state"] == "broken":
                findings["broken"].append({"agent": agent, "sid": it["sid"], "name": it["name"]})
            elif it["state"] == "conflict":
                findings["conflict"].append({"agent": agent, "sid": it["sid"], "name": it["name"]})

    # 孤儿: store 里有目录但索引里没有
    if STORE_DIR.exists():
        for d in sorted(STORE_DIR.iterdir()):
            if d.is_dir() and d.name not in index:
                findings["orphan_store"].append({"sid": d.name})

    # 同名多版本: 同一个 name 对应多个 sid
    by_name: Dict[str, List[str]] = {}
    for sid, man in index.items():
        by_name.setdefault(man.get("name", ""), []).append(sid)
    for name, sids in sorted(by_name.items()):
        if len(sids) > 1:
            findings["duplicate_names"][name] = sorted(sids)

    # copy 漂移: copy 模式的投影内容与中央库 SKILL.md 不一致
    for agent, cfg in AGENTS.items():
        if cfg.get("mode") != "copy":
            continue
        root = cfg["skill_dir"]
        if not root.exists():
            continue
        for sid, man in index.items():
            target = target_path(agent, man)
            if projection_state(target, sid) != "copy":
                continue
            src_file = STORE_DIR / sid / "SKILL.md"
            dst_file = target / "SKILL.md"
            if src_file.exists() and dst_file.exists():
                try:
                    if src_file.read_bytes() != dst_file.read_bytes():
                        findings["copy_drift"].append(
                            {"agent": agent, "sid": sid, "name": man.get("name", "")})
                except OSError:
                    pass

    for agent, cfg in AGENTS.items():
        if not cfg["skill_dir"].exists():
            findings["missing_dirs"].append({"agent": agent, "dir": str(cfg["skill_dir"])})

    findings["summary"] = {
        "skills": len(index),
        "broken": len(findings["broken"]),
        "conflict": len(findings["conflict"]),
        "orphan_store": len(findings["orphan_store"]),
        "copy_drift": len(findings["copy_drift"]),
        "duplicate_names": len(findings["duplicate_names"]),
        "missing_dirs": len(findings["missing_dirs"]),
    }
    return findings
