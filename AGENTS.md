# AGENTS.md — skillhub

本机多 agent（pi / codex / opencode / workbuddy / claude / hermes）的 **Skill 与 MCP 配置集中管理**。纯 Python 3 标准库实现，零第三方依赖；一个包 `skillhub/` + 一个集成测试 `tests/test_projection.py`。

## 常用命令

均从项目根目录执行：

```bash
python3 -m skillhub scan                      # 扫描各 agent 的 skill 数量
python3 -m skillhub import --apply            # 导入中央库（默认 dry-run）
python3 -m skillhub list [--risky]            # 列出中央库 skill
python3 -m skillhub link <id> --agents pi,codex [--force] [--dry-run]   # 投影
python3 -m skillhub unlink <id> --agents pi,codex                       # 解除投影
python3 -m skillhub status [--agent pi] [--verbose]                     # 投影状态
python3 -m skillhub backups / rollback <ts>   # 备份与回滚
python3 -m skillhub mcp import [--apply]      # 导入 MCP server 定义（默认 dry-run）
python3 -m skillhub mcp list                  # 列出中央库 MCP server
python3 -m skillhub mcp generate <sid> --agents pi,codex [--dry-run] [--resolve]
python3 tests/test_projection.py              # 集成测试（用临时目录，不碰真实环境）
```

## 设计不变量（改动时必须保持）

1. **中央库是唯一权威副本**：`~/.skillhub/`（store / index.json / mcp/index.json），agent 目录里只放投影（symlink）或适配片段。
2. **导入只复制**：把 agent 的 skill 复制进中央库，绝不删除/修改 agent 原文件。
3. **写操作默认 dry-run**：所有可能写盘的命令默认只预览，显式 `--apply` 才落盘。
4. **投影前先备份**：`~/.skillhub/backups/<ts>/` 保存 store + index + 被替换的冲突目录，支持 `rollback`。
5. **MCP 密钥零明文**：中央库定义只存 `{{env:VAR}}` 引用；生成默认写引用，`--resolve` 才注入字面值（仅用于不展开 env 的目标，如 pi 的 host-core）。
6. **去重规则**：skill_id = `<name>--<md5前8>`，同名不同内容互不冲突；相同内容合并来源 agent 记录。

## 可调环境变量（测试/自定义用）

- `SKILLHUB_HOME`：中央库位置，默认 `~/.skillhub`
- `SKILLHUB_AGENT_DIR_<agent>`：覆盖某 agent 的 skill 目录
- `SKILLHUB_MCP_FILE_<agent>`：覆盖某 agent 的 MCP 配置文件
- `SKILLHUB_MCP_PI_DIR`：覆盖 pi 的 servers 目录
