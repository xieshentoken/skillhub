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
>
> 项目要求 Python 3.11+（使用标准库 `tomllib` 解析 Codex/Grok 配置）。

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
    webgui.py              # 本地 Web GUI 服务（127.0.0.1，查看 + link/unlink 写端点）
    gui.html               # GUI 单页界面
    cli.py                 # 命令行入口
  tests/test_projection.py # 集成测试（临时目录，不触碰真实环境）

~/.skillhub/               # 中央库数据（用户目录）
  store/<name>--<md5前8>/  # skill 唯一权威副本
  index.json               # skill_id -> manifest（来源、版本、风险）
  mcp/index.json           # MCP server 定义（密钥用 {{env:VAR}} 引用，零明文）
  backups/<ts>/            # 投影/生成前备份
  trash/<entry>/           # 单 skill 旧版本回收区（默认 7 天）
  groups.json              # 中央分组关系，不自动写 agent
  distributions.json       # 每个 agent 保存的分组/单 skill 来源选择
  events.jsonl             # 无密钥审计日志
```

## 用法

```bash
cd <repo-root>

# 扫描所有 agent 的 skill 数量
python3 -m skillhub scan

# 导入到中央库（默认只预览，--apply 才写；只复制，不动 agent 原目录）
python3 -m skillhub import
python3 -m skillhub import --apply

# 列出中央库
python3 -m skillhub list [--risky]

# 投影一个 skill 到指定 agent（默认只预览；--apply 才写）
python3 -m skillhub link dws --agents pi,codex --dry-run   # 预览（兼容别名）
python3 -m skillhub link dws --agents pi,codex --apply
python3 -m skillhub link dws --agents pi --apply --force   # 冲突时备份后替换

# 批量投影（--all 全部 / --all-missing 只投影尚未投影的）
# --skip-conflicts：批量时跳过冲突项继续其余项（默认整批停止）
python3 -m skillhub link --all --agents workbuddy,codex
python3 -m skillhub link --all-missing --agents workbuddy,codex --apply --force
python3 -m skillhub link --all --agents workbuddy --skip-conflicts

# 解除投影
python3 -m skillhub unlink dws --agents pi --apply

# 查看各 agent 投影状态（含断链检测：store 副本被删时报告 broken）
python3 -m skillhub status [--agent pi] [--verbose]

# 健康检查：断链投影 / 孤儿 copy 产物 / 风险项 / 备份膨胀 一次体检
python3 -m skillhub doctor [--json]

# 备份与回滚（默认只预览，--apply 才恢复/清理）
python3 -m skillhub backups
python3 -m skillhub rollback <时间戳> --apply
python3 -m skillhub cleanup --keep 3 --apply

# formal / ul 版本流程（默认预览；ul 是独立副本）
python3 -m skillhub ul create <formal-sid> --apply
python3 -m skillhub ul edit <ul-sid> --file run.py --text $'print(2)\n' --apply
python3 -m skillhub ul trial <formal-sid> --agents claude --apply
python3 -m skillhub ul publish <ul-sid> --apply

# 单项回收区：恢复只影响该逻辑 skill；清理必须显式 --apply
python3 -m skillhub trash list --json
python3 -m skillhub trash retain <entry> --apply
python3 -m skillhub trash restore <entry> --apply
python3 -m skillhub trash cleanup --apply

# 静态依赖诊断（不执行 skill/脚本/网络）
python3 -m skillhub diagnose <sid> --json

# 分组与选择性分发：组变化本身不写 agent，分发才写
python3 -m skillhub group set release --members <sid1>,<sid2> --apply
python3 -m skillhub group distribute --groups release --agents pi,codex --replace --apply
# 按各 agent 上次保存的来源重新计算（组成员变化会在这里体现）
python3 -m skillhub group distribute --agents pi,codex --apply

# 发现实际配置中的模型名；只有已验证 agent API 或显式无工具直接 API 才可调用
python3 -m skillhub models --json

# 导出 / 导入 skill 包（跨机器迁移）
python3 -m skillhub export <skill_id|名称前缀> -o out.zip   # 打包 skill + manifest.json
python3 -m skillhub add out.zip --apply                      # 导入 zip 或目录（校验 manifest 一致性）

