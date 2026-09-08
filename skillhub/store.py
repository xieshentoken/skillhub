"""中央库：保存 skill 的完整、可验证的权威副本。

中央库文件只在显式写操作中创建。索引损坏会抛错，避免把真实数据误当
成空索引覆盖；所有目录名都按单一路径组件处理，避免 ``..`` 和父级
软链接把写入带出允许的根目录。
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager, nullcontext
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Dict, Iterator, List, Optional

from .config import (AGENTS, DISTRIBUTIONS_FILE, GROUPS_FILE, INDEX_FILE,
                     LOG_FILE, MANIFEST_NAME, SETTINGS_FILE, STORE_DIR, TRASH_DIR,
                     TRASH_RETENTION_DAYS)


EXCLUDED_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv",
                 "dist", ".cache"}
MARKER_NAME = ".skillhub-projection.json"
FORMAL_ROLE = "formal"
UL_ROLE = "ul"
CANDIDATE_ROLE = "candidate"
TRASH_METADATA = "metadata.json"


class CorruptIndexError(ValueError):
    """中央索引不是可安全使用的 JSON 对象。"""


_WRITE_LOCK = threading.RLock()
_LOCK_STATE = threading.local()


def safe_component(value: str) -> str:
    """验证外部名称、分类和 ID 是一个安全的路径组件。"""
    if not isinstance(value, str) or not value:
        raise ValueError("名称必须是非空字符串")
    if value in (".", ".."):
        raise ValueError("名称不能是 . 或 ..")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("名称不能包含控制字符")
    if "/" in value or "\\" in value:
        raise ValueError("名称不能包含路径分隔符")
    # 在 macOS/Linux 上 Path.is_absolute() 不识别 Windows 盘符；配置和
    # zip 可能跨平台传递，因此额外拒绝常见的 Windows 绝对路径形式。
    if Path(value).is_absolute() or value.startswith(("/", "\\")):
        raise ValueError("名称不能是绝对路径")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("名称不能是 Windows 绝对路径")
    return value


def _within(root: Path, target: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def safe_path(root: Path, *parts: str, projection: bool = False) -> Path:
    """拼接受信根目录下的路径，并检查已有父级软链接。

    ``projection=True`` 允许最终目标本身是冲突软链接，以便调用方把它
    当作冲突备份并删除；最终目标的父级仍必须留在根目录内。
    """
    root = Path(root)
    for part in parts:
        safe_component(part)
    target = root.joinpath(*parts)
    try:
        boundary = root.resolve(strict=False)
        check = target.parent if projection else target
        resolved = check.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValueError("目标路径存在无法解析的软链接") from exc
    if not _within(boundary, resolved):
        raise ValueError("目标路径超出允许目录")
    # resolve() 已经检测了父级软链接的最终位置；逐级检查可以让错误更
    # 明确，也防止一个尚不存在的最终组件掩盖中间的外部软链接。
    current = root
    for part in parts[:-1] if projection else parts:
        current = current / part
        if current.is_symlink():
            try:
                resolved_current = current.resolve(strict=False)
            except (OSError, RuntimeError) as exc:
                raise ValueError("目标路径存在无法解析的父级软链接") from exc
            if not _within(boundary, resolved_current):
                raise ValueError("目标路径的父级软链接指向允许目录之外")
    return target


@contextmanager
def write_lock():
    """同一进程线程锁 + Unix 进程锁；锁文件只在写路径创建。"""
    with _WRITE_LOCK:
        depth = getattr(_LOCK_STATE, "depth", 0)
        if depth:
            _LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _LOCK_STATE.depth -= 1
            return
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = open(INDEX_FILE.parent / ".write.lock", "a", encoding="utf-8")
        try:
            try:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - Windows fallback
                pass
            _LOCK_STATE.depth = 1
            try:
                yield
            finally:
                _LOCK_STATE.depth = 0
                try:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except ImportError:  # pragma: no cover
                    pass
        finally:
            handle.close()


def locked(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with write_lock():
            return fn(*args, **kwargs)
    return wrapper


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    """在同一目录用唯一临时文件写入并原子替换。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".skillhub-tmp-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            raw = data.encode("utf-8") if isinstance(data, str) else data
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def ensure_home() -> None:
    """仅供写操作调用；只读函数不得借此创建真实目录。"""
    INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
    STORE_DIR.mkdir(parents=True, exist_ok=True)


def _validate_index(index: object) -> Dict[str, dict]:
    if not isinstance(index, dict):
        raise CorruptIndexError("skill 索引必须是 JSON 对象")
    for sid, manifest in index.items():
        try:
            safe_component(sid)
        except ValueError as exc:
            raise CorruptIndexError(f"索引含不安全 skill id: {sid!r}") from exc
        if not isinstance(manifest, dict):
            raise CorruptIndexError(f"skill {sid!r} 的 manifest 必须是对象")
        name = manifest.get("name")
        if not isinstance(name, str):
            raise CorruptIndexError(f"skill {sid!r} 缺少有效 name")
        try:
            safe_component(name)
            category = manifest.get("category")
            if category:
                safe_component(category)
            for key in ("logical_id", "formal_sid", "ul_sid"):
                if key in manifest and manifest[key] is not None:
                    safe_component(manifest[key])
            if manifest.get("channel") not in {None, FORMAL_ROLE, UL_ROLE, CANDIDATE_ROLE}:
                raise ValueError("未知版本通道")
            trial_agents = manifest.get("trial_agents")
            if trial_agents is not None:
                if (not isinstance(trial_agents, list) or
                        not all(isinstance(agent, str) and agent in AGENTS
                                for agent in trial_agents) or
                        len(set(trial_agents)) != len(trial_agents)):
                    raise ValueError("trial_agents 必须是不重复的已知 agent 数组")
        except ValueError as exc:
            raise CorruptIndexError(f"skill {sid!r} 的 name/category 不安全") from exc
    return index


def load_index() -> Dict[str, dict]:
    """读取索引；缺失返回空对象，但损坏索引明确报错且不创建目录。"""
    if not INDEX_FILE.exists():
        return {}
    try:
        raw = INDEX_FILE.read_text(encoding="utf-8")
        return _validate_index(json.loads(raw))
    except CorruptIndexError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptIndexError(f"skill 索引损坏，未读取或覆盖: {INDEX_FILE}: {exc}") from exc


@locked
def save_index(index: Dict[str, dict]) -> None:
    _validate_index(index)
    ensure_home()
    atomic_write(INDEX_FILE, json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def skill_id_for(name: str, md5: str) -> str:
    safe_component(name)
    safe_component(md5[:8])
    return f"{name}--{md5[:8]}"


def ul_id_for(name: str, md5: str) -> str:
    """ul 使用同一内容 ID 基础再加角色后缀，避免与 formal 共用目录。

    基础 content id 仍严格是 ``<name>--<md5前8>``；后缀只表示同一
    内容的独立试用副本，不参与文件摘要。
    """
    base = skill_id_for(name, md5)
    safe_component(base + "--ul")
    return base + "--ul"


def skill_files(root: Path) -> Iterator[Path]:
    """枚举完整有效文件集。

    skill 包内部不接受软链接：这样不会把 agent 目录外的密钥或其它
    文件跟随复制进中央库。skill 根目录本身可以是 agent 的投影软链接，
    但其内部必须是普通文件/目录。
    """
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"skill 目录不存在: {root}")
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs[:] = sorted(dirs)
        for dirname in list(dirs):
            item = Path(directory) / dirname
            if dirname in EXCLUDED_DIRS:
                dirs.remove(dirname)
                continue
            if item.is_symlink():
                raise ValueError(f"skill 内部不允许符号链接: {item}")
        for filename in sorted(files):
            if filename in {MARKER_NAME, ".DS_Store"}:
                continue
            item = Path(directory) / filename
            if item.is_symlink():
                raise ValueError(f"skill 内部不允许符号链接: {item}")
            if not stat.S_ISREG(item.stat().st_mode):
                raise ValueError(f"skill 包含非普通文件: {item}")
            yield item


def file_summary(root: Path) -> dict:
    """返回完整文件集的稳定摘要、逻辑大小和逐文件摘要。"""
    digest = hashlib.md5()
    total_size = 0
    files: List[dict] = []
    for item in sorted(skill_files(root), key=lambda p: p.relative_to(root).as_posix()):
        data = item.read_bytes()
        rel = item.relative_to(root).as_posix()
        mode = item.stat().st_mode & 0o111
        file_md5 = hashlib.md5(data).hexdigest()
        rel_bytes = rel.encode("utf-8")
        digest.update(len(rel_bytes).to_bytes(8, "big"))
        digest.update(rel_bytes)
        digest.update(mode.to_bytes(2, "big"))
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
        total_size += len(data)
        files.append({"path": rel, "size": len(data), "md5": file_md5,
                      "mode": mode})
    return {"md5": digest.hexdigest(), "size": total_size,
            "file_count": len(files), "files": files}


def content_info(root: Path):
    """兼容旧调用方的 ``(md5, size)`` 摘要接口。"""
    info = file_summary(root)
    return info["md5"], info["size"]


def safe_relative(value: str) -> tuple[str, ...]:
    """把 GUI/导入提供的相对文件名限制在一个 skill 根目录内。"""
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("文件路径必须是非空 POSIX 相对路径")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError("文件路径不能越出 skill 目录")
    for part in pure.parts:
        safe_component(part)
    return tuple(pure.parts)


