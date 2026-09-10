"""本地 Web GUI。

服务只监听 loopback。浏览器必须带上启动时打印的一次性令牌才能拿到
HttpOnly 会话 cookie。GET 校验 Host 和 cookie（同源 GET fetch 不带 Origin）；
POST 额外要求匹配的 Origin。写接口默认预览，忽略 ``allow_risky``，全库
rollback 不走 GUI。
"""
from __future__ import annotations

import json
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import adapters, mcp, scan, store
from .config import (AGENTS, BACKUP_DIR, INDEX_FILE, MCP_INDEX_FILE, RISK_GATE,
                     STORE_DIR, discover_models, suggest_model_groups)


HTML_FILE = Path(__file__).with_name("gui.html")
DEFAULT_PORT = 8317
MAX_BODY_BYTES = 1024 * 1024
BACKUP_PAGE_SIZE = 50
_BACKUP_CACHE: dict = {}
UNAUTH_HTML = (
    "<!DOCTYPE html><meta charset=utf-8><title>skillhub</title>"
    "<body style='font:14px sans-serif;padding:2rem'>"
    "<p>未授权。请使用终端打印的带 token 的 URL 打开本控制台。</p>"
).encode("utf-8")
WRITE_PATHS = {
    "/api/link", "/api/unlink", "/api/skill/create-ul", "/api/skill/edit",
    "/api/skill/rename", "/api/skill/refresh", "/api/skill/trial", "/api/skill/publish",
    "/api/skill/import", "/api/diagnose/confirm", "/api/diagnose/dependencies", "/api/groups",
    "/api/groups/delete", "/api/backups/cleanup",
    "/api/distribute", "/api/trash/restore", "/api/trash/retain",
    "/api/trash/cleanup", "/api/settings", "/api/editor/open",
    "/api/mcp/import", "/api/mcp/edit", "/api/mcp/generate",
    "/api/model-groups", "/api/models/suggest",
}
CONFIRM_PHRASES = {
    "/api/skill/publish": "PUBLISH",
    "/api/trash/cleanup": "CLEANUP",
    "/api/backups/cleanup": "CLEANUP",
    "/api/mcp/generate": "GENERATE",
}


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(root) / name).is_symlink()]
        for name in files:
            item = Path(root) / name
            try:
                if not item.is_symlink():
                    total += item.stat().st_size
            except OSError:
                pass
    return total


def _list_store_files(sid: str, limit: int = 200) -> list:
    index = store.load_index()
    if sid not in index:
        return []
    root = store.safe_path(STORE_DIR, sid)
    if not root.is_dir() or root.is_symlink():
        return []
    output = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith(".") and
                         name not in store.EXCLUDED_DIRS and not (Path(directory) / name).is_symlink())
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = -1
            output.append({"path": str(path.relative_to(root)), "size": size})
            if len(output) >= limit:
                output.append({"path": f"... 共超过 {limit} 个文件", "size": -1})
                return output
    return output


def _backup_rows(page: int = 0, page_size: int = BACKUP_PAGE_SIZE) -> tuple[list, int]:
    page = max(0, int(page))
    page_size = max(1, min(int(page_size), 200))
    try:
        stamp = BACKUP_DIR.stat().st_mtime_ns
    except OSError:
        stamp = 0
    cache_key = (stamp, len(adapters.list_backups()))
    cached = _BACKUP_CACHE.get("rows")
    if not cached or _BACKUP_CACHE.get("key") != cache_key:
        # 只读取每个快照的 metadata/journal，不递归计算整个 store 大小。
        cached = [adapters.backup_info(backup) for backup in adapters.list_backups()]
        _BACKUP_CACHE.update({"key": cache_key, "rows": cached})
    total = len(cached)
    start = page * page_size
    return cached[start:start + page_size], total


