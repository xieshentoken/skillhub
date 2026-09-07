# skillhub — 本机多 Agent Skill / MCP 集中管理

[English](README.en.md) | **简体中文**

集中管理一台机器上多个 agent（pi / codex / opencode / workbuddy / claude / grok / hermes）的 **skill** 与 **MCP 配置**：
**一份权威副本在中央库，各 agent 目录只放投影（符号链接或复制）或生成的目标格式片段**。

## 安装

```bash
# 本地安装（可编辑模式，改动即时生效）
pip install -e .
```

> 系统自带 Python 的 pip 版本可能过旧（不支持 `pyproject.toml` 的 editable 安装），
> 安装前请先升级：`python3 -m pip install --upgrade pip`。

安装后可直接使用 `skillhub` 命令；未安装时也可在项目根目录用 `python3 -m skillhub` 运行。

## 目录结构

```
<repo-root>/
  skillhub/                # Python 包
    config.py              # agent 定义、中央库路径、风险特征
    scan.py                # 扫描各 agent 的 SKILL.md
    store.py               # 中央库：导入、去重、索引
    adapters.py            # 投影引擎：link/unlink、冲突、备份回滚
    mcp.py                 # MCP 配置层：中央定义 + 各 agent 格式生成
    cli.py                 # 命令行入口
  tests/test_projection.py # 集成测试（临时目录，不触碰真实环境）

~/.skillhub/               # 中央库数据（用户目录）
  store/<name>--<md5前8>/  # skill 唯一权威副本
  index.json               # skill_id -> manifest（来源、版本、风险）
  mcp/index.json           # MCP server 定义（密钥用 {{env:VAR}} 引用，零明文）
  backups/<ts>/            # 投影/生成前备份
```

## 用法

```bash
cd <repo-root>

# 扫描所有 agent 的 skill 数量
python3 -m skillhub scan

# 导入到中央库（默认 dry-run，--apply 才写；只复制，不动 agent 原目录）
python3 -m skillhub import --apply

# 列出中央库
python3 -m skillhub list [--risky]

# 投影一个 skill 到指定 agent（默认执行；--dry-run 预览）
python3 -m skillhub link dws --agents pi,codex --dry-run   # 预览
python3 -m skillhub link dws --agents pi,codex              # 无冲突时直接执行
python3 -m skillhub link dws --agents pi --force            # 冲突时备份后替换

# 批量投影（--all 全部 / --all-missing 只投影尚未投影的）
python3 -m skillhub link --all --agents workbuddy,codex --dry-run
python3 -m skillhub link --all-missing --agents workbuddy,codex --force

# 解除投影
python3 -m skillhub unlink dws --agents pi

# 查看各 agent 投影状态
python3 -m skillhub status [--agent pi] [--verbose]

# 备份与回滚
python3 -m skillhub backups
python3 -m skillhub rollback <时间戳>
```

`link`/`unlink` 可用 skill_id 或名称前缀定位 skill。

## MCP 配置层（阶段 2）

把现有 agent 的 MCP server 统一收进中央库，再生成各 agent 自己的配置格式。

```bash
# 从 workbuddy 导入 MCP server 定义（默认 dry-run；--apply 才写中央库）
python3 -m skillhub mcp import [--apply]

# 列出中央库 MCP server 定义
python3 -m skillhub mcp list

# 生成 MCP 配置到目标 agent（默认 dry-run）
python3 -m skillhub mcp generate qcc-company --agents pi,codex --dry-run   # 预览
python3 -m skillhub mcp generate qcc-company --agents pi,codex             # 执行
python3 -m skillhub mcp generate qcc-company --agents pi --resolve         # 从环境变量注入字面值
```

**密钥处理（默认零明文）**：导入时检测到的密钥（Bearer token 等）不写入中央库，
只用 `{{env:QCC_COMPANY_TOKEN}}` 占位，并在导入输出中提示你设置对应的环境变量。
生成的 agent 配置里：
- claude 用 `{env:VAR}`（官方支持展开）
- codex / opencode / pi / workbuddy 用 `${VAR}` 引用
- **若目标 agent 不展开环境变量**（如 pi 的 host-core 把 header 当字面值），
  生成前先在 shell 里 `export QCC_COMPANY_TOKEN='...'`，再带 `--resolve` 注入字面值。

生成目标格式：

