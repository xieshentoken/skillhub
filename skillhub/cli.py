"""skillhub — 集中管理本机多 agent 的 skill (阶段1: 中央库 + 适配投影)。

用法:
  skillhub scan                   扫描所有 agent 的 skill
  skillhub import [--apply]       把扫描到的 skill 导入中央库 (默认 dry-run)
  skillhub list [--risky]         列出中央库 skill
  skillhub link <skill_id> --agents a,b [--force] [--dry-run]
                                  投影 skill 到指定 agent (默认 dry-run)
  skillhub link --all --agents a,b [--force] [--dry-run]
                                  批量: 投影中央库全部 skill (整批只备份一次)
  skillhub link --all-missing --agents a,b [--force] [--dry-run]
                                  批量: 只投影目标 agent 上尚未投影的 skill
  skillhub unlink <skill_id> --agents a,b [--dry-run]
                                  解除投影
  skillhub status [--agent X]     查看各 agent 投影状态
  skillhub backups                列出备份
  skillhub cleanup [--keep N]     清理旧备份 (默认 dry-run, 保留最近 N 份完整快照)
  skillhub rollback <backup_ts>   回滚 store 到备份点
  skillhub mcp import [--apply]   导入 MCP server 定义 (默认 dry-run)
  skillhub mcp list               列出中央库 MCP server 定义
  skillhub mcp generate <sid> --agents a,b [--dry-run]
                                  生成 MCP 配置到 agent (密钥用环境变量引用)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import adapters, store
from .config import AGENTS
from .scan import scan_all

SUCCESS, ERROR = 0, 1


def _print_actions(plan: dict) -> int:
    bad = 0
    for agent, actions in plan.items():
        for a in actions:
            is_bad = a["type"] in ("conflict", "error")
            flag = "✗" if is_bad else "✓"
            if is_bad:
                bad += 1
            print(f"  {flag} [{agent}] {a['type']:8s} {a['target']}  {a['detail']}")
    return bad


def cmd_scan(args) -> int:
    results = scan_all()
    total = 0
    print(f"{'agent':10s} {'skills':>6s}  {'risky':>6s}")
    print("-" * 30)
    for agent in AGENTS:
        recs = results[agent]
        risky = sum(1 for r in recs if r["risks"])
        total += len(recs)
        print(f"{agent:10s} {len(recs):>6d}  {risky:>6d}")
    print("-" * 30)
    print(f"{'total':10s} {total:>6d}")
    return SUCCESS


def cmd_import(args) -> int:
    results = scan_all()
    if args.agent:
        agents = [a for a in args.agent.split(",") if a]
    else:
        agents = list(AGENTS)
    to_import = []
    for agent in agents:
        for rec in results.get(agent, []):
            sid = store.import_skill(agent, rec, apply=False)
            existing = store.get_skill(sid)
            # 需要导入: 中央库没有, 或中央库已有但缺该 agent 的来源记录
            if existing is None or agent not in existing.get("agents", []):
                to_import.append((agent, rec, sid))
    print(f"待导入 {len(to_import)} 个 skill (中央库已有 {len(store.load_index())} 个):")
    for agent, rec, sid in sorted(to_import, key=lambda x: x[1]["name"]):
        risks = ",".join(rec["risks"]) or "-"
        print(f"  {sid:40s} from={agent:10s} risks={risks}")
    if args.apply:
        for agent, rec, sid in to_import:
            store.import_skill(agent, rec, apply=True)
        print(f"\n已导入 {len(to_import)} 个 → {store.STORE_DIR}")
    else:
        print("\n(未写入。加 --apply 执行导入。只复制到中央库, 不改动 agent 原目录。)")
    return SUCCESS


def cmd_list(args) -> int:
    skills = store.list_skills(only_risky=args.risky)
    print(f"中央库共 {len(skills)} 个 skill" + (" (仅含风险标记)" if args.risky else "") + ":")
    for s in skills:
        risks = ",".join(s["risks"]) or "-"
        agents = ",".join(s.get("agents", []))
        print(f"  {s['id']:42s} agents=[{agents}] risks={risks}")
    return SUCCESS


def _resolve_sid(name_or_id: str) -> str:
    """支持按 skill_id 或名称前缀定位。"""
    if store.get_skill(name_or_id):
        return name_or_id
    index = store.load_index()
    matches = [sid for sid in index if sid.startswith(name_or_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"名称 {name_or_id!r} 匹配多个 skill: {matches}", file=sys.stderr)
        raise SystemExit(ERROR)
    print(f"中央库中找不到 skill: {name_or_id}", file=sys.stderr)
    print("可用: skillhub list", file=sys.stderr)
    raise SystemExit(ERROR)


def _collect_sids(args, agents: list) -> list:
    """确定本次要投影的 skill 集合。"""
    if args.all and args.all_missing:
        print("--all 与 --all-missing 只能选一个", file=sys.stderr)
        raise SystemExit(ERROR)
    if args.all:
        return sorted(store.load_index().keys())
    if args.all_missing:
        # 未投影 = status 里不是 linked (含 not_linked 与 conflict)
        st = adapters.status()
        sids = set()
        for agent in agents:
            for item in st.get(agent, []):
                if item["state"] != "linked":
                    sids.add(item["sid"])
        return sorted(sids)
    if not args.skill:
        print("请指定 skill, 或用 --all / --all-missing 批量投影", file=sys.stderr)
        raise SystemExit(ERROR)
    return [_resolve_sid(args.skill)]


def _summarize(results: dict):
    """按 action type 汇总批量结果, 返回 (统计, 冲突+错误数)。"""
    stat: dict = {}
    for agent, actions in results.items():
        for a in actions:
            stat[a["type"]] = stat.get(a["type"], 0) + 1
    bad = stat.get("conflict", 0) + stat.get("error", 0)
    return stat, bad


def _fmt_stat(stat: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(stat.items())) or "无"


def cmd_link(args) -> int:
    agents = [a for a in args.agents.split(",") if a]
    if not agents:
        print("请用 --agents 指定目标 agent (如 pi,codex)", file=sys.stderr)
        return ERROR
    sids = _collect_sids(args, agents)
    batch = bool(args.all or args.all_missing)

    if not batch:
        sid = sids[0]
        plan = adapters.plan_link(sid, agents)
        print(f"投影计划: {sid}")
        bad = _print_actions(plan)
        if args.dry_run:
            print("\n(预览模式, 未写入。去掉 --dry-run 执行。)")
            return SUCCESS
        if bad and not args.force:
            print("\n(存在冲突未执行。对冲突项加 --force 才会备份后替换。)")
            return SUCCESS
        results = adapters.apply_link(sid, agents, force=args.force)
        print("\n执行结果:")
        bad = _print_actions(results)
        return SUCCESS if bad == 0 else ERROR

    print(f"批量投影: {len(sids)} 个 skill → agents={agents}")
    plan: dict = {}
    conflict_sids: list = []
    for sid in sids:
        for agent, actions in adapters.plan_link(sid, agents).items():
            plan.setdefault(agent, []).extend(actions)
            if any(a["type"] == "conflict" for a in actions):
                conflict_sids.append(sid)
    stat, bad = _summarize(plan)
    print(f"  计划: {_fmt_stat(stat)}")
    if args.dry_run:
        print("\n(预览模式, 未写入。去掉 --dry-run 执行。)")
        return SUCCESS
    if bad and not args.force:
        print(f"\n(存在 {bad} 项冲突未执行。加 --force 才会备份后替换。)")
        if conflict_sids:
            head = ", ".join(conflict_sids[:10])
            more = f" ...(共 {len(conflict_sids)} 个)" if len(conflict_sids) > 10 else ""
            print(f"  冲突项: {head}{more}")
        return SUCCESS
    results = adapters.apply_link_batch(sids, agents, force=args.force)
    stat, bad = _summarize(results)
    print(f"\n执行结果: {_fmt_stat(stat)}")
    return SUCCESS if bad == 0 else ERROR


def cmd_unlink(args) -> int:
    sid = _resolve_sid(args.skill)
    agents = [a for a in args.agents.split(",") if a]
    if not agents:
        print("请用 --agents 指定目标 agent", file=sys.stderr)
        return ERROR
    plan = adapters.plan_unlink(sid, agents)
    print(f"解除投影计划: {sid}")
    bad = _print_actions(plan)
    if args.dry_run:
        print("\n(未写入。去掉 --dry-run 执行。)")
        return SUCCESS
    results = adapters.apply_unlink(sid, agents)
    print("\n执行结果:")
    bad = _print_actions(results)
    return SUCCESS if bad == 0 else ERROR


def cmd_status(args) -> int:
    st = adapters.status()
    for agent, items in st.items():
        if args.agent and agent != args.agent:
            continue
        linked = sum(1 for i in items if i["state"] == "linked")
        conflict = sum(1 for i in items if i["state"] == "conflict")
        print(f"{agent:10s} linked={linked:4d} conflict={conflict:4d} total={len(items)}")
        if args.verbose:
            for i in items:
                if i["state"] != "not_linked":
                    print(f"    {i['state']:9s} {i['name']}")
    return SUCCESS


def cmd_backups(args) -> int:
    bks = adapters.list_backups()
    if not bks:
        print("无备份。")
        return SUCCESS
    print("可用备份:")
    for b in bks:
        print(f"  {b.name}")
    return SUCCESS


def cmd_cleanup(args) -> int:
    plan = adapters.plan_cleanup(keep=args.keep)
    if not plan:
        print("无备份可清理。")
        return SUCCESS
    freed = sum(a["size"] for a in plan if a["type"] != "keep")
    print(f"清理计划 (保留最近 {args.keep} 份完整快照):")
    for a in plan:
        flag = "✓" if a["type"] == "keep" else "→"
        size_mb = a["size"] / 1024 / 1024
        print(f"  {flag} {a['ts']}  {a['type']:12s} {size_mb:8.1f}MB  {a['detail']}")
    print(f"\n预计释放 {freed / 1024 / 1024:.1f}MB")
    if args.dry_run:
        print("(预览模式, 未删除。去掉 --dry-run 执行。)")
        return SUCCESS
    done = adapters.apply_cleanup(keep=args.keep)
    print(f"\n已清理: {sum(1 for a in done if a['type'] != 'keep')} 项。")
    return SUCCESS


def cmd_rollback(args) -> int:
    bks = adapters.list_backups()
    if not any(b.name == args.backup for b in bks):
        print(f"找不到备份 {args.backup!r}", file=sys.stderr)
        return ERROR
    import shutil
    src_store = adapters.BACKUP_DIR / args.backup / "store"
    src_index = adapters.BACKUP_DIR / args.backup / "index.json"
    if src_store.exists():
        shutil.rmtree(store.STORE_DIR, ignore_errors=True)
        shutil.copytree(src_store, store.STORE_DIR)
    if src_index.exists():
        shutil.copy2(src_index, store.INDEX_FILE)
    print(f"已回滚 store + index 到 {args.backup}")
    return SUCCESS


def cmd_mcp_import(args) -> int:
    from . import mcp
    try:
        n, definitions, envs, masks = mcp.import_from_workbuddy(apply=args.apply)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        return ERROR
    print(f"从 workbuddy 导入 {n} 个 MCP server 定义到中央库:")
    for server in definitions:
        hdrs = ",".join(server.get("headers", {}).keys()) if server.get("headers") else "-"
        print(f"  {server['id']:24s} {server['transport']:5s} url={server.get('url','-')} headers=[{hdrs}]")
    if envs:
        print("\n检测到密钥, 已用环境变量引用 (定义里不存明文):")
        for name, val in zip(envs, masks):
            print(f"  {name} = {val}  [请自行设置环境变量]")
    if args.apply:
        print(f"\n已写入 → {mcp.MCP_INDEX_FILE}")
    else:
        print("\n(未写入。加 --apply 执行导入。原 workbuddy 配置不会被修改。)")
    return SUCCESS


def cmd_mcp_list(args) -> int:
    from . import mcp
    servers = mcp.list_servers()
    print(f"中央库 MCP server 定义共 {len(servers)} 个:")
    for s in servers:
        agents = ",".join(s.get("agents", []))
        print(f"  {s['id']:24s} {s['transport']:5s} agents=[{agents}]")
    return SUCCESS


def cmd_mcp_generate(args) -> int:
    from . import mcp
    import os
    sid = args.server
    if mcp.get_server(sid) is None:
        print(f"中央库中不存在 MCP server: {sid}", file=sys.stderr)
        print("可用: skillhub mcp list", file=sys.stderr)
        return ERROR
    agents = [a for a in args.agents.split(",") if a]
    if not agents:
        print("请用 --agents 指定目标 agent (如 pi,codex)", file=sys.stderr)
        return ERROR
    # --resolve: 从当前环境读取密钥真实值, 注入字面量 (用于不展开 env 的 agent)
    resolve = None
    if args.resolve:
        definition = mcp.get_server(sid)
        resolve = {}
        missing = []
        raw = json.dumps(definition)
        for var in set(mcp.ENV_REF.findall(raw)):
            val = os.environ.get(var)
            if val:
                resolve[var] = val
            else:
                missing.append(var)
        if missing:
            print(f"警告: 环境变量未设置, 保留引用: {', '.join(missing)}", file=sys.stderr)
    plan = mcp.plan_generate(sid, agents)
    print(f"生成计划: {sid}" + (" (--resolve 注入字面值)" if resolve else " (环境变量引用)"))
    bad = _print_actions(plan)
    if args.dry_run:
        print("\n(预览模式, 未写入。去掉 --dry-run 执行。)")
        return SUCCESS
    results = mcp.apply_generate(sid, agents, resolve=resolve)
    print("\n执行结果:")
    bad = _print_actions(results)
    return SUCCESS if bad == 0 else ERROR


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="skillhub", description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("scan", help="扫描所有 agent 的 skill")
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("import", help="导入扫描结果到中央库 (默认 dry-run)")
    sp.add_argument("--agent", help="只导入指定 agent (逗号分隔)")
    sp.add_argument("--apply", action="store_true", help="实际执行导入")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("list", help="列出中央库 skill")
    sp.add_argument("--risky", action="store_true", help="只显示带风险标记的")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("link", help="投影 skill 到 agent (默认 dry-run)")
    sp.add_argument("skill", nargs="?",
                    help="skill_id 或名称前缀 (用 --all/--all-missing 时可省略)")
    sp.add_argument("--agents", required=True,
                    help="目标 agent, 逗号分隔 (" + ",".join(AGENTS) + ")")
    sp.add_argument("--all", action="store_true", help="批量: 中央库全部 skill")
    sp.add_argument("--all-missing", action="store_true",
                    help="批量: 只投影目标 agent 上尚未投影 (或冲突) 的 skill")
    sp.add_argument("--force", action="store_true", help="冲突时备份后替换")
    sp.add_argument("--dry-run", action="store_true", help="只显示计划")
    sp.set_defaults(fn=cmd_link)

    sp = sub.add_parser("unlink", help="解除投影 (默认 dry-run)")
    sp.add_argument("skill", help="skill_id 或名称前缀")
    sp.add_argument("--agents", required=True, help="目标 agent, 逗号分隔")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_unlink)

    sp = sub.add_parser("status", help="查看各 agent 投影状态")
    sp.add_argument("--agent", help="只看某个 agent")
    sp.add_argument("--verbose", action="store_true", help="显示明细")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("backups", help="列出备份")
    sp.set_defaults(fn=cmd_backups)

    sp = sub.add_parser("cleanup", help="清理旧备份 (默认 dry-run, 保留最近 N 份完整快照)")
    sp.add_argument("--keep", type=int, default=3, help="保留最近几份完整快照 (默认 3)")
    sp.add_argument("--dry-run", action="store_true", help="只显示清理计划")
    sp.set_defaults(fn=cmd_cleanup)

    sp = sub.add_parser("rollback", help="回滚 store 到备份点")
    sp.add_argument("backup", help="备份时间戳 (skillhub backups 查看)")
    sp.set_defaults(fn=cmd_rollback)

    sp = sub.add_parser("mcp", help="MCP 配置层统一 (阶段2)")
    msub = sp.add_subparsers(dest="mcp_cmd", required=True)

    msp = msub.add_parser("import", help="从 workbuddy 导入 MCP server 定义 (默认 dry-run)")
    msp.add_argument("--apply", action="store_true", help="实际写入中央库")
    msp.set_defaults(fn=cmd_mcp_import)

    msp = msub.add_parser("list", help="列出中央库 MCP server 定义")
    msp.set_defaults(fn=cmd_mcp_list)

    msp = msub.add_parser("generate", help="生成 MCP 配置到 agent (默认 dry-run)")
    msp.add_argument("server", help="server id (skillhub mcp list 查看)")
    msp.add_argument("--agents", required=True, help="目标 agent, 逗号分隔")
    msp.add_argument("--dry-run", action="store_true", help="只显示计划")
    msp.add_argument("--resolve", action="store_true",
                     help="从当前环境变量读真实值注入字面量 (用于不展开 env 的 agent, 如 pi)")
    msp.set_defaults(fn=cmd_mcp_generate)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return SUCCESS
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n中断。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