def collect_data(backup_page: int = 0, backup_page_size: int = BACKUP_PAGE_SIZE) -> dict:
    index = store.load_index()
    statuses = adapters.status()
    state_by_sid = {}
    for agent, items in statuses.items():
        for item in items:
            state_by_sid.setdefault(item["sid"], {})[agent] = item["state"]

    agents = []
    for agent, cfg in AGENTS.items():
        counts = Counter(item["state"] for item in statuses.get(agent, []))
        target = mcp.MCP_TARGETS.get(agent)
        agents.append({
            "name": agent,
            "skill_dir": str(cfg["skill_dir"]),
            "dir_exists": cfg["skill_dir"].exists(),
            "mode": cfg.get("mode", "symlink"),
            "nested": bool(cfg.get("nested")),
            "mcp_target": target[0] if target else None,
            "mcp_key": mcp.MCP_TARGET_KEYS.get(agent),
            "mcp_supported": agent in mcp.MCP_TARGETS,
            "linked": counts.get("linked", 0),
            "conflict": counts.get("conflict", 0),
            "broken": counts.get("broken", 0) + counts.get("copy_broken", 0),
            "store_drift": counts.get("store_drift", 0),
            "copy_drift": counts.get("copy_drift", 0),
            "not_linked": counts.get("not_linked", 0),
            "total_in_store": len(index),
        })

    skills = []
    for sid, manifest in sorted(index.items(), key=lambda item: item[1].get("name", "")):
        try:
            pair = store.version_pair(index, sid)
        except (OSError, ValueError):
            pair = {"formal_sid": manifest.get("formal_sid"), "ul_sid": manifest.get("ul_sid")}
        skills.append({
            "id": sid,
            "name": manifest.get("name", ""),
            "category": manifest.get("category", ""),
            "logical_id": manifest.get("logical_id", manifest.get("name", "")),
            "channel": manifest.get("channel", "formal"),
            "formal_sid": pair.get("formal_sid"),
            "ul_sid": pair.get("ul_sid"),
            "trial_agents": manifest.get("trial_agents", []),
            "desc": manifest.get("fm_desc", ""),
            "risks": manifest.get("risks", []),
            "size": manifest.get("size", 0),
            "file_count": manifest.get("file_count", 0),
            "imported_at": manifest.get("imported_at", ""),
            "source_agents": manifest.get("agents", []),
            "source_paths": [item.get("path", "") for item in manifest.get("sources", [])],
            "states": state_by_sid.get(sid, {}),
        })

    servers = []
    for server in mcp.redact_for_output(mcp.list_servers()):
        original = mcp.get_server(server.get("id", "")) or {}
        references = mcp.editable_references(original)
        servers.append({
            "id": server.get("id", ""),
            "label": server.get("label", ""),
            "transport": server.get("transport", ""),
            "url": server.get("url", ""),
            "command": server.get("command", ""),
            "args": server.get("args", []),
            "env_keys": sorted((server.get("env") or {}).keys()),
            "header_keys": sorted((server.get("headers") or {}).keys()),
            "env_refs": references["env"],
            "header_refs": references["headers"],
            "enabled": server.get("enabled", True),
            "agents": server.get("agents", []),
            "source": server.get("source", ""),
        })
    backups, total_backups = _backup_rows(backup_page, backup_page_size)
    try:
        groups = store.load_groups()
    except (OSError, ValueError):
        groups = {"schema_version": 1, "groups": {}}
    return {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "paths": {"store": str(STORE_DIR), "index": str(INDEX_FILE),
                  "mcp_index": str(MCP_INDEX_FILE), "backups": str(BACKUP_DIR)},
        "summary": {"skills": len(skills), "risky": sum(1 for item in skills if item["risks"]),
                    "agents": len(agents), "mcp_servers": len(servers),
                    "backups": total_backups, "linked_total": sum(item["linked"] for item in agents)},
        "agents": agents, "skills": skills, "mcp_servers": servers,
        "groups": groups.get("groups", {}),
        "distribution_sources": store.load_distribution_sources().get("agents", {}),
        "trash": store.list_trash(),
        "trash_plan": store.plan_trash_cleanup(), "logs": store.read_audit_log(100),
        "settings": store.load_settings(),
        "risk_gate": list(RISK_GATE),
        "backups": backups, "backup_page": backup_page,
        "backup_page_size": backup_page_size, "backup_total": total_backups,
    }


