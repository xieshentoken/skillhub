"""把中央库 skill 安全地投影到各 agent，并管理可恢复快照。"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .config import AGENTS, BACKUP_DIR, INDEX_FILE, RISK_GATE, STORE_DIR, TRASH_DIR
from .store import (atomic_write, content_info, file_summary, get_skill,
                    load_index, locked, safe_component, safe_path, write_lock)


MARKER_NAME = ".skillhub-projection.json"
try:
    MAX_BACKUPS = int(os.environ.get("SKILLHUB_MAX_BACKUPS", "10"))
except ValueError:
    MAX_BACKUPS = 10


def target_path(agent: str, manifest: dict) -> Path:
    if agent not in AGENTS:
        raise ValueError(f"未知 agent: {agent}")
    name = manifest.get("name")
    safe_component(name)
    root = AGENTS[agent]["skill_dir"]
    if AGENTS[agent].get("nested"):
        category = manifest.get("category") or "uncategorized"
        safe_component(category)
        return safe_path(root, category, name, projection=True)
    return safe_path(root, name, projection=True)


def _read_marker(target: Path) -> Optional[dict]:
    marker = target / MARKER_NAME
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _write_marker(target: Path, manifest: dict, sid: str) -> None:
    data = {
        "schema_version": 2,
        "skillhub": 1,
        "sid": sid,
        "md5": manifest.get("md5", ""),
        "file_count": manifest.get("file_count", 0),
        "size": manifest.get("size", 0),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    atomic_write(target / MARKER_NAME, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _source_dir(sid: str) -> Path:
    return safe_path(STORE_DIR, sid)


def _central_state(sid: str, manifest: Optional[dict] = None) -> str:
    """验证中央副本本身仍与索引摘要一致。"""
    try:
        source = _source_dir(sid)
        manifest = manifest if manifest is not None else get_skill(sid)
    except (OSError, ValueError):
        return "missing"
    if manifest is None or not source.is_dir() or source.is_symlink():
        return "missing"
    try:
        _reject_tree_symlinks(source)
        actual_md5, _ = content_info(source)
    except (OSError, ValueError):
        return "store_drift"
    return "ok" if not manifest.get("md5") or actual_md5 == manifest.get("md5") else "store_drift"


def projection_state(target: Path, sid: str, *, central: Optional[str] = None,
                     manifest: Optional[dict] = None) -> str:
    """返回 ownership 与内容状态，不把 marker 当成内容完整性的证明。"""
    source = _source_dir(sid)
    expected = source.resolve(strict=False)
    central = central if central is not None else _central_state(sid, manifest)
    if target.is_symlink():
        try:
            same_source = target.resolve(strict=False) == expected
        except (OSError, RuntimeError):
            same_source = False
        if not same_source:
            return "conflict"
        if not target.exists() or central == "missing":
            return "broken"
        if central != "ok":
            return "store_drift"
        return "linked"
    # 没有投影目标时不要把中央库漂移误报成已映射。漂移只描述「目标存在
    # 且指向本库，但中央副本已偏离索引」。
    if not target.exists():
        return "not_linked" if central != "missing" else "broken"
    if not target.is_dir():
        return "conflict"
    marker = _read_marker(target)
    if not marker or marker.get("skillhub") != 1 or marker.get("sid") != sid:
        return "conflict"
    if central == "missing":
        return "copy_broken"
    if central != "ok":
        return "store_drift"
    try:
        actual_md5, _ = content_info(target)
    except (OSError, ValueError):
        return "copy_drift"
    expected_md5 = (manifest if manifest is not None else get_skill(sid) or {}).get("md5")
    return "copy" if actual_md5 == marker.get("md5") == expected_md5 else "copy_drift"


def _is_our_projection(target: Path, sid: str) -> bool:
    return projection_state(target, sid) in {"linked", "copy", "broken", "copy_broken"}


def _is_owned(target: Path, sid: str) -> bool:
    return _is_our_projection(target, sid) or projection_state(target, sid) == "copy_drift"


def backup_timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for filename in files:
            item = Path(root) / filename
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                pass
    return total


def _metadata_path(backup: Path) -> Path:
    return backup / "snapshot.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"备份元数据损坏: {path}") from exc


def has_conflicts(backup: Path) -> bool:
    journal = backup / "targets.json"
    if journal.exists():
        entries = _read_json(journal, [])
        return any(isinstance(e, dict) and e.get("conflict") for e in entries)
    old = backup / "conflicts"
    return old.is_dir() and any(old.iterdir())


def _copy_store(src: Path, dst: Path) -> None:
    """复制中央库内容，不使用硬链接，也不保留内部软链接。"""
    if not src.exists():
        dst.mkdir(parents=True, exist_ok=True)
        return
    if src.is_symlink():
        raise ValueError("中央库根目录不能是软链接")
    # copytree(symlinks=False) 会跟随源软链接。中央库应当是完全普通的
    # 文件树，因此先检查所有目录（包括 node_modules 等被 skill 摘要
    # 忽略的目录），再以 symlinks=True 复制，最后再次检查副本。这样即使
    # 检查与复制之间出现竞态，也不会把链接目标内容读进备份。
    _reject_tree_symlinks(src)
    try:
        shutil.copytree(src, dst, symlinks=True)
        _reject_tree_symlinks(dst)
    except Exception:
        shutil.rmtree(dst, ignore_errors=True)
        raise


def _reject_tree_symlinks(root: Path) -> None:
    """拒绝目录树中任意位置的软链接，避免复制时跟随外部路径。"""
    root = Path(root)
    if root.is_symlink():
        raise ValueError(f"目录树根不能是软链接: {root}")
    if not root.is_dir():
        raise ValueError(f"目录树不是目录: {root}")
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        for name in dirs + files:
            item = Path(directory) / name
            if item.is_symlink():
                raise ValueError(f"目录树包含不安全软链接: {item}")


def _backup_all(ts: str) -> Path:
    backup = safe_path(BACKUP_DIR, ts)
    backup.mkdir(parents=True, exist_ok=True)
    meta_path = _metadata_path(backup)
    if meta_path.exists():
        return backup
    store_exists = STORE_DIR.is_dir() and not STORE_DIR.is_symlink()
    index_exists = INDEX_FILE.is_file() and not INDEX_FILE.is_symlink()
    if store_exists and not (backup / "store").exists():
        _copy_store(STORE_DIR, backup / "store")
    else:
        (backup / "store").mkdir(parents=True, exist_ok=True)
    if index_exists:
        shutil.copy2(INDEX_FILE, backup / "index.json")
        (backup / "index.json").chmod(0o600)
    metadata = {
        "schema_version": 2,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "store_exists": store_exists,
        "index_exists": index_exists,
        "logical_size": _dir_size(backup),
    }
    atomic_write(meta_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    return backup


def _backup_entry_path(backup: Path, target: Path) -> Path:
    key = hashlib.sha256(str(target.absolute()).encode("utf-8")).hexdigest()
    return backup / "targets" / key


def _target_entries(backup: Path) -> list:
    path = backup / "targets.json"
    if not path.exists():
        return []
    entries = _read_json(path, [])
    if not isinstance(entries, list):
        raise ValueError(f"备份目标清单损坏: {path}")
    return entries


def backup_target(ts: str, target: Path, conflict: bool = False) -> None:
    backup = safe_path(BACKUP_DIR, ts)
    backup.mkdir(parents=True, exist_ok=True)
    entries = _target_entries(backup)
    original = str(target.absolute())
    if any(isinstance(e, dict) and e.get("target") == original for e in entries):
        return
    saved = _backup_entry_path(backup, target)
    entry = {"target": original, "conflict": bool(conflict)}
    if target.is_symlink():
        entry.update({"kind": "symlink", "link": os.readlink(target)})
    elif target.is_dir():
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(target, saved, symlinks=True)
        entry.update({"kind": "dir", "saved": str(saved.relative_to(backup))})
    elif target.exists():
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, saved)
        saved.chmod(0o600)
        entry.update({"kind": "file", "saved": str(saved.relative_to(backup))})
    else:
        entry["kind"] = "missing"
    entries.append(entry)
    atomic_write(backup / "targets.json", json.dumps(entries, ensure_ascii=False, indent=2) + "\n")


def _remove_target(target: Path) -> None:
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.exists():
        shutil.rmtree(target)


def _allowed_restore_target(target: Path) -> bool:
    """只允许恢复到具体的、当前配置声明过的投影目标。"""
    absolute = Path(os.path.abspath(os.fspath(target)))
    for cfg in AGENTS.values():
        root = Path(os.path.abspath(os.fspath(cfg["skill_dir"])))
        try:
            relative = absolute.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        expected = 2 if cfg.get("nested") else 1
        if len(parts) != expected:
            continue
        try:
            for part in parts:
                safe_component(part)
            candidate = safe_path(root, *parts, projection=True)
        except (OSError, ValueError):
            continue
        if Path(os.path.abspath(os.fspath(candidate))) == absolute:
            return True
    try:
        from . import mcp
        for agent in mcp.MCP_TARGETS:
            if agent == "pi":
                candidate = mcp._pi_server_path("validation")
                root = Path(os.path.abspath(os.fspath(candidate.parent)))
                if absolute.parent == root and absolute.suffix == ".json":
                    safe_component(absolute.name)
                    return True
            else:
                candidate = Path(os.path.abspath(os.fspath(mcp._agent_mcp_file(agent))))
                if absolute == candidate:
                    return True
    except (OSError, ValueError, AttributeError):
        pass
    return False


def _validate_snapshot(source: Path) -> tuple[dict, dict, list]:
    source = Path(source)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("备份目录不是安全目录，不能回滚")
    store_copy = source / "store"
    mcp_journal = source / "mcp" / "targets.json"
    has_store = store_copy.is_dir() and not store_copy.is_symlink()
    if store_copy.is_symlink():
        raise ValueError("备份 store 副本不能是软链接，不能回滚")
    if not has_store:
        if mcp_journal.exists() or mcp_journal.is_symlink():
            from . import mcp
            mcp._mcp_backup_entries(source)
            return {"schema_version": 1, "mcp_only": True,
                    "store_exists": False, "index_exists": False}, {}, []
        raise ValueError("备份 store 副本不是安全目录，不能回滚")
    for fixed_name in ("snapshot.json", "index.json", "targets.json"):
        fixed = source / fixed_name
        if fixed.is_symlink():
            raise ValueError(f"备份元数据不能是软链接: {fixed_name}")
    metadata = _read_json(_metadata_path(source), {})
    if not metadata:
        # 兼容旧快照：没有元数据时只能接受完整 store + index。
        metadata = {"schema_version": 1, "store_exists": True,
                    "index_exists": (source / "index.json").is_file()}
    if not isinstance(metadata, dict) or not store_copy.is_dir():
        raise ValueError("该备份不是完整快照，不能回滚")
    for key in ("store_exists", "index_exists"):
        if key in metadata and type(metadata[key]) is not bool:
            raise ValueError(f"备份元数据字段无效: {key}")
    if metadata.get("index_exists") and not (source / "index.json").is_file():
        raise ValueError("备份索引副本缺失，不能回滚")
    index = {}
    if metadata.get("index_exists"):
        try:
            index = json.loads((source / "index.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("备份索引损坏，不能回滚") from exc
        from .store import _validate_index
        _validate_index(index)
    # 回滚将把整个 store 复制到 live 目录；先确认备份内部没有任何链接，
    # 防止 copytree 在真正删除当前状态后跟随恶意链接。
    _reject_tree_symlinks(store_copy)
    if metadata.get("index_exists"):
        # A snapshot directory can be present while an individual file has
        # been deleted or edited.  Verify every indexed skill before rollback
        # creates a current backup or removes any live state.
        for sid, manifest in index.items():
            saved_skill = store_copy / sid
            if saved_skill.is_symlink() or not saved_skill.is_dir():
                raise ValueError(f"备份缺少中央 skill 副本: {sid}")
            try:
                summary = file_summary(saved_skill)
            except (OSError, ValueError) as exc:
                raise ValueError(f"备份 skill 内容无法验证: {sid}") from exc
            expected_md5 = manifest.get("md5")
            if expected_md5 and summary["md5"] != expected_md5:
                raise ValueError(f"备份 skill 摘要不匹配: {sid}")
            if ("file_count" in manifest and summary["file_count"] != manifest["file_count"] or
                    "size" in manifest and summary["size"] != manifest["size"]):
                raise ValueError(f"备份 skill 文件集不匹配: {sid}")
    if mcp_journal.exists() or mcp_journal.is_symlink():
        from . import mcp
        mcp._mcp_backup_entries(source)
    entries = _target_entries(source)
    seen_targets = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
            raise ValueError("备份目标清单损坏")
        target = Path(os.path.abspath(entry["target"]))
        if not target.is_absolute() or not _allowed_restore_target(target):
            raise ValueError("备份目标不在当前配置允许的路径内")
        target_key = str(target)
        if target_key in seen_targets:
            raise ValueError("备份目标清单含重复目标")
        seen_targets.add(target_key)
        kind = entry.get("kind")
        if kind not in {"missing", "symlink", "file", "dir"}:
            raise ValueError("备份目标类型无效")
        if kind == "symlink":
            link = entry.get("link")
            if not isinstance(link, str) or not link or "\x00" in link:
                raise ValueError("备份软链接目标无效")
            if "saved" in entry:
                raise ValueError("软链接备份不应包含 saved 路径")
        if kind == "missing" and any(key in entry for key in ("saved", "link")):
            raise ValueError("缺失目标备份字段无效")
        if kind in {"file", "dir"}:
            saved_value = entry.get("saved")
            if not isinstance(saved_value, str) or not saved_value or "\\" in saved_value:
                raise ValueError("备份副本路径无效")
            relative = Path(saved_value)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("备份副本路径必须是快照内的相对路径")
            try:
                for part in relative.parts:
                    safe_component(part)
            except ValueError as exc:
                raise ValueError("备份副本路径包含不安全组件") from exc
            saved = source / relative
            try:
                source_boundary = source.resolve(strict=False)
                saved_boundary = saved.resolve(strict=False)
                saved_boundary.relative_to(source_boundary)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("备份副本路径越出快照目录") from exc
            current = source
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise ValueError("备份副本路径的父级不能是软链接")
            if not saved.exists() or saved.is_symlink():
                raise ValueError("备份目标副本缺失或是软链接")
            if kind == "file" and not saved.is_file():
                raise ValueError("文件备份副本类型不匹配")
            if kind == "dir" and not saved.is_dir():
                raise ValueError("备份目标副本缺失")
    return metadata, index, entries


@locked
def rollback(ts: str) -> None:
    safe_component(ts)
    source = safe_path(BACKUP_DIR, ts)
    metadata, index, entries = _validate_snapshot(source)
    from . import mcp
    mcp_journal = source / "mcp" / "targets.json"
    if metadata.get("mcp_only"):
        mcp_entries = mcp._mcp_backup_entries(source)
        current = backup_timestamp()
        mcp._backup_mcp_targets(current, [Path(item["target"]) for item in mcp_entries])
        mcp.restore_backup(source)
        prune_backups()
        return
    current = backup_timestamp()
    _backup_all(current)
    for entry in entries:
        backup_target(current, Path(entry["target"]), conflict=entry.get("conflict", False))
    if mcp_journal.exists() or mcp_journal.is_symlink():
        mcp_entries = mcp._mcp_backup_entries(source)
        mcp._backup_mcp_targets(current, [Path(item["target"]) for item in mcp_entries])

    with tempfile.TemporaryDirectory(prefix=".rollback-", dir=STORE_DIR.parent) as tmp:
        staged = Path(tmp) / "store"
        shutil.copytree(source / "store", staged, symlinks=True)
        _reject_tree_symlinks(staged)
        if STORE_DIR.exists() or STORE_DIR.is_symlink():
            _remove_target(STORE_DIR)
        if metadata.get("store_exists", True):
            staged.rename(STORE_DIR)
    if metadata.get("index_exists"):
        atomic_write(INDEX_FILE, (source / "index.json").read_bytes())
    elif INDEX_FILE.exists() or INDEX_FILE.is_symlink():
        _remove_target(INDEX_FILE)

    for entry in entries:
        target = Path(entry["target"])
        _remove_target(target)
        if entry["kind"] == "missing":
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "symlink":
            target.symlink_to(entry["link"])
        elif entry["kind"] == "dir":
            shutil.copytree(source / entry["saved"], target, symlinks=True)
        else:
            shutil.copy2(source / entry["saved"], target)
    if mcp_journal.exists() or mcp_journal.is_symlink():
        mcp.restore_backup(source)
    prune_backups()


def prune_backups(max_keep: int = MAX_BACKUPS) -> List[str]:
    if max_keep <= 0:
        return []
    removed = []
    for backup in list_backups()[max_keep:]:
        if has_conflicts(backup):
            continue
        shutil.rmtree(backup)
        removed.append(backup.name)
    return removed


def _risk_blocked(manifest: dict) -> List[str]:
    return sorted(set(manifest.get("risks") or []) & set(RISK_GATE))


def _effective_projection(agent: str, sid: str, manifest: dict,
                          index: Optional[dict] = None) -> tuple[str, dict]:
    """Resolve a formal request to the UL actually used by a trial agent."""
    if manifest.get("channel") != "formal" or agent not in set(manifest.get("trial_agents", [])):
        return sid, manifest
    from . import store
    index = index if index is not None else store.load_index()
    pair = store.version_pair(index, sid)
    ul_sid = pair.get("ul_sid")
    if isinstance(ul_sid, str) and ul_sid in index:
        return ul_sid, index[ul_sid]
    # A stale trial marker must not silently make a normal formal projection
    # impossible; status/doctor still expose the relation problem elsewhere.
    return sid, manifest


def plan_link(sid: str, agents: List[str], force: bool = False,
              allow_risky: bool = False) -> Dict[str, List[dict]]:
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    plan: Dict[str, List[dict]] = {}
    for agent in agents:
        if agent not in AGENTS:
            plan[agent] = [{"type": "error", "target": agent, "detail": "未知 agent"}]
            continue
        effective_sid, effective_manifest = _effective_projection(agent, sid, manifest, index)
        target = target_path(agent, manifest)
        state = projection_state(target, effective_sid, manifest=effective_manifest)
        if state in {"linked", "copy"}:
            plan[agent] = [{"type": "skip", "target": str(target), "detail": f"已投影({state})"}]
            continue
        if state == "store_drift" or _central_state(effective_sid, effective_manifest) == "store_drift":
            plan[agent] = [{"type": "store_drift", "target": str(target),
                            "detail": "中央库文件已偏离索引摘要，需先恢复或重新导入"}]
            continue
        if state in {"broken", "copy_broken"}:
            plan[agent] = [{"type": "broken", "target": str(target),
                            "detail": "中央库副本缺失，需先 import 或 rollback"}]
            continue
        actions = []
        if state in {"conflict", "copy_drift"}:
            actions.append({"type": state, "target": str(target),
                            "detail": "目标已有非完整本库内容，需 --force 替换"})
        blocked = _risk_blocked(effective_manifest)
        if blocked and not allow_risky:
            actions.append({"type": "blocked", "target": str(target),
                            "detail": f"风险门禁: {','.join(blocked)}，需 --allow-risky 放行"})
        if not actions:
            actions.append({"type": "link", "target": str(target),
                            "detail": f"{AGENTS[agent].get('mode', 'symlink')} -> store/{effective_sid}"})
        plan[agent] = actions
    return plan


def _install_projection(src: Path, target: Path, mode: str,
                        manifest: dict, sid: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_name = target.parent / f".skillhub-install-{os.getpid()}-{next(tempfile._get_candidate_names())}"
    try:
        if mode == "symlink":
            temp_name.symlink_to(src, target_is_directory=True)
        else:
            _reject_tree_symlinks(src)
            shutil.copytree(src, temp_name, symlinks=True)
            _reject_tree_symlinks(temp_name)
            _write_marker(temp_name, manifest, sid)
        os.replace(temp_name, target)
    finally:
        if temp_name.is_symlink() or temp_name.exists():
            _remove_target(temp_name)


def _raw_projection_sid(target: Path) -> Optional[str]:
    """读取投影的 ownership，不依赖中央副本当前是否仍存在。"""
    if target.is_symlink():
        try:
            resolved = target.resolve(strict=False)
            store_root = STORE_DIR.resolve(strict=False)
            relative = resolved.relative_to(store_root)
            if len(relative.parts) == 1:
                return relative.parts[0]
        except (OSError, RuntimeError, ValueError):
            return None
    marker = _read_marker(target)
    if marker and marker.get("skillhub") == 1 and isinstance(marker.get("sid"), str):
        try:
            safe_component(marker["sid"])
        except ValueError:
            return None
        return marker["sid"]
    return None


def _copy_target_for_rollback(target: Path, root: Path) -> dict:
    record = {"target": str(target), "kind": "missing"}
    if target.is_symlink():
        record.update({"kind": "symlink", "link": os.readlink(target)})
    elif target.is_dir():
        saved = root / f"dir-{len(list(root.iterdir()))}"
        shutil.copytree(target, saved, symlinks=True)
        record.update({"kind": "dir", "saved": str(saved)})
    elif target.exists():
        saved = root / f"file-{len(list(root.iterdir()))}"
        shutil.copy2(target, saved)
        record.update({"kind": "file", "saved": str(saved)})
    return record


def _restore_target_record(record: dict) -> None:
    target = Path(record["target"])
    _remove_target(target)
    if record["kind"] == "missing":
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if record["kind"] == "symlink":
        target.symlink_to(record["link"])
    elif record["kind"] == "dir":
        shutil.copytree(record["saved"], target, symlinks=True)
    else:
        shutil.copy2(record["saved"], target)


@locked
def retarget_skill(old_sid: str, new_sid: str, old_manifest: dict,
                   new_manifest: dict) -> dict:
    """把已有的本库投影从一个 content id 迁到另一个，只处理受影响目标。"""
    safe_component(old_sid)
    safe_component(new_sid)
    if old_sid == new_sid:
        return {"changed": []}
    temp_root = Path(tempfile.mkdtemp(prefix=".retarget-", dir=BACKUP_DIR.parent))
    records = []
    changed = []
    try:
        for agent in AGENTS:
            old_target = target_path(agent, old_manifest)
            new_target = target_path(agent, new_manifest)
            owner = _raw_projection_sid(old_target)
            if owner != old_sid:
                continue
            state = projection_state(old_target, old_sid, central="ok", manifest=old_manifest)
            if state in {"copy_drift", "conflict"}:
                raise ValueError(f"{agent} 投影已被修改或冲突，不能自动迁移")
            if new_target != old_target and (new_target.exists() or new_target.is_symlink()):
                if _raw_projection_sid(new_target) != old_sid:
                    raise ValueError(f"{agent} 的新投影目标存在非本库内容")
            records.append(_copy_target_for_rollback(old_target, temp_root))
            if new_target != old_target:
                records.append(_copy_target_for_rollback(new_target, temp_root))
            _remove_target(old_target)
            if new_target != old_target:
                _remove_target(new_target)
            _install_projection(_source_dir(new_sid), new_target,
                                AGENTS[agent].get("mode", "symlink"), new_manifest, new_sid)
            changed.append(agent)
        return {"changed": changed}
    except Exception:
        for record in reversed(records):
            try:
                _restore_target_record(record)
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def plan_trial(formal_sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    index = load_index()
    formal = index.get(formal_sid)
    if not formal:
        raise ValueError(f"中央库中不存在 skill: {formal_sid}")
    pair = __import__("skillhub.store", fromlist=["version_pair"]).version_pair(index, formal_sid)
    ul_sid = pair.get("ul_sid")
    if not ul_sid or ul_sid not in index:
        raise ValueError("该 skill 没有 ul 版本")
    result = {}
    for agent in agents:
        if agent not in AGENTS:
            result[agent] = [{"type": "error", "detail": "未知 agent"}]
            continue
        target = target_path(agent, formal)
        owner = _raw_projection_sid(target)
        result[agent] = [{"type": "trial", "target": str(target),
                          "from": owner or "none", "to": ul_sid}]
    return result


@locked
def apply_trial(formal_sid: str, agents: List[str], *, enabled: bool = True,
                force: bool = False) -> Dict[str, List[dict]]:
    """为指定 agent 切换 formal/ul；未指定 agent 的投影保持原样。"""
    index = load_index()
    formal = index.get(formal_sid)
    if not formal:
        raise ValueError(f"中央库中不存在 skill: {formal_sid}")
    from . import store
    pair = store.version_pair(index, formal_sid)
    ul_sid = pair.get("ul_sid")
    if not ul_sid or ul_sid not in index:
        raise ValueError("该 skill 没有 ul 版本")
    ul = index[ul_sid]
    requested = set(agents)
    if any(agent not in AGENTS for agent in requested):
        raise ValueError("包含未知 agent")
    current_trial = set(formal.get("trial_agents", []))
    desired_trial = (current_trial | requested) if enabled else (current_trial - requested)
    all_agents = sorted(current_trial | requested)
    ts = backup_timestamp()
    temp_root = Path(tempfile.mkdtemp(prefix=".trial-", dir=BACKUP_DIR.parent))
    records = []
    try:
        for agent in all_agents:
            target = target_path(agent, formal)
            owner = _raw_projection_sid(target)
            wanted = ul_sid if agent in desired_trial else formal_sid
            if owner is None:
                continue
            if owner not in {formal_sid, ul_sid}:
                if not force:
                    raise ValueError(f"{agent} 上存在非本库内容")
                continue
            state = projection_state(target, owner, manifest=index.get(owner))
            if state in {"copy_drift", "store_drift"} and not force:
                raise ValueError(f"{agent} 投影存在漂移，不能切换")
            records.append(_copy_target_for_rollback(target, temp_root))
            _remove_target(target)
            _install_projection(_source_dir(wanted), target,
                                AGENTS[agent].get("mode", "symlink"), index[wanted], wanted)
        formal["trial_agents"] = sorted(desired_trial)
        if not formal["trial_agents"]:
            formal.pop("trial_agents", None)
        index[formal_sid] = formal
        store.save_index(index)
        audit = getattr(store, "audit_log", None)
        if audit:
            audit("trial_switch", sid=formal_sid,
                  detail={"agents": sorted(requested), "enabled": enabled})
        return {agent: [{"type": "trial", "target": str(target_path(agent, formal)),
                         "enabled": agent in desired_trial}]
                for agent in requested}
    except Exception:
        for record in reversed(records):
            try:
                _restore_target_record(record)
            except Exception:
                pass
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def plan_publish(ul_sid: str) -> dict:
    from . import store
    safe_component(ul_sid)
    index = load_index()
    ul = index.get(ul_sid)
    if not ul or ul.get("channel") != store.UL_ROLE:
        raise ValueError("指定版本不是 ul")
    formal_sid = ul.get("formal_sid")
    formal = index.get(formal_sid) if isinstance(formal_sid, str) else None
    if not formal:
        raise ValueError("ul 没有有效的正式版关系")
    if _central_state(ul_sid, ul) != "ok" or _central_state(formal_sid, formal) != "ok":
        raise ValueError("正式版或 ul 中央副本存在漂移")
    diagnosis = store.diagnose_skill(ul_sid)
    if diagnosis["blocking"]:
        raise ValueError("依赖诊断存在缺失或待人工确认项，不能发布")
    affected = []
    for agent in AGENTS:
        target = target_path(agent, formal)
        owner = _raw_projection_sid(target)
        if owner in {formal_sid, ul_sid}:
            state = projection_state(target, owner, manifest=index.get(owner))
            if state in {"copy_drift", "store_drift", "conflict"}:
                raise ValueError(f"{agent} 投影状态为 {state}，不能发布")
            affected.append({"agent": agent, "target": str(target), "owner": owner})
    new_formal_sid = store.skill_id_for(ul.get("name", ""), ul.get("md5", ""))
    return {"mode": "plan", "formal_sid": formal_sid, "ul_sid": ul_sid,
            "new_formal_sid": new_formal_sid, "affected": affected,
            "diagnostics": diagnosis}


@locked
def publish_ul(ul_sid: str, *, apply: bool = False) -> dict:
    """把 ul 原子提升为正式版，只备份/恢复该逻辑 skill 与其投影。"""
    from . import store
    if not apply:
        return plan_publish(ul_sid)
    plan = plan_publish(ul_sid)
    formal_sid = plan["formal_sid"]
    index_before = json.loads(json.dumps(load_index(), ensure_ascii=False))
    formal_before = index_before[formal_sid]
    ul_before = index_before[ul_sid]
    groups_existed, groups_raw = store._groups_snapshot()
    distribution_existed, distribution_raw = store._distribution_snapshot()
    temp_root = Path(tempfile.mkdtemp(prefix=".publish-", dir=BACKUP_DIR.parent))
    records = []
    backup_ts = backup_timestamp()
    related_backup = safe_path(BACKUP_DIR, backup_ts)
    trash_result = None
    restore_failures = []
    try:
        # 这是独立的 skill 级备份，不包含整个中央库，避免回滚时覆盖无关项。
        related_backup.mkdir(parents=True, exist_ok=False)
        related_store = related_backup / "related" / "store"
        related_store.mkdir(parents=True, exist_ok=True)
        for sid in {formal_sid, ul_sid}:
            src = _source_dir(sid)
            shutil.copytree(src, related_store / sid, symlinks=True)
        atomic_write(related_backup / "related" / "index.json",
                     json.dumps({formal_sid: formal_before, ul_sid: ul_before},
                                ensure_ascii=False, indent=2) + "\n")
        if groups_existed and groups_raw is not None:
            atomic_write(related_backup / "related" / "groups.json", groups_raw)
        if distribution_existed and distribution_raw is not None:
            atomic_write(related_backup / "related" / "distributions.json", distribution_raw)
        atomic_write(related_backup / "snapshot.json", json.dumps({
            "schema_version": 1, "kind": "skill_publish", "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "formal_sid": formal_sid, "ul_sid": ul_sid,
            "new_formal_sid": plan["new_formal_sid"],
            "affected_agents": [row["agent"] for row in plan["affected"]],
            "groups_exists": groups_existed}, ensure_ascii=False, indent=2) + "\n")
        for row in plan["affected"]:
            target = Path(row["target"])
            backup_target(backup_ts, target, conflict=False)
            records.append(_copy_target_for_rollback(target, temp_root))
        trash_result = store.trash_skill(formal_sid, reason="publish", remove=True)
        index = load_index()
        promoted = index.pop(ul_sid)
        new_formal_sid = plan["new_formal_sid"]
        old_ul_path = _source_dir(ul_sid)
        new_formal_path = _source_dir(new_formal_sid)
        if new_formal_sid != ul_sid:
            if new_formal_path.exists() or new_formal_path.is_symlink():
                raise ValueError(f"发布后的正式版 id 已存在: {new_formal_sid}")
            old_ul_path.rename(new_formal_path)
        promoted["channel"] = store.FORMAL_ROLE
        promoted["version_role"] = store.FORMAL_ROLE
        promoted.pop("formal_sid", None)
        promoted.pop("ul_sid", None)
        promoted["id"] = new_formal_sid
        promoted["published_at"] = store._now_iso()
        # 发布后原 ul 消失，旧的试用标记不能继续把新正式投影伪装成
        # trial；需要新一轮试用时再创建 ul 并显式选择 agent。
        promoted.pop("trial_agents", None)
        index[new_formal_sid] = promoted
        store.save_index(index)
        store._replace_group_member(formal_sid, new_formal_sid)
        store._replace_distribution_sids({formal_sid: new_formal_sid,
                                          ul_sid: new_formal_sid})
        for row in plan["affected"]:
            agent = row["agent"]
            target = target_path(agent, promoted)
            _remove_target(target)
            _install_projection(_source_dir(new_formal_sid), target,
                                AGENTS[agent].get("mode", "symlink"), promoted, new_formal_sid)
        store.audit_log("publish", sid=new_formal_sid,
                        detail={"old_formal_sid": formal_sid,
                                "affected_agents": [r["agent"] for r in plan["affected"]]})
        prune_backups()
        return {**plan, "mode": "apply", "old_formal_sid": formal_sid,
                "new_formal_sid": new_formal_sid, "trash": trash_result}
    except Exception as exc:
        for record in reversed(records):
            try:
                _restore_target_record(record)
            except Exception as restore_exc:
                restore_failures.append(f"projection {record.get('target')}: {restore_exc}")
        try:
            current = load_index()
            # 合并恢复：只替换本次 transaction 涉及的正式/ul 记录，保留
            # 期间可能由其它逻辑产生的无关条目。
            for sid in {formal_sid, ul_sid, plan["new_formal_sid"]}:
                current.pop(sid, None)
            current.update({formal_sid: formal_before, ul_sid: ul_before})
            store.save_index(current)
            formal_path = _source_dir(formal_sid)
            if not formal_path.exists():
                saved = related_backup / "related" / "store" / formal_sid
                shutil.copytree(saved, formal_path, symlinks=True)
            new_path = _source_dir(plan["new_formal_sid"])
            if plan["new_formal_sid"] != ul_sid and new_path.exists():
                shutil.rmtree(new_path, ignore_errors=True)
            ul_path = _source_dir(ul_sid)
            if not ul_path.exists():
                saved = related_backup / "related" / "store" / ul_sid
                shutil.copytree(saved, ul_path, symlinks=True)
        except Exception as restore_exc:
            restore_failures.append(f"central skill: {restore_exc}")
        try:
            store._restore_groups_snapshot(groups_existed, groups_raw)
        except Exception as restore_exc:
            restore_failures.append(f"groups: {restore_exc}")
        try:
            store._restore_distribution_snapshot(distribution_existed, distribution_raw)
        except Exception as restore_exc:
            restore_failures.append(f"distribution sources: {restore_exc}")
        store.audit_log("publish", sid=ul_sid, status="failed",
                        detail={"error": str(exc), "restore_failures": restore_failures})
        if restore_failures:
            raise ValueError(f"发布失败，且部分恢复失败: {'; '.join(restore_failures)}") from exc
        raise
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


@locked
def apply_link(sid: str, agents: List[str], force: bool = False,
               backup: bool = True, ts: Optional[str] = None,
               allow_risky: bool = False) -> Dict[str, List[dict]]:
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    src = _source_dir(sid)
    if not src.is_dir() or src.is_symlink():
        raise ValueError("中央库副本缺失或不是安全目录")
    for agent in agents:
        if agent not in AGENTS:
            raise ValueError(f"未知 agent: {agent}")
    ts = ts or backup_timestamp()
    results: Dict[str, List[dict]] = {}
    snapshot_done = False
    for agent in agents:
        effective_sid, effective_manifest = _effective_projection(agent, sid, manifest, index)
        target = target_path(agent, manifest)
        state = projection_state(target, effective_sid, manifest=effective_manifest)
        if state in {"linked", "copy"}:
            results[agent] = [{"type": "skip", "target": str(target), "detail": f"已投影({state})"}]
            continue
        if state == "store_drift" or _central_state(effective_sid, effective_manifest) == "store_drift":
            results[agent] = [{"type": "store_drift", "target": str(target),
                               "detail": "中央库文件已偏离索引摘要，拒绝继续投影"}]
            continue
        if state in {"broken", "copy_broken"}:
            results[agent] = [{"type": "broken", "target": str(target), "detail": "中央库副本缺失，需先修复"}]
            continue
        if state in {"conflict", "copy_drift"} and not force:
            results[agent] = [{"type": state, "target": str(target), "detail": "需 --force 替换，当前内容已保留"}]
            continue
        blocked = _risk_blocked(effective_manifest)
        if blocked and not allow_risky:
            results[agent] = [{"type": "blocked", "target": str(target),
                               "detail": f"风险门禁: {','.join(blocked)}，需 --allow-risky 放行"}]
            continue
        if backup and not snapshot_done:
            _backup_all(ts)
            snapshot_done = True
        if not backup and not _metadata_path(safe_path(BACKUP_DIR, ts)).exists():
            _backup_all(ts)
        backup_target(ts, target, conflict=state in {"conflict", "copy_drift"})
        if state in {"conflict", "copy_drift"}:
            _remove_target(target)
        try:
            _install_projection(_source_dir(effective_sid), target,
                                AGENTS[agent].get("mode", "symlink"),
                                effective_manifest, effective_sid)
            results[agent] = [{"type": "link", "target": str(target),
                               "detail": f"{AGENTS[agent].get('mode', 'symlink')} -> store/{effective_sid}"}]
        except Exception as exc:
            results[agent] = [{"type": "error", "target": str(target), "detail": str(exc)}]
    if snapshot_done:
        prune_backups()
    return results


@locked
def apply_link_batch(sids: List[str], agents: List[str], force: bool = False,
                     allow_risky: bool = False) -> Dict[str, List[dict]]:
    ts = backup_timestamp()
    merged: Dict[str, List[dict]] = {}
    # 快照只创建一次；若所有条目都 skip，仍是显式 apply 的可回滚点。
    _backup_all(ts)
    for sid in sids:
        results = apply_link(sid, agents, force=force, backup=False,
                             ts=ts, allow_risky=allow_risky)
        for agent, actions in results.items():
            merged.setdefault(agent, []).extend(actions)
    prune_backups()
    return merged


def plan_unlink(sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    plan: Dict[str, List[dict]] = {}
    for agent in agents:
        if agent not in AGENTS:
            plan[agent] = [{"type": "error", "target": agent, "detail": "未知 agent"}]
            continue
        effective_sid, effective_manifest = _effective_projection(agent, sid, manifest, index)
        target = target_path(agent, manifest)
        state = projection_state(target, effective_sid, manifest=effective_manifest)
        if state == "store_drift":
            plan[agent] = [{"type": "store_drift", "target": str(target),
                            "detail": "中央库文件已偏离索引摘要，不自动删除投影"}]
        elif state in {"linked", "copy", "broken", "copy_broken"}:
            plan[agent] = [{"type": "unlink", "target": str(target), "detail": "移除本库投影"}]
        elif state == "copy_drift":
            plan[agent] = [{"type": "copy_drift", "target": str(target),
                            "detail": "copy 内容已修改，默认不删除；需 --force 明确替换"}]
        elif state == "conflict":
            plan[agent] = [{"type": "conflict", "target": str(target), "detail": "非本库投影，不动"}]
        else:
            plan[agent] = [{"type": "skip", "target": str(target), "detail": "不存在"}]
    return plan


@locked
def apply_unlink(sid: str, agents: List[str], force: bool = False) -> Dict[str, List[dict]]:
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    results: Dict[str, List[dict]] = {}
    for agent in agents:
        if agent not in AGENTS:
            results[agent] = [{"type": "error", "target": agent, "detail": "未知 agent"}]
            continue
        effective_sid, effective_manifest = _effective_projection(agent, sid, manifest, index)
        target = target_path(agent, manifest)
        state = projection_state(target, effective_sid, manifest=effective_manifest)
        if state == "store_drift":
            results[agent] = [{"type": "store_drift", "target": str(target),
                               "detail": "中央库文件已偏离索引摘要，当前未删除"}]
            continue
        if state == "copy_drift" and not force:
            results[agent] = [{"type": "copy_drift", "target": str(target),
                               "detail": "copy 内容已修改，当前未删除；需 --force"}]
            continue
        if state not in {"linked", "copy", "broken", "copy_broken", "copy_drift"}:
            results[agent] = [{"type": "skip", "target": str(target), "detail": "非本库投影或不存在"}]
            continue
        ts = backup_timestamp()
        _backup_all(ts)
        backup_target(ts, target, conflict=state == "copy_drift")
        _remove_target(target)
        results[agent] = [{"type": "unlink", "target": str(target), "detail": "已移除"}]
        prune_backups()
    return results


def list_backups() -> List[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted([p for p in BACKUP_DIR.iterdir() if p.is_dir()], reverse=True)


def backup_info(backup: Path) -> dict:
    metadata = _read_json(_metadata_path(backup), {})
    size = metadata.get("logical_size") if isinstance(metadata, dict) else None
    if size is None:
        size = _dir_size(backup)
    skill_complete = ((backup / "store").is_dir() and
                      (not metadata or metadata.get("index_exists", True) ==
                       (backup / "index.json").is_file()))
    mcp_complete = (backup / "mcp" / "targets.json").is_file()
    related_complete = (isinstance(metadata, dict) and metadata.get("kind") == "skill_publish"
                        and (backup / "related" / "index.json").is_file())
    return {"ts": backup.name, "size": size, "has_conflicts": has_conflicts(backup),
            "complete": skill_complete or mcp_complete or related_complete,
            "kind": "skill_publish" if related_complete else
                    "skill+mcp" if skill_complete and mcp_complete else
                    "skill" if skill_complete else "mcp" if mcp_complete else "unknown"}


def plan_cleanup(keep: int = 3) -> List[dict]:
    if keep < 1:
        raise ValueError("至少保留一份完整快照")
    plan = []
    for idx, backup in enumerate(list_backups()):
        info = backup_info(backup)
        size = info["size"] if info["size"] is not None else _dir_size(backup)
        if idx < keep:
            action = "keep"
            detail = "保留完整快照 (可 rollback)"
        elif info["has_conflicts"]:
            action = "prune_store"
            detail = "删 store/index，保留 conflicts/ 目标副本"
        else:
            action = "remove"
            detail = "无冲突目标，整体删除"
        plan.append({"type": action, "ts": backup.name, "size": size,
                     "logical_size": size, "detail": detail})
    return plan


@locked
def apply_cleanup(keep: int = 3) -> List[dict]:
    plan = plan_cleanup(keep)
    for action in plan:
        backup = safe_path(BACKUP_DIR, action["ts"])
        if action["type"] == "prune_store":
            if (backup / "store").exists():
                shutil.rmtree(backup / "store")
            if (backup / "index.json").exists():
                (backup / "index.json").unlink()
        elif action["type"] == "remove":
            shutil.rmtree(backup)
    return plan


def status() -> Dict[str, List[dict]]:
    index = load_index()
    out: Dict[str, List[dict]] = {agent: [] for agent in AGENTS}
    for sid, manifest in index.items():
        central = _central_state(sid, manifest)
        for agent in AGENTS:
            target = target_path(agent, manifest)
            state = projection_state(target, sid, central=central, manifest=manifest)
            trial_agents = set(manifest.get("trial_agents", []))
            if manifest.get("channel") == "formal" and agent in trial_agents:
                # 同一路径当前实际使用的是 ul；把 formal 显示为 trial，
                # 不把用户误导为正式版仍在生效。
                state = "trial"
            if state == "copy":
                state = "linked"
            out[agent].append({"sid": sid, "name": manifest["name"], "state": state})
    return out


def doctor() -> dict:
    index = load_index()
    states = status()
    findings = {"broken": [], "store_drift": [], "orphan_store": [], "copy_drift": [],
                "conflict": [], "duplicate_names": {}, "missing_dirs": []}
    for agent, items in states.items():
        for item in items:
            if item["state"] in {"broken", "copy_broken"}:
                findings["broken"].append({"agent": agent, **item})
            elif item["state"] == "store_drift":
                findings["store_drift"].append({"agent": agent, **item})
            elif item["state"] == "copy_drift":
                findings["copy_drift"].append({"agent": agent, **item})
            elif item["state"] == "conflict":
                findings["conflict"].append({"agent": agent, **item})
    if STORE_DIR.exists() and not STORE_DIR.is_symlink():
        for item in sorted(STORE_DIR.iterdir()):
            if item.is_dir() and item.name not in index:
                findings["orphan_store"].append({"sid": item.name})
    by_name: Dict[str, List[str]] = {}
    for sid, manifest in index.items():
        by_name.setdefault(manifest.get("name", ""), []).append(sid)
    findings["duplicate_names"] = {name: sorted(sids) for name, sids in sorted(by_name.items())
                                    if len(sids) > 1}
    for agent, cfg in AGENTS.items():
        if not cfg["skill_dir"].exists():
            findings["missing_dirs"].append({"agent": agent, "dir": str(cfg["skill_dir"])})
    findings["summary"] = {
        "skills": len(index),
        "broken": len(findings["broken"]),
        "store_drift": len(findings["store_drift"]),
        "conflict": len(findings["conflict"]),
        "orphan_store": len(findings["orphan_store"]),
        "copy_drift": len(findings["copy_drift"]),
        "duplicate_names": len(findings["duplicate_names"]),
        "missing_dirs": len(findings["missing_dirs"]),
    }
    return findings


def distribution_sids(group_ids: Optional[List[str]] = None,
                      sids: Optional[List[str]] = None) -> list[str]:
    from . import store
    index = store.load_index()
    groups = store.load_groups()["groups"]
    wanted = []
    for sid in sids or []:
        safe_component(sid)
        if sid not in index:
            raise ValueError(f"分发选择了不存在的 skill: {sid}")
        wanted.append(sid)
    for gid in group_ids or []:
        safe_component(gid)
        if gid not in groups:
            raise ValueError(f"不存在的分组: {gid}")
        wanted.extend(groups[gid].get("members", []))
    return sorted(set(wanted))


def _distribution_sources_for_agents(agents: List[str], *,
                                     group_ids: Optional[List[str]],
                                     sids: Optional[List[str]],
                                     replace: Optional[bool]) -> tuple[dict, bool]:
    """Resolve one source selection per agent.

    Passing both source lists as ``None`` means "use the saved source for
    each agent".  Passing either list (including an empty list) is an
    explicit selection that is saved by ``apply_distribution``.  This keeps
    a multi-agent call from silently applying one agent's saved choices to
    another agent.
    """
    from . import store

    if not isinstance(agents, list) or not agents:
        raise ValueError("agents 必须是非空数组")
    if any(agent not in AGENTS for agent in agents):
        raise ValueError("包含未知 agent")
    use_saved = group_ids is None and sids is None
    saved = store.load_distribution_sources()["agents"] if use_saved else {}
    resolved = {}
    for agent in dict.fromkeys(agents):
        if use_saved:
            raw = saved.get(agent, {})
            selected_groups = list(raw.get("groups", []))
            selected_sids = list(raw.get("sids", []))
            selected_replace = (bool(raw.get("replace", False))
                                if replace is None else bool(replace))
        else:
            selected_groups = list(group_ids or [])
            selected_sids = list(sids or [])
            selected_replace = bool(replace) if replace is not None else False
        desired = distribution_sids(selected_groups, selected_sids)
        resolved[agent] = {
            "groups": selected_groups,
            "sids": selected_sids,
            "replace": selected_replace,
            "desired": desired,
        }
    return resolved, use_saved


def plan_distribution(agents: List[str], *, group_ids: Optional[List[str]] = None,
                       sids: Optional[List[str]] = None, replace: Optional[bool] = None,
                       force: bool = False, allow_risky: bool = False) -> dict:
    sources, use_saved = _distribution_sources_for_agents(
        agents, group_ids=group_ids, sids=sids, replace=replace)
    index = load_index()
    result = {agent: {"add": [], "remove": [], "actions": []} for agent in agents}
    desired_by_agent = {}
    replace_by_agent = {}
    for agent in agents:
        source = sources[agent]
        desired = source["desired"]
        desired_by_agent[agent] = desired
        replace_by_agent[agent] = source["replace"]
        # A formal and its UL share a target path.  Deduplicate requests by
        # target/effective owner so a trial formal selection cannot cause a
        # second request for the same target.
        desired_requests = []
        desired_targets = set()
        for sid in desired:
            manifest = index[sid]
            target_key = str(target_path(agent, manifest).absolute())
            effective_sid, _ = _effective_projection(agent, sid, manifest, index)
            if target_key in desired_targets:
                continue
            desired_targets.add(target_key)
            desired_requests.append(sid)

        owned = {}
        seen_targets = set()
        for sid, manifest in index.items():
            target = target_path(agent, manifest)
            target_key = str(target.absolute())
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)
            owner = _raw_projection_sid(target)
            if owner in index:
                owned[target_key] = owner
        for sid in desired_requests:
            plan = plan_link(sid, [agent], force=force, allow_risky=allow_risky)[agent]
            result[agent]["actions"].extend(plan)
            forceable = (force and plan and
                         all(item.get("type") in {"conflict", "copy_drift"}
                             for item in plan))
            if any(item.get("type") in {"link", "skip"} for item in plan) or forceable:
                result[agent]["add"].append(sid)
        if source["replace"]:
            for target_key, sid in sorted(owned.items(), key=lambda item: item[1]):
                if target_key in desired_targets:
                    continue
                plan = plan_unlink(sid, [agent])[agent]
                result[agent]["actions"].extend(plan)
                result[agent]["remove"].append(sid)
    all_desired = sorted({sid for values in desired_by_agent.values() for sid in values})
    same_desired = len({tuple(values) for values in desired_by_agent.values()}) == 1
    same_replace = len(set(replace_by_agent.values())) == 1
    return {"mode": "plan", "desired": (next(iter(desired_by_agent.values()))
                                           if same_desired else all_desired),
            "desired_by_agent": desired_by_agent,
            "replace": (next(iter(replace_by_agent.values()))
                         if same_replace else None),
            "replace_by_agent": replace_by_agent,
            "source_by_agent": {agent: {"groups": source["groups"],
                                         "sids": source["sids"],
                                         "replace": source["replace"]}
                                for agent, source in sources.items()},
            "source_mode": "saved" if use_saved else "explicit",
            "agents": result}


@locked
def apply_distribution(agents: List[str], *, group_ids: Optional[List[str]] = None,
                       sids: Optional[List[str]] = None, replace: Optional[bool] = None,
                       force: bool = False, allow_risky: bool = False) -> dict:
    plan = plan_distribution(agents, group_ids=group_ids, sids=sids,
                             replace=replace, force=force, allow_risky=allow_risky)
    bad = {"error", "blocked", "broken", "store_drift", "conflict", "copy_drift"}
    if any(item.get("type") in bad and not (force and item.get("type") in {"conflict", "copy_drift"})
           for rows in plan["agents"].values() for item in rows["actions"]):
        raise ValueError("分发预检未通过，未修改任何 agent")
    linked = {}
    for agent, rows in plan["agents"].items():
        additions = rows["add"]
        linked[agent] = (apply_link_batch(additions, [agent], force=force,
                                          allow_risky=allow_risky).get(agent, [])
                         if additions else [])
    removed = {}
    for agent, rows in plan["agents"].items():
        if plan["replace_by_agent"][agent]:
            removed[agent] = []
            for sid in rows["remove"]:
                current = apply_unlink(sid, [agent], force=force)
                removed[agent].extend(current.get(agent, []))
        else:
            removed[agent] = []
    # Explicit selections become the durable source of truth only after all
    # projection actions have passed their preflight.  A call with no source
    # lists reuses the already saved per-agent choices and leaves them intact.
    if group_ids is not None or sids is not None:
        from . import store
        saved = store.set_distribution_sources(
            agents, list(group_ids or []), list(sids or []),
            replace=bool(replace) if replace is not None else False)
    else:
        saved = None
    return {"mode": "apply", "desired": plan["desired"], "linked": linked,
            "desired_by_agent": plan["desired_by_agent"],
            "removed": removed, "saved_sources": saved, "plan": plan}
