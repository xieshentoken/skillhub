"""MCP 定义导入、脱敏和各 agent 配置生成。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - project requires Python 3.11+
    tomllib = None

from .config import AGENTS, BACKUP_DIR, MCP_DIR, MCP_INDEX_FILE
from .store import (CorruptIndexError, atomic_write, locked, safe_component,
                    safe_path, write_lock)


MCP_TARGETS = {
    "pi": ("servers", {}),
    "codex": ("config.toml", {}),
    "grok": ("config.toml", {}),
    "workbuddy": ("mcp.json", {}),
    "claude": (".claude.json", {}),
    "opencode": ("opencode.jsonc", {}),
}
MCP_TARGET_KEYS = {
    "pi": "__per_file__",
    "codex": "mcp_servers",
    "grok": "mcp_servers",
    "workbuddy": "mcpServers",
    "claude": "mcpServers",
    "opencode": "mcp",
}

ENV_REF = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
CANON_ENV_REF = re.compile(r"\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}")
SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|passwd|credential|api[_-]?key|private[_-]?key|authorization|bearer|cookie|auth|(?:^|[-_])key(?:$|[-_]))",
    re.I,
)
SENSITIVE_QUERY = re.compile(r"(?:token|secret|password|passwd|key|auth|credential)", re.I)
SENSITIVE_FLAG = re.compile(r"(?:token|secret|password|api[-_]?key|auth|credential|(?:^|[-_])key(?:$|[-_]))", re.I)


def ensure_home() -> None:
    MCP_DIR.mkdir(parents=True, exist_ok=True)


def _validate_index(index: object) -> Dict[str, dict]:
    if not isinstance(index, dict):
        raise CorruptIndexError("MCP 索引必须是 JSON 对象")
    for sid, definition in index.items():
        try:
            safe_component(sid)
        except ValueError as exc:
            raise CorruptIndexError(f"MCP server ID 不安全: {sid!r}") from exc
        if not isinstance(definition, dict):
            raise CorruptIndexError(f"MCP server {sid!r} 定义必须是对象")
    return index


def load_index() -> Dict[str, dict]:
    if not MCP_INDEX_FILE.exists():
        return {}
    try:
        value = json.loads(MCP_INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptIndexError(f"MCP 索引损坏，未覆盖: {MCP_INDEX_FILE}: {exc}") from exc
    return _validate_index(value)


@locked
def save_index(index: Dict[str, dict]) -> None:
    _validate_index(index)
    ensure_home()
    atomic_write(MCP_INDEX_FILE, json.dumps(index, ensure_ascii=False, indent=2) + "\n")


def _env_match(match: re.Match) -> str:
    return next(group for group in match.groups() if group)


def _canonical_env_refs(value: str) -> str:
    return ENV_REF.sub(lambda match: "{{env:%s}}" % _env_match(match), value)


def mask(value: str, keep: int = 0) -> str:
    """日志/API 只显示类型和长度，不显示短凭证前缀。"""
    if not isinstance(value, str):
        return "***"
    return "*** (len=%d)" % len(value)


def looks_secret(value: str, key: str = "") -> bool:
    if not isinstance(value, str) or not value:
        return False
    return bool(SENSITIVE_KEY.search(key) or
                re.search(r"^Bearer\s+\S+", value, re.I) or
                re.search(r"(?:sk-|gh[pousr]_\w+|xox[baprs]-|AKIA[0-9A-Z]{16})", value, re.I))


def _env_name(sid: str, key: str = "token") -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "_", sid).strip("_").upper() or "MCP"
    suffix = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper() or "TOKEN"
    if suffix in {"AUTHORIZATION", "BEARER"}:
        suffix = "TOKEN"
    if base[0].isdigit():
        base = "MCP_" + base
    return f"{base}_{suffix}"


def _new_env_name(sid: str, key: str, value_to_env: dict[str, str],
                  seen_env: list[str], reserved_env: Optional[set[str]] = None) -> str:
    """Return a valid, unused variable name for a newly seen secret.

    A URL may contain the same query key more than once.  Deriving the name
    from only ``sid`` and that key would make two different values share one
    environment variable, so add a stable-in-this-import suffix when needed.
    """
    base = _env_name(sid, key)
    used = set(value_to_env.values()) | set(seen_env) | (reserved_env or set())
    if base not in used:
        return base
    index = 2
    while f"{base}_{index}" in used:
        index += 1
    return f"{base}_{index}"


def _secret_reference(value: str, sid: str, key: str,
                      value_to_env: dict[str, str], seen_env: list[str],
                      seen_masks: list[str],
                      reserved_env: Optional[set[str]] = None) -> str:
    """Convert one complete secret value to a canonical environment ref."""
    value = _canonical_env_refs(value)
    if ENV_REF.search(value):
        if CANON_ENV_REF.fullmatch(value):
            return value
        if re.fullmatch(r"(?i:Bearer)\s+\{\{env:[A-Za-z_][A-Za-z0-9_]*\}\}", value):
            return value
        raise ValueError("敏感值同时包含环境引用和明文，无法安全拆分")
    var = value_to_env.get(value)
    if not var:
        var = _new_env_name(sid, key, value_to_env, seen_env, reserved_env)
        value_to_env[value] = var
        seen_env.append(var)
        seen_masks.append(mask(value))
        if reserved_env is not None:
            reserved_env.add(var)
    return "{{env:%s}}" % var


def _quote_with_env_refs(value: str, safe: str = "") -> str:
    """Quote a URL component while keeping canonical/target env refs readable."""
    pieces = []
    cursor = 0
    for match in ENV_REF.finditer(value):
        pieces.append(quote(value[cursor:match.start()], safe=safe))
        pieces.append(match.group(0))
        cursor = match.end()
    pieces.append(quote(value[cursor:], safe=safe))
    return "".join(pieces)


def _url_query(value: str, sid: str, key: str,
               value_to_env: dict[str, str], seen_env: list[str],
               seen_masks: list[str],
               reserved_env: Optional[set[str]] = None) -> str:
    """Sanitize query components without percent-encoding env references."""
    try:
        pairs = parse_qsl(value, keep_blank_values=True)
    except ValueError:
        pairs = []
    if not pairs and value:
        # Keep a malformed/opaque query safe rather than silently dropping it.
        return _quote_with_env_refs(_canonical_env_refs(value), safe="-._~")
    rendered = []
    for name, item in pairs:
        name = _quote_with_env_refs(_canonical_env_refs(name), safe="-._~")
        item = _canonical_env_refs(item)
        if SENSITIVE_QUERY.search(name):
            if item and not CANON_ENV_REF.fullmatch(item):
                item = _secret_reference(item, sid, name, value_to_env,
                                         seen_env, seen_masks, reserved_env)
        item = _quote_with_env_refs(item, safe="-._~!$'()*+,;:@/?")
        rendered.append(f"{name}={item}")
    return "&".join(rendered)


def _replace_secret(value: str, sid: str, key: str,
                    value_to_env: dict[str, str], seen_env: list[str],
                    seen_masks: list[str],
                    reserved_env: Optional[set[str]] = None) -> str:
    value = _canonical_env_refs(value)
    parsed = None
    try:
        parsed = urlsplit(value)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme and parsed.netloc:
        # Process userinfo and query independently.  Returning after userinfo
        # used to leave a second secret in the query untouched.
        user = parsed.username
        password = parsed.password
        netloc = parsed.netloc
        if user is not None or password is not None:
            user = unquote(user or "")
            password = unquote(password or "") if password is not None else None
            if password is not None:
                password = _secret_reference(password, sid, "url_password",
                                             value_to_env, seen_env, seen_masks,
                                             reserved_env)
            elif user:
                user = _secret_reference(user, sid, "url_user",
                                         value_to_env, seen_env, seen_masks,
                                         reserved_env)
            user_part = _quote_with_env_refs(user, safe="-._~")
            if password is not None:
                user_part += ":" + _quote_with_env_refs(password, safe="-._~")
            host = parsed.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError:
                port = ""
            netloc = f"{user_part}@{host}{port}"
        query = _url_query(parsed.query, sid, key or "url_query",
                           value_to_env, seen_env, seen_masks, reserved_env)
        return urlunsplit((parsed.scheme, netloc, parsed.path, query,
                           parsed.fragment))
    if re.fullmatch(r"(?i:Bearer)\s+\{\{env:[A-Za-z_][A-Za-z0-9_]*\}\}", value):
        return value
    if looks_secret(value, key):
        bearer = re.fullmatch(r"(?i:Bearer)\s+(.+)", value)
        if bearer and str(key).lower() in {"authorization", "bearer"}:
            # Keep the scheme in the central definition, but store only the
            # token in the generated variable so Codex can use its native
            # bearer_token_env_var field without a pseudo `${VAR}` literal.
            return "Bearer " + _secret_reference(
                bearer.group(1), sid, key, value_to_env, seen_env, seen_masks,
                reserved_env)
        return _secret_reference(value, sid, key, value_to_env, seen_env,
                                 seen_masks, reserved_env)
    if ENV_REF.search(value):
        return value
    return value


def _sanitize(value, sid: str, key: str, value_to_env: dict[str, str],
              seen_env: list[str], seen_masks: list[str],
              reserved_env: Optional[set[str]] = None):
    # Codex uses these fields as environment-variable *names*, not secret
    # values.  Do not let the generic sensitive-key sanitizer turn ``TOKEN``
    # into a different generated variable name.
    if key == "bearer_token_env_var" and isinstance(value, str):
        return _canonical_env_refs(value)
    if key == "env_http_headers" and isinstance(value, dict):
        return {str(name): (_canonical_env_refs(item) if isinstance(item, str) else item)
                for name, item in value.items()}
    if isinstance(value, dict):
        return {str(k): _sanitize(v, sid, str(k), value_to_env, seen_env,
                                   seen_masks, reserved_env)
                for k, v in value.items()}
    if isinstance(value, list):
        out = []
        previous_flag = ""
        for item in value:
            if isinstance(item, str) and "=" in item:
                flag, raw = item.split("=", 1)
                if (flag.startswith("-") and SENSITIVE_FLAG.search(flag) and raw and
                        not ENV_REF.search(raw)):
                    out.append(flag + "=" + _replace_secret(
                        raw, sid, flag, value_to_env, seen_env, seen_masks,
                        reserved_env))
                    previous_flag = ""
                    continue
            item_key = previous_flag or key
            out.append(_sanitize(item, sid, item_key, value_to_env, seen_env,
                                 seen_masks, reserved_env))
            if isinstance(item, str) and item.startswith("-"):
                previous_flag = item
            else:
                previous_flag = ""
        return out
    if isinstance(value, str):
        return _replace_secret(value, sid, key, value_to_env, seen_env,
                               seen_masks, reserved_env)
    return value


def _server_transport(cfg: dict) -> str:
    kind = str(cfg.get("transport") or cfg.get("type") or "").lower()
    if kind in {"stdio", "local"} or cfg.get("command"):
        return "stdio"
    return "http"


def _server_fields(cfg: dict, sid: str, value_to_env: dict[str, str],
                   seen_env: list[str], seen_masks: list[str],
                   reserved_env: Optional[set[str]] = None) -> dict:
    return _sanitize(copy.deepcopy(cfg), sid, "", value_to_env, seen_env,
                     seen_masks, reserved_env)


def _collect_env_names(value, reserved: set[str], key: str = "") -> None:
    """Reserve source env references before deriving new names."""
    if isinstance(value, dict):
        for name, item in value.items():
            name = str(name)
            if name == "bearer_token_env_var" and isinstance(item, str):
                canonical = _canonical_env_refs(item)
                match = CANON_ENV_REF.fullmatch(canonical)
                if match:
                    reserved.add(match.group(1))
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", canonical):
                    reserved.add(canonical)
            elif name == "env_http_headers" and isinstance(item, dict):
                for variable in item.values():
                    if isinstance(variable, str):
                        canonical = _canonical_env_refs(variable)
                        match = CANON_ENV_REF.fullmatch(canonical)
                        if match:
                            reserved.add(match.group(1))
                        elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", canonical):
                            reserved.add(canonical)
            _collect_env_names(item, reserved, name)
    elif isinstance(value, list):
        for item in value:
            _collect_env_names(item, reserved, key)
    elif isinstance(value, str):
        reserved.update(_env_match(match) for match in ENV_REF.finditer(value))


def _definition_from_config(sid: str, cfg: dict, source: str,
                            value_to_env: dict[str, str], seen_env: list[str],
                            seen_masks: list[str],
                            reserved_env: Optional[set[str]] = None) -> dict:
    safe_component(sid)
    if not isinstance(cfg, dict):
        raise ValueError(f"MCP server {sid!r} 定义必须是对象")
    if reserved_env is not None:
        _collect_env_names(cfg, reserved_env)
    fields = _server_fields(cfg, sid, value_to_env, seen_env, seen_masks,
                            reserved_env)
    transport = _server_transport(fields)
    definition = {
        "schema_version": 2,
        "id": sid,
        "label": fields.get("label") or sid,
        "transport": transport,
        "enabled": (not bool(fields.get("disabled"))) if "disabled" in fields else bool(fields.get("enabled", True)),
        "agents": [source],
        "source": source,
        "imported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "fields": fields,
    }
    if transport == "http":
        definition["url"] = fields.get("url")
        if fields.get("headers"):
            definition["headers"] = fields["headers"]
        if fields.get("http_headers"):
            definition["http_headers"] = fields["http_headers"]
        if fields.get("env_http_headers"):
            definition["env_http_headers"] = fields["env_http_headers"]
        if fields.get("bearer_token_env_var"):
            definition["bearer_token_env_var"] = fields["bearer_token_env_var"]
    else:
        definition["command"] = fields.get("command")
        definition["args"] = fields.get("args") or []
        if fields.get("env"):
            definition["env"] = fields["env"]
        if fields.get("environment"):
            definition["environment"] = fields["environment"]
    return definition


def _jsonc_strip(text: str) -> str:
    out, i, quoted, escaped = [], 0, False, False
    while i < len(text):
        char = text[i]
        if quoted:
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            i += 1; continue
        if char == '"':
            quoted = True; out.append(char); i += 1; continue
        if char == "/" and i + 1 < len(text) and text[i + 1] == "/":
            i += 2
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        if char == "/" and i + 1 < len(text) and text[i + 1] == "*":
            i += 2
            while i + 1 < len(text) and text[i:i + 2] != "*/":
                i += 1
            i += 2
            continue
        out.append(char); i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _json_servers(agent: str, data: dict) -> dict:
    if agent == "opencode":
        mcp = data.get("mcp") or {}
        nested = mcp.get("servers") if isinstance(mcp, dict) else None
        # 仅把 servers 当兼容容器：direct mcp 中名为 "servers" 的合法
        # server 自身会含 type/command/url 等字段，不能误判为容器。
        if (isinstance(nested, dict) and
                not any(key in nested for key in ("type", "command", "url", "enabled", "disabled"))):
            return mcp["servers"]
        return mcp if isinstance(mcp, dict) else {}
    value = data.get(MCP_TARGET_KEYS[agent])
    return value if isinstance(value, dict) else {}


def _parse_toml(path: Path) -> dict:
    if tomllib is None:
        raise ValueError("Python 3.11 的 tomllib 是 Codex TOML 解析要求")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"TOML 配置无法解析，已停止且未覆盖: {path}: {exc}") from exc


def _source_servers(agent: str, path: Path) -> dict:
    kind = MCP_TARGETS[agent][0]
    if not path.exists():
        raise FileNotFoundError(f"找不到 {path}")
    if kind == "config.toml":
        return (_parse_toml(path).get("mcp_servers") or {})
    raw = path.read_text(encoding="utf-8")
    if kind == "opencode.jsonc":
        raw = _jsonc_strip(raw)
    try:
        data = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON/JSONC 配置无法解析，已停止且未覆盖: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"MCP 配置根必须是对象: {path}")
    return _json_servers(agent, data)


def _agent_mcp_file(agent: str) -> Path:
    if agent not in MCP_TARGETS:
        raise ValueError(f"未知 MCP agent: {agent}")
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


def _pi_server_path(sid: str) -> Path:
    safe_component(sid)
    override = os.environ.get("SKILLHUB_MCP_PI_DIR")
    base = Path(override) if override else AGENTS["pi"]["skill_dir"].parent / "servers"
    return safe_path(base, f"{sid}.json", projection=True)


def import_from_agent(agent: str, apply: bool = False):
    if agent not in MCP_TARGETS or agent == "pi":
        raise ValueError(f"{agent} 未发现可反向导入的 MCP 文件")
    path = _agent_mcp_file(agent)
    servers = _source_servers(agent, path)
    value_to_env, seen_env, seen_masks, reserved_env = {}, [], [], set()
    definitions = []
    for sid, cfg in servers.items():
        if isinstance(cfg, list):
            if len(cfg) != 1 or not isinstance(cfg[0], dict):
                raise ValueError(f"MCP server {sid!r} 的旧 TOML 数组格式无歧义单项")
            cfg = cfg[0]
        definitions.append(_definition_from_config(str(sid), cfg, agent,
                                                   value_to_env, seen_env,
                                                   seen_masks, reserved_env))
    if apply:
        with write_lock():
            index = load_index()
            backup_ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            _backup_mcp_targets(backup_ts, [MCP_INDEX_FILE])
            for definition in definitions:
                index[definition["id"]] = definition
            save_index(index)
    return len(definitions), definitions, seen_env, seen_masks


def import_from_workbuddy(apply: bool = False):
    """兼容旧调用方；实际读取仍走统一的 agent 路径和解析校验。"""
    return import_from_agent("workbuddy", apply=apply)


def required_env_vars(definition: dict) -> list[str]:
    found = set()
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "bearer_token_env_var" and isinstance(item, str):
                    found.update(_env_match(match) for match in ENV_REF.finditer(item))
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
                        found.add(item)
                elif key == "env_http_headers" and isinstance(item, dict):
                    for variable in item.values():
                        if isinstance(variable, str):
                            found.update(_env_match(match) for match in ENV_REF.finditer(variable))
                            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                                found.add(variable)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            found.update(_env_match(match) for match in ENV_REF.finditer(value))
            # Older central entries may contain a URL whose reference was
            # percent-encoded by urlencode.  Decode only for discovery; the
            # stored value remains untouched until it is rewritten safely.
            decoded = unquote(value)
            found.update(_env_match(match) for match in ENV_REF.finditer(decoded))
    walk(definition)
    return sorted(found)


def _render_value(value, syntax: str, resolve: Optional[dict] = None):
    if not isinstance(value, str):
        return value
    def replace(match):
        var = _env_match(match)
        if resolve is not None:
            if var not in resolve:
                raise ValueError(f"缺少环境变量: {var}")
            return resolve[var]
        return syntax.replace("VAR", var)
    return ENV_REF.sub(replace, value)


def _render_url(value, syntax: str, resolve: Optional[dict] = None):
    """Render URL references, encoding resolved credentials/query values."""
    if not isinstance(value, str):
        return value
    value = _canonical_env_refs(value)
    try:
        parsed = urlsplit(value)
    except ValueError:
        return _render_value(value, syntax, resolve)
    if not parsed.scheme or not parsed.netloc:
        return _render_value(value, syntax, resolve)

    user_part = ""
    if parsed.username is not None or parsed.password is not None:
        user = _render_value(unquote(parsed.username or ""), syntax, resolve)
        user_part = (quote(user, safe="-._~") if resolve is not None
                     else _quote_with_env_refs(user, safe="-._~"))
        if parsed.password is not None:
            password = _render_value(unquote(parsed.password or ""), syntax, resolve)
            password = (quote(password, safe="-._~") if resolve is not None
                        else _quote_with_env_refs(password, safe="-._~"))
            user_part += ":" + password
        user_part += "@"
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    netloc = user_part + host + port

    query_parts = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        rendered_name = _render_value(name, syntax, resolve)
        rendered_item = _render_value(item, syntax, resolve)
        if resolve is None:
            rendered_name = _quote_with_env_refs(rendered_name, safe="-._~")
            rendered_item = _quote_with_env_refs(
                rendered_item, safe="-._~!$'()*+,;:@/?{}")
        else:
            rendered_name = quote(rendered_name, safe="-._~")
            rendered_item = quote(rendered_item, safe="-._~!$'()*+,;:@/")
        query_parts.append(f"{rendered_name}={rendered_item}")
    query = "&".join(query_parts)
    return urlunsplit((parsed.scheme, netloc, parsed.path, query,
                       parsed.fragment))


def _base_fields(definition: dict) -> dict:
    fields = copy.deepcopy(definition.get("fields") or {})
    if not fields:
        for key in ("url", "command", "args", "env", "environment", "headers",
                    "http_headers", "env_http_headers", "bearer_token_env_var"):
            if key in definition:
                fields[key] = copy.deepcopy(definition[key])
    return fields


def _stdio_parts(fields: dict, definition: dict, syntax: str,
                 resolve: Optional[dict]) -> tuple[str, list]:
    """Normalize string/array command forms before rendering them."""
    command = fields.get("command", definition.get("command"))
    args = fields.get("args", definition.get("args", [])) or []
    if isinstance(command, list):
        parts = list(command) + (list(args) if isinstance(args, list) else [args])
        command, args = (parts[0] if parts else ""), parts[1:]
    elif not isinstance(args, list):
        args = [args]
    command = _render_value(command, syntax, resolve) if isinstance(command, str) else command
    args = [_render_value(item, syntax, resolve) if isinstance(item, str) else item
            for item in args]
    return command or "", args


def _render_env(fields: dict, syntax: str, resolve: Optional[dict]):
    env = fields.get("env") or fields.get("environment")
    if not isinstance(env, dict):
        return None
    return {key: _render_value(value, syntax, resolve) if isinstance(value, str) else value
            for key, value in env.items()}


def _json_headers(fields: dict, definition: dict, syntax: str,
                  resolve: Optional[dict]) -> dict:
    """将 Claude/WorkBuddy/OpenCode 都能表达的 header 形状归一化。"""
    headers = {}
    for key in ("headers", "http_headers"):
        values = fields.get(key) or {}
        if isinstance(values, dict):
            headers.update(copy.deepcopy(values))
    for key, value in list(headers.items()):
        headers[key] = (_render_value(value, syntax, resolve)
                        if isinstance(value, str) else value)
    # Codex 的 env_http_headers 是 header -> 环境变量名，而 JSON 目标需要
    # 一个显式环境引用；不要把变量名误当成凭证字面值写出去。
    env_headers = fields.get("env_http_headers") or {}
    if isinstance(env_headers, dict):
        for key, variable in env_headers.items():
            if key in headers or not isinstance(variable, str):
                continue
            if not CANON_ENV_REF.fullmatch(variable) and not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", variable):
                raise ValueError(f"环境变量名无效: {variable}")
            reference = variable if CANON_ENV_REF.fullmatch(variable) else "{{env:%s}}" % variable
            headers[key] = _render_value(reference, syntax, resolve)
    bearer = definition.get("bearer_token_env_var") or fields.get("bearer_token_env_var")
    if bearer and "Authorization" not in headers and isinstance(bearer, str):
        reference = bearer if CANON_ENV_REF.fullmatch(bearer) else "{{env:%s}}" % bearer
        headers["Authorization"] = _render_value("Bearer " + reference, syntax, resolve)
    return headers


def _render_object(value, syntax: str, resolve: Optional[dict]):
    if isinstance(value, dict):
        return {key: _render_object(item, syntax, resolve) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_object(item, syntax, resolve) for item in value]
    return _render_value(value, syntax, resolve)


def _render_json(definition: dict, agent: str, resolve: Optional[dict] = None) -> dict:
    if definition.get("enabled") is False and agent == "claude":
        raise ValueError("Claude 目标没有确认可用的禁用字段，拒绝生成 disabled server")
    fields = _base_fields(definition)
    transport = definition.get("transport", "http")
    structural = {"transport", "type", "enabled", "disabled", "label",
                  "command", "args", "env", "environment", "url",
                  "headers", "http_headers", "env_http_headers",
                  "bearer_token_env_var"}
    if agent == "opencode":
        output = {key: _render_object(value, "{env:VAR}", resolve) for key, value in fields.items()
                  if key not in structural}
        if transport == "stdio":
            output["type"] = "local"
            command, args = _stdio_parts(fields, definition, "{env:VAR}", resolve)
            output["command"] = ([command] if command else []) + args
            env = _render_env(fields, "{env:VAR}", resolve)
            if env:
                output["environment"] = env
        else:
            output["type"] = "remote"
            output["url"] = _render_url(definition.get("url", fields.get("url")),
                                         "{env:VAR}", resolve)
            headers = _json_headers(fields, definition, "{env:VAR}", resolve)
            if headers:
                output["headers"] = headers
        # OpenCode's current schema uses a direct ``mcp`` map and the
        # ``enabled`` boolean on each local/remote server.  Do not translate
        # this to the older ``disabled`` spelling: doing so silently loses the
        # enabled state on a round trip.
        output["enabled"] = bool(definition.get("enabled", True))
        return output
    output = {key: _render_object(value, "${VAR}", resolve) for key, value in fields.items()
              if key not in structural}
    if agent == "claude":
        output["type"] = "stdio" if transport == "stdio" else "http"
    if transport == "stdio":
        command, args = _stdio_parts(fields, definition, "${VAR}", resolve)
        output["command"] = command
        output["args"] = args
        env = _render_env(fields, "${VAR}", resolve)
        if env:
            output["env"] = env
    else:
        output["url"] = _render_url(definition.get("url", fields.get("url")),
                                     "${VAR}", resolve)
        headers = _json_headers(fields, definition, "${VAR}", resolve)
        if headers:
            output["headers"] = headers
    if definition.get("enabled") is False:
        if agent == "workbuddy":
            output["enabled"] = False
        else:
            output["disabled"] = True
    return output


def _render_pi(definition: dict, resolve: Optional[dict] = None) -> dict:
    """Render the host-core ``McpConfig`` contract, independently of Claude."""
    if definition.get("enabled") is False:
        raise ValueError("Pi host-core McpConfig 没有确认可用的禁用字段，拒绝生成")
    fields = _base_fields(definition)
    transport = definition.get("transport", "http")
    output = {
        "id": definition["id"],
        "label": definition.get("label", definition["id"]),
        "transport": transport,
    }
    description = definition.get("description") or fields.get("description")
    if description:
        output["description"] = description
    if transport == "http":
        output["url"] = _render_url(definition.get("url", fields.get("url")),
                                     "${VAR}", resolve)
        headers = _json_headers(fields, definition, "${VAR}", resolve)
        if headers:
            output["headers"] = headers
    else:
        command, args = _stdio_parts(fields, definition, "${VAR}", resolve)
        output["command"] = command
        output["args"] = args
        env = _render_env(fields, "${VAR}", resolve)
        if env:
            output["env"] = env
    return output


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value, ensure_ascii=False)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_value(value):
    if isinstance(value, bool): return "true" if value else "false"
    if isinstance(value, (int, float)): return str(value)
    if isinstance(value, str): return _toml_string(value)
    if isinstance(value, list): return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}" for k, v in value.items()) + "}"
    return _toml_string(str(value))


def _codex_fields(definition: dict, resolve: Optional[dict]) -> dict:
    fields = _base_fields(definition)
    transport = definition.get("transport", "http")
    fields.pop("type", None); fields.pop("transport", None); fields.pop("label", None)
    fields.pop("enabled", None); fields.pop("disabled", None)
    if transport == "stdio":
        command, args = _stdio_parts(fields, definition, "${VAR}", resolve)
        fields["command"] = command
        fields["args"] = args
        env = _render_env(fields, "${VAR}", resolve)
        if env:
            fields["env"] = env
        for key in ("url", "headers", "http_headers", "env_http_headers",
                    "bearer_token_env_var", "environment"):
            fields.pop(key, None)
    else:
        fields["url"] = _render_url(fields.get("url", definition.get("url")),
                                     "${VAR}", resolve)
        ordinary_headers = {}
        for key in ("headers", "http_headers"):
            values = fields.pop(key, None)
            if isinstance(values, dict):
                ordinary_headers.update(values)
        existing_env = fields.pop("env_http_headers", None)
        env_headers = {}
        if isinstance(existing_env, dict):
            for key, variable in existing_env.items():
                if not isinstance(variable, str):
                    continue
                canonical = _canonical_env_refs(variable)
                match = CANON_ENV_REF.fullmatch(canonical)
                if match:
                    env_headers[key] = match.group(1)
                elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", canonical):
                    env_headers[key] = canonical
                else:
                    raise ValueError(f"Codex 环境变量名无效: {variable}")

        bearer = fields.pop("bearer_token_env_var", None)
        bearer_name = None
        if isinstance(bearer, str):
            canonical = _canonical_env_refs(bearer)
            match = CANON_ENV_REF.fullmatch(canonical)
            if match:
                bearer_name = match.group(1)
            elif re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", canonical):
                bearer_name = canonical
            else:
                raise ValueError("Codex bearer_token_env_var 必须是合法环境变量名")

        literal_headers = {}
        for key, raw in ordinary_headers.items():
            if not isinstance(raw, str):
                literal_headers[key] = raw
                continue
            raw = _canonical_env_refs(raw)
            refs = list(ENV_REF.finditer(raw))
            if refs and resolve is None:
                bearer_match = re.fullmatch(
                    r"(?i:Bearer)\s+\{\{env:([A-Za-z_][A-Za-z0-9_]*)\}\}", raw)
                exact_match = CANON_ENV_REF.fullmatch(raw)
                if key.lower() == "authorization" and bearer_match:
                    candidate = bearer_match.group(1)
                    if bearer_name and bearer_name != candidate:
                        raise ValueError("Authorization 与 bearer_token_env_var 冲突")
                    bearer_name = candidate
                    continue
                if exact_match:
                    candidate = exact_match.group(1)
                    if key in env_headers and env_headers[key] != candidate:
                        raise ValueError(f"Codex header {key} 的环境变量引用冲突")
                    env_headers[key] = candidate
                    continue
                raise ValueError(
                    f"Codex header {key} 含复合环境引用，无法安全表达；请使用 --resolve")
            if looks_secret(raw, str(key)) and resolve is None:
                raise ValueError(
                    f"Codex header {key} 看起来是敏感字面值，请使用环境引用或 --resolve")
            literal_headers[key] = _render_value(raw, "${VAR}", resolve)

        fields.pop("command", None); fields.pop("args", None)
        fields.pop("env", None); fields.pop("environment", None)
        if literal_headers:
            fields["http_headers"] = literal_headers
        if env_headers:
            fields["env_http_headers"] = env_headers
        if bearer_name:
            fields["bearer_token_env_var"] = bearer_name
    fields["enabled"] = bool(definition.get("enabled", True))
    return fields


_TOML_CONTROLLED_KEYS = {
    "url", "command", "args", "env", "environment", "headers",
    "http_headers", "env_http_headers", "bearer_token_env_var", "enabled",
    "disabled", "transport", "type", "label",
}


def _merge_toml_target_definition(definition: dict, existing) -> dict:
    """Keep target-only TOML fields while central fields remain authoritative."""
    if isinstance(existing, list):
        combined = {}
        for item in existing:
            if isinstance(item, dict):
                combined.update(item)
        existing = combined
    if not isinstance(existing, dict):
        return definition
    merged = copy.deepcopy(definition)
    fields = {key: copy.deepcopy(value) for key, value in existing.items()
              if key not in _TOML_CONTROLLED_KEYS}
    fields.update(_base_fields(definition))
    merged["fields"] = fields
    return merged


def _render_toml(definition: dict, resolve: Optional[dict] = None) -> str:
    sid = _toml_key(definition["id"])
    fields = _codex_fields(definition, resolve)
    lines = [f"[mcp_servers.{sid}]"]
    scalar = {key: value for key, value in fields.items() if not isinstance(value, dict)}
    nested = {key: value for key, value in fields.items() if isinstance(value, dict)}
    for key, value in scalar.items():
        if value is None:
            continue
        lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")
    for key, value in nested.items():
        lines.append("")
        lines.append(f"[mcp_servers.{sid}.{_toml_key(str(key))}]")
        for subkey, subvalue in value.items():
            lines.append(f"{_toml_key(str(subkey))} = {_toml_value(subvalue)}")
    return "\n".join(lines) + "\n"


def _replace_toml_server(raw: str, sid: str, block: str) -> str:
    safe_component(sid)
    parent = r'(?:mcp_servers|"mcp_servers"|\'mcp_servers\')'
    id_forms = {sid, json.dumps(sid, ensure_ascii=False), f"'{sid}'"}
    id_pattern = "(?:" + "|".join(re.escape(item) for item in id_forms) + ")"
    target_path = re.compile(
        rf"^{parent}\s*\.\s*{id_pattern}(?:\s*\.\s*.+)?$")

    def without_comment(line: str) -> str:
        # Keep '#' inside a quoted key intact while accepting both
        # '[... ] # comment' and the compact '[...]#comment' form.
        quoted = None
        escaped = False
        for index, char in enumerate(line):
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\" and quoted == '"':
                    escaped = True
                elif char == quoted:
                    quoted = None
            elif char in {'"', "'"}:
                quoted = char
            elif char == "#":
                return line[:index].strip()
        return line.strip()

    def is_table(line: str) -> bool:
        return without_comment(line).startswith("[")

    def is_target(line: str) -> bool:
        stripped = without_comment(line)
        if stripped.startswith("[[") and stripped.endswith("]]"):
            path = stripped[2:-2].strip()
        elif stripped.startswith("[") and stripped.endswith("]"):
            path = stripped[1:-1].strip()
        else:
            return False
        return bool(target_path.fullmatch(path))

    lines = raw.splitlines(keepends=True)
    starts = [idx for idx, line in enumerate(lines) if is_target(line)]
    if starts:
        ranges = []
        for start in starts:
            end = len(lines)
            for idx in range(start + 1, len(lines)):
                if is_table(lines[idx]):
                    end = idx
                    break
            if ranges and start <= ranges[-1][1]:
                ranges[-1] = (ranges[-1][0], max(ranges[-1][1], end))
            else:
                ranges.append((start, end))
        first = ranges[0][0]
        replacement = block if block.endswith("\n") else block + "\n"
        output = []
        cursor = 0
        inserted = False
        for start, end in ranges:
            output.extend(lines[cursor:start])
            if not inserted and start == first:
                output.append(replacement)
                inserted = True
            cursor = end
        output.extend(lines[cursor:])
        return "".join(output)
    suffix = "" if not raw or raw.endswith("\n") else "\n"
    return raw + suffix + "\n" + block


def _render_json_document(agent: str, raw: dict, definition: dict,
                          resolve: Optional[dict]) -> dict:
    doc = copy.deepcopy(raw)
    block = _render_json(definition, agent, resolve)
    sid = definition["id"]

    def merge_server(existing):
        if not isinstance(existing, dict):
            return block
        controlled = {
            "type", "transport", "command", "args", "env", "environment",
            "url", "headers", "http_headers", "env_http_headers",
            "bearer_token_env_var", "enabled", "disabled", "label",
        }
        merged = {key: value for key, value in existing.items()
                  if key not in controlled}
        merged.update(block)
        return merged

    if agent == "opencode":
        mcp = doc.setdefault("mcp", {})
        if not isinstance(mcp, dict):
            raise ValueError("OpenCode mcp 字段必须是对象")
        nested = mcp.get("servers")
        if (isinstance(nested, dict) and
                not any(key in nested for key in ("type", "command", "url", "enabled", "disabled"))):
            mcp["servers"][sid] = merge_server(mcp["servers"].get(sid))
        else:
            # 当前官方 schema 是 direct mcp map；保留现有字段和合法的
            # server ID="servers"，新项也写在 mcp.<id>。
            mcp[sid] = merge_server(mcp.get(sid))
        return doc
    key = MCP_TARGET_KEYS[agent]
    current = doc.setdefault(key, {})
    if not isinstance(current, dict):
        raise ValueError(f"{key} 字段必须是对象")
    current[sid] = merge_server(current.get(sid))
    return doc


def _backup_mcp_targets(ts: str, paths: list[Path]) -> None:
    backup = safe_path(BACKUP_DIR, ts)
    directory = backup / "mcp"
    directory.mkdir(parents=True, exist_ok=True)
    journal_path = directory / "targets.json"
    entries = []
    if journal_path.exists():
        try:
            entries = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("MCP 备份清单损坏，停止写入") from exc
    unique_paths = []
    seen_paths = set()
    for path in paths:
        path = Path(path).absolute()
        if str(path) not in seen_paths:
            seen_paths.add(str(path))
            unique_paths.append(path)
    for path in unique_paths:
        if path.exists() and not path.is_file() and not path.is_symlink():
            raise ValueError(f"MCP 目标不是普通文件: {path}")
    for path in unique_paths:
        path = path.absolute()
        original = str(path)
        if any(item.get("target") == original for item in entries):
            continue
        key = (re.sub(r"[^A-Za-z0-9_.-]", "_", path.name) + "-" +
               hashlib.sha256(original.encode("utf-8")).hexdigest()[:16])
        saved = directory / "files" / key
        entry = {"target": original, "missing": not path.exists()}
        if path.is_symlink():
            entry.update({"kind": "symlink", "link": os.readlink(path)})
        elif path.is_file():
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved); saved.chmod(0o600)
            entry.update({"kind": "file", "saved": str(saved.relative_to(backup))})
        else:
            entry["kind"] = "missing"
        entries.append(entry)
    atomic_write(journal_path, json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
    atomic_write(directory / "snapshot.json", json.dumps({
        "schema_version": 1,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "target_count": len(entries),
    }, ensure_ascii=False, indent=2) + "\n")


def _mcp_backup_entries(backup: Path) -> list[dict]:
    """验证 MCP 备份清单，返回可安全恢复的条目。"""
    directory = backup / "mcp"
    journal = directory / "targets.json"
    if directory.is_symlink() or journal.is_symlink() or not journal.is_file():
        raise ValueError("MCP 备份清单缺失或不是安全文件")
    try:
        entries = json.loads(journal.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("MCP 备份清单损坏") from exc
    if not isinstance(entries, list):
        raise ValueError("MCP 备份清单必须是数组")
    seen = set()
    backup_boundary = backup.resolve(strict=False)
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("target"), str):
            raise ValueError("MCP 备份目标清单损坏")
        target = Path(os.path.abspath(entry["target"]))
        if not _mcp_restore_target_allowed(target):
            raise ValueError("MCP 备份目标不在当前配置允许的路径内")
        if str(target) in seen:
            raise ValueError("MCP 备份目标清单含重复目标")
        seen.add(str(target))
        kind = entry.get("kind")
        if kind not in {"missing", "file", "symlink"}:
            raise ValueError("MCP 备份目标类型无效")
        if kind == "symlink":
            link = entry.get("link")
            if not isinstance(link, str) or not link or "\x00" in link or "saved" in entry:
                raise ValueError("MCP 备份软链接字段无效")
        elif kind == "missing":
            if "saved" in entry or "link" in entry:
                raise ValueError("MCP 缺失目标备份字段无效")
        else:
            saved_value = entry.get("saved")
            if not isinstance(saved_value, str) or not saved_value or "\\" in saved_value:
                raise ValueError("MCP 备份副本路径无效")
            relative = Path(saved_value)
            if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
                raise ValueError("MCP 备份副本必须是相对路径")
            for part in relative.parts:
                safe_component(part)
            saved = backup / relative
            try:
                saved.resolve(strict=False).relative_to(backup_boundary)
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError("MCP 备份副本越出快照目录") from exc
            current = backup
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise ValueError("MCP 备份副本父级不能是软链接")
            if not saved.is_file() or saved.is_symlink():
                raise ValueError("MCP 备份副本缺失或类型不匹配")
    return entries


def _mcp_restore_target_allowed(target: Path) -> bool:
    absolute = Path(os.path.abspath(os.fspath(target)))
    if absolute == Path(os.path.abspath(os.fspath(MCP_INDEX_FILE))):
        return True
    for agent in MCP_TARGETS:
        if agent == "pi":
            candidate = _pi_server_path("validation")
            root = Path(os.path.abspath(os.fspath(candidate.parent)))
            if absolute.parent == root and absolute.suffix == ".json":
                try:
                    safe_component(absolute.name)
                except ValueError:
                    continue
                return True
        else:
            candidate = Path(os.path.abspath(os.fspath(_agent_mcp_file(agent))))
            if absolute == candidate:
                return True
    return False


def restore_backup(backup: Path) -> None:
    """恢复一个 MCP targets journal；调用前后都验证，且不跟随链接。"""
    backup = Path(backup)
    entries = _mcp_backup_entries(backup)
    targets = [Path(os.path.abspath(entry["target"])) for entry in entries]
    for target in targets:
        if target.exists() and target.is_dir() and not target.is_symlink():
            raise ValueError(f"MCP 当前目标是目录，拒绝删除: {target}")
    # 再验证一次，覆盖验证与删除之间的清单竞态。
    entries = _mcp_backup_entries(backup)
    for entry in entries:
        target = Path(os.path.abspath(entry["target"]))
        if target.is_symlink() or target.is_file():
            target.unlink()
        if entry["kind"] == "missing":
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry["kind"] == "symlink":
            target.symlink_to(entry["link"])
        else:
            atomic_write(target, (backup / entry["saved"]).read_bytes())


def plan_generate(sid: str, agents: List[str]) -> Dict[str, List[dict]]:
    definition = get_server(sid)
    if definition is None:
        raise ValueError(f"中央库中不存在 MCP server: {sid}")
    plan = {}
    for agent in agents:
        if agent not in MCP_TARGETS:
            plan[agent] = [{"type": "error", "target": agent, "detail": "未知或不支持的 agent"}]
        elif definition.get("enabled") is False and agent in {"pi", "claude"}:
            plan[agent] = [{"type": "error", "target": str(_pi_server_path(sid) if agent == "pi" else _agent_mcp_file(agent)),
                            "detail": f"{agent} 没有确认可用的禁用字段，拒绝生成"}]
        elif agent == "pi":
            target = _pi_server_path(sid)
            plan[agent] = [{"type": "write", "target": str(target), "detail": "每 server 一个 JSON 文件"}]
        else:
            target = _agent_mcp_file(agent)
            plan[agent] = [{"type": "merge", "target": str(target),
                            "detail": f"更新 {MCP_TARGET_KEYS[agent]}.{sid}，保留未知字段"}]
    return plan


def _load_json_document(path: Path, jsonc: bool = False) -> dict:
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(_jsonc_strip(text) if jsonc else text)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"配置无法解析，已停止且未覆盖: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"配置根必须是对象: {path}")
    return value


def _write_json_document(path: Path, document: dict) -> None:
    atomic_write(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")


def _read_existing_server(agent: str, sid: str):
    if agent == "pi":
        path = _pi_server_path(sid)
        if not path.exists():
            return None, "missing"
        return _load_json_document(path), "match"
    path = _agent_mcp_file(agent)
    if not path.exists():
        return None, "missing"
    try:
        servers = _source_servers(agent, path)
    except ValueError as exc:
        return None, f"invalid: {exc}"
    return servers.get(sid), "match" if sid in servers else "missing"


def _comparable_fields(agent: str, cfg: dict) -> tuple[str, dict]:
    """把不同目标格式的等价字段归一，status 不只检查 server ID。"""
    value = copy.deepcopy(cfg)
    transport = _server_transport(value)
    headers = {}
    for key in ("headers", "http_headers"):
        current = value.pop(key, None)
        if isinstance(current, dict):
            headers.update(current)
    env_headers = value.pop("env_http_headers", None)
    if isinstance(env_headers, dict):
        for key, variable in env_headers.items():
            if key not in headers and isinstance(variable, str):
                reference = variable if CANON_ENV_REF.fullmatch(variable) else "{{env:%s}}" % variable
                headers[key] = reference
    bearer = value.pop("bearer_token_env_var", None)
    if bearer and isinstance(bearer, str):
        if "Authorization" not in headers:
            reference = bearer if CANON_ENV_REF.fullmatch(bearer) else "{{env:%s}}" % bearer
            headers["Authorization"] = "Bearer " + reference
    if headers:
        value["headers"] = headers
    if isinstance(value.get("command"), list):
        command = value["command"]
        value["command"], value["args"] = (command[0] if command else "",
                                              command[1:])
    if agent == "opencode":
        if "environment" in value and "env" not in value:
            value["env"] = value.pop("environment")
    elif "environment" in value and "env" not in value:
        value["env"] = value.pop("environment")
    if transport == "stdio":
        value.setdefault("args", [])
    else:
        for key in ("command", "args", "env", "environment"):
            value.pop(key, None)
    enabled = (not bool(value.get("disabled"))) if "disabled" in value else bool(value.get("enabled", True))
    value.pop("type", None); value.pop("transport", None)
    value.pop("disabled", None); value.pop("label", None)
    # Pi stores the server id in each host-core file; it is metadata rather
    # than part of the transport definition kept in the central index.
    value.pop("id", None)
    value["enabled"] = enabled
    controlled = {"url", "command", "args", "env", "headers", "enabled"}
    value = {key: item for key, item in value.items() if key in controlled}
    return transport, _canonical_env_refs_in_object(value)


def _canonical_env_refs_in_object(value):
    if isinstance(value, dict):
        return {key: _canonical_env_refs_in_object(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical_env_refs_in_object(item) for item in value]
    if isinstance(value, str):
        return _canonical_env_refs(value)
    return value


def compare_server(agent: str, definition: dict) -> dict:
    sid = definition["id"]
    target = _pi_server_path(sid) if agent == "pi" else _agent_mcp_file(agent)
    try:
        actual, state = _read_existing_server(agent, sid)
    except (OSError, ValueError) as exc:
        return {"match": False, "state": "invalid", "target": str(target), "error": str(exc)}
    if actual is None:
        return {"match": False, "state": state, "target": str(target)}
    try:
        actual_transport, actual_fields = _comparable_fields(agent, actual)
        expected_transport, expected_fields = _comparable_fields(agent, _base_fields(definition))
        match = actual_transport == expected_transport and actual_fields == expected_fields
    except ValueError as exc:
        return {"match": False, "state": "invalid", "target": str(target), "error": str(exc)}
    return {"match": match, "state": "match" if match else "drift", "target": str(target)}


def agent_has_server(agent: str, sid: str) -> bool:
    definition = get_server(sid)
    return bool(definition and compare_server(agent, definition)["match"])


def status() -> dict:
    servers = list_servers()
    rows = []
    not_aligned = 0
    for definition in servers:
        row = {"id": definition["id"], "transport": definition.get("transport", ""), "agents": {}}
        for agent in AGENTS:
            if agent in MCP_TARGETS:
                row["agents"][agent] = compare_server(agent, definition)
        if not any(item["match"] for item in row["agents"].values()):
            not_aligned += 1
        rows.append(row)
    return {"servers": rows, "summary": {"total": len(rows), "not_in_any_agent": not_aligned}}


def list_servers() -> List[dict]:
    return sorted(load_index().values(), key=lambda item: item.get("id", ""))


def get_server(sid: str) -> Optional[dict]:
    safe_component(sid)
    return load_index().get(sid)


def _editor_reference(value: object) -> Optional[str]:
    """Return an environment reference safe to show in the local editor.

    The central index may contain non-sensitive values as well as secret
    references.  The GUI only needs to round-trip references; returning
    anything else here would either expose a credential or make a later
    browser save overwrite an opaque value.
    """
    if not isinstance(value, str):
        return None
    value = _canonical_env_refs(value)
    if CANON_ENV_REF.fullmatch(value):
        return value
    if re.fullmatch(r"(?i:Bearer)\s+\{\{env:[A-Za-z_][A-Za-z0-9_]*\}\}", value):
        return value
    return None


def editable_references(definition: dict) -> dict:
    """Expose only environment references for the GUI's MCP form.

    Literal secrets are deliberately omitted.  The editor endpoint merges
    submitted references into the existing mapping, so omitted values remain
    untouched instead of being copied into the browser or accidentally
    cleared by a save.
    """
    env_values = {}
    for field in ("env", "environment"):
        values = definition.get(field)
        if isinstance(values, dict):
            for name, value in values.items():
                reference = _editor_reference(value)
                if reference is not None:
                    env_values[str(name)] = reference
    header_values = {}
    for field in ("headers", "http_headers"):
        values = definition.get(field)
        if isinstance(values, dict):
            for name, value in values.items():
                reference = _editor_reference(value)
                if reference is not None:
                    header_values[str(name)] = reference
    env_headers = definition.get("env_http_headers")
    if isinstance(env_headers, dict):
        for name, value in env_headers.items():
            reference = _editor_reference(value)
            if reference is None and isinstance(value, str) and re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", value):
                reference = "{{env:%s}}" % value
            if reference is not None:
                header_values.setdefault(str(name), reference)
    bearer = definition.get("bearer_token_env_var")
    if isinstance(bearer, str):
        reference = _editor_reference(bearer)
        if reference is None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bearer):
            reference = "{{env:%s}}" % bearer
        if reference is not None:
            header_values.setdefault("Authorization", "Bearer " + reference)
    return {"env": dict(sorted(env_values.items())),
            "headers": dict(sorted(header_values.items()))}


def _validate_editor_references(value: object, field: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    output = {}
    for name, item in value.items():
        if (not isinstance(name, str) or
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", name) or
                not isinstance(item, str)):
            raise ValueError(f"{field} 的键和值必须是字符串")
        reference = _editor_reference(item)
        if reference is None:
            raise ValueError(f"{field} 只能填写完整的环境变量引用")
        output[name] = reference
    return output


@locked
def edit_definition(sid: str, changes: dict) -> dict:
    """编辑中央 MCP 的非敏感元数据和环境变量引用。"""
    safe_component(sid)
    if not isinstance(changes, dict):
        raise ValueError("MCP 修改必须是对象")
    index = load_index()
    current = index.get(sid)
    if current is None:
        raise ValueError(f"中央库中不存在 MCP server: {sid}")
    allowed = {"label", "enabled", "url", "command", "args", "env", "headers",
               "environment", "env_http_headers", "bearer_token_env_var",
               "env_refs", "header_refs"}
    unknown = set(changes) - allowed
    if unknown:
        raise ValueError(f"MCP 修改包含不支持字段: {', '.join(sorted(unknown))}")
    candidate = copy.deepcopy(current)
    fields = candidate.setdefault("fields", {})
    for key, value in changes.items():
        if key in {"env_refs", "header_refs"}:
            refs = _validate_editor_references(
                value, "环境引用" if key == "env_refs" else "请求头引用")
            source_keys = ("env", "environment") if key == "env_refs" else (
                "headers", "http_headers")
            target_key = next((name for name in source_keys
                               if isinstance(candidate.get(name), dict)), source_keys[0])
            merged = dict(candidate.get(target_key) or {})
            merged.update(refs)
            fields[target_key] = copy.deepcopy(merged)
            candidate[target_key] = copy.deepcopy(merged)
            continue
        if key in {"env", "environment", "headers", "env_http_headers"}:
            if not isinstance(value, dict):
                raise ValueError(f"{key} 必须是对象")
            for name, item in value.items():
                if not isinstance(name, str) or not isinstance(item, str):
                    raise ValueError("MCP 引用键值必须是字符串")
                if looks_secret(item, name) and not ENV_REF.fullmatch(item):
                    raise ValueError("敏感 MCP 值只能写环境变量引用")
        if key in {"command", "url", "label", "bearer_token_env_var"} and value is not None:
            if not isinstance(value, str) or len(value) > 2000:
                raise ValueError(f"MCP 字段 {key} 无效")
            if key == "url" and SENSITIVE_QUERY.search(value) and not ENV_REF.search(value):
                raise ValueError("URL 中的敏感查询参数只能使用环境变量引用")
            if looks_secret(value, key) and not ENV_REF.fullmatch(value) and key != "url":
                raise ValueError("敏感 MCP 值只能写环境变量引用")
        if key == "enabled" and type(value) is not bool:
            raise ValueError("enabled 必须是布尔值")
        if key == "args" and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
            raise ValueError("args 必须是字符串数组")
        fields[key] = copy.deepcopy(value)
        if key != "label":
            candidate[key] = copy.deepcopy(value)
    if "label" in changes:
        candidate["label"] = changes["label"]
    candidate["schema_version"] = max(2, int(candidate.get("schema_version", 2)))
    index[sid] = candidate
    save_index(index)
    return redact_for_output(candidate)


def redact_for_output(value, key: str = ""):
    if isinstance(value, dict):
        return {name: redact_for_output(item, str(name)) for name, item in value.items()}
    if isinstance(value, list):
        output, previous_flag = [], ""
        for item in value:
            if isinstance(item, str) and "=" in item:
                flag, raw = item.split("=", 1)
                if flag.startswith("-") and SENSITIVE_FLAG.search(flag):
                    output.append(flag + "=" + redact_for_output(raw, flag))
                    previous_flag = ""
                    continue
            item_key = previous_flag or key
            output.append(redact_for_output(item, item_key))
            previous_flag = item if isinstance(item, str) and item.startswith("-") else ""
        return output
    if isinstance(value, str):
        if looks_secret(value, key):
            return mask(value)
        try:
            parsed = urlsplit(value)
            if parsed.query and any(SENSITIVE_QUERY.search(name) for name, _ in parse_qsl(parsed.query, keep_blank_values=True)):
                return _replace_secret(value, "OUTPUT", key, {}, [], [])
        except ValueError:
            pass
    return value


@locked
def apply_generate(sid: str, agents: List[str], backup: bool = True,
                   resolve: Optional[dict] = None) -> Dict[str, List[dict]]:
    definition = get_server(sid)
    if definition is None:
        raise ValueError(f"中央库中不存在 MCP server: {sid}")
    for agent in agents:
        if agent not in MCP_TARGETS:
            raise ValueError(f"未知或不支持的 agent: {agent}")
        if definition.get("enabled") is False and agent in {"pi", "claude"}:
            raise ValueError(f"{agent} 没有确认可用的禁用字段，拒绝生成")
    results, targets = {}, {}
    seen_targets = {}
    for agent in agents:
        target = _pi_server_path(sid) if agent == "pi" else _agent_mcp_file(agent)
        target_key = str(target.absolute())
        if target_key in seen_targets:
            results[agent] = [{"type": "error", "target": str(target),
                               "detail": f"与 agent {seen_targets[target_key]} 共用同一配置路径，拒绝重复写入"}]
            continue
        seen_targets[target_key] = agent
        targets[agent] = target
        results[agent] = [{"type": "write", "target": str(target), "detail": "待原子更新配置"}]
    if any(row[0].get("type") == "error" for row in results.values()):
        return results
    # Parse and render every destination before touching any of them.  A bad
    # TOML/JSONC target must not leave earlier agents partially updated.
    prepared = {}
    for agent in agents:
        target = targets[agent]
        if agent == "pi":
            prepared[agent] = ("json", _render_pi(definition, resolve))
        elif MCP_TARGETS[agent][0] == "config.toml":
            raw = target.read_text(encoding="utf-8") if target.exists() else ""
            existing = None
            if target.exists():
                parsed = _parse_toml(target)
                existing = (parsed.get("mcp_servers") or {}).get(sid)
            merged_definition = _merge_toml_target_definition(definition, existing)
            rendered = _render_toml(merged_definition, resolve)
            candidate = _replace_toml_server(raw, sid, rendered)
            if tomllib is None:
                raise ValueError("Python 3.11 的 tomllib 是 Codex TOML 验证要求")
            try:
                tomllib.loads(candidate)
            except tomllib.TOMLDecodeError as exc:
                raise ValueError(f"生成的 TOML 无法解析，已停止且未覆盖: {target}: {exc}") from exc
            prepared[agent] = ("text", candidate)
        else:
            document = _load_json_document(target, jsonc=MCP_TARGETS[agent][0] == "opencode.jsonc")
            prepared[agent] = ("json", _render_json_document(agent, document, definition, resolve))

    target_list = list(targets.values())
    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    if backup and target_list:
        _backup_mcp_targets(ts, target_list)
    final = {}
    for agent in agents:
        target = targets[agent]
        kind, document = prepared[agent]
        if kind == "text":
            atomic_write(target, document)
        else:
            _write_json_document(target, document)
        final[agent] = [{"type": "write", "target": str(target), "detail": "已原子更新"}]
    return final