def _scan_sources() -> list:
    sources = []
    try:
        for agent, records in scan.scan_all().items():
            for record in records:
                if record.get("error"):
                    sources.append({"agent": agent, "name": record.get("name", ""),
                                    "path": record.get("path", ""), "error": record["error"]})
                    continue
                sources.append({"agent": agent, "name": record.get("name", ""),
                                "category": record.get("category", ""),
                                "path": record.get("path", ""), "md5": record.get("md5", ""),
                                "size": record.get("size", 0), "desc": record.get("fm_desc", ""),
                                "risks": record.get("risks", [])})
    except (OSError, ValueError):
        return []
    return sources


def _header_host(handler) -> str:
    return (handler.headers.get("Host") or "").split("/", 1)[0].lower()


def _host_allowed(handler) -> bool:
    port = handler.server.server_address[1]
    allowed = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    return _header_host(handler) in allowed


def _origin_allowed(handler, *, required: bool) -> bool:
    """POST 必须带同源 Origin。GET 允许缺 Origin（浏览器同源 fetch 不发该头）。"""
    origin = handler.headers.get("Origin")
    if not origin:
        return not required
    parsed = urlparse(origin)
    port = handler.server.server_address[1]
    return parsed.scheme == "http" and parsed.netloc.lower() in {
        f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"
    }


def _cookie_value(header: str, name: str) -> str:
    for item in header.split(";"):
        key, _, value = item.strip().partition("=")
        if key == name:
            return value
    return ""


def _tokens_match(got: str, expected: str) -> bool:
    if not got or not expected:
        return False
    try:
        return secrets.compare_digest(got, expected)
    except (TypeError, ValueError):
        return False


def _query_token(handler) -> str:
    values = parse_qs(urlparse(handler.path).query).get("token", [])
    return values[0] if values else ""


def _session_token(handler) -> str:
    header = handler.headers.get("X-Skillhub-Token", "")
    return header or _cookie_value(handler.headers.get("Cookie", ""), "skillhub_session")


def _session_ok(handler) -> bool:
    expected = getattr(handler.server, "skillhub_token", "") or ""
    return _tokens_match(_session_token(handler), expected)


