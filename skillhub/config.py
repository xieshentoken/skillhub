"""skillhub — 中央库 + 适配生成器 (阶段1)。

设计原则:
- 中央库是唯一权威副本, agent 目录里只放投影(symlink)或适配片段
- 导入只复制、不删除原文件
- 任何写操作默认 dry-run, 显式 --apply 才生效
- 每次投影前先备份, 可 rollback
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOME = Path.home()

# ---- 中央库位置 (数据放用户目录, 不混进项目工作区) ----
SKILLHUB_HOME = Path(os.environ.get("SKILLHUB_HOME", HOME / ".skillhub"))
STORE_DIR = SKILLHUB_HOME / "store"      # 每个 skill 一个目录: store/<skill_id>/
BACKUP_DIR = SKILLHUB_HOME / "backups"   # 投影前的备份
TRASH_DIR = SKILLHUB_HOME / "trash"      # 单 skill 版本回收区
LOG_FILE = SKILLHUB_HOME / "events.jsonl"  # 不含密钥的审计日志
GROUPS_FILE = SKILLHUB_HOME / "groups.json"  # GUI/CLI 共用的分组关系
DISTRIBUTIONS_FILE = SKILLHUB_HOME / "distributions.json"  # 每个 agent 的来源选择
SETTINGS_FILE = SKILLHUB_HOME / "settings.json"
INDEX_FILE = SKILLHUB_HOME / "index.json"
MCP_DIR = SKILLHUB_HOME / "mcp"          # 中央 MCP server 定义: mcp/index.json
MCP_INDEX_FILE = MCP_DIR / "index.json"  # server_id -> 定义 (密钥用 {{env:VAR}} 引用, 不存明文)

# ---- Agent 定义 ----
# mode: 投影方式。symlink=符号链接目录; copy=复制(用于不信任 symlink 的 agent)
# skill_dir 可用环境变量 SKILLHUB_AGENT_DIR_<agent> 覆盖 (测试/自定义用)
AGENTS = {
    "pi": {
        "skill_dir": HOME / ".agents" / "skills",
        "mode": "symlink",
    },
    "codex": {
        "skill_dir": HOME / ".codex" / "skills",
        "mode": "symlink",
    },
    "opencode": {
        "skill_dir": HOME / ".config" / "opencode" / "skills",
        "mode": "symlink",
    },
    "workbuddy": {
        "skill_dir": HOME / ".workbuddy" / "skills",
        "mode": "copy",
    },
    "claude": {
        "skill_dir": HOME / ".claude" / "skills",
        "mode": "symlink",
    },
    "grok": {
        # grok 还会兼容读取 ~/.agents/skills 与 ~/.claude/skills (compat 扫描)。
        # 实测: 同名 skill 只注册一次, ~/.grok/skills 优先覆盖兼容目录。
        # 显式投影到 ~/.grok/skills 可摆脱对 pi 投影的依赖。
        "skill_dir": HOME / ".grok" / "skills",
        "mode": "symlink",
    },
    "hermes": {
        "skill_dir": HOME / "Library" / "Application Support" / "cn.org.hermesagent.desktop"
                  / "runtime" / "hermes-home" / "skills",
        "mode": "symlink",
        "nested": True,   # hermes 按分类层级存放: skills/<category>/<skill>
    },
}

for _agent, _cfg in AGENTS.items():
    _override = os.environ.get(f"SKILLHUB_AGENT_DIR_{_agent}")
    if _override:
        _cfg["skill_dir"] = Path(_override)

MANIFEST_NAME = "manifest.json"

try:
    TRASH_RETENTION_DAYS = max(1, int(os.environ.get("SKILLHUB_TRASH_RETENTION_DAYS", "7")))
except ValueError:
    TRASH_RETENTION_DAYS = 7

# 这里只列出可安全读取的配置文件，不把配置里的命令当作 shell 执行。
# 具体模型是否能安全调用仍由 model_discovery() 根据实际配置判断。
MODEL_CONFIG_FILES = {
    "pi": HOME / ".pi" / "agent" / "models.json",
    "codex": HOME / ".codex" / "config.toml",
    "opencode": HOME / ".config" / "opencode" / "opencode.jsonc",
    "workbuddy": HOME / ".workbuddy" / "config.json",
    "claude": HOME / ".claude" / "settings.json",
    "grok": HOME / ".grok" / "config.toml",
    "hermes": HOME / "Library" / "Application Support" / "cn.org.hermesagent.desktop"
                  / "runtime" / "hermes-home" / "config.json",
}
for _agent in tuple(MODEL_CONFIG_FILES):
    _override = os.environ.get(f"SKILLHUB_MODEL_FILE_{_agent}")
    if _override:
        MODEL_CONFIG_FILES[_agent] = Path(_override)


def _model_values(value, key: str = "") -> list[str]:
    values = []
    if isinstance(value, dict):
        for name, item in value.items():
            values.extend(_model_values(item, str(name)))
    elif isinstance(value, list):
        for item in value:
            values.extend(_model_values(item, key))
    elif isinstance(value, str) and (key.lower() in {"model", "models", "model_id", "model_name", "default_model"}
                                     or key.lower().endswith("model")):
        if value and len(value) <= 200 and "${" not in value and "{{" not in value:
            values.append(value)
    return values


def _strip_jsonc_comments(text: str) -> str:
    # 保留字符串里的 URL/转义字符，同时支持 OpenCode 常见的行/块注释
    # 和尾逗号；模型发现是只读辅助，不执行其中任何表达式。
    output = []
    quote = False
    escape = False
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quote = False
            index += 1
            continue
        if char == '"':
            quote = True
            output.append(char)
            index += 1
        elif text.startswith("//", index):
            newline = text.find("\n", index)
            if newline < 0:
                break
            output.append("\n")
            index = newline + 1
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                break
            index = end + 2
        else:
            output.append(char)
            index += 1
    cleaned = "".join(output)
    output = []
    quote = False
    escape = False
    index = 0
    while index < len(cleaned):
        char = cleaned[index]
        if quote:
            output.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                quote = False
            index += 1
            continue
        if char == '"':
            quote = True
            output.append(char)
            index += 1
        elif char == ',':
            tail = index + 1
            while tail < len(cleaned) and cleaned[tail].isspace():
                tail += 1
            if tail < len(cleaned) and cleaned[tail] in "}]":
                index += 1
                continue
            output.append(char)
            index += 1
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _load_model_data(path: Path):
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".toml":
        return tomllib.loads(raw)
    return json.loads(_strip_jsonc_comments(raw))


def _model_config_path(agent: str, path: Path) -> Path:
    """Use known legacy/default paths only when no explicit override is set."""
    if not path.is_file() and not os.environ.get(f"SKILLHUB_MODEL_FILE_{agent}"):
        fallbacks = []
        if agent == "pi":
            fallbacks.append(HOME / ".pi" / "settings.json")
        elif agent == "opencode":
            fallbacks.append(HOME / ".config" / "opencode" / "opencode.json")
        for fallback in fallbacks:
            if fallback.is_file() and not fallback.is_symlink():
                return fallback
    return path


def _codex_cli_path(data=None) -> tuple[Path | None, str]:
    """只检测受信任的 Codex CLI 路径，不执行它。

    ``codex exec`` 的只读 sandbox 仍然可以向模型提供 shell/MCP 等工具，
    因此这个结果只能用于诊断，不能作为 ``safe_to_call`` 的依据。
    """
    candidates = []
    override = os.environ.get("SKILLHUB_CODEX_CLI")
    if override:
        candidates.append(override)
    if isinstance(data, dict):
        servers = data.get("mcp_servers") or {}
        if isinstance(servers, dict):
            for server in servers.values():
                if isinstance(server, dict):
                    env = server.get("env")
                    if isinstance(env, dict) and isinstance(env.get("CODEX_CLI_PATH"), str):
                        candidates.append(env["CODEX_CLI_PATH"])
    which = shutil.which("codex")
    if which:
        candidates.append(which)
    candidates.append("/Applications/ChatGPT.app/Contents/Resources/codex")
    allowed_roots = [Path("/Applications/ChatGPT.app/Contents/Resources"),
                     Path("/usr/local/bin"), Path("/opt/homebrew/bin"), Path("/usr/bin")]
    for raw in candidates:
        try:
            path = Path(raw).expanduser()
            if not path.is_absolute() or path.name != "codex" or not path.is_file() or not os.access(path, os.X_OK):
                continue
            resolved = path.resolve(strict=True)
            if any(resolved == root or root in resolved.parents for root in allowed_roots):
                return resolved, "检测到 Codex 官方 CLI，但当前 CLI 没有已验证的无工具调用模式"
        except (OSError, RuntimeError):
            continue
    return None, "未找到受信任的 Codex 官方 CLI"


_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _model_api_block(agent: str, data) -> dict:
    """读取显式的 skillhub 直接 API 配置，不从 agent 的任意字段推断。

    支持配置文件中的 ``[skillhub_model_api]``，或
    ``[skillhub.model_api]``；环境变量只覆盖 endpoint、协议和环境变量名，
    永远不接受 API key 字面值。这样不会把 agent 自己的 MCP/provider 配置
    当成可以执行的命令或凭证来源。
    """
    block = {}
    if isinstance(data, dict):
        direct = data.get("skillhub_model_api")
        if isinstance(direct, dict):
            block.update(direct)
        skillhub = data.get("skillhub")
        if isinstance(skillhub, dict) and isinstance(skillhub.get("model_api"), dict):
            block.update(skillhub["model_api"])
    prefix = f"SKILLHUB_MODEL_API_"
    for key, env_name in (("endpoint", f"{prefix}ENDPOINT_{agent}"),
                          ("api_key_env", f"{prefix}KEY_ENV_{agent}"),
                          ("protocol", f"{prefix}PROTOCOL_{agent}")):
        if os.environ.get(env_name):
            block[key] = os.environ[env_name]
    return block


def _secret_value(value, *, allow_literal: bool = True) -> tuple[str, str] | None:
    """Resolve only an environment reference or an explicitly configured value.

    Command/file/credential interpolation is deliberately unsupported: resolving
    those would grant model grouping a new command or file-reading capability.
    The returned second item is the environment variable name, if there is one.
    """
    if not isinstance(value, str) or not value:
        return None
    match = re.fullmatch(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", value)
    if match:
        name = next(item for item in match.groups() if item)
        secret = os.environ.get(name)
        return (secret, name) if secret else None
    if value.startswith("!") or value.startswith("{file:") or value.startswith("{cred:"):
        return None
    return (value, "") if allow_literal else None


def _api_endpoint(base: str, protocol: str, *, strict: bool = False) -> str | None:
    suffix = "/chat/completions" if protocol == "openai-chat-json" else "/messages"
    if not isinstance(base, str) or not base.strip():
        return None
    base = base.strip()
    try:
        parsed = urllib.parse.urlsplit(base)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return None
    loopback = hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        return None
    if not hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        return None
    path = parsed.path.rstrip("/")
    if protocol == "openai-chat-json" and path.endswith("/chat/completions"):
        return base
    if protocol == "anthropic-messages-json" and path.endswith("/messages"):
        return base
    if strict:
        return None
    return base.rstrip("/") + suffix


def _model_spec(endpoint: str, protocol: str, credential: tuple[str, str] | None,
                *, source: str) -> dict | None:
    if not credential:
        return None
    url = _api_endpoint(endpoint, protocol)
    if not url:
        return None
    secret, env_name = credential
    return {"endpoint": url, "protocol": protocol, "api_key": secret,
            "api_key_env": env_name, "auth_source": source}


def _direct_model_api(agent: str, data, models: list[str]) -> tuple[dict | None, str]:
    """Validate an explicit tool-free OpenAI-compatible configuration."""
    block = _model_api_block(agent, data)
    if not block:
        return None, "未配置无工具直接 API"
    if "api_key" in block or "token" in block:
        return None, "直接 API 禁止在配置文件中保存明文凭证，请使用 api_key_env"
    endpoint = block.get("endpoint") or block.get("url")
    key_env = block.get("api_key_env")
    protocol = str(block.get("protocol") or "openai-chat").strip().lower()
    if protocol not in {"openai-chat", "openai-chat-json"}:
        return None, "直接 API 仅支持 openai-chat 协议"
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None, "直接 API 缺少 endpoint"
    if not isinstance(key_env, str) or not _ENV_NAME_RE.fullmatch(key_env):
        return None, "直接 API 缺少合法的 api_key_env"
    url = _api_endpoint(endpoint, "openai-chat-json", strict=True)
    if not url:
        return None, "直接 API endpoint 必须是 HTTPS/loopback HTTP 的 /chat/completions"
    credential = _secret_value("{" + "env:" + key_env + "}", allow_literal=False)
    if not credential:
        return None, f"直接 API 已配置，但环境变量 {key_env} 不存在"
    configured_model = block.get("model")
    if configured_model is not None and (not isinstance(configured_model, str) or configured_model not in models):
        return None, "直接 API 的 model 不在本机实际发现的模型列表中"
    return {"endpoint": url, "api_key": credential[0], "api_key_env": key_env,
            "protocol": "openai-chat-json", "auth_source": "skillhub explicit API"}, "已验证无工具直接 API"


def _provider_model_entries(models) -> list[tuple[str, dict]]:
    """Normalize OpenCode's model map and Pi's model list/map to model IDs."""
    entries = []
    if isinstance(models, dict):
        for model_id, value in models.items():
            entries.append((str(model_id), value if isinstance(value, dict) else {}))
    elif isinstance(models, list):
        for value in models:
            if isinstance(value, str):
                entries.append((value, {}))
            elif isinstance(value, dict) and isinstance(value.get("id"), str):
                entries.append((value["id"], value))
    return entries


def _open_code_model_specs(data) -> dict[str, dict]:
    providers = data.get("provider") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return {}
    output = {}
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        options = provider.get("options") if isinstance(provider.get("options"), dict) else {}
        npm = str(provider.get("npm") or "").lower()
        if "anthropic" in npm or str(provider_id).lower() == "anthropic":
            protocol = "anthropic-messages-json"
        elif "openai-compatible" in npm or str(provider_id).lower() in {"openai", "openrouter"}:
            protocol = "openai-chat-json"
        else:
            continue
        whitelist = set(provider.get("whitelist", [])) if isinstance(provider.get("whitelist"), list) else None
        blacklist = set(provider.get("blacklist", [])) if isinstance(provider.get("blacklist"), list) else set()
        for model_id, model in _provider_model_entries(provider.get("models")):
            if whitelist is not None and model_id not in whitelist:
                continue
            if model_id in blacklist:
                continue
            model_options = model.get("options") if isinstance(model.get("options"), dict) else {}
            merged = dict(options)
            merged.update({key: value for key, value in model.items()
                           if key in {"baseURL", "apiKey", "api", "npm"}})
            merged.update(model_options)
            model_npm = str(merged.get("npm") or npm).lower()
            model_protocol = ("anthropic-messages-json" if "anthropic" in model_npm
                              else protocol)
            endpoint = merged.get("baseURL")
            if not isinstance(endpoint, str):
                continue
            if isinstance(endpoint, str):
                endpoint_ref = _secret_value(endpoint)
                endpoint = endpoint_ref[0] if endpoint_ref else ""
            credential = _secret_value(merged.get("apiKey"))
            spec = _model_spec(endpoint, model_protocol, credential,
                               source=f"opencode provider {provider_id}")
            if spec:
                spec["provider"] = str(provider_id)
                output[model_id] = spec
    return output


def _pi_model_specs(data) -> dict[str, dict]:
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return {}
    output = {}
    for provider_id, provider in providers.items():
        if not isinstance(provider, dict):
            continue
        provider_api = str(provider.get("api") or "").lower()
        for model_id, model in _provider_model_entries(provider.get("models")):
            model = model if isinstance(model, dict) else {}
            merged = dict(provider)
            merged.update(model)
            protocol = {"openai-completions": "openai-chat-json",
                        "anthropic-messages": "anthropic-messages-json"}.get(
                            str(merged.get("api") or provider_api).lower())
            if not protocol:
                continue
            base = merged.get("baseUrl")
            credential = _secret_value(merged.get("apiKey"))
            if credential is None and str(provider_id) in {"openai", "anthropic", "deepseek", "openrouter"}:
                known_env = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
                             "deepseek": "DEEPSEEK_API_KEY", "openrouter": "OPENROUTER_API_KEY"}[str(provider_id)]
                credential = _secret_value("{" + "env:" + known_env + "}", allow_literal=False)
            if not isinstance(base, str):
                continue
            spec = _model_spec(base, protocol, credential,
                               source=f"pi provider {provider_id}")
            if spec:
                spec["provider"] = str(provider_id)
                output[model_id] = spec
    return output


def _claude_model_specs(data, models: list[str]) -> dict[str, dict]:
    env = data.get("env") if isinstance(data, dict) else None
    if not isinstance(env, dict):
        return {}
    base = env.get("ANTHROPIC_BASE_URL")
    base_ref = _secret_value(base)
    base = base_ref[0] if base_ref else ""
    credential = _secret_value(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"))
    if not base or not credential:
        return {}
    output = {}
    for model_id in models:
        spec = _model_spec(base.rstrip("/") + "/v1/messages", "anthropic-messages-json",
                           credential, source="claude settings env")
        if spec:
            output[model_id] = spec
    return output


def _model_call_specs(agent: str, data, models: list[str]) -> tuple[dict[str, dict], str]:
    direct, reason = _direct_model_api(agent, data, models)
    if direct:
        return {model_id: dict(direct) for model_id in models}, reason
    if agent == "opencode":
        specs = _open_code_model_specs(data)
    elif agent == "pi":
        specs = _pi_model_specs(data)
    elif agent == "claude":
        specs = _claude_model_specs(data, models)
    else:
        specs = {}
    if specs:
        return specs, "已验证 agent 现有的无工具 API 配置"
    return {}, reason


def _discovered_models(agent: str, data) -> list[str]:
    values = _model_values(data)
    if agent == "opencode" and isinstance(data, dict):
        providers = data.get("provider")
        if isinstance(providers, dict):
            for provider in providers.values():
                if isinstance(provider, dict):
                    values.extend(model_id for model_id, _ in _provider_model_entries(provider.get("models")))
    if agent == "pi" and isinstance(data, dict):
        providers = data.get("providers")
        if isinstance(providers, dict):
            for provider in providers.values():
                if isinstance(provider, dict):
                    values.extend(model_id for model_id, _ in _provider_model_entries(provider.get("models")))
    return sorted(set(value for value in values if isinstance(value, str) and value))


def _call_direct_model_api(api: dict, model: str, prompt: str, *, timeout: int) -> str:
    """发出没有任何工具定义的 Chat Completions 请求并提取文本。"""
    key_env = api.get("api_key_env")
    api_key = api.get("api_key") or (os.environ.get(key_env, "") if isinstance(key_env, str) else "")
    if not api_key:
        raise ValueError("模型 API 凭证环境变量不存在")
    protocol = api.get("protocol")
    if protocol == "anthropic-messages-json":
        payload = {"model": model, "max_tokens": 1024,
                   "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "tools": []}
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        payload = {"model": model,
                   "messages": [{"role": "user", "content": prompt}],
                   "temperature": 0, "tools": [], "tool_choice": "none"}
        headers = {"Accept": "application/json", "Content-Type": "application/json",
                   "Authorization": f"Bearer {api_key}"}
    request = urllib.request.Request(
        api["endpoint"],
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    try:
        opener = urllib.request.build_opener(_NoRedirect)
        with opener.open(request, timeout=max(1, min(int(timeout), 300))) as response:
            raw = response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        # 不把响应正文或 URL（可能包含服务端细节）写进错误/audit 日志。
        raise ValueError(f"无工具模型 API 返回 HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ValueError(f"无工具模型 API 调用失败: {type(exc).__name__}") from exc
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("无工具模型 API 返回不是 JSON") from exc
    if not isinstance(result, dict):
        raise ValueError("无工具模型 API 返回格式无效")
    if isinstance(result.get("error"), dict):
        raise ValueError("无工具模型 API 返回错误")
    if protocol == "anthropic-messages-json":
        content = result.get("content")
        if not isinstance(content, list):
            raise ValueError("Anthropic-compatible API 缺少 content")
        parts = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") != "text":
                raise ValueError("模型返回了工具或非文本内容")
            if not isinstance(item.get("text"), str):
                raise ValueError("模型文本内容格式无效")
            parts.append(item["text"])
        return "".join(parts)
    choices = result.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ValueError("无工具模型 API 缺少 choices")
    choice = choices[0]
    message = choice.get("message")
    if isinstance(message, dict):
        if "tool_calls" in message or "function_call" in message:
            raise ValueError("模型返回了工具调用，拒绝使用该响应")
        content = message.get("content")
    else:
        content = choice.get("text")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, dict) or item.get("type") not in {"text", "output_text"}:
                raise ValueError("模型返回了非文本内容")
            if not isinstance(item.get("text"), str):
                raise ValueError("模型文本内容格式无效")
            parts.append(item["text"])
        return "".join(parts)
    raise ValueError("无工具模型 API 缺少文本内容")


def discover_models() -> dict:
    """发现真实配置，并标出可由无工具协议调用的模型。

    只读配置本身；模型建议不会调用 ``codex exec``，因为其只读 sandbox
    仍可能向模型暴露文件、shell、MCP 或其它自动上下文。只有显式配置且
    认证环境变量存在的直接 API 才标记为 callable。
    """
    output = {}
    for agent, configured_path in MODEL_CONFIG_FILES.items():
        path = _model_config_path(agent, configured_path)
        row = {"agent": agent, "path": str(path), "models": [],
               "available": False, "safe_to_call": False, "callable": False,
               "protocol": "", "runner_path": "", "api_endpoint": "",
               "api_key_env": "", "auth_source": "", "callable_models": [],
               "reason": "未找到可读取的本机模型配置"}
        if not path.is_file() or path.is_symlink():
            output[agent] = row
            continue
        try:
            data = _load_model_data(path)
            models = _discovered_models(agent, data)
            row["models"] = models
            row["available"] = bool(models)
            row["reason"] = "配置可读，但没有明确的模型名" if not models else "已发现模型配置"
            if models:
                specs, api_reason = _model_call_specs(agent, data, models)
                row["callable_models"] = sorted(model_id for model_id in specs if model_id in models)
                if specs:
                    protocols = sorted(set(spec["protocol"] for spec in specs.values()))
                    endpoints = sorted(set(spec["endpoint"] for spec in specs.values()))
                    env_names = sorted(set(spec["api_key_env"] for spec in specs.values() if spec.get("api_key_env")))
                    sources = sorted(set(spec["auth_source"] for spec in specs.values()))
                    row.update({"safe_to_call": True, "callable": True,
                                "protocol": protocols[0] if len(protocols) == 1 else "mixed",
                                "api_endpoint": endpoints[0] if len(endpoints) == 1 else "",
                                "api_key_env": env_names[0] if len(env_names) == 1 else "",
                                "auth_source": sources[0] if len(sources) == 1 else "multiple",
                                "reason": api_reason})
                elif agent == "codex":
                    runner, cli_reason = _codex_cli_path(data)
                    if runner:
                        row["runner_path"] = str(runner)
                        row["reason"] = api_reason + "；" + cli_reason + "；Codex OAuth 仅复用于 CLI 会话，本功能不把它当作无工具 API"
                    else:
                        row["reason"] = api_reason + "；" + cli_reason + "；当前没有可复用的 Codex 无工具认证路径"
                else:
                    row["reason"] = api_reason
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError):
            row["reason"] = "配置存在但无法安全解析，未执行其中任何命令"
        output[agent] = row
    safe = any(row["safe_to_call"] for row in output.values())
    return {"agents": list(output.values()), "safe_to_call": safe,
            "limitations": "模型建议不会使用 codex exec、shell、MCP 或自动上下文；只接受已验证的 agent API 配置或显式 HTTPS/loopback 无工具端点，凭证不返回前端，并只发送 skill 名称和描述"}


def _model_text(value: str) -> str:
    value = str(value or "")
    value = re.sub(r"(?i)(token|secret|password|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+",
                   r"\1=[redacted]", value)
    return value[:1000]


def _model_prompt(summaries: list[dict]) -> str:
    lines = [
        "Classify the following skills into useful groups.",
        "Return JSON only: {\"groups\":[{\"name\":\"group\",\"members\":[\"exact skill name\"],\"description\":\"short\"}]}",
        "Use only exact names below. Do not invent members. A skill may appear in at most one group.",
        "Skills:",
    ]
    for item in summaries:
        # 只把 name/description 送入模型；sid、来源路径和文件内容留在本地。
        lines.append(f"- name: {_model_text(item['name'])}\n  description: {_model_text(item.get('description', ''))}")
    return "\n".join(lines)


def _parse_model_output(output: str) -> dict:
    candidates = []
    for line in str(output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        candidates.append(value)
        if isinstance(value, dict) and ("groups" in value or "suggestions" in value):
            return value
        if isinstance(value, dict):
            item = value.get("item")
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                try:
                    return json.loads(item["text"])
                except json.JSONDecodeError:
                    pass
    for value in reversed(candidates):
        if isinstance(value, dict) and ("groups" in value or "suggestions" in value):
            return value
    try:
        value = json.loads(str(output).strip())
    except json.JSONDecodeError as exc:
        raise ValueError("模型没有返回可解析的 JSON 分组建议") from exc
    if not isinstance(value, dict):
        raise ValueError("模型结果必须是 JSON 对象")
    return value


def validate_model_suggestions(result: dict, summaries: list[dict]) -> list[dict]:
    """把模型按名称返回的结果映射为本地 sid，并拒绝越权/歧义结果。"""
    if not isinstance(result, dict):
        raise ValueError("模型结果必须是对象")
    raw_groups = result.get("groups", result.get("suggestions"))
    if not isinstance(raw_groups, list) or not raw_groups:
        raise ValueError("模型结果缺少 groups 数组")
    by_name = {}
    for item in summaries:
        name = str(item.get("name") or "")
        if not name or name in by_name:
            raise ValueError("同名 skill 无法安全映射模型结果，请先人工分组或改名")
        by_name[name] = item["sid"]
    seen = set()
    output = []
    for position, group in enumerate(raw_groups):
        if not isinstance(group, dict):
            raise ValueError("模型分组项必须是对象")
        name = str(group.get("name") or f"model-group-{position + 1}")[:120]
        if not name or name in {".", ".."} or "/" in name or "\\" in name:
            raise ValueError("模型返回了不安全分组名")
        members = group.get("members")
        if not isinstance(members, list) or not members or not all(isinstance(v, str) for v in members):
            raise ValueError("模型分组 members 必须是非空名称数组")
        mapped = []
        for member in members:
            if member not in by_name:
                raise ValueError(f"模型返回了输入之外的 skill: {member}")
            sid = by_name[member]
            if sid in seen:
                raise ValueError("一个 skill 被模型放入多个组，拒绝自动应用")
            seen.add(sid)
            mapped.append(sid)
        output.append({"status": "suggested", "group_id": re.sub(r"[^A-Za-z0-9_.-]+", "-", name)[:80] or f"model-{position + 1}",
                       "name": name, "members": mapped,
                       "description": str(group.get("description") or "")[:500]})
    return output


def suggest_model_groups(summaries: list[dict], agent: str, model: str,
                         *, runner=None, timeout: int = 90) -> dict:
    """调用实际发现的无工具模型协议；``runner`` 仅供测试注入 mock。"""
    if agent not in {"codex", "claude", "opencode", "pi", "workbuddy", "grok", "hermes"}:
        raise ValueError("未知 agent")
    if not isinstance(summaries, list) or not summaries or len(summaries) > 200:
        raise ValueError("待分组 skill 数量无效")
    for item in summaries:
        if not isinstance(item, dict) or not isinstance(item.get("sid"), str) or not isinstance(item.get("name"), str):
            raise ValueError("skill 摘要缺少安全 sid/name")
        if len(item.get("description", "")) > 1000:
            raise ValueError("skill 描述过长")
    prompt = _model_prompt(summaries)
    if runner is None:
        discovery = discover_models()
        row = next(item for item in discovery["agents"] if item["agent"] == agent)
        if model not in row.get("models", []):
            raise ValueError("请求的模型不是本机配置中实际发现的模型")
        if model not in row.get("callable_models", []):
            raise ValueError(row.get("reason") or "当前 agent 没有安全可调用协议")
        try:
            data = _load_model_data(Path(row["path"]))
        except (OSError, UnicodeError, ValueError, tomllib.TOMLDecodeError) as exc:
            raise ValueError("模型配置在发现后发生变化，未调用") from exc
        specs, _ = _model_call_specs(agent, data, row["models"])
        spec = specs.get(model)
        if not spec:
            raise ValueError("请求的模型当前没有经过验证的无工具 API 配置")
        raw_output = _call_direct_model_api(spec, model, prompt, timeout=timeout)
        protocol = spec["protocol"]
    else:
        # 测试 mock 只接收与真实协议同形的 argv/prompt，不读取真实凭证。
        raw_output = runner(["mock", "exec", "--json", "--model", model], prompt)
        protocol = "mock"
    result = _parse_model_output(raw_output)
    suggestions = validate_model_suggestions(result, summaries)
    return {"status": "ok", "agent": agent, "model": model,
            "protocol": protocol, "suggestions": suggestions,
            "sent_fields": ["name", "description"], "count": len(summaries)}

# ---- 风险门禁 ----
# 命中这些风险标记的 skill 默认禁止投影, 需显式 --allow-risky 才放行。
# 用环境变量覆盖: SKILLHUB_RISK_GATE="sudo,env_secrets" 或 "" 关闭门禁。
RISK_GATE = [r.strip() for r in os.environ.get("SKILLHUB_RISK_GATE", "sudo").split(",") if r.strip()]

RISK_PATTERNS = {
    "exec_shell": r"```(bash|sh|shell|zsh)",
    "network": r"(curl|wget|http://|https://|urllib|requests\.(get|post))",
    "write_files": r"(rm\s+-rf|> */|/etc/|chmod\s+\+x|os\.remove|shutil\.rmtree)",
    "sudo": r"\bsudo\b",
    "pip_install": r"(pip|npm|brew|cargo)\s+(install|add)",
    "env_secrets": r"(os\.environ|process\.env|getenv\(|GITHUB_TOKEN|OPENAI_API_KEY|ANTHROPIC_API_KEY)",
}