# 脚本化输出
python3 -m skillhub list --json
python3 -m skillhub status --json --agent pi

# 本地 Web GUI（127.0.0.1；带一次性令牌；写操作先预览）
python3 -m skillhub gui [--port 8317] [--no-browser] [--read-only]
```

`link`/`unlink` 可用 skill_id 或名称前缀定位 skill。

## Web GUI

`skillhub gui` 启动一个本地网页控制台（自动打开浏览器）：

- **Agent 概览**：每个 agent 的目录、投影方式（symlink/copy/nested）、已投影/冲突/未投影计数与比例条、MCP 支持情况；
- **中央库 Skills**：搜索 + 按 agent/状态/风险过滤；列表与 Obsidian 式关系图谱（每个 skill 一个点，已投影到 agent 的画连线；悬停、单击高亮邻居、双击打开详情）；详情里可执行 link/unlink、创建/编辑 ul、切换试用 agent、发布、改名、版本差异、静态诊断和文本保存；
- **MCP Servers**：中央库定义一览（transport、目标、env/header 变量名、适用 agent）；
- **备份 / 回收区**：备份点、单 skill 回收项、手动保留、单项恢复和明确确认的到期清理；全库 rollback 只走 CLI；GUI 启动或刷新不会自动删除；
- **导入 / 分组 / 分发**：从已发现的 agent 来源选择版本导入；同名版本可比较；分组支持拖拽和勾选替代操作，分发按 agent 保存分组与单 skill 来源，组变化本身不写 agent，下一次预览按当前成员重新计算；显式单 skill 选择不会因移出分组而丢失，trial 的 formal/ul 共享目标也不会被 replace 误删；
- **日志 / 设置 / 模型**：查看无密钥审计日志、设置回收期限、发现实际本机模型配置；不会把 Codex 的 `exec --json` 只读 sandbox 当作无工具协议，只有已验证的 Claude/OpenCode/Pi API 配置或显式无工具直接 API 才生成建议，其它模型仅显示配置但不可调用；
- **表格可排序 + CSV 导出**：点击列头排序，导出当前过滤结果为 CSV；
- **自动刷新**：可勾选定时刷新，配合外部 CLI 操作使用。

安全边界：只绑定 `127.0.0.1`；启动时在终端打印一次性令牌，浏览器必须用该 URL 才能拿到 HttpOnly 会话 cookie（无令牌的 `GET /` 不会发 cookie）。GET 校验 loopback Host 和会话 cookie（同源 GET fetch 不带 Origin，缺 Origin 放行；若带了 Origin 则必须同源）。POST 必须带匹配的 loopback Origin。写端点要求 `application/json`、大小限制和显式 `apply`，投影/解除也先预览再确认。GUI **拒绝** `allow_risky`、拒绝改 MCP `command`/`args`、拒绝配置外部编辑器、不提供全库 rollback。发布、MCP 生成、replace 分发和清理需再输入确认短语。`--read-only` 禁用全部 POST。风险门禁在 GUI 同样生效且不能从页面绕过。
GUI 与 CLI 共享同一份 `~/.skillhub` 数据，刷新即最新。

## MCP 配置层（阶段 2）

把现有 agent 的 MCP server 统一收进中央库，再生成各 agent 自己的配置格式。

```bash
# 从任意 agent 导入 MCP server 定义（默认只预览；--apply 才写中央库）
# --from 可选 codex / claude / opencode / grok，默认 workbuddy
python3 -m skillhub mcp import [--apply] [--from codex]

# 列出中央库 MCP server 定义
python3 -m skillhub mcp list

# 检查各 agent MCP 配置与中央库的漂移（缺失 / 已存在 / 内容不一致）
python3 -m skillhub mcp status