def safe_file_path(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    parts = safe_relative(relative)
    path = safe_path(Path(root), *parts)
    if must_exist and (not path.is_file() or path.is_symlink()):
        raise ValueError("文件不存在或不是普通文件")
    return path


def _manifest_copy(value: dict) -> dict:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _logical_id(manifest: dict) -> str:
    value = manifest.get("logical_id") or manifest.get("name")
    return str(value or manifest.get("id") or "")


def version_pair(index: Dict[str, dict], sid: str) -> dict:
    """返回兼容旧索引的 formal/ul 关系，不修改传入索引。"""
    if sid not in index:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    manifest = index[sid]
    logical = _logical_id(manifest)
    formal = None
    ul = None
    explicit_formal = manifest.get("formal_sid")
    explicit_ul = manifest.get("ul_sid")
    if isinstance(explicit_formal, str) and explicit_formal in index:
        formal = explicit_formal
    if isinstance(explicit_ul, str) and explicit_ul in index:
        ul = explicit_ul
    for candidate, item in index.items():
        if _logical_id(item) != logical:
            continue
        role = item.get("channel") or item.get("version_role")
        if role == UL_ROLE and ul is None:
            ul = candidate
        elif role == FORMAL_ROLE and formal is None:
            formal = candidate
    if formal is None:
        # 老索引没有 channel：本身视为正式版，除非明确指向了另一个 formal。
        formal = sid if manifest.get("channel") != UL_ROLE else None
    if ul is None and manifest.get("channel") == UL_ROLE:
        ul = sid
    if formal is None and ul != sid:
        formal = sid
    return {"logical_id": logical, "formal_sid": formal, "ul_sid": ul}


def related_sids(index: Dict[str, dict], sid: str) -> list[str]:
    pair = version_pair(index, sid)
    return [item for item in (pair.get("formal_sid"), pair.get("ul_sid"))
            if isinstance(item, str) and item in index]


def _link_versions(index: Dict[str, dict], formal_sid: Optional[str],
                   ul_sid: Optional[str], logical_id: Optional[str] = None) -> None:
    """只给一对记录写关系字段；旧字段不会被删除。"""
    if formal_sid and formal_sid in index:
        fm = index[formal_sid]
        fm["channel"] = FORMAL_ROLE
        fm["version_role"] = FORMAL_ROLE
        fm["logical_id"] = logical_id or _logical_id(fm)
        if ul_sid and ul_sid in index:
            fm["ul_sid"] = ul_sid
        else:
            fm.pop("ul_sid", None)
        fm.pop("formal_sid", None)
    if ul_sid and ul_sid in index:
        ul = index[ul_sid]
        ul["channel"] = UL_ROLE
        ul["version_role"] = UL_ROLE
        ul["logical_id"] = logical_id or _logical_id(ul)
        if formal_sid and formal_sid in index:
            ul["formal_sid"] = formal_sid
        else:
            ul.pop("formal_sid", None)
        ul.pop("ul_sid", None)


def _read_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    result = {}
    for line in text[3:end].splitlines():
        match = re.match(r"^([A-Za-z_][\w]*)\s*:\s*(.*)$", line)
        if match:
            result[match.group(1)] = match.group(2).strip().strip("'\"")
    return result


def _refresh_manifest(sid: str, manifest: dict, root: Path) -> dict:
    """根据当前文件集重算摘要，保留来源、关系和未知旧字段。"""
    info = file_summary(root)
    updated = _manifest_copy(manifest)
    updated["schema_version"] = max(int(updated.get("schema_version", 1) or 1), 3)
    updated["id"] = sid
    updated["md5"] = info["md5"]
    updated["size"] = info["size"]
    updated["file_count"] = info["file_count"]
    updated["files"] = info["files"]
    frontmatter = _read_frontmatter(root / "SKILL.md")
    if frontmatter.get("name"):
        safe_component(frontmatter["name"])
        updated["fm_name"] = frontmatter["name"]
    if "description" in frontmatter:
        updated["fm_desc"] = frontmatter.get("description", "")[:60]
    try:
        from .scan import risks_for_skill
        updated["risks"] = risks_for_skill(root)
    except (OSError, ValueError):
        pass
    return updated


def _copytree(src: Path, dst: Path) -> int:
    count = 0
    for item in skill_files(src):
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)
        count += 1
    return count


def _merge_source(index: dict, sid: str, agent: str, path: str) -> None:
    manifest = index[sid]
    sources = manifest.setdefault("sources", [])
    agents = manifest.setdefault("agents", [])
    if agent not in agents:
        agents.append(agent)
    if path not in [s.get("path") for s in sources if isinstance(s, dict)]:
        sources.append({"agent": agent, "path": path})


def import_skill(agent: str, record: dict, apply: bool = False,
                 channel: Optional[str] = None,
                 logical_id: Optional[str] = None) -> Optional[str]:
    """导入完整 skill；``apply=False`` 只计算并验证，不创建目录。

    ``channel`` 是新增的版本提示。未提供时保留旧行为；如果同一逻辑名
    已有正式版，新的不同内容会标成 candidate，而不是制造第二个 formal。
    """
    if not isinstance(record, dict):
        raise ValueError("skill 记录必须是对象")
    name = record.get("name")
    safe_component(name)
    category = record.get("category", "") or ""
    if category:
        safe_component(category)
    path = Path(record.get("path", ""))
    src_dir = path.parent
    info = file_summary(src_dir)
    sid = skill_id_for(name, info["md5"])
    dst_dir = safe_path(STORE_DIR, sid)
    index = load_index()
    existing = index.get(sid)

    if dst_dir.is_symlink() or dst_dir.exists():
        if not dst_dir.is_dir() or dst_dir.is_symlink():
            raise ValueError(f"中央库副本不是安全目录: {dst_dir}")
        if existing is None:
            raise ValueError(f"中央库副本缺少索引记录: {sid}")
        existing_md5 = existing.get("md5")
        actual_md5 = file_summary(dst_dir)["md5"]
        if actual_md5 != info["md5"] or (existing_md5 and actual_md5 != existing_md5):
            raise ValueError("中央副本与索引不一致，不会覆盖既有内容")
        if not apply:
            return sid
        with write_lock():
            index = load_index()
            _merge_source(index, sid, agent, str(path))
            save_index(index)
        return sid

    if not apply:
        return sid

    with write_lock():
        ensure_home()
        # 重新读取索引和摘要，避免预览期间源文件被替换。
        index = load_index()
        info = file_summary(src_dir)
        sid = skill_id_for(name, info["md5"])
        dst_dir = safe_path(STORE_DIR, sid)
        if dst_dir.is_symlink() or dst_dir.exists():
            if dst_dir.is_symlink():
                raise ValueError(f"中央库副本不是安全目录: {dst_dir}")
            if sid not in index:
                raise ValueError(f"中央库副本缺少索引记录: {sid}")
            existing = index[sid]
            actual_md5 = file_summary(dst_dir)["md5"] if dst_dir.is_dir() else ""
            if actual_md5 != info["md5"] or (existing.get("md5") and actual_md5 != existing.get("md5")):
                raise ValueError("中央副本与索引不一致，不会覆盖既有内容")
            _merge_source(index, sid, agent, str(path))
            save_index(index)
            return sid
        with tempfile.TemporaryDirectory(prefix=".import-", dir=STORE_DIR.parent) as tmp:
            stage = Path(tmp) / "skill"
            stage.mkdir()
            _copytree(src_dir, stage)
            if file_summary(stage)["md5"] != info["md5"]:
                raise ValueError("导入期间源文件发生变化")
            stage.rename(dst_dir)
        role = channel if channel in {FORMAL_ROLE, UL_ROLE, CANDIDATE_ROLE} else None
        if role is None and any(item.get("name") == name and
                               (item.get("channel") or FORMAL_ROLE) == FORMAL_ROLE
                               for item in index.values()):
            role = CANDIDATE_ROLE
        if role is None:
            role = FORMAL_ROLE
        logical = logical_id or name
        safe_component(logical)
        manifest = {
            "schema_version": 2,
            "id": sid,
            "name": name,
            "category": category,
            "logical_id": logical,
            "channel": role,
            "version_role": role,
            "source_agent": agent,
            "source_path": str(path),
            "md5": info["md5"],
            "size": info["size"],
            "file_count": info["file_count"],
            "files": info["files"],
            "fm_name": record.get("fm_name", ""),
            "fm_desc": record.get("fm_desc", ""),
            "risks": list(record.get("risks") or []),
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "agents": [agent],
            "sources": [{"agent": agent, "path": str(path)}],
        }
        index[sid] = manifest
        # 只有显式要求作为 ul 时才自动建立 pair；普通导入的同名候选
        # 保持可比较但不改变当前正式/试用关系。
        if role == UL_ROLE:
            pair = version_pair(index, sid)
            formal = pair.get("formal_sid")
            if formal and formal != sid:
                _link_versions(index, formal, sid, logical)
        try:
            save_index(index)
        except Exception:
            shutil.rmtree(dst_dir, ignore_errors=True)
            raise
        return sid


def list_skills(only_risky: bool = False) -> List[dict]:
    skills = sorted(load_index().values(), key=lambda m: m.get("name", ""))
    if only_risky:
        skills = [s for s in skills if s.get("risks")]
    return skills


