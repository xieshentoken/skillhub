"""MCP 配置层统一 (阶段2) — 中央 MCP server 定义 + 各 agent 格式生成器。

中央定义 (~/.skillhub/mcp/index.json):
  server_id -> {id, label, transport: http|stdio, url / command+args,
                headers / env, enabled, agents: [适用agent], source}

安全原则:
- 定义里只写 {{env:VAR}} 引用密钥, 绝不落盘明文。
- import 只复制定义、不写回原文件; 密钥值仅在内存中用于推导变量名, 输出只给掩码。
- 生成目标配置前先备份, 默认 dry-run。

生成格式:
  pi        -> ~/.agents/servers/<id>.json      (host-core McpConfig, camelCase)
  codex     -> ~/.codex/config.toml             [[mcp_servers.<id>]] 段
  workbuddy -> ~/.workbuddy/mcp.json            {"mcpServers": {...}}
  claude    -> ~/.claude.json                   {"mcpServers": {...}}
  opencode  -> ~/.config/opencode/opencode.jsonc  {"mcp": {...}}
  hermes    -> 未发现 MCP 配置支持, 跳过
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from .config import AGENTS, BACKUP_DIR, MCP_INDEX_FILE, MCP_DIR

# 各 agent 的 MCP 配置文件 (不存在则生成时新建)
MCP_TARGETS = {
    "pi": ("servers", {"level": "global"}),          # 每个 server 一个文件
    "codex": ("config.toml", {}),
    "workbuddy": ("mcp.json", {}),
    "claude": (".claude.json", {}),
    "opencode": ("opencode.jsonc", {}),
}
# hermes 未发现 MCP 配置支持
MCP_TARGET_KEYS = {           # agent -> 写入键 (或特殊模式)
    "pi": "__per_file__",
    "codex": "mcp_servers",
    "workbuddy": "mcpServers",
    "claude": "mcpServers",
    "opencode": "mcp",
}

ENV_REF = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")

# 疑似密钥的值 (以掩码形式报告, 不存明文)
SECRET_PATTERNS = [
    re.compile(r"^Bearer\s+\S+", re.I),
    re.compile(r"^sk-[A-Za-z0-9]{8,}", re.I),
    re.compile(r"^gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"^xox[baprs]-"),
    re.compile(r"^AKIA[0-9A-Z]{16}"),
    re.compile(r"^[A-Za-z0-9_\-\.]{24,}$"),          # 长 token (通用兜底)
]


def ensure_home() -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)


def load_index() -> Dict[str, dict]:
    ensure_home()
    if not MCP_INDEX_FILE.exists():
        return {}
    try:
        return json.loads(MCP_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_index(index: Dict[str, dict]) -> None:
    ensure_home()
    tmp = MCP_INDEX_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(MCP_INDEX_FILE)


def mask(v: str, keep: int = 8) -> str:
    """掩码: 保留前 keep 字符, 其余打星。"""
    if len(v) <= keep:
        return "*" * len(v)
    return v[:keep] + "…" + "*" * 6 + f" (len={len(v)})"


def looks_secret(v: str) -> bool:
    if not isinstance(v, str) or not v.strip():
        return False
    return any(p.search(v) for p in SECRET_PATTERNS)


def _env_name(sid: str) -> str:
    """从 server id 推导环境变量名: qcc-company -> QCC_COMPANY_TOKEN。"""
    base = re.sub(r"[^A-Za-z0-9]+", "_", sid).strip("_").upper()
    return f"{base}_TOKEN"


def import_from_workbuddy(apply: bool = False) -> tuple[int, List[dict], List[str], List[str]]:
    """从 ~/.workbuddy/mcp.json 导入 mcpServers 到中央库。

    返回 (导入数, 定义列表, 环境变量名列表, 掩码值列表)。只复制定义, 不改原文件。
    """
    path = AGENTS["workbuddy"]["skill_dir"].parent / "mcp.json"
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or {}
    return _import_from_source("workbuddy", str(path), servers, apply)


def _import_from_source(source: str, path: str, servers: dict,
                        apply: bool) -> tuple[int, List[dict], List[str], List[str]]:
    index = load_index()
    value_to_env: Dict[str, str] = {}
    seen_env: List[str] = []
    seen_mask: List[str] = []
    imported = 0
    definitions: List[dict] = []

    for key, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        cfg = dict(cfg)
        headers = dict(cfg.get("headers") or {})
        env = dict(cfg.get("env") or {})
        for container in (headers, env):
            for k, v in list(container.items()):
                s = str(v)
                if looks_secret(s):
                    if s not in value_to_env:
                        name = _env_name(key if k.lower() == "authorization" else f"{key}_{k}")
                        # 同名冲突: 若该值已在其它 server 出现过, 复用旧变量名
                        existing = next((n for n, val in value_to_env.items() if val == s), None)
                        if existing:
                            name = existing
                        else:
                            value_to_env[s] = name
                            seen_env.append(name)
                            seen_mask.append(mask(s, 10))
                    container[k] = "{{env:%s}}" % value_to_env[s]
        transport = (cfg.get("transport") or
                     ("stdio" if cfg.get("command") else "http"))
        definition = {
            "id": key,
            "label": cfg.get("label") or key,
            "transport": transport,
            "enabled": bool(cfg.get("enabled", True)),
            "agents": [source],
            "source": source,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        if transport == "http":
            definition["url"] = cfg.get("url")
            if headers:
                definition["headers"] = headers
        else:
            definition["command"] = cfg.get("command")
            definition["args"] = cfg.get("args") or []
            if env:
                definition["env"] = env
        definitions.append(definition)
        if apply:
            index[key] = definition
        imported += 1

    if apply:
        save_index(index)
    return imported, definitions, seen_env, seen_mask


def list_servers() -> List[dict]:
    index = load_index()
    return sorted(index.values(), key=lambda d: d.get("id", ""))


def get_server(sid: str) -> Optional[dict]:
    return load_index().get(sid)


# ---------------- 各 agent 格式渲染 ----------------

def _render_value(v: str, syntax: str, resolve: Optional[dict] = None) -> str:
    """把 {{env:VAR}} 引用转成目标语法; resolve 提供 {VAR: 字面值} 时直接注入。"""
    def repl(m: re.Match) -> str:
        var = m.group(1)
        if resolve and var in resolve:
            return resolve[var]
        return syntax.replace("VAR", var)
    return ENV_REF.sub(repl, v)


def _render_pi(definition: dict, resolve: Optional[dict] = None) -> dict:
    """host-core McpConfig (camelCase), 写 ~/.agents/servers/<id>.json。"""
    d = {
        "id": definition["id"],
        "label": definition.get("label", definition["id"]),
        "transport": definition["transport"],
    }
    if definition.get("description"):
        d["description"] = definition["description"]
    if definition["transport"] == "http":
        d["url"] = _render_value(definition["url"], "${VAR}", resolve)
        if definition.get("headers"):
            d["headers"] = {k: _render_value(v, "${VAR}", resolve)
                            for k, v in definition["headers"].items()}
    else:
        d["command"] = definition.get("command")
        d["args"] = definition.get("args") or []
        if definition.get("env"):
            d["env"] = {k: _render_value(v, "${VAR}", resolve)
                        for k, v in definition["env"].items()}
    return d


def _render_toml(definition: dict, resolve: Optional[dict] = None) -> str:
    """codex config.toml 的 [[mcp_servers.<id>]] 段。"""
    sid = definition["id"]
    lines = [f"[[mcp_servers.{sid}]]"]
    if definition["transport"] == "http":
        lines.append(f"  url = \"{_render_value(definition['url'], '${VAR}', resolve)}\"")
        for k, v in (definition.get("headers") or {}).items():
            lines.append(f"  [mcp_servers.{sid}.headers]")
            lines.append(f"  {k} = \"{_render_value(v, '${VAR}', resolve)}\"")
    else:
        lines.append(f"  command = \"{definition.get('command')}\"")
        args = definition.get("args") or []
        if args:
            lines.append("  args = [" + ", ".join(f'"{a}"' for a in args) + "]")
        for k, v in (definition.get("env") or {}).items():
            lines.append(f"  [mcp_servers.{sid}.env]")
            lines.append(f"  {k} = \"{_render_value(v, '${VAR}', resolve)}\"")
    return "\n".join(lines) + "\n"


def _render_json_block(definition: dict, syntax: str, type_name: str, resolve: Optional[dict] = None) -> dict:
    """workbuddy/claude/opencode 的 mcpServers / mcp 条目。

    type_name: workbuddy 原格式无 type 字段 (兼容), claude 用 "http",
               opencode 远程 server 用 "remote"。
    """
    d: dict = {}
    if type_name:
        d["type"] = type_name
    if definition["transport"] == "http":
        d["url"] = _render_value(definition["url"], syntax, resolve)
        if definition.get("headers"):
            d["headers"] = {k: _render_value(v, syntax, resolve)
                            for k, v in definition["headers"].items()}
    else:
        d["command"] = definition.get("command")
        d["args"] = definition.get("args") or []
        if definition.get("env"):
            d["env"] = {k: _render_value(v, syntax, resolve)
                        for k, v in definition["env"].items()}
    return d


# 各 agent 的 type 字段 (None=不加, 保持原格式)
_JSON_TYPE = {"workbuddy": None, "claude": "http", "opencode": "remote"}


def plan_generate(sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    """生成计划(不写入)。返回 {agent: [action,...]}。"""
    definition = get_server(sid)
    if definition is None:
        raise ValueError(f"中央库中不存在 MCP server: {sid}")
    plan: Dict[str, List[dict]] = {}
    for agent in agents:
        if agent == "hermes":
            plan[agent] = [{"type": "skip", "target": "hermes",
                            "detail": "hermes 未发现 MCP 配置支持"}]
            continue
        if agent == "pi":
            target = _pi_server_path(definition["id"])
            plan[agent] = [{"type": "write", "target": str(target),
                            "detail": f"{len(json.dumps(_render_pi(definition)))}B JSON"}]
        else:
            target = _agent_mcp_file(agent)
            plan[agent] = [{"type": "merge", "target": str(target),
                            "detail": f"merge key {MCP_TARGET_KEYS[agent]}.{sid}"}]
    return plan


def _pi_server_path(sid: str) -> Path:
    """pi 的 server 文件: ~/.agents/servers/<id>.json, 可用 SKILLHUB_MCP_PI_DIR 覆盖。"""
    override = os.environ.get("SKILLHUB_MCP_PI_DIR")
    base = Path(override) if override else AGENTS["pi"]["skill_dir"].parent / "servers"
    return base / f"{sid}.json"


def _agent_mcp_file(agent: str) -> Path:
    """各 agent 的 MCP 配置文件, 可用 SKILLHUB_MCP_FILE_<agent> 覆盖 (测试用)。"""
    override = os.environ.get(f"SKILLHUB_MCP_FILE_{agent}")
    if override:
        return Path(override)
    root = AGENTS[agent]["skill_dir"].parent
    kind = MCP_TARGETS[agent][0]
    if kind == "config.toml":
        return root / "config.toml"
    if kind == ".claude.json":
        return Path.home() / ".claude.json"
    if kind == "opencode.jsonc":
        return Path.home() / ".config" / "opencode" / "opencode.jsonc"
    return root / "mcp.json"


def _load_json_doc(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_doc(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _toml_has_server(path: Path, sid: str) -> bool:
    if not path.exists():
        return False
    try:
        return f"[[mcp_servers.{sid}]]" in path.read_text(encoding="utf-8")
    except Exception:
        return False


def apply_generate(sid: str, agents: List[str], backup: bool = True,
                   resolve: Optional[dict] = None) -> Dict[str, List[dict]]:
    """执行生成。返回执行结果。resolve: {VAR: 字面值} 用于注入不展开 env 的 agent。"""
    definition = get_server(sid)
    if definition is None:
        raise ValueError(f"中央库中不存在 MCP server: {sid}")
    results: Dict[str, List[dict]] = {}
    ts = time.strftime("%Y%m%d-%H%M%S")
    touched: List[Path] = []
    for agent in agents:
        if agent == "hermes":
            results[agent] = [{"type": "skip", "target": "hermes",
                               "detail": "hermes 未发现 MCP 配置支持"}]
            continue
        if agent == "pi":
            target = _pi_server_path(definition["id"])
            target.parent.mkdir(parents=True, exist_ok=True)
            _write_doc(target, _render_pi(definition, resolve))
            touched.append(target)
            results[agent] = [{"type": "write", "target": str(target),
                               "detail": "pi McpConfig 已写入"}]
            continue
        if agent == "codex":
            target = _agent_mcp_file(agent)
            if _toml_has_server(target, definition["id"]):
                results[agent] = [{"type": "skip", "target": str(target),
                                   "detail": f"config.toml 已含 {definition['id']}"}]
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "a", encoding="utf-8") as f:
                f.write("\n" + _render_toml(definition, resolve))
            touched.append(target)
            results[agent] = [{"type": "append", "target": str(target),
                               "detail": "config.toml 追加 mcp_servers 段"}]
            continue
        target = _agent_mcp_file(agent)
        doc = _load_json_doc(target)
        key = MCP_TARGET_KEYS[agent]
        block = doc.setdefault(key, {})
        syntax = "{env:VAR}" if agent == "claude" else "${VAR}"
        block[definition["id"]] = _render_json_block(definition, syntax, _JSON_TYPE.get(agent), resolve)
        _write_doc(target, doc)
        touched.append(target)
        results[agent] = [{"type": "merge", "target": str(target),
                           "detail": f"{key}.{definition['id']} 已合并"}]
    if backup and touched:
        dest = BACKUP_DIR / ts / "mcp"
        dest.mkdir(parents=True, exist_ok=True)
        for t in touched:
            if t.exists():
                rel = t.relative_to(t.anchor) if t.is_absolute() else t
                target_bk = dest / str(rel).replace("/", "__")
                shutil.copy2(t, target_bk)
    return results
