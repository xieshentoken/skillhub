# AGENTS.md — skillhub

本机多 agent（pi / codex / opencode / workbuddy / claude / grok / hermes）的 **Skill 与 MCP 配置集中管理**。纯 Python 3.11+ 标准库实现，零第三方依赖；一个包 `skillhub/` + 一个集成测试 `tests/test_projection.py`。

## 常用命令

均从项目根目录执行：

```bash
python3 -m skillhub scan                      # 扫描各 agent 的 skill 数量
python3 -m skillhub import --apply            # 导入中央库（默认 dry-run）
python3 -m skillhub list [--risky] [--query kw] [--json]   # 列出/搜索中央库 skill
python3 -m skillhub link <id> --agents pi,codex [--force] [--allow-risky] [--dry-run] [--skip-conflicts] [--json]
python3 -m skillhub link --all|--all-missing --agents ...    # 批量投影
python3 -m skillhub unlink <id> --agents pi,codex [--json]  # 解除投影
python3 -m skillhub status [--agent pi] [--verbose] [--json]  # 投影状态（含 broken 断链检测）
python3 -m skillhub doctor [--json]           # 健康检查：断链/孤儿 copy/风险项/备份膨胀
python3 -m skillhub backups [--json] / rollback <ts>        # 备份与回滚（独立副本）
python3 -m skillhub export <id> -o out.zip    # 导出 skill 包（zip + manifest.json）
python3 -m skillhub add <zip|dir> [--apply]   # 导入 skill 包（校验 manifest 一致性）
python3 -m skillhub mcp import [--apply] [--from codex|claude|opencode|grok]   # MCP 反向导入
python3 -m skillhub mcp list                  # 列出中央库 MCP server
python3 -m skillhub mcp status                # 检查各 agent MCP 配置与中央库的漂移
python3 -m skillhub mcp generate <sid> --agents pi,codex [--dry-run] [--resolve]
python3 -m skillhub ul create|edit|trial|publish ...   # formal/ul 独立版本流程
python3 -m skillhub trash list|restore|retain|cleanup   # 单 skill 回收区
python3 -m skillhub group list|set|distribute ...      # 分组与选择性分发
python3 -m skillhub diagnose <id> [--confirm ...]      # 静态依赖诊断，不执行 skill
python3 -m skillhub models [--json]                    # 只读发现实际配置模型
python3 -m skillhub gui [--port 8317]         # Web GUI（查看 + link/unlink + CSV 导出）
python3 tests/test_projection.py              # 集成测试（用临时目录，不碰真实环境）
```

## 设计不变量（改动时必须保持）

1. **中央库是唯一权威副本**：`~/.skillhub/`（store / index.json / mcp/index.json），agent 目录里只放投影（symlink）或适配片段。
2. **导入只复制**：把 agent 的 skill 复制进中央库，绝不删除/修改 agent 原文件。
3. **写操作默认 dry-run**：所有可能写盘的命令默认只预览，显式 `--apply` 才落盘。
4. **投影前先备份**：`~/.skillhub/backups/<ts>/` 保存 store + index + 被替换的冲突目录，支持 `rollback`；store/index 使用独立副本，避免备份与当前中央库共享 inode，自动只保留最近 `SKILLHUB_MAX_BACKUPS` 份。
5. **copy 投影必须带标记**：copy 模式产物内写 `.skillhub-projection.json`（sid + md5），link 凭标记幂等跳过；标记缺失/被篡改才按冲突处理。
6. **断链必须如实上报**：symlink 源被删时 status/doctor 报 `broken`，不得谎报 linked。
7. **风险门禁**：高危 skill（`SKILLHUB_RISK_GATE`，默认 `sudo`）link 默认拦截，需 `--allow-risky`；`--force` 只用于替换冲突。
8. **MCP 密钥零明文**：中央库定义只存 `{{env:VAR}}` 引用；生成默认写引用，`--resolve` 才注入字面值（仅用于不展开 env 的目标，如 pi 的 host-core）。
9. **去重规则**：skill_id = `<name>--<md5前8>`，同名不同内容互不冲突；相同内容合并来源 agent 记录。
10. **formal/ul 关系**：每个逻辑 skill 最多一个 formal 与一个 ul；ul 是独立目录副本，试用只切换明确指定的 agent，发布只恢复/替换本逻辑 skill 的关系与投影。
11. **单 skill 回收区**：被替换版本进入 `trash/` 并带摘要/保留期限；恢复与清理均按单项处理，清理必须显式确认，不能用全库 rollback 覆盖无关 skill。
12. **模型建议边界**：只读发现真实配置；`codex exec` 的只读 sandbox 不算无工具协议，只有已验证现有 agent API 或显式配置且凭证来自配置/环境的无工具直接 API 才标记 callable。模型输入只含名称和描述，结果必须通过本地 sid 校验并再次明确确认后才写入分组；密钥不得返回 GUI；模型失败不得改写人工分组。
13. **分发来源持久化**：`distributions.json` 按 agent 保存 `group_ids` 与显式 `sids`（以及 replace 选项）；组成员变化不自动写 agent，重新预览/分发时按当前组成员重算，显式 sids 独立保留；formal/ul 共享投影目标按目标去重。

## 可调环境变量（测试/自定义用）

- `SKILLHUB_HOME`：中央库位置，默认 `~/.skillhub`
- `SKILLHUB_AGENT_DIR_<agent>`：覆盖某 agent 的 skill 目录
- `SKILLHUB_MCP_FILE_<agent>`：覆盖某 agent 的 MCP 配置文件
- `SKILLHUB_MCP_PI_DIR`：覆盖 pi 的 servers 目录
- `SKILLHUB_RISK_GATE`：风险门禁关键词（逗号分隔），默认 `sudo`
- `SKILLHUB_MAX_BACKUPS`：备份保留份数，默认 `10`