def get_skill(sid: str) -> Optional[dict]:
    safe_component(sid)
    return load_index().get(sid)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _redact_log(value: Any, key: str = "") -> Any:
    """日志只接受结构化、无凭证的数据。"""
    lowered = key.lower()
    if any(word in lowered for word in ("secret", "token", "password", "credential", "api_key")):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(k): _redact_log(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_log(v, key) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        # 不记录可能包含密钥的长自由文本；路径和摘要足够支持审计。
        if isinstance(value, str) and len(value) > 1000:
            return value[:1000] + "…"
        return value
    return str(value)


def audit_log(event: str, *, sid: str = "", status: str = "ok",
              detail: Optional[dict] = None) -> None:
    safe_component(event)
    safe_component(status)
    ensure_home()
    row = {"at": _now_iso(), "event": event, "status": status}
    if sid:
        row["sid"] = sid
    if detail:
        row["detail"] = _redact_log(detail)
    with open(LOG_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_audit_log(limit: int = 200) -> list[dict]:
    if not LOG_FILE.is_file():
        return []
    try:
        rows = []
        for line in LOG_FILE.read_text(encoding="utf-8").splitlines()[-max(1, min(limit, 1000)):]:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows
    except (OSError, UnicodeError):
        return []


def load_settings() -> dict:
    if not SETTINGS_FILE.exists():
        return {"trash_retention_days": TRASH_RETENTION_DAYS}
    try:
        value = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptIndexError(f"设置文件损坏，未覆盖: {SETTINGS_FILE}") from exc
    if not isinstance(value, dict):
        raise CorruptIndexError("设置必须是 JSON 对象")
    result = {"trash_retention_days": TRASH_RETENTION_DAYS}
    result.update({key: value[key] for key in value if key in {
        "trash_retention_days", "editor", "ui_page_size"}})
    try:
        result["trash_retention_days"] = max(1, int(result["trash_retention_days"]))
    except (TypeError, ValueError) as exc:
        raise ValueError("回收区保留天数必须是正整数") from exc
    return result


@locked
def save_settings(settings: dict) -> dict:
    if not isinstance(settings, dict):
        raise ValueError("设置必须是对象")
    current = load_settings()
    if "trash_retention_days" in settings:
        try:
            value = max(1, int(settings["trash_retention_days"]))
        except (TypeError, ValueError) as exc:
            raise ValueError("回收区保留天数必须是正整数") from exc
        current["trash_retention_days"] = value
    if "editor" in settings:
        editor = settings["editor"]
        if editor is not None and (not isinstance(editor, str) or len(editor) > 300):
            raise ValueError("编辑器设置无效")
        current["editor"] = editor
    if "ui_page_size" in settings:
        current["ui_page_size"] = max(1, min(200, int(settings["ui_page_size"])))
    ensure_home()
    atomic_write(SETTINGS_FILE, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    audit_log("settings", detail={"keys": sorted(settings)})
    return current


def load_groups() -> dict:
    if not GROUPS_FILE.exists():
        return {"schema_version": 1, "groups": {}}
    try:
        value = json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptIndexError(f"分组文件损坏，未覆盖: {GROUPS_FILE}") from exc
    # 兼容早期实验格式：顶层直接是 group_id -> members。
    if isinstance(value, dict) and "groups" not in value:
        value = {"schema_version": 1, "groups": value}
    if not isinstance(value, dict) or not isinstance(value.get("groups"), dict):
        raise CorruptIndexError("分组文件必须包含 groups 对象")
    groups = {}
    index = load_index()
    for gid, raw in value["groups"].items():
        safe_component(gid)
        if isinstance(raw, list):
            raw = {"name": gid, "members": raw}
        if not isinstance(raw, dict):
            raise CorruptIndexError(f"分组 {gid} 格式无效")
        members = raw.get("members", [])
        if not isinstance(members, list) or any(not isinstance(s, str) for s in members):
            raise CorruptIndexError(f"分组 {gid} members 格式无效")
        clean = []
        for sid in dict.fromkeys(members):
            safe_component(sid)
            if sid in index:
                clean.append(sid)
        groups[gid] = {
            "name": str(raw.get("name") or gid),
            "members": clean,
            "kind": "model" if raw.get("kind") == "model" else "manual",
            "description": str(raw.get("description") or "")[:500],
            "updated_at": raw.get("updated_at", ""),
        }
    return {"schema_version": 1, "groups": groups}


def _groups_snapshot() -> tuple[bool, Optional[bytes]]:
    """Capture the central group document without filtering stale members."""
    if not GROUPS_FILE.exists():
        return False, None
    if GROUPS_FILE.is_symlink() or not GROUPS_FILE.is_file():
        raise CorruptIndexError(f"分组文件不是安全普通文件: {GROUPS_FILE}")
    try:
        raw = GROUPS_FILE.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptIndexError(f"分组文件损坏，未覆盖: {GROUPS_FILE}") from exc
    if isinstance(value, dict) and "groups" not in value:
        value = {"schema_version": 1, "groups": value}
    if not isinstance(value, dict) or not isinstance(value.get("groups"), dict):
        raise CorruptIndexError("分组文件必须包含 groups 对象")
    return True, raw


def _restore_groups_snapshot(existed: bool, raw: Optional[bytes]) -> None:
    """Restore a group document captured by ``_groups_snapshot``."""
    if existed:
        if raw is None:
            raise ValueError("分组快照缺少文件内容")
        atomic_write(GROUPS_FILE, raw)
    elif GROUPS_FILE.is_symlink() or GROUPS_FILE.exists():
        if GROUPS_FILE.is_symlink() or GROUPS_FILE.is_file():
            GROUPS_FILE.unlink()
        else:
            raise ValueError("分组恢复目标不是普通文件")


def _replace_group_member(old_sid: str, new_sid: str) -> bool:
    """Replace one skill id in groups without dropping stale references.

    ``load_groups`` intentionally filters members that are absent from the
    current index.  Version replacement needs the opposite behaviour: the
    old id may be temporarily absent between removing the current version and
    adding the restored/promoted version, so this helper edits the raw
    document and preserves every unrelated group member.
    """
    safe_component(old_sid)
    safe_component(new_sid)
    if old_sid == new_sid or not GROUPS_FILE.exists():
        return False
    existed, raw = _groups_snapshot()
    if not existed or raw is None:
        return False
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:  # pragma: no cover - snapshot validated above
        raise CorruptIndexError("分组文件损坏，未覆盖") from exc
    if "groups" not in value:
        value = {"schema_version": 1, "groups": value}
    changed = False
    for gid, group in value["groups"].items():
        safe_component(gid)
        if isinstance(group, list):
            members = group
        elif isinstance(group, dict):
            members = group.get("members", [])
        else:
            raise CorruptIndexError(f"分组 {gid} 格式无效")
        if not isinstance(members, list) or any(not isinstance(item, str) for item in members):
            raise CorruptIndexError(f"分组 {gid} members 格式无效")
        for item in members:
            safe_component(item)
        if old_sid not in members:
            continue
        replaced = []
        for item in members:
            item = new_sid if item == old_sid else item
            if item not in replaced:
                replaced.append(item)
        if isinstance(group, list):
            value["groups"][gid] = replaced
        else:
            group["members"] = replaced
        changed = True
    if changed:
        atomic_write(GROUPS_FILE, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return changed


@locked
def save_groups(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("groups"), dict):
        raise ValueError("分组必须是 groups 对象")
    index = load_index()
    normalized = {"schema_version": 1, "groups": {}}
    for gid, raw in value["groups"].items():
        safe_component(gid)
        if not isinstance(raw, dict):
            raise ValueError(f"分组 {gid} 格式无效")
        members = raw.get("members", [])
        if not isinstance(members, list):
            raise ValueError(f"分组 {gid} members 必须是数组")
        clean = []
        for sid in dict.fromkeys(members):
            safe_component(sid)
            if sid not in index:
                raise ValueError(f"分组 {gid} 引用了不存在的 skill: {sid}")
            clean.append(sid)
        normalized["groups"][gid] = {
            "name": str(raw.get("name") or gid)[:120],
            "members": clean,
            "kind": "model" if raw.get("kind") == "model" else "manual",
            "description": str(raw.get("description") or "")[:500],
            "updated_at": _now_iso(),
        }
    ensure_home()
    atomic_write(GROUPS_FILE, json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
    audit_log("groups", detail={"groups": sorted(normalized["groups"])})
    return normalized


def update_group(group_id: str, members: list[str], *, name: str = "",
                 kind: str = "manual", description: str = "",
                 apply: bool = False) -> dict:
    safe_component(group_id)
    if kind not in {"manual", "model"}:
        raise ValueError("分组类型无效")
    current = load_groups()
    groups = _manifest_copy(current)["groups"]
    groups[group_id] = {"name": name or group_id, "members": members,
                        "kind": kind, "description": description}
    candidate = {"schema_version": 1, "groups": groups}
    # 先完整校验，即使只是 preview 也不返回不安全关系。
    normalized = save_groups(candidate) if apply else _validate_groups_preview(candidate)
    return {"mode": "apply" if apply else "plan", "group": group_id,
            "groups": normalized}


@locked
def delete_group(group_id: str) -> dict:
    """Delete one central group without changing any skill or projection."""
    safe_component(group_id)
    current = load_groups()
    if group_id not in current["groups"]:
        return {"mode": "apply", "group": group_id, "deleted": False,
                "groups": current["groups"]}
    groups = _manifest_copy(current)["groups"]
    del groups[group_id]
    groups_existed, groups_raw = _groups_snapshot()
    distribution_existed, distribution_raw = _distribution_snapshot()
    try:
        normalized = save_groups({"schema_version": 1, "groups": groups})
        _remove_distribution_group(group_id)
    except Exception:
        _restore_groups_snapshot(groups_existed, groups_raw)
        _restore_distribution_snapshot(distribution_existed, distribution_raw)
        raise
    audit_log("group_delete", detail={"group": group_id})
    return {"mode": "apply", "group": group_id, "deleted": True,
            "groups": normalized["groups"]}


def apply_model_suggestions(suggestions: list[dict], *, confirm: bool = False) -> dict:
    """只接受已校验的模型建议；人工分组和失败结果都不被改写。"""
    if not confirm:
        raise ValueError("应用模型分组建议必须明确确认")
    if not isinstance(suggestions, list) or not suggestions:
        raise ValueError("没有可应用的模型建议")
    current = load_groups()
    groups = _manifest_copy(current)["groups"]
    index = load_index()
    validated = []
    for suggestion in suggestions:
        if not isinstance(suggestion, dict) or suggestion.get("status") not in {"ok", "suggested"}:
            raise ValueError("模型结果失败或状态未知，不会修改当前分组")
        gid = suggestion.get("group_id")
        members = suggestion.get("members")
        name = suggestion.get("name") or gid
        safe_component(gid)
        if not isinstance(members, list) or not all(isinstance(sid, str) and sid in index for sid in members):
            raise ValueError("模型结果包含不存在或不安全的 skill id")
        safe_component(str(name))
        validated.append((gid, {"name": str(name), "members": list(dict.fromkeys(members)),
                                "kind": "model", "description": str(suggestion.get("description") or "")[:500]}))
    for gid, value in validated:
        existing = groups.get(gid)
        if existing and existing.get("kind") == "manual":
            continue
        groups[gid] = value
    result = save_groups({"schema_version": 1, "groups": groups})
    audit_log("model_groups", detail={"groups": [gid for gid, _ in validated]})
    return result


def _validate_groups_preview(value: dict) -> dict:
    index = load_index()
    output = {"schema_version": 1, "groups": {}}
    for gid, raw in value.get("groups", {}).items():
        safe_component(gid)
        if not isinstance(raw, dict) or not isinstance(raw.get("members", []), list):
            raise ValueError(f"分组 {gid} 格式无效")
        members = list(dict.fromkeys(raw.get("members", [])))
        for sid in members:
            safe_component(sid)
            if sid not in index:
                raise ValueError(f"分组 {gid} 引用了不存在的 skill: {sid}")
        output["groups"][gid] = {"name": str(raw.get("name") or gid),
                                  "members": members,
                                  "kind": raw.get("kind", "manual"),
                                  "description": str(raw.get("description") or "")}
    return output


def _normalize_distribution_sources(value: object, *, check_sids: bool = False) -> dict:
    """Validate the per-agent distribution source document.

    The document deliberately stores source selections, not the current
    projection result.  Group membership is resolved only when a
    distribution is planned, so changing a group never changes an agent by
    itself and explicit skill selections remain independent of the group.
    """
    if not isinstance(value, dict) or not isinstance(value.get("agents"), dict):
        raise CorruptIndexError("分发来源文件必须包含 agents 对象")
    index = load_index() if check_sids else None
    agents = {}
    for agent, raw in value["agents"].items():
        if agent not in AGENTS:
            raise CorruptIndexError(f"分发来源包含未知 agent: {agent}")
        if not isinstance(raw, dict):
            raise CorruptIndexError(f"agent {agent} 的分发来源格式无效")
        groups = raw.get("groups", [])
        sids = raw.get("sids", [])
        if (not isinstance(groups, list) or not isinstance(sids, list) or
                any(not isinstance(item, str) for item in groups + sids)):
            raise CorruptIndexError(f"agent {agent} 的分组或 skill 来源格式无效")
        clean_groups = []
        for gid in groups:
            safe_component(gid)
            if gid not in clean_groups:
                clean_groups.append(gid)
        clean_sids = []
        for sid in sids:
            safe_component(sid)
            if check_sids and sid not in index:
                raise ValueError(f"分发选择了不存在的 skill: {sid}")
            if sid not in clean_sids:
                clean_sids.append(sid)
        replace = raw.get("replace", False)
        if type(replace) is not bool:
            raise CorruptIndexError(f"agent {agent} 的 replace 必须是布尔值")
        agents[agent] = {"groups": clean_groups, "sids": clean_sids,
                         "replace": replace}
    return {"schema_version": 1, "agents": agents}


def load_distribution_sources() -> dict:
    """Read saved source selections without creating a file on read."""
    if not DISTRIBUTIONS_FILE.exists():
        return {"schema_version": 1, "agents": {}}
    if DISTRIBUTIONS_FILE.is_symlink() or not DISTRIBUTIONS_FILE.is_file():
        raise CorruptIndexError(f"分发来源文件不是安全普通文件: {DISTRIBUTIONS_FILE}")
    try:
        value = json.loads(DISTRIBUTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptIndexError(f"分发来源文件损坏，未覆盖: {DISTRIBUTIONS_FILE}") from exc
    try:
        return _normalize_distribution_sources(value)
    except CorruptIndexError:
        raise
    except ValueError as exc:
        raise CorruptIndexError(f"分发来源文件包含不安全值: {DISTRIBUTIONS_FILE}") from exc


@locked
def save_distribution_sources(value: dict) -> dict:
    # Existing agents may temporarily keep a stale explicit id while a skill
    # is in the single-skill trash.  The next plan reports it; saving another
    # agent's selection must not silently erase or make that record unsafe.
    normalized = _normalize_distribution_sources(value, check_sids=False)
    ensure_home()
    atomic_write(DISTRIBUTIONS_FILE,
                 json.dumps(normalized, ensure_ascii=False, indent=2) + "\n")
    audit_log("distribution_sources", detail={
        "agents": sorted(normalized["agents"]),
    })
    return normalized


@locked
def set_distribution_sources(agents: list[str], groups: list[str],
                             sids: list[str], *, replace: bool = False) -> dict:
    """Save one explicit source selection for every selected agent."""
    if (not isinstance(agents, list) or not agents or
            any(agent not in AGENTS for agent in agents)):
        raise ValueError("agents 必须是非空的已知 agent 数组")
    if not isinstance(groups, list) or not isinstance(sids, list):
        raise ValueError("groups/sids 必须是数组")
    if type(replace) is not bool:
        raise ValueError("replace 必须是布尔值")
    current_groups = load_groups()["groups"]
    clean_groups = []
    for gid in groups:
        safe_component(gid)
        if gid not in current_groups:
            raise ValueError(f"不存在的分组: {gid}")
        if gid not in clean_groups:
            clean_groups.append(gid)
    index = load_index()
    clean_sids = []
    for sid in sids:
        safe_component(sid)
        if sid not in index:
            raise ValueError(f"分发选择了不存在的 skill: {sid}")
        if sid not in clean_sids:
            clean_sids.append(sid)
    current = load_distribution_sources()
    saved = _manifest_copy(current)
    for agent in dict.fromkeys(agents):
        saved["agents"][agent] = {"groups": list(clean_groups),
                                   "sids": list(clean_sids),
                                   "replace": replace}
    return save_distribution_sources(saved)


def _distribution_snapshot() -> tuple[bool, Optional[bytes]]:
    """Capture the raw source document for a skill-scoped rollback."""
    if not DISTRIBUTIONS_FILE.exists():
        return False, None
    if DISTRIBUTIONS_FILE.is_symlink() or not DISTRIBUTIONS_FILE.is_file():
        raise CorruptIndexError(f"分发来源文件不是安全普通文件: {DISTRIBUTIONS_FILE}")
    raw = DISTRIBUTIONS_FILE.read_bytes()
    try:
        _normalize_distribution_sources(json.loads(raw.decode("utf-8")))
    except (UnicodeError, json.JSONDecodeError, CorruptIndexError, ValueError) as exc:
        raise CorruptIndexError(f"分发来源文件损坏，未覆盖: {DISTRIBUTIONS_FILE}") from exc
    return True, raw


def _restore_distribution_snapshot(existed: bool, raw: Optional[bytes]) -> None:
    if existed:
        if raw is None:
            raise ValueError("分发来源快照缺少文件内容")
        atomic_write(DISTRIBUTIONS_FILE, raw)
    elif DISTRIBUTIONS_FILE.is_symlink() or DISTRIBUTIONS_FILE.exists():
        if DISTRIBUTIONS_FILE.is_symlink() or DISTRIBUTIONS_FILE.is_file():
            DISTRIBUTIONS_FILE.unlink()
        else:
            raise ValueError("分发来源恢复目标不是普通文件")


def _replace_distribution_sids(mapping: dict[str, str]) -> bool:
    """Migrate explicit source ids while preserving group selections."""
    clean_mapping = {}
    for old_sid, new_sid in mapping.items():
        safe_component(old_sid)
        safe_component(new_sid)
        if old_sid != new_sid:
            clean_mapping[old_sid] = new_sid
    if not clean_mapping or not DISTRIBUTIONS_FILE.exists():
        return False
    existed, raw = _distribution_snapshot()
    if not existed or raw is None:  # pragma: no cover - guarded above
        return False
    value = _normalize_distribution_sources(json.loads(raw.decode("utf-8")))
    changed = False
    for source in value["agents"].values():
        migrated = []
        for sid in source["sids"]:
            value_sid = clean_mapping.get(sid, sid)
            if value_sid not in migrated:
                migrated.append(value_sid)
            if value_sid != sid:
                changed = True
        source["sids"] = migrated
    if changed:
        atomic_write(DISTRIBUTIONS_FILE,
                     json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return changed


def _remove_distribution_group(group_id: str) -> bool:
    safe_component(group_id)
    if not DISTRIBUTIONS_FILE.exists():
        return False
    existed, raw = _distribution_snapshot()
    if not existed or raw is None:  # pragma: no cover - guarded above
        return False
    value = _normalize_distribution_sources(json.loads(raw.decode("utf-8")))
    changed = False
    for source in value["agents"].values():
        if group_id in source["groups"]:
            source["groups"] = [gid for gid in source["groups"] if gid != group_id]
            changed = True
    if changed:
        atomic_write(DISTRIBUTIONS_FILE,
                     json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return changed


def _copy_plain_tree(src: Path, dst: Path) -> None:
    if src.is_symlink() or not src.is_dir():
        raise ValueError("skill 副本不是安全目录")
    _reject_tree_symlinks(src)
    shutil.copytree(src, dst, symlinks=True)


def _stage_version_copy(src: Path, *, parent: Optional[Path] = None) -> Path:
    # Preview staging must not create temporary entries beside the live
    # central store.  Apply paths intentionally stage there so a final rename
    # remains on the same filesystem; preview paths use the system temp dir.
    parent = Path(parent) if parent is not None else STORE_DIR.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".version-", dir=parent)) / "skill"
    _copy_plain_tree(src, stage)
    return stage


def _replace_index_key(index: dict, old_sid: str, new_sid: str,
                       manifest: dict) -> dict:
    if new_sid != old_sid and new_sid in index:
        raise ValueError(f"目标 skill id 已存在: {new_sid}")
    old = index.pop(old_sid)
    index[new_sid] = manifest
    # 修复正式/试用关系中的旧 sid，并同步分组引用。
    for item in index.values():
        for key in ("formal_sid", "ul_sid"):
            if item.get(key) == old_sid:
                item[key] = new_sid
    try:
        groups = load_groups()
    except CorruptIndexError:
        groups = {"schema_version": 1, "groups": {}}
    changed = False
    for group in groups["groups"].values():
        members = group.get("members", [])
        if old_sid in members:
            group["members"] = [new_sid if value == old_sid else value for value in members]
            changed = True
    if changed and GROUPS_FILE.exists():
        # 此时 index 只在内存中已经换 key，save_groups() 若重新读取旧
        # index 会把新 sid 误判为不存在；直接写已验证的关系快照。
        ensure_home()
        atomic_write(GROUPS_FILE, json.dumps(groups, ensure_ascii=False, indent=2) + "\n")
    _replace_distribution_sids({old_sid: new_sid})
    return old


def _retarget_projections(old_sid: str, new_sid: str, old_manifest: dict,
                          new_manifest: dict) -> None:
    """在 store 更新后把关联投影安全地迁到新内容 ID。"""
    try:
        from . import adapters
        adapters.retarget_skill(old_sid, new_sid, old_manifest, new_manifest)
    except ImportError:
        return


def _version_plan(sid: str) -> dict:
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    pair = version_pair(index, sid)
    return {"sid": sid, "manifest": manifest, "pair": pair,
            "related_sids": related_sids(index, sid)}


def create_ul(sid: str, *, apply: bool = False) -> dict:
    """为正式版创建一个独立的试用副本。"""
    safe_component(sid)
    with write_lock() if apply else nullcontext():
        index = load_index()
        plan = _version_plan(sid)
        manifest = plan["manifest"]
        if manifest.get("channel") == UL_ROLE:
            raise ValueError("不能从试用版再次创建试用副本")
        if plan["pair"].get("ul_sid") and plan["pair"]["ul_sid"] in index:
            raise ValueError("该逻辑 skill 已有试用版")
        source = safe_path(STORE_DIR, sid)
        if _read_frontmatter(source / "SKILL.md").get("name") not in {None, manifest.get("name")}:
            raise ValueError("正式副本的 SKILL.md 声明名与索引不一致")
        info = file_summary(source)
        ul_sid = ul_id_for(manifest["name"], info["md5"])
        if ul_sid in index or safe_path(STORE_DIR, ul_sid).exists():
            raise ValueError("该 skill 的 ul 副本已存在")
        result = {"mode": "apply" if apply else "plan", "formal_sid": sid,
                  "ul_sid": ul_sid, "independent_copy": True}
        if not apply:
            return result
        ensure_home()
        stage = _stage_version_copy(source)
        destination = safe_path(STORE_DIR, ul_sid)
        try:
            stage.rename(destination)
            ul_manifest = _manifest_copy(manifest)
            ul_manifest.update({"schema_version": 3, "id": ul_sid,
                                "channel": UL_ROLE, "version_role": UL_ROLE,
                                "logical_id": _logical_id(manifest),
                                "formal_sid": sid, "created_from": sid,
                                "created_at": _now_iso()})
            ul_manifest = _refresh_manifest(ul_sid, ul_manifest, destination)
            index[ul_sid] = ul_manifest
            _link_versions(index, sid, ul_sid, _logical_id(manifest))
            save_index(index)
        except Exception:
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            if stage.exists():
                shutil.rmtree(stage.parent, ignore_errors=True)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage.parent, ignore_errors=True)
        audit_log("ul_create", sid=ul_sid, detail={"formal_sid": sid})
        return result


def _editable_path(root: Path, relative: str) -> Path:
    path = safe_file_path(root, relative, must_exist=True)
    if Path(relative).name in {MARKER_NAME, MANIFEST_NAME}:
        raise ValueError("不能编辑内部标记文件")
    suffix = path.suffix.lower()
    if suffix not in {".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash",
                      ".json", ".jsonc", ".yaml", ".yml", ".toml", ".txt", ".html",
                      ".css", ".xml", ".csv"}:
        raise ValueError("只允许编辑已识别的文本 skill 文件")
    return path


def edit_ul(sid: str, relative: str, text: str, *, apply: bool = False) -> dict:
    """编辑 ul 的文本文件并按完整文件集重新计算 content id。"""
    if not isinstance(text, str) or len(text.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("编辑内容为空或超过 2MiB 限制")
    with write_lock() if apply else nullcontext():
        index = load_index()
        manifest = index.get(sid)
        if not manifest or manifest.get("channel") != UL_ROLE:
            raise ValueError("只有 ul 版本可编辑")
        source = safe_path(STORE_DIR, sid)
        target = _editable_path(source, relative)
        stage = _stage_version_copy(source, parent=None if apply else Path(tempfile.gettempdir()))
        stage_target = safe_file_path(stage, relative)
        atomic_write(stage_target, text)
        declared_name = _read_frontmatter(stage / "SKILL.md").get("name")
        if declared_name and declared_name != manifest.get("name"):
            shutil.rmtree(stage.parent, ignore_errors=True)
            raise ValueError("SKILL.md name 变更请使用 rename 操作")
        new_info = file_summary(stage)
        new_sid = ul_id_for(manifest["name"], new_info["md5"])
        result = {"mode": "apply" if apply else "plan", "old_sid": sid,
                  "sid": new_sid, "file": relative, "summary": new_info}
        if not apply:
            shutil.rmtree(stage.parent, ignore_errors=True)
            return result
        destination = safe_path(STORE_DIR, new_sid)
        if new_sid != sid and (new_sid in index or destination.exists()):
            shutil.rmtree(stage.parent, ignore_errors=True)
            raise ValueError(f"编辑结果与现有 skill 冲突: {new_sid}")
        old_index = _manifest_copy(index)
        old_manifest = _manifest_copy(manifest)
        distribution_existed, distribution_raw = _distribution_snapshot()
        source_backup = _stage_version_copy(source)
        try:
            updated = _refresh_manifest(new_sid, manifest, stage)
            updated["channel"] = UL_ROLE
            updated["version_role"] = UL_ROLE
            updated["formal_sid"] = manifest.get("formal_sid")
            if new_sid != sid:
                stage.rename(destination)
                shutil.rmtree(source)
                _replace_index_key(index, sid, new_sid, updated)
            else:
                shutil.rmtree(stage.parent, ignore_errors=True)
                index[sid] = updated
            formal = updated.get("formal_sid")
            _link_versions(index, formal, new_sid, _logical_id(updated))
            save_index(index)
            if new_sid != sid:
                _retarget_projections(sid, new_sid, old_manifest, updated)
        except Exception:
            # 回滚只恢复本 skill 的目录和相关索引关系，绝不覆盖别的条目。
            if new_sid != sid and destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if not source.exists() and source_backup.exists():
                source_backup.rename(source)
            save_index(old_index)
            _restore_distribution_snapshot(distribution_existed, distribution_raw)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage.parent, ignore_errors=True)
            if source_backup.exists():
                shutil.rmtree(source_backup.parent, ignore_errors=True)
        audit_log("ul_edit", sid=new_sid, detail={"old_sid": sid, "file": relative})
        return result


@locked
def refresh_ul(sid: str, *, apply: bool = False) -> dict:
    """Refresh an externally edited UL and migrate its content ID safely.

    External editors change the files below ``store/<sid>`` without updating
    ``index.json``.  Treat that as a normal UL edit: rehash the complete file
    set, move the UL to its new content ID when needed, repair the formal/UL
    relation and retarget only this skill's existing projections.
    """
    safe_component(sid)
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    if manifest.get("channel") != UL_ROLE:
        raise ValueError("只有 ul 版本支持刷新外部编辑摘要")
    source = safe_path(STORE_DIR, sid)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("ul 中央副本不是安全目录")
    info = file_summary(source)
    new_sid = ul_id_for(manifest.get("name", ""), info["md5"])
    result = {"mode": "apply" if apply else "plan", "old_sid": sid,
              "sid": new_sid, "changed": new_sid != sid,
              "retarget_projections": new_sid != sid, "summary": info}
    if not apply:
        return result
    destination = safe_path(STORE_DIR, new_sid)
    if new_sid != sid and (new_sid in index or destination.exists()):
        raise ValueError(f"刷新结果与现有 skill 冲突: {new_sid}")

    from . import adapters
    index_before = _manifest_copy(index)
    groups_existed, groups_raw = _groups_snapshot()
    distribution_existed, distribution_raw = _distribution_snapshot()
    transaction_root = Path(tempfile.mkdtemp(prefix=".refresh-", dir=STORE_DIR.parent))
    stage = transaction_root / "stage"
    target_root = transaction_root / "targets"
    target_root.mkdir(parents=True, exist_ok=True)
    target_records = []
    restore_failures = []
    try:
        # Snapshot targets before the index changes.  The retarget helper also
        # has its own rollback, but this outer snapshot covers failures after
        # it returns (for example an index write or audit failure).
        seen_targets = set()
        for agent in AGENTS:
            target = adapters.target_path(agent, manifest)
            key = str(target.absolute())
            if key in seen_targets:
                continue
            seen_targets.add(key)
            if adapters._raw_projection_sid(target) == sid:
                target_records.append(adapters._copy_target_for_rollback(
                    target, target_root))

        if new_sid != sid:
            # Keep the old directory in place until retargeting finishes.  A
            # trial symlink must remain valid while retarget_skill inspects
            # and snapshots it; moving it first would look like a conflict.
            _copy_plain_tree(source, stage)
            updated = _refresh_manifest(new_sid, manifest, stage)
            updated["channel"] = UL_ROLE
            updated["version_role"] = UL_ROLE
            updated["formal_sid"] = manifest.get("formal_sid")
            stage.rename(destination)
            index = load_index()
            _replace_index_key(index, sid, new_sid, updated)
        else:
            updated = _refresh_manifest(sid, manifest, source)
            updated["channel"] = UL_ROLE
            updated["version_role"] = UL_ROLE
            index[sid] = updated

        pair = version_pair(index, new_sid)
        _link_versions(index, pair.get("formal_sid"), new_sid,
                       _logical_id(updated))
        save_index(index)
        if new_sid != sid:
            _retarget_projections(sid, new_sid, manifest, updated)
        audit_log("ul_refresh", sid=new_sid,
                  detail={"old_sid": sid, "changed": new_sid != sid})
        if new_sid != sid:
            shutil.rmtree(source)
        return result
    except Exception as exc:
        # Restore projections before bringing the old index/path back.  Do not
        # let a failed cleanup hide the original error without recording it.
        for record in reversed(target_records):
            try:
                adapters._restore_target_record(record)
            except Exception as restore_exc:
                restore_failures.append(f"projection {record.get('target')}: {restore_exc}")
        try:
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
        except Exception as restore_exc:
            restore_failures.append(f"refreshed store cleanup: {restore_exc}")
        try:
            save_index(index_before)
        except Exception as restore_exc:
            restore_failures.append(f"index: {restore_exc}")
        try:
            _restore_groups_snapshot(groups_existed, groups_raw)
        except Exception as restore_exc:
            restore_failures.append(f"groups: {restore_exc}")
        try:
            _restore_distribution_snapshot(distribution_existed, distribution_raw)
        except Exception as restore_exc:
            restore_failures.append(f"distribution sources: {restore_exc}")
        audit_log("ul_refresh", sid=sid, status="failed",
                  detail={"error": str(exc), "restore_failures": restore_failures})
        if restore_failures:
            raise ValueError("刷新失败，且部分恢复失败: " + "; ".join(restore_failures)) from exc
        raise
    finally:
        shutil.rmtree(transaction_root, ignore_errors=True)


def _replace_frontmatter_name(text: str, name: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            head = text[:end]
            if re.search(r"^name\s*:", head, re.MULTILINE):
                head = re.sub(r"^name\s*:\s*.*$", f"name: {name}", head,
                              count=1, flags=re.MULTILINE)
                return head + text[end:]
    raise ValueError("SKILL.md 缺少可编辑的 frontmatter name 声明")


def rename_skill(sid: str, new_name: str, *, apply: bool = False) -> dict:
    safe_component(sid)
    safe_component(new_name)
    with write_lock() if apply else nullcontext():
        index = load_index()
        if sid not in index:
            raise ValueError(f"中央库中不存在 skill: {sid}")
        manifest = index[sid]
        source = safe_path(STORE_DIR, sid)
        stage = _stage_version_copy(source, parent=None if apply else Path(tempfile.gettempdir()))
        md = safe_file_path(stage, "SKILL.md", must_exist=True)
        atomic_write(md, _replace_frontmatter_name(md.read_text(encoding="utf-8"), new_name))
        info = file_summary(stage)
        new_sid = (ul_id_for(new_name, info["md5"])
                   if manifest.get("channel") == UL_ROLE else skill_id_for(new_name, info["md5"]))
        result = {"mode": "apply" if apply else "plan", "old_sid": sid,
                  "sid": new_sid, "old_name": manifest.get("name"), "name": new_name}
        if not apply:
            shutil.rmtree(stage.parent, ignore_errors=True)
            return result
        destination = safe_path(STORE_DIR, new_sid)
        if new_sid != sid and (new_sid in index or destination.exists()):
            shutil.rmtree(stage.parent, ignore_errors=True)
            raise ValueError(f"改名结果与现有 skill 冲突: {new_sid}")
        old_index = _manifest_copy(index)
        old_manifest = _manifest_copy(manifest)
        distribution_existed, distribution_raw = _distribution_snapshot()
        source_backup = _stage_version_copy(source)
        try:
            updated = _refresh_manifest(new_sid, manifest, stage)
            updated["name"] = new_name
            updated["logical_id"] = _logical_id(manifest)
            if updated["logical_id"] == manifest.get("name"):
                updated["logical_id"] = new_name
            if new_sid != sid:
                stage.rename(destination)
                shutil.rmtree(source)
                _replace_index_key(index, sid, new_sid, updated)
            else:
                stage.rename(destination) if destination != source else None
                index[sid] = updated
            pair = version_pair(index, new_sid)
            _link_versions(index, pair.get("formal_sid"), pair.get("ul_sid"),
                           updated.get("logical_id") or new_name)
            save_index(index)
            if new_sid != sid:
                _retarget_projections(sid, new_sid, old_manifest, updated)
        except Exception:
            if new_sid != sid and destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            if not source.exists() and source_backup.exists():
                source_backup.rename(source)
            save_index(old_index)
            _restore_distribution_snapshot(distribution_existed, distribution_raw)
            raise
        finally:
            if stage.exists():
                shutil.rmtree(stage.parent, ignore_errors=True)
            if source_backup.exists():
                shutil.rmtree(source_backup.parent, ignore_errors=True)
        audit_log("skill_rename", sid=new_sid,
                  detail={"old_sid": sid, "old_name": old_manifest.get("name"),
                          "name": new_name})
        return result


def compare_skills(left_sid: str, right_sid: str, *, max_bytes: int = 20000) -> dict:
    index = load_index()
    for sid in (left_sid, right_sid):
        safe_component(sid)
        if sid not in index:
            raise ValueError(f"中央库中不存在 skill: {sid}")
    left = safe_path(STORE_DIR, left_sid)
    right = safe_path(STORE_DIR, right_sid)
    left_files = {item.relative_to(left).as_posix(): item for item in skill_files(left)}
    right_files = {item.relative_to(right).as_posix(): item for item in skill_files(right)}
    changes = []
    import difflib
    for name in sorted(set(left_files) | set(right_files)):
        if name not in left_files:
            changes.append({"path": name, "kind": "added"})
            continue
        if name not in right_files:
            changes.append({"path": name, "kind": "removed"})
            continue
        a = left_files[name].read_bytes()
        b = right_files[name].read_bytes()
        if a == b:
            continue
        item = {"path": name, "kind": "changed", "left_size": len(a), "right_size": len(b)}
        if len(a) <= max_bytes and len(b) <= max_bytes:
            try:
                item["diff"] = "".join(difflib.unified_diff(
                    a.decode("utf-8").splitlines(True), b.decode("utf-8").splitlines(True),
                    fromfile=left_sid + "/" + name, tofile=right_sid + "/" + name))[:max_bytes]
            except UnicodeDecodeError:
                pass
        changes.append(item)
    return {"left": left_sid, "right": right_sid, "changes": changes}


def _trash_entry(entry_id: str) -> Path:
    safe_component(entry_id)
    return safe_path(TRASH_DIR, entry_id)


def _reject_tree_symlinks(root: Path) -> None:
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("目录树不是安全普通目录")
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        for name in dirs + files:
            if (Path(directory) / name).is_symlink():
                raise ValueError("目录树包含软链接")


def _validate_trash_entry(path: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("回收项不是安全目录")
    metadata_path = path / TRASH_METADATA
    if metadata_path.is_symlink() or not metadata_path.is_file():
        raise ValueError("回收项缺少安全元数据")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("回收项元数据损坏") from exc
    if not isinstance(metadata, dict) or not isinstance(metadata.get("sid"), str):
        raise ValueError("回收项元数据无效")
    safe_component(metadata["sid"])
    saved = path / "store" / metadata["sid"]
    if saved.is_symlink() or not saved.is_dir():
        raise ValueError("回收项 skill 副本缺失或是软链接")
    _reject_tree_symlinks(saved)
    actual = file_summary(saved)
    expected = metadata.get("md5")
    if expected and actual["md5"] != expected:
        raise ValueError("回收项摘要不匹配")
    manifest_path = path / MANIFEST_NAME
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("回收项缺少 manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("回收项 manifest 损坏") from exc
    if not isinstance(manifest, dict) or manifest.get("id") != metadata["sid"]:
        raise ValueError("回收项 manifest 与路径不匹配")
    return {"metadata": metadata, "manifest": manifest, "path": path,
            "store": saved, "summary": actual}


def list_trash() -> list[dict]:
    if not TRASH_DIR.exists():
        return []
    rows = []
    for path in sorted(TRASH_DIR.iterdir(), reverse=True):
        if not path.is_dir() or path.is_symlink():
            continue
        try:
            item = _validate_trash_entry(path)
            meta = item["metadata"]
            rows.append({"id": path.name, "sid": meta["sid"],
                         "name": item["manifest"].get("name", ""),
                         "logical_id": meta.get("logical_id", ""),
                         "reason": meta.get("reason", ""),
                         "created_at": meta.get("created_at", ""),
                         "retain_until": meta.get("retain_until", ""),
                         "retained": bool(meta.get("retained")),
                         "valid": True, "size": item["summary"]["size"]})
        except (OSError, ValueError) as exc:
            rows.append({"id": path.name, "valid": False, "error": str(exc)})
    return rows


def _detach_index_version(index: dict, sid: str) -> None:
    """Remove one version and only its direct relation pointers."""
    index.pop(sid, None)
    for other in index.values():
        if other.get("formal_sid") == sid:
            other.pop("formal_sid", None)
        if other.get("ul_sid") == sid:
            other.pop("ul_sid", None)


@locked
def trash_skill(sid: str, *, reason: str = "replace", keep_days: Optional[int] = None,
                remove: bool = True) -> dict:
    """把单个版本复制到回收区；默认从当前索引移除，不触碰其它 skill。"""
    safe_component(sid)
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    source = safe_path(STORE_DIR, sid)
    info = file_summary(source)
    days = max(1, int(keep_days if keep_days is not None else load_settings()["trash_retention_days"]))
    entry_id = f"{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}-{sid[:40]}"
    safe_component(entry_id)
    destination = _trash_entry(entry_id)
    destination.mkdir(parents=True, exist_ok=False)
    try:
        _copy_plain_tree(source, destination / "store" / sid)
        atomic_write(destination / MANIFEST_NAME,
                     json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        metadata = {"schema_version": 1, "sid": sid,
                    "logical_id": _logical_id(manifest), "reason": str(reason)[:120],
                    "created_at": _now_iso(),
                    "retain_until": (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat(),
                    "retained": False, "md5": info["md5"], "size": info["size"]}
        atomic_write(destination / TRASH_METADATA,
                     json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        if remove:
            _detach_index_version(index, sid)
            save_index(index)
            if source.is_symlink() or source.is_file():
                source.unlink()
            else:
                shutil.rmtree(source)
        audit_log("trash", sid=sid, detail={"entry": entry_id, "reason": reason,
                                             "retain_until": metadata["retain_until"]})
        return {"id": entry_id, "sid": sid, "retain_until": metadata["retain_until"],
                "mode": "apply"}
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


@locked
def set_trash_retained(entry_id: str, retained: bool = True) -> dict:
    entry = _validate_trash_entry(_trash_entry(entry_id))
    metadata = entry["metadata"]
    metadata["retained"] = bool(retained)
    atomic_write(entry["path"] / TRASH_METADATA,
                 json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
    audit_log("trash_retain", sid=metadata["sid"], detail={"entry": entry_id,
                                                             "retained": bool(retained)})
    return {"id": entry_id, "retained": bool(retained)}


@locked
def restore_trash(entry_id: str, *, keep_days: Optional[int] = None) -> dict:
    """Restore one trash entry as a skill-scoped transaction.

    The current version, its projections, relation pointers, and groups are
    snapshotted before any live state is removed.  A failed copy/index/target
    update restores those pieces only; it never invokes the whole-library
    rollback path.
    """
    from . import adapters

    entry = _validate_trash_entry(_trash_entry(entry_id))
    old_manifest = _manifest_copy(entry["manifest"])
    sid = old_manifest["id"]
    index_before = load_index()
    if sid in index_before:
        raise ValueError(f"恢复目标索引已存在: {sid}")
    current_sid = None
    current_manifest = None
    old_role = old_manifest.get("channel") or FORMAL_ROLE
    for candidate, manifest in index_before.items():
        if (_logical_id(manifest) == entry["metadata"].get("logical_id") and
                (manifest.get("channel") or FORMAL_ROLE) == old_role):
            current_sid = candidate
            current_manifest = _manifest_copy(manifest)
            break

    destination = safe_path(STORE_DIR, sid)
    if destination.exists() or destination.is_symlink():
        raise ValueError(f"恢复目标已存在: {sid}")

    related_ids = {sid}
    if current_sid:
        related_ids.add(current_sid)
    related_keys = set()
    for candidate, manifest in index_before.items():
        if (candidate in related_ids or manifest.get("formal_sid") in related_ids or
                manifest.get("ul_sid") in related_ids or
                (_logical_id(manifest) == entry["metadata"].get("logical_id"))):
            related_keys.add(candidate)
    # ``sid`` is absent from the pre-restore index by definition, but is
    # created before a later step can fail; include it in the rollback set.
    related_keys.update(related_ids)

    groups_existed, groups_raw = _groups_snapshot()
    distribution_existed, distribution_raw = _distribution_snapshot()
    transaction_root = Path(tempfile.mkdtemp(prefix=".restore-", dir=STORE_DIR.parent))
    saved_store = transaction_root / "store"
    saved_targets = transaction_root / "targets"
    saved_store.mkdir(parents=True, exist_ok=True)
    saved_targets.mkdir(parents=True, exist_ok=True)
    target_records = []
    stage = None
    current_trash = None
    restore_failures = []
    current_source = safe_path(STORE_DIR, current_sid) if current_sid else None

    try:
        if current_sid:
            if current_source is None or not current_source.is_dir() or current_source.is_symlink():
                raise ValueError("当前版本副本缺失或不是安全目录，不能恢复替换")
            _copy_plain_tree(current_source, saved_store / current_sid)

        # Preflight and snapshot every target path whose name/relation can be
        # touched.  Unmanaged content is never overwritten by a restore.
        allowed_owners = {None, sid}
        trial_ul = None
        if current_sid and current_manifest:
            allowed_owners.add(current_sid)
            if current_manifest.get("channel") == FORMAL_ROLE:
                pair = version_pair(index_before, current_sid)
                trial_ul = pair.get("ul_sid")
                if trial_ul:
                    allowed_owners.add(trial_ul)
        seen_targets = set()
        manifests = [manifest for manifest in (current_manifest, old_manifest) if manifest]
        for agent in AGENTS:
            for manifest in manifests:
                target = adapters.target_path(agent, manifest)
                target_key = str(target.absolute())
                if target_key in seen_targets:
                    continue
                seen_targets.add(target_key)
                owner = adapters._raw_projection_sid(target)
                if (target.exists() or target.is_symlink()) and owner not in allowed_owners:
                    raise ValueError(f"{agent} 恢复目标存在非本库内容: {target}")
                target_records.append(adapters._copy_target_for_rollback(target, saved_targets))

        # Prepare the old copy before detaching/removing the current version.
        stage = _stage_version_copy(entry["store"])
        if current_sid:
            # Keep the replaced current version in trash on success, but do
            # not let trash_skill mutate the live index until our transaction
            # has a rollback copy and the staged restore is ready.
            current_trash = trash_skill(current_sid, reason="restore_replace",
                                        keep_days=keep_days, remove=False)
            index = load_index()
            _detach_index_version(index, current_sid)
            save_index(index)
            if current_source.is_symlink() or current_source.is_file():
                current_source.unlink()
            else:
                shutil.rmtree(current_source)

        stage.rename(destination)
        stage = None
        index = load_index()
        restored_manifest = _manifest_copy(old_manifest)
        if restored_manifest.get("channel") == FORMAL_ROLE:
            # Trial is environment state.  Preserve it from the live formal
            # version so restoring old bytes does not unexpectedly move an
            # agent off its currently selected UL.
            if current_manifest and current_manifest.get("channel") == FORMAL_ROLE:
                if current_manifest.get("trial_agents"):
                    restored_manifest["trial_agents"] = list(current_manifest["trial_agents"])
                else:
                    restored_manifest.pop("trial_agents", None)
            else:
                restored_manifest.pop("trial_agents", None)
        index[sid] = restored_manifest
        pair = version_pair(index, sid)
        _link_versions(index, pair.get("formal_sid"), pair.get("ul_sid"),
                       _logical_id(restored_manifest))
        save_index(index)
        if current_sid and current_sid != sid:
            _replace_group_member(current_sid, sid)
            _replace_distribution_sids({current_sid: sid})
            _retarget_projections(current_sid, sid, current_manifest, restored_manifest)
        _validate_trash_entry(entry["path"])  # verify source remained intact
        metadata = _manifest_copy(entry["metadata"])
        metadata["restored_at"] = _now_iso()
        atomic_write(entry["path"] / TRASH_METADATA,
                     json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")
        audit_log("restore", sid=sid,
                  detail={"entry": entry_id, "replaced_sid": current_sid or ""})
        return {"sid": sid, "entry": entry_id, "replaced_sid": current_sid,
                "mode": "apply"}
    except Exception as exc:
        # Restore projections first; they are independent of the index and
        # this also covers a failure injected during the second target move.
        for record in reversed(target_records):
            try:
                adapters._restore_target_record(record)
            except Exception as restore_exc:
                restore_failures.append(f"projection {record.get('target')}: {restore_exc}")
        try:
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
        except Exception as restore_exc:
            restore_failures.append(f"restored store cleanup: {restore_exc}")
        if current_sid and current_source and (saved_store / current_sid).exists():
            try:
                if current_source.exists() or current_source.is_symlink():
                    if current_source.is_dir() and not current_source.is_symlink():
                        shutil.rmtree(current_source)
                    else:
                        current_source.unlink()
                _copy_plain_tree(saved_store / current_sid, current_source)
            except Exception as restore_exc:
                restore_failures.append(f"current store: {restore_exc}")
        try:
            current_index = load_index()
            for key in related_keys:
                current_index.pop(key, None)
            current_index.update({key: _manifest_copy(index_before[key])
                                  for key in related_keys if key in index_before})
            save_index(current_index)
        except Exception as restore_exc:
            restore_failures.append(f"related index: {restore_exc}")
        try:
            _restore_groups_snapshot(groups_existed, groups_raw)
        except Exception as restore_exc:
            restore_failures.append(f"groups: {restore_exc}")
        try:
            _restore_distribution_snapshot(distribution_existed, distribution_raw)
        except Exception as restore_exc:
            restore_failures.append(f"distribution sources: {restore_exc}")
        if current_trash:
            try:
                artifact = _trash_entry(current_trash["id"])
                if artifact.exists() or artifact.is_symlink():
                    if artifact.is_symlink() or not artifact.is_dir():
                        artifact.unlink()
                    else:
                        shutil.rmtree(artifact)
            except Exception as restore_exc:
                restore_failures.append(f"transaction trash cleanup: {restore_exc}")
        audit_log("restore", sid=sid, status="failed",
                  detail={"entry": entry_id, "error": str(exc),
                          "restore_failures": restore_failures})
        if restore_failures:
            raise ValueError(f"恢复失败，且部分恢复失败: {'; '.join(restore_failures)}") from exc
        raise
    finally:
        if stage is not None and stage.exists():
            shutil.rmtree(stage.parent, ignore_errors=True)
        shutil.rmtree(transaction_root, ignore_errors=True)


def plan_trash_cleanup(now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    plan = []
    for row in list_trash():
        if not row.get("valid"):
            plan.append({"id": row["id"], "type": "invalid", "detail": row.get("error", "")})
            continue
        try:
            expires = datetime.fromisoformat(row["retain_until"])
        except (TypeError, ValueError):
            plan.append({"id": row["id"], "type": "invalid", "detail": "保留截止时间无效"})
            continue
        action = "keep" if row["retained"] or expires > now else "remove"
        plan.append({"id": row["id"], "sid": row["sid"], "type": action,
                     "retain_until": row["retain_until"], "detail":
                     "手动保留" if row["retained"] else
                     "未到期" if action == "keep" else "已到期"})
    return plan


@locked
def cleanup_trash(*, authorized: bool = False, now: Optional[datetime] = None) -> list[dict]:
    if not authorized:
        raise ValueError("清理回收区必须经过用户明确确认")
    done = []
    for action in plan_trash_cleanup(now):
        if action["type"] != "remove":
            done.append(action)
            continue
        path = _trash_entry(action["id"])
        try:
            _validate_trash_entry(path)
            shutil.rmtree(path)
            done.append({**action, "type": "removed"})
            audit_log("trash_cleanup", sid=action.get("sid", ""),
                      detail={"entry": action["id"]})
        except Exception as exc:
            done.append({**action, "type": "failed", "detail": str(exc)})
            audit_log("trash_cleanup", sid=action.get("sid", ""), status="failed",
                      detail={"entry": action["id"], "error": str(exc)})
    return done


def _dependency_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ValueError("依赖声明必须是字符串数组")
        return list(dict.fromkeys(value))
    raise ValueError("依赖声明必须是字符串或字符串数组")


def diagnose_skill(sid: str) -> dict:
    """只做静态存在性检查，绝不运行 skill、脚本、服务或联网动作。"""
    safe_component(sid)
    index = load_index()
    manifest = index.get(sid)
    if manifest is None:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    root = safe_path(STORE_DIR, sid)
    text_parts = []
    for item in skill_files(root):
        try:
            text_parts.append(item.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    text = "\n".join(text_parts)
    declared = manifest.get("dependencies") or {}
    if not isinstance(declared, dict):
        declared = {"tools": declared}
    import shutil as _shutil
    tools = []
    for name in _dependency_list(declared.get("tools")):
        present = bool(_shutil.which(name))
        tools.append({"name": name, "source": "declared", "certainty": "explicit",
                      "kind": "tool", "status": "present" if present else "missing"})
    envs = []
    env_names = []
    for name in _dependency_list(declared.get("env")):
        if name not in env_names:
            env_names.append(name)
    env_names.extend(name for name in re.findall(r"(?:os\.environ(?:\.get)?|getenv)\s*\(?[\"']([A-Z][A-Z0-9_]+)", text)
                     if name not in env_names)
    env_names.extend(name for name in re.findall(r"\$\{([A-Z][A-Z0-9_]+)\}|\{\{env:([A-Z][A-Z0-9_]+)\}\}", text)
                     for name in name if name and name not in env_names)
    verified = set(manifest.get("diagnostics_verified") or [])
    for name in env_names:
        present = bool(os.environ.get(name))
        explicit = name in _dependency_list(declared.get("env"))
        key = f"env:{name}"
        envs.append({"name": name, "source": "declared" if explicit else "inferred",
                     "certainty": "explicit" if explicit else "inferred", "kind": "env",
                     "status": "present" if present else
                     "verified" if key in verified else "missing" if explicit else "verify"})
    inferred = []
    declared_tool_names = {item["name"] for item in tools}
    for name in sorted(set(re.findall(r"\b(?:python3?|node|npm|ffmpeg|git|curl|wget|docker)\b", text)) - declared_tool_names):
        inferred.append({"name": name, "kind": "tool", "source": "inferred",
                         "certainty": "inferred", "status": "verified" if f"tool:{name}" in verified else "verify"})
    missing = [item for item in tools + envs if item["status"] == "missing"]
    needs_manual = [item for item in envs + inferred if item["status"] == "verify"]
    return {"sid": sid, "tools": tools, "env": envs, "inferred": inferred,
            "missing": missing, "needs_manual": needs_manual,
            "blocking": bool(missing or needs_manual),
            "actions": [{"type": "manual", "scope": "用户在可信终端确认工具/环境/服务，不执行 skillhub 之外的代码"}]
            if needs_manual else []}


@locked
def set_dependencies(sid: str, dependencies: dict) -> dict:
    safe_component(sid)
    if not isinstance(dependencies, dict):
        raise ValueError("dependencies 必须是对象")
    allowed = {"tools", "env", "services", "network"}
    clean = {}
    for key, value in dependencies.items():
        if key not in allowed:
            raise ValueError(f"不认识的依赖类型: {key}（请标记为待验证）")
        clean[key] = _dependency_list(value) if key in {"tools", "env", "services"} else bool(value)
    index = load_index()
    if sid not in index:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    index[sid]["dependencies"] = clean
    index[sid].pop("diagnostics_verified", None)
    save_index(index)
    audit_log("dependencies", sid=sid, detail={"types": sorted(clean)})
    return diagnose_skill(sid)


@locked
def confirm_diagnostics(sid: str, keys: list[str], note: str = "") -> dict:
    safe_component(sid)
    if not isinstance(keys, list) or not all(isinstance(key, str) for key in keys):
        raise ValueError("待验证项必须是字符串数组")
    index = load_index()
    if sid not in index:
        raise ValueError(f"中央库中不存在 skill: {sid}")
    diagnosis = diagnose_skill(sid)
    available = {f"{item['kind']}:{item['name']}" for item in diagnosis["needs_manual"]}
    if any(key not in available for key in keys):
        raise ValueError("确认项不是当前诊断列出的待验证项")
    index[sid]["diagnostics_verified"] = sorted(set(index[sid].get("diagnostics_verified", [])) | set(keys))
    save_index(index)
    audit_log("manual_verify", sid=sid,
              detail={"keys": keys, "note": str(note)[:300], "scope": "user-confirmed"})
    return diagnose_skill(sid)


@locked
def remove_skill(sid: str) -> bool:
    safe_component(sid)
    index = load_index()
    if sid not in index:
        return False
    del index[sid]
    save_index(index)
    dst = safe_path(STORE_DIR, sid)
    if dst.exists() or dst.is_symlink():
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        else:
            shutil.rmtree(dst)
    return True