# 生成 MCP 配置到目标 agent（默认只预览）
python3 -m skillhub mcp generate qcc-company --agents pi,codex             # 预览
python3 -m skillhub mcp generate qcc-company --agents pi,codex --apply     # 执行
python3 -m skillhub mcp generate qcc-company --agents pi --resolve         # 从环境变量注入字面值
```

**密钥处理（默认零明文）**：导入时检测到的密钥（Bearer token 等）不写入中央库，
只用 `{{env:QCC_COMPANY_TOKEN}}` 占位，并在导入输出中提示你设置对应的环境变量。
生成的 agent 配置里：
- claude 使用官方支持的 `${VAR}` 引用；
- codex / grok 使用 `${VAR}`，pi / workbuddy 也保留 `${VAR}`；
- OpenCode 使用当前 schema 的直接 `mcp.<id>` server map：stdio 为 `type=local`、`command` 数组、`environment`，HTTP 为 `type=remote`、`url`/`headers`，环境引用为 `{env:VAR}`。
- **若目标 agent 不展开环境变量**（如 pi 的 host-core 把 header 当字面值），
  生成前先在 shell 里 `export QCC_COMPANY_TOKEN='...'`，再带 `--resolve` 注入字面值。

生成目标格式：

| agent | 目标文件 | 写入方式 |
|---|---|---|
| pi | `~/.agents/servers/<id>.json` | 每 server 一个 McpConfig 文件 |
| codex | `~/.codex/config.toml` | 写入 `[mcp_servers.<id>]`，HTTP 使用 `http_headers` / `env_http_headers` / `bearer_token_env_var` |
| grok | `~/.grok/config.toml` | 同 Codex 的 `[mcp_servers.<id>]` 结构 |
| workbuddy | `~/.workbuddy/mcp.json` | 合并进 `mcpServers`，保留原格式（不强加 `type`） |
| claude | `~/.claude.json` | 合并进 `mcpServers`（stdio/http，环境引用 `${VAR}`） |
| opencode | `~/.config/opencode/opencode.jsonc` | 合并进当前官方的直接 `mcp` map（local/remote） |
| hermes | — | 未发现 MCP 配置支持，跳过 |

## 安全设计

- **导入只复制**：把 agent 的 skill 复制进中央库，原目录分毫不动。
- **所有写操作默认预览**：`import`、`link`、`unlink`、`add`、`cleanup`、`rollback`、MCP 导入/生成默认只显示计划；只有显式 `--apply` 才写入，`--dry-run` 是兼容别名且与 `--apply` 互斥。`export` 必须显式指定 `--out`。
- **每次投影先备份**：`backups/<时间戳>/` 保存 store + index + 被替换的冲突目录，可 `rollback --apply`；store 使用独立副本，不与当前中央库共享 inode，避免通过投影修改 store 时连带改坏备份。快照列表显示逻辑大小，实际可回收空间取决于文件系统；自动清理只保留最近 `SKILLHUB_MAX_BACKUPS` 份（默认 10）。
- **批量只备份一次**：`link --all` / `--all-missing` 在整批开始前做一次全量备份，再让每个 skill 跳过自己的备份。逐个备份会让几百个 skill 重复复制整个 store（实测 196 个 × 59M ≈ 11GB）。
- **批量遇冲突整批停止 / 或跳过**：批量时只要有一项冲突就不写入任何内容，需显式 `--force`；加 `--skip-conflicts` 则跳过冲突项继续其余项，输出会分别列出冲突项与被跳过项。
- **copy 模式产物有凭据**：copy 产物内写入 `.skillhub-projection.json` 标记（sid + md5），再次 `link` 可凭标记识别"这是本库副本"从而幂等跳过，不再误判为冲突；标记被篡改/缺失才按冲突处理。
- **断链检测**：symlink 投影的 store 源被删除后，`status` 会如实报告 `broken`（而不是谎报 linked），`doctor` 会列出全部断链并给出修复建议（重新 link 或 unlink）。
- **风险门禁**：识别为高危的 skill（默认特征：文档/脚本含 `sudo` 等，可用 `SKILLHUB_RISK_GATE` 调整）在 link 时默认拦截，需显式 `--allow-risky` 放行；`--force` 只负责替换冲突，拦截项在 `status`/`doctor`/GUI 中均有标注。
- **漂移拒绝**：中央 store 文件与 index 摘要不一致时报告 `store_drift`，link/unlink 不会继续删除或投影；workbuddy 等 copy 投影被修改时报告 `copy_drift`，默认不删除。
- **MCP 可回滚**：MCP 导入/生成会把原配置路径、缺失状态和独立副本记录到 `backups/<ts>/mcp/targets.json`；同一 `rollback <ts> --apply` 可恢复 MCP 文件，解析失败不会覆盖原文件。
- **去重**：skill_id = `<name>--<md5前8>`，同名不同内容互不冲突；相同内容合并来源 agent 记录。
- **formal / ul 关系**：旧索引不补写、不丢字段；新版本用 `channel`、`logical_id` 和关系 sid 增量记录，每个逻辑 skill 最多一个 formal 和一个 ul。ul 目录是独立副本，基础 content id 仍按完整文件集摘要计算，ul 目录额外使用 `--ul` 角色后缀避免与 formal 共用 inode/路径。
- **发布与恢复边界**：发布只备份相关 formal/ul、投影和关系索引，写入失败按 skill 级合并恢复，不使用全库 rollback 覆盖无关 skill；正式旧版本进入 `trash/`，恢复当前版本也先进入回收区。
- **依赖诊断**：只读工具路径和环境变量存在性，推断项标记 `inferred/verify`；不运行 skill、脚本、服务或网络。显式缺失/未人工确认会阻止发布，人工确认会留下无密钥日志。
- **扫描跟随软链**：`Path.rglob` 不会进入符号链接目录，而 symlink 模式 agent（pi / claude / grok）的 skill 目录本身就是指向中央库的软链，用 rglob 会恒返回 0 条。`scan` 改为手动下钻（用 realpath 集合做环路保护），并跳过隐藏目录（如 codex 的 `.system` 内置技能）与 `node_modules` 等第三方依赖目录。
- **MCP 密钥零明文**：中央库 `mcp/index.json` 只存 `{{env:VAR}}` 引用；导入会检查敏感字段、URL 查询参数和 args；生成默认也写引用；`--resolve` 才注入字面值，且中央库仍保持零明文。导入或生成前解析失败会停止，不覆盖原配置。

## 可调配置（环境变量）

- `SKILLHUB_HOME`：中央库位置，默认 `~/.skillhub`
- `SKILLHUB_AGENT_DIR_<agent>`：覆盖某 agent 的 skill 目录（测试/自定义用）
- `SKILLHUB_MCP_FILE_<agent>`：覆盖某 agent 的 MCP 配置文件（测试用）
- `SKILLHUB_MCP_PI_DIR`：覆盖 pi 的 servers 目录（测试用）
- `SKILLHUB_RISK_GATE`：风险门禁关键词（逗号分隔），默认 `sudo`
- `SKILLHUB_MAX_BACKUPS`：备份保留份数，默认 `10`
- `SKILLHUB_TRASH_RETENTION_DAYS`：回收区默认保留天数，默认 `7`
- `SKILLHUB_MODEL_FILE_<agent>`：只读模型配置文件覆盖；不会执行配置中的命令
- `SKILLHUB_MODEL_API_ENDPOINT_<agent>` / `SKILLHUB_MODEL_API_KEY_ENV_<agent>`：显式配置无工具 OpenAI-compatible `/chat/completions` 端点和凭证环境变量名（只支持 HTTPS 或 loopback HTTP）

例如：`export SKILLHUB_MODEL_API_ENDPOINT_codex=https://api.example.com/v1/chat/completions`、
`export SKILLHUB_MODEL_API_KEY_ENV_codex=OPENAI_API_KEY`；模型名仍必须先在 `SKILLHUB_MODEL_FILE_codex`
（默认 `~/.codex/config.toml`）中被发现，skillhub 不读取配置文件中的明文 key。

默认还会读取 OpenCode 的 `~/.config/opencode/opencode.jsonc`（`provider.*.options.baseURL/apiKey/models`）和
Pi 的 `~/.pi/agent/models.json`（`providers.*.baseUrl/api/apiKey/models`）；Claude 的
`~/.claude/settings.json` 只复用其现有 Anthropic-compatible `env` 配置。密钥只留在后端请求内存中，不返回 GUI。

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
python3 tests/test_projection.py   # 全程用临时目录，不碰真实环境
```