class _Handler(BaseHTTPRequestHandler):
    server_version = "skillhub-gui"

    def log_message(self, fmt, *args):
        if args and args[1] != 200:
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str,
              headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: dict, code: int = 200) -> None:
        if code == 200 and getattr(self, "_audit_write", False):
            store.audit_log("gui", detail={"path": getattr(self, "_audit_path", ""),
                                           "apply": True})
            self._audit_write = False
        self._send(code, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _authorized(self, *, require_origin: bool = False) -> bool:
        return (_host_allowed(self) and _session_ok(self) and
                _origin_allowed(self, required=require_origin))

    def _page_authorized(self) -> bool:
        expected = getattr(self.server, "skillhub_token", "") or ""
        return _host_allowed(self) and (
            _session_ok(self) or _tokens_match(_query_token(self), expected))

    def _cookie_header(self) -> dict:
        token = getattr(self.server, "skillhub_token", "")
        return {"Set-Cookie": f"skillhub_session={token}; Path=/; HttpOnly; SameSite=Strict"}

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in {"/", "/index.html"}:
                if not self._page_authorized():
                    self._send(403, UNAUTH_HTML, "text/html; charset=utf-8")
                    return
                body = HTML_FILE.read_bytes()
                self._send(200, body, "text/html; charset=utf-8", self._cookie_header())
                return
            if not self._authorized():
                self._json({"error": "未授权的本地会话"}, 403)
                return
            if path == "/api/data":
                params = parse_qs(urlparse(self.path).query)
                page = int(params.get("backup_page", ["0"])[0])
                payload = collect_data(page)
                payload["read_only"] = bool(getattr(self.server, "skillhub_read_only", False))
                self._json(payload)
            elif path == "/api/sources":
                self._json({"sources": _scan_sources()})
            elif path == "/api/models":
                self._json(discover_models())
            elif path == "/api/logs":
                self._json({"logs": store.read_audit_log(500)})
            elif path == "/api/trash":
                self._json({"items": store.list_trash(), "plan": store.plan_trash_cleanup()})
            elif path == "/api/groups":
                self._json(store.load_groups())
            elif path == "/api/settings":
                self._json(store.load_settings())
            elif path.startswith("/api/skill/") and path.endswith("/diagnose"):
                sid = unquote(path[len("/api/skill/"):-len("/diagnose")].strip("/"))
                store.safe_component(sid)
                self._json(store.diagnose_skill(sid))
            elif path.startswith("/api/skill/") and path.endswith("/diff"):
                sid = unquote(path[len("/api/skill/"):-len("/diff")].strip("/"))
                store.safe_component(sid)
                params = parse_qs(urlparse(self.path).query)
                other = unquote(params.get("with", [""])[0])
                self._json(store.compare_skills(sid, other))
            elif path.startswith("/api/skill/") and "/file" in path:
                raw = path[len("/api/skill/"):]
                raw_sid, _, _ = raw.partition("/file")
                sid = unquote(raw_sid.strip("/"))
                store.safe_component(sid)
                index = store.load_index()
                if sid not in index:
                    self._json({"error": f"中央库中不存在 skill: {sid}"}, 404); return
                params = parse_qs(urlparse(self.path).query)
                relative = unquote(params.get("path", [""])[0])
                path_obj = store.safe_file_path(store.safe_path(STORE_DIR, sid), relative, must_exist=True)
                if path_obj.stat().st_size > 2 * 1024 * 1024:
                    self._json({"error": "文件超过 2MiB，不在编辑器中打开"}, 413); return
                try:
                    text = path_obj.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    self._json({"error": "文件不是 UTF-8 文本"}, 415); return
                editable = path_obj.suffix.lower() in {".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".txt", ".html", ".css", ".xml", ".csv"}
                self._json({"sid": sid, "path": relative, "text": text, "editable": editable,
                            "channel": index[sid].get("channel", "formal")})
            elif path.startswith("/api/skill/"):
                raw_sid = path[len("/api/skill/"):].strip("/")
                sid = unquote(raw_sid)
                store.safe_component(sid)
                index = store.load_index()
                if sid not in index:
                    self._json({"error": f"中央库中不存在 skill: {sid}"}, 404); return
                manifest = index[sid]
                states = {}
                for agent in AGENTS:
                    target = adapters.target_path(agent, manifest)
                    effective_sid, effective_manifest = adapters._effective_projection(
                        agent, sid, manifest, index)
                    state = adapters.projection_state(
                        target, effective_sid,
                        central=adapters._central_state(effective_sid, effective_manifest),
                        manifest=effective_manifest)
                    if effective_sid != sid and state == "linked":
                        state = "trial"
                    if state == "copy":
                        state = "linked"
                    states[agent] = {"state": state, "target": str(target)}
                self._json({"manifest": manifest, "files": _list_store_files(sid),
                            "projections": states, "store_dir": str(STORE_DIR / sid)})
            elif path == "/api/health":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:
            try:
                self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            except Exception:
                pass

    def _read_json_body(self) -> dict | None:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._json({"error": "Content-Type 必须是 application/json"}, 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY_BYTES:
            self._json({"error": "请求体大小无效或超过限制"}, 413)
            return None
        raw = self.rfile.read(length)
        if len(raw) != length:
            self._json({"error": "请求体长度不完整"}, 400)
            return None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return None
        if not isinstance(value, dict):
            self._json({"error": "请求体必须是 JSON 对象"}, 400)
            return None
        return value

    def do_POST(self):
        if not self._authorized(require_origin=True):
            self._json({"error": "未授权的本地会话"}, 403)
            return
        path = urlparse(self.path).path
        if path not in WRITE_PATHS:
            self._json({"error": "not found"}, 404); return
        if getattr(self.server, "skillhub_read_only", False):
            self._json({"error": "GUI 处于只读模式，写操作已禁用"}, 403)
            return
        body = self._read_json_body()
        if body is None:
            return
        try:
            apply = bool(body.get("apply", False))
            if any(key in body and type(body[key]) is not bool for key in {"force", "allow_risky", "apply", "dry_run", "confirm", "enable", "replace", "retained"}):
                raise ValueError("布尔字段类型无效")
            if body.get("apply") and body.get("dry_run"):
                raise ValueError("apply 与 dry_run 互斥")
            if body.get("allow_risky"):
                raise ValueError("GUI 不允许 --allow-risky，请用 CLI 显式放行")
            required_phrase = CONFIRM_PHRASES.get(path)
            if path == "/api/distribute" and apply and body.get("replace"):
                required_phrase = "REPLACE"
            if apply and required_phrase and body.get("confirm_phrase") != required_phrase:
                raise ValueError(f"此操作必须输入 {required_phrase} 确认")
            self._audit_write = bool(apply) or path == "/api/models/suggest"
            self._audit_path = path
            if path in {"/api/link", "/api/unlink"}:
                sid = body.get("sid")
                agents = body.get("agents")
                if not isinstance(sid, str) or not sid:
                    raise ValueError("sid 必须是非空字符串")
                if not isinstance(agents, list) or not agents or not all(isinstance(item, str) for item in agents):
                    raise ValueError("agents 必须是非空字符串数组")
                store.safe_component(sid)
                if any(agent not in AGENTS for agent in agents):
                    raise ValueError("包含未知 agent")
                if sid not in store.load_index():
                    self._json({"error": f"中央库中不存在 skill: {sid}"}, 404); return
                if path == "/api/link":
                    actions = (adapters.apply_link(sid, agents, force=body.get("force", False),
                                                    allow_risky=False)
                               if apply else adapters.plan_link(sid, agents, force=body.get("force", False),
                                                               allow_risky=False))
                else:
                    actions = (adapters.apply_unlink(sid, agents, force=body.get("force", False))
                               if apply else adapters.plan_unlink(sid, agents))
                self._json({"mode": "apply" if apply else "plan", "actions": actions}); return
            if path == "/api/skill/create-ul":
                sid = body.get("sid"); store.safe_component(sid)
                self._json(store.create_ul(sid, apply=apply)); return
            if path == "/api/skill/edit":
                sid = body.get("sid"); store.safe_component(sid)
                if not isinstance(body.get("path"), str) or not isinstance(body.get("text"), str):
                    raise ValueError("path/text 无效")
                self._json(store.edit_ul(sid, body["path"], body["text"], apply=apply)); return
            if path == "/api/skill/rename":
                sid = body.get("sid"); store.safe_component(sid)
                name = body.get("name")
                if not isinstance(name, str):
                    raise ValueError("name 无效")
                self._json(store.rename_skill(sid, name, apply=apply)); return
            if path == "/api/skill/refresh":
                sid = body.get("sid"); store.safe_component(sid)
                self._json(store.refresh_ul(sid, apply=apply)); return
            if path == "/api/skill/trial":
                sid = body.get("sid"); store.safe_component(sid)
                agents = body.get("agents")
                if not isinstance(agents, list) or not agents:
                    raise ValueError("agents 必须是非空数组")
                result = adapters.plan_trial(sid, agents)
                if apply:
                    result = {"mode": "apply", "actions": adapters.apply_trial(
                        sid, agents, enabled=body.get("enable", True), force=body.get("force", False))}
                self._json(result); return
            if path == "/api/skill/publish":
                sid = body.get("sid"); store.safe_component(sid)
                self._json(adapters.publish_ul(sid, apply=apply)); return
            if path == "/api/skill/import":
                agent = body.get("agent"); source_path = body.get("path")
                if agent not in AGENTS or not isinstance(source_path, str):
                    raise ValueError("来源 agent/path 无效")
                root = Path(AGENTS[agent]["skill_dir"])
                source = Path(source_path)
                source.relative_to(root)
                # 允许 agent 条目本身是本库 symlink，但不允许用户通过
                # GUI 任意指定 root 之外的绝对路径。
                source = store.safe_path(root, *source.relative_to(root).parts,
                                         projection=True)
                records = [item for item in scan.scan_agent(agent)
                           if item.get("path") == str(source_path)]
                if len(records) != 1 or records[0].get("error"):
                    raise ValueError("未找到可安全导入的来源 skill")
                record = records[0]
                self._json({"mode": "apply" if apply else "plan",
                            "sid": store.import_skill(agent, record, apply=False,
                                                      channel=body.get("channel")),
                            "agent": agent,
                            "result": store.import_skill(agent, record, apply=apply,
                                                         channel=body.get("channel")) if apply else None}); return
            if path == "/api/diagnose/confirm":
                sid = body.get("sid"); store.safe_component(sid)
                keys = body.get("keys")
                if not isinstance(keys, list):
                    raise ValueError("keys 必须是数组")
                self._json(store.confirm_diagnostics(sid, keys, body.get("note", "")) if apply else store.diagnose_skill(sid)); return
            if path == "/api/diagnose/dependencies":
                sid = body.get("sid"); store.safe_component(sid)
                dependencies = body.get("dependencies")
                if not isinstance(dependencies, dict):
                    raise ValueError("dependencies 必须是对象")
                self._json(store.set_dependencies(sid, dependencies) if apply else {"mode": "plan", "sid": sid, "dependencies": dependencies}); return
            if path == "/api/groups":
                gid = body.get("group_id")
                members = body.get("members", [])
                if not isinstance(gid, str) or not isinstance(members, list):
                    raise ValueError("group_id/members 无效")
                self._json(store.update_group(gid, members, name=body.get("name", ""),
                                              kind=body.get("kind", "manual"),
                                              description=body.get("description", ""), apply=apply)); return
            if path == "/api/groups/delete":
                gid = body.get("group_id")
                store.safe_component(gid)
                if not apply:
                    current = store.load_groups().get("groups", {})
                    if gid not in current:
                        raise ValueError(f"分组不存在: {gid}")
                    self._json({"mode": "plan", "group": gid,
                                "name": current[gid].get("name", gid)}); return
                self._json(store.delete_group(gid)); return
            if path == "/api/distribute":
                agents = body.get("agents"); groups = body.get("groups"); sids = body.get("sids")
                if not isinstance(agents, list):
                    raise ValueError("agents 必须是数组")
                if groups is not None and not isinstance(groups, list):
                    raise ValueError("groups 必须是数组")
                if sids is not None and not isinstance(sids, list):
                    raise ValueError("sids 必须是数组")
                replace = body.get("replace") if "replace" in body else None
                result = adapters.plan_distribution(agents, group_ids=groups, sids=sids,
                                                    replace=replace, force=body.get("force", False),
                                                    allow_risky=False)
                if apply:
                    result = adapters.apply_distribution(agents, group_ids=groups, sids=sids,
                                                         replace=replace, force=body.get("force", False),
                                                         allow_risky=False)
                self._json(result); return
            if path == "/api/trash/restore":
                entry = body.get("entry"); store.safe_component(entry)
                self._json(store.restore_trash(entry) if apply else {"mode": "plan", "entry": entry}); return
            if path == "/api/trash/retain":
                entry = body.get("entry"); store.safe_component(entry)
                self._json(store.set_trash_retained(entry, body.get("retained", True)) if apply else {"mode": "plan", "entry": entry, "retained": body.get("retained", True)}); return
            if path == "/api/trash/cleanup":
                self._json({"mode": "apply", "actions": store.cleanup_trash(authorized=True)} if apply and body.get("confirm") else {"mode": "plan", "actions": store.plan_trash_cleanup(), "needs_confirm": True}); return
            if path == "/api/backups/cleanup":
                keep = body.get("keep", 3)
                if type(keep) is not int or keep < 1 or keep > 10000:
                    raise ValueError("keep 必须是 1 到 10000 的整数")
                actions = adapters.apply_cleanup(keep) if apply and body.get("confirm") else adapters.plan_cleanup(keep)
                self._json({"mode": "apply" if apply and body.get("confirm") else "plan",
                            "keep": keep, "actions": actions}); return
            if path == "/api/settings":
                settings = body.get("settings", body)
                if not isinstance(settings, dict):
                    raise ValueError("settings 必须是对象")
                if "editor" in settings:
                    raise ValueError("GUI 不能配置外部编辑器，请用环境变量 VISUAL 或 EDITOR")
                self._json(store.save_settings(settings) if apply else {"mode": "plan", "settings": settings}); return
            if path == "/api/editor/open":
                sid = body.get("sid"); relative = body.get("path")
                store.safe_component(sid)
                manifest = store.get_skill(sid)
                if not manifest or manifest.get("channel") != store.UL_ROLE:
                    raise ValueError("外部编辑器只允许打开 ul 文件")
                file_path = store._editable_path(store.safe_path(STORE_DIR, sid), relative)
                if not apply:
                    self._json({"mode": "plan", "path": str(file_path)}); return
                settings = store.load_settings()
                command = settings.get("editor") or os.environ.get("VISUAL") or os.environ.get("EDITOR")
                if not command:
                    raise ValueError("未配置安全的外部编辑器")
                argv = shlex.split(command)
                if not argv or not shutil.which(argv[0]):
                    raise ValueError("编辑器命令不可执行或未找到")
                subprocess.Popen(argv + [str(file_path)], shell=False, close_fds=True)
                store.audit_log("editor_open", sid=sid, detail={"path": relative, "editor": argv[0]})
                self._json({"mode": "apply", "opened": True, "path": str(file_path)}); return
            if path == "/api/mcp/import":
                source = body.get("source", "workbuddy")
                result = mcp.import_from_agent(source, apply=apply)
                self._json({"mode": "apply" if apply else "plan", "source": source,
                            "count": result[0], "servers": result[1], "envs": result[2]}); return
            if path == "/api/mcp/edit":
                sid = body.get("sid"); store.safe_component(sid)
                changes = body.get("changes", {})
                if not isinstance(changes, dict):
                    raise ValueError("changes 必须是对象")
                if any(key in changes for key in ("command", "args")):
                    raise ValueError("GUI 不能修改 MCP command/args，请用 CLI")
                self._json(mcp.edit_definition(sid, changes) if apply else {"mode": "plan", "sid": sid, "changes": changes}); return
            if path == "/api/mcp/generate":
                sid = body.get("sid"); agents = body.get("agents", [])
                if not isinstance(sid, str) or not isinstance(agents, list):
                    raise ValueError("MCP sid/agents 无效")
                result = mcp.plan_generate(sid, agents)
                if apply:
                    result = mcp.apply_generate(sid, agents, resolve=None)
                self._json({"mode": "apply" if apply else "plan", "actions": result}); return
            if path == "/api/model-groups":
                suggestions = body.get("suggestions")
                if not isinstance(suggestions, list):
                    raise ValueError("suggestions 必须是数组")
                self._json(store.apply_model_suggestions(suggestions, confirm=True) if apply and body.get("confirm") else {"mode": "plan", "suggestions": suggestions, "safe_to_call": False}); return
            if path == "/api/models/suggest":
                agent = body.get("agent"); model = body.get("model"); sids = body.get("sids", [])
                if not isinstance(agent, str) or not isinstance(model, str) or not isinstance(sids, list) or not sids:
                    raise ValueError("agent/model/sids 无效")
                index = store.load_index()
                summaries = []
                for sid in sids:
                    store.safe_component(sid)
                    manifest = index.get(sid)
                    if manifest is None:
                        raise ValueError(f"不存在的 skill: {sid}")
                    summaries.append({"sid": sid, "name": manifest.get("name", ""),
                                      "description": manifest.get("fm_desc", "")})
                self._json(suggest_model_groups(summaries, agent, model)); return
            self._json({"error": "not found"}, 404)
        except (ValueError, OSError) as exc:
            self._audit_write = False
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        except Exception as exc:
            self._audit_write = False
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def serve(port: int = DEFAULT_PORT, open_browser: bool = True,
          read_only: bool = False) -> None:
    store.harden_home_permissions()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    httpd.skillhub_token = secrets.token_urlsafe(32)
    httpd.skillhub_read_only = bool(read_only)
    url = f"http://127.0.0.1:{port}/?token={httpd.skillhub_token}"
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    mode = "只读" if read_only else "可写"
    print(f"skillhub GUI ({mode}): {url}")
    print("请使用上面的 URL 打开；令牌只打印一次。Ctrl-C 退出。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
