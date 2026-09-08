"""skillhub — 中央库 + 适配生成器 (阶段1)。

设计原则:
- 中央库是唯一权威副本, agent 目录里只放投影(symlink)或适配片段
- 导入只复制、不删除原文件
- 任何写操作默认 dry-run, 显式 --apply 才生效
- 每次投影前先备份, 可 rollback
"""
from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

# ---- 中央库位置 (数据放用户目录, 不混进项目工作区) ----
SKILLHUB_HOME = Path(os.environ.get("SKILLHUB_HOME", HOME / ".skillhub"))
STORE_DIR = SKILLHUB_HOME / "store"      # 每个 skill 一个目录: store/<skill_id>/
BACKUP_DIR = SKILLHUB_HOME / "backups"   # 投影前的备份
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

# ---- 风险门禁 ----
# 命中这些风险标记的 skill 默认禁止投影, 需显式 --force 才放行。
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
