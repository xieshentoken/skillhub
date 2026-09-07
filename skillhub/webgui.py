"""本地 Web GUI (只读) — 浏览 agent 接入状态 / 中央库 / MCP / 备份。

启动: skillhub gui [--port 8317] [--no-browser]
- 只绑定 127.0.0.1, 只实现 GET, 不提供任何写操作 (投影/导入仍走 CLI)。
- 数据接口:
    GET /api/data        一次性返回概览数据 (agents + skills + mcp + backups)
    GET /api/skill/<sid> 单个 skill 详情 (manifest + store 文件清单 + 各 agent 投影目标)
"""
from __future__ import annotations

import json
import os
import threading
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import adapters, mcp, store
from .config import AGENTS, BACKUP_DIR, INDEX_FILE, MCP_INDEX_FILE, STORE_DIR

HTML_FILE = Path(__file__).with_name("gui.html")
DEFAULT_PORT = 8317


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _list_store_files(sid: str, limit: int = 200) -> list:
    """列出中央库某 skill 的文件 (相对路径), sid 必须在索引里, 防路径穿越。"""
    if sid not in store.load_index():
        return []
    root = STORE_DIR / sid
    out = []
    if not root.exists():
        return out
    excluded = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", ".cache"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in excluded and not d.startswith(".")]
        for f in sorted(filenames):
            p = Path(dirpath) / f
            rel = str(p.relative_to(root))
            try:
                size = p.stat().st_size
            except OSError:
                size = -1
            out.append({"path": rel, "size": size})
            if len(out) >= limit:
                out.append({"path": f"... 共超过 {limit} 个文件", "size": -1})
                return out
    return out


def collect_data() -> dict:
    """一次性收集 GUI 首屏需要的全部数据。"""
    index = store.load_index()
    st = adapters.status()  # {agent: [{sid,name,state}]}

    # 反转成 sid -> {agent: state}
    state_by_sid: dict = {}
    for agent, items in st.items():
        for item in items:
            state_by_sid.setdefault(item["sid"], {})[agent] = item["state"]

    agents = []
    for agent, cfg in AGENTS.items():
        items = st.get(agent, [])
        counts = Counter(i["state"] for i in items)
        mcp_target = mcp.MCP_TARGETS.get(agent)
        agents.append({
            "name": agent,
            "skill_dir": str(cfg["skill_dir"]),
            "dir_exists": cfg["skill_dir"].exists(),
            "mode": cfg.get("mode", "symlink"),
            "nested": bool(cfg.get("nested")),
            "mcp_target": (mcp_target[0] if mcp_target else None),
            "mcp_key": mcp.MCP_TARGET_KEYS.get(agent),
            "mcp_supported": agent in mcp.MCP_TARGETS,
            "linked": counts.get("linked", 0),
            "conflict": counts.get("conflict", 0),
            "not_linked": counts.get("not_linked", 0),
            "total_in_store": len(index),
        })

    skills = []
    for sid, man in sorted(index.items(), key=lambda kv: kv[1].get("name", "")):
        skills.append({
            "id": sid,
            "name": man.get("name", ""),
            "category": man.get("category", ""),
            "desc": man.get("fm_desc", ""),
            "risks": man.get("risks", []),
            "size": man.get("size", 0),
            "imported_at": man.get("imported_at", ""),
            "source_agents": man.get("agents", []),
            "source_paths": [s.get("path", "") for s in man.get("sources", [])],
            "states": state_by_sid.get(sid, {}),
        })

    servers = []
    for s in mcp.list_servers():
        servers.append({
            "id": s.get("id", ""),
            "label": s.get("label", ""),
            "transport": s.get("transport", ""),
            "url": s.get("url", ""),
            "command": s.get("command", ""),
            "args": s.get("args", []),
            "env_keys": sorted((s.get("env") or {}).keys()),
            "header_keys": sorted((s.get("headers") or {}).keys()),
            "enabled": s.get("enabled", True),
            "agents": s.get("agents", []),
            "source": s.get("source", ""),
        })

    backups = []
    for b in adapters.list_backups():
        try:
            size = _dir_size(b)
        except OSError:
            size = 0
        has_conflicts = (b / "conflicts").exists() and any((b / "conflicts").iterdir())
        backups.append({"ts": b.name, "size": size, "has_conflicts": has_conflicts})

    risky = sum(1 for s in skills if s["risks"])
    return {
        "generated_at": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
        "paths": {
            "store": str(STORE_DIR),
            "index": str(INDEX_FILE),
            "mcp_index": str(MCP_INDEX_FILE),
            "backups": str(BACKUP_DIR),
        },
        "summary": {
            "skills": len(skills),
            "risky": risky,
            "agents": len(agents),
            "mcp_servers": len(servers),
            "backups": len(backups),
            "linked_total": sum(a["linked"] for a in agents),
        },
        "agents": agents,
        "skills": skills,
        "mcp_servers": servers,
        "backups": backups,
    }


class _Handler(BaseHTTPRequestHandler):
    server_version = "skillhub-gui"

    def log_message(self, fmt, *args):  # 安静模式: 只记非 200
        if args and args[1] != 200:
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                body = HTML_FILE.read_bytes()
                self._send(200, body, "text/html; charset=utf-8")
            elif path == "/api/data":
                self._json(collect_data())
            elif path.startswith("/api/skill/"):
                sid = path[len("/api/skill/"):].strip("/")
                index = store.load_index()
                if sid not in index:
                    self._json({"error": f"中央库中不存在 skill: {sid}"}, 404)
                    return
                man = index[sid]
                states = {}
                for agent in AGENTS:
                    target = adapters.target_path(agent, man)
                    ok = adapters._is_our_projection(target, sid)
                    states[agent] = {
                        "state": "linked" if ok else ("conflict" if target.exists() else "not_linked"),
                        "target": str(target),
                    }
                self._json({
                    "manifest": man,
                    "files": _list_store_files(sid),
                    "projections": states,
                    "store_dir": str(STORE_DIR / sid),
                })
            elif path == "/api/health":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:  # 任何异常都转成 JSON, 前端可见
            try:
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)
            except Exception:
                pass


def serve(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"skillhub GUI: {url}  (Ctrl-C 退出; 只读, 不提供写操作)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        httpd.server_close()