| agent | 目标文件 | 写入方式 |
|---|---|---|
| pi | `~/.agents/servers/<id>.json` | 每 server 一个 McpConfig 文件 |
| codex | `~/.codex/config.toml` | 追加 `[[mcp_servers.<id>]]` 段（已存在则跳过） |
| workbuddy | `~/.workbuddy/mcp.json` | 合并进 `mcpServers`（保持原格式无 type） |
| claude | `~/.claude.json` | 合并进 `mcpServers`（type=http） |
| opencode | `~/.config/opencode/opencode.jsonc` | 合并进 `mcp`（type=remote） |
| hermes | — | 未发现 MCP 配置支持，跳过 |

## 安全设计

- **导入只复制**：把 agent 的 skill 复制进中央库，原目录分毫不动。
- **投影可预览**：`link`/`unlink` 加 `--dry-run` 只看计划；执行时遇到冲突（目标已有非本库目录）会拒绝，需显式 `--force`。
- **每次投影先备份**：`backups/<时间戳>/` 保存 store + index + 被替换的冲突目录，可 `rollback`。
- **批量只备份一次**：`link --all` / `--all-missing` 在整批开始前做一次全量备份，再让每个 skill 跳过自己的备份。逐个备份会让几百个 skill 重复复制整个 store（实测 196 个 × 59M ≈ 11GB）。
- **批量遇冲突整批停止**：批量时只要有一项冲突就不写入任何内容，需显式 `--force`；输出会列出冲突项，方便先挑出来单独处理。
- **copy 模式（workbuddy）重复投影会判为冲突**：copy 产物不像 symlink 那样能被验证指向，再次 `link` 同一 skill 会被当成"已存在的非本库目录"，需 `--force` 备份后替换。
- **去重**：skill_id = `<name>--<md5前8>`，同名不同内容互不冲突；相同内容合并来源 agent 记录。
- **扫描跟随软链**：`Path.rglob` 不会进入符号链接目录，而 symlink 模式 agent（pi / claude / grok）的 skill 目录本身就是指向中央库的软链，用 rglob 会恒返回 0 条。`scan` 改为手动下钻（用 realpath 集合做环路保护），并跳过隐藏目录（如 codex 的 `.system` 内置技能）与 `node_modules` 等第三方依赖目录。
- **MCP 密钥零明文**：中央库 `mcp/index.json` 只存 `{{env:VAR}}` 引用；生成默认也写引用；`--resolve` 注入字面值仅在目标 agent 不支持环境变量展开时使用，且中央库仍保持零明文。

## 可调配置（环境变量）

- `SKILLHUB_HOME`：中央库位置，默认 `~/.skillhub`
- `SKILLHUB_AGENT_DIR_<agent>`：覆盖某 agent 的 skill 目录（测试/自定义用）
- `SKILLHUB_MCP_FILE_<agent>`：覆盖某 agent 的 MCP 配置文件（测试用）
- `SKILLHUB_MCP_PI_DIR`：覆盖 pi 的 servers 目录（测试用）

## 各 agent 适配方式

| agent | 目录 | 投影方式 | 说明 |
|---|---|---|---|
| pi | `~/.agents/skills` | symlink | |
| codex | `~/.codex/skills` | symlink | |
| opencode | `~/.config/opencode/skills` | symlink | |
| workbuddy | `~/.workbuddy/skills` | copy | 不信任 symlink 的用复制 |
| claude | `~/.claude/skills` | symlink | 目录默认不存在，首次 link 时创建 |
| grok | `~/.grok/skills` | symlink | 目录默认不存在；grok 另外还会兼容扫描 `~/.agents/skills`、`~/.claude/skills` |
| hermes | App Support hermes-home/skills | symlink | 分类层级，按 `<category>/<skill>` 投影 |

### grok 的兼容扫描与优先级（实测 v1.0.13）

grok 除了 `~/.grok/skills`（含项目级 `./.grok/skills`、`[skills] paths` 指定目录）外，
还会自动读取 `~/.agents/skills`、`~/.claude/skills`、`~/.cursor` 等（可用
`[compat.claude] skills = false` 关闭）。实测行为：

- **同名 skill 只注册一次**，不会重复出现（`grok inspect --json` 计数不变）。
- **`~/.grok/skills` 优先**：同名时该目录覆盖兼容目录，`inspect` 的 `source.path` 会指向它。

所以即使不做投影，grok 也能通过 `~/.agents/skills` 读到 skillhub 给 pi 的投影；
但显式投影到 `~/.grok/skills` 可以摆脱对 pi 的依赖（unlink pi 时 grok 不受影响），
推荐执行 `skillhub link --all --agents grok`。

## 测试

```bash
python3 tests/test_projection.py   # 12 项，全程用临时目录，不碰真实环境
```
