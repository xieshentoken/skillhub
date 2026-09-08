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
  skillhub doctor [--json]        体检: 断链/孤儿/漂移/冲突/同名多版本
  skillhub backups                列出备份
  skillhub cleanup [--keep N]     清理旧备份 (默认 dry-run, 保留最近 N 份完整快照)
  skillhub rollback <backup_ts>   回滚 store 到备份点
  skillhub mcp import [--apply]   导入 MCP server 定义 (默认 dry-run)
  skillhub mcp list               列出中央库 MCP server 定义
  skillhub mcp generate <sid> --agents a,b [--dry-run]
                                  生成 MCP 配置到 agent (密钥用环境变量引用)
  skillhub mcp import --from X [--apply]
                                  从指定 agent 的现有配置反向导入 (X=workbuddy/codex/claude/opencode/grok)
  skillhub mcp status            各 agent 配置里是否已存在中央库的 server
  skillhub export <skill_id> [--out x.zip]   打包单个 skill 为可分发的 zip
  skillhub add <zip|dir> [--apply]           从 zip 或目录导入 skill 到中央库

大部分查看类命令支持 --json, 便于脚本消费 (scan/list/status/doctor/backups/link/unlink/mcp list)。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from . import adapters, store
from .config import AGENTS
from .scan import scan_all

SUCCESS, ERROR = 0, 1


def add_json_arg(sp) -> None:
    """给子命令加 --json (幂等, 重复调用无副作用)。"""
    try:
        sp.add_argument("--json", action="store_true", help="以 JSON 输出, 便于脚本消费")
    except argparse.ArgumentError:
        pass


def jout(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _print_actions(plan: dict) -> int:
    bad = 0
    for agent, actions in plan.items():
        for a in actions:
            is_bad = a["type"] in ("conflict", "error", "blocked", "broken")
            flag = "✗" if is_bad else "✓"
            if is_bad:
                bad += 1
            print(f"  {flag} [{agent}] {a['type']:8s} {a['target']}  {a['detail']}")
    return bad


def cmd_scan(args) -> int:
    results = scan_all()
    rows = []
    total = 0
    for agent in AGENTS:
        recs = results[agent]
        risky = sum(1 for r in recs if r["risks"])
        total += len(recs)
        rows.append({"agent": agent, "skills": len(recs), "risky": risky})
    if getattr(args, "json", False):
        jout({"agents": rows, "total": total})
        return SUCCESS
    print(f"{'agent':10s} {'skills':>6s}  {'risky':>6s}")
    print("-" * 30)
    for r in rows:
        print(f"{r['agent']:10s} {r['skills']:>6d}  {r['risky']:>6d}")
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


def _filter_skills(skills: list, query: str) -> list:
    """按关键词过滤 (名称 / 描述 / skill_id / 分类 / 风险)。"""
    q = (query or "").strip().lower()
    if not q:
        return skills
    out = []
    for s in skills:
        hay = " ".join([s.get("name", ""), s.get("fm_desc", ""), s["id"],
                        s.get("category", ""), ",".join(s.get("risks", []))]).lower()
        if q in hay:
            out.append(s)
    return out


def cmd_list(args) -> int:
    skills = store.list_skills(only_risky=args.risky)
    skills = _filter_skills(skills, getattr(args, "query", ""))
    if getattr(args, "json", False):
        jout({"count": len(skills), "skills": skills})
        return SUCCESS
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
    """按 action type 汇总批量结果, 返回 (统计, 需要人工处理的数量)。"""
    stat: dict = {}
    for agent, actions in results.items():
        for a in actions:
            stat[a["type"]] = stat.get(a["type"], 0) + 1
    bad = sum(stat.get(t, 0) for t in ("conflict", "error", "blocked", "broken"))
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
        plan = adapters.plan_link(sid, agents, force=args.force)
        if getattr(args, "json", False):
            jout({"mode": "plan" if args.dry_run else "apply", "sid": sid,
                  "agents": agents, "actions": plan})
            if args.dry_run:
                return SUCCESS
        else:
            print(f"投影计划: {sid}")
            bad = _print_actions(plan)
            if args.dry_run:
                print("\n(预览模式, 未写入。去掉 --dry-run 执行。)")
                return SUCCESS
        if bad and not args.force:
            if not getattr(args, "json", False):
                print("\n(存在需处理项未执行: conflict/blocked 可加 --force; broken 需先修复中央库。)")
            return SUCCESS if not getattr(args, "json", False) else ERROR
        results = adapters.apply_link(sid, agents, force=args.force)
        if getattr(args, "json", False):
            jout({"mode": "apply", "sid": sid, "agents": agents, "actions": results})
            return SUCCESS if _summarize(results)[1] == 0 else ERROR
        print("\n执行结果:")
        bad = _print_actions(results)
        return SUCCESS if bad == 0 else ERROR

    def _plan_for(target_sids):
        p: dict = {}
        conflicts: list = []
        for sid in target_sids:
            for agent, actions in adapters.plan_link(sid, agents, force=args.force).items():
                p.setdefault(agent, []).extend(actions)
                if any(a["type"] == "conflict" for a in actions):
                    conflicts.append(sid)
        return p, conflicts

    plan, conflict_sids = _plan_for(sids)
    stat, bad = _summarize(plan)
    # --skip-conflicts: 丢掉冲突项, 把其余的做完 (而不是整批停)
    if bad and not args.force and getattr(args, "skip_conflicts", False):
        skipped = sorted(set(conflict_sids))
        sids = [s for s in sids if s not in set(skipped)]
        plan, conflict_sids = _plan_for(sids)
        stat, bad = _summarize(plan)
        if not getattr(args, "json", False):
            print(f"批量投影: {len(sids)} 个 skill → agents={agents} (已跳过 {len(skipped)} 个冲突项)")
    elif not getattr(args, "json", False):
        print(f"批量投影: {len(sids)} 个 skill → agents={agents}")

    if getattr(args, "json", False):
        jout({"mode": "plan" if args.dry_run else "apply", "sids": sids,
              "agents": agents, "stat": stat, "actions": plan})
        if args.dry_run:
            return SUCCESS
    else:
        print(f"  计划: {_fmt_stat(stat)}")
        if args.dry_run:
            print("\n(预览模式, 未写入。去掉 --dry-run 执行。)")
            return SUCCESS
    if bad and not args.force:
        if getattr(args, "json", False):
            return ERROR
        print(f"\n(存在 {bad} 项需处理未执行。conflict/blocked 可加 --force, 或用 --skip-conflicts 跳过; broken 需先修复中央库。)")
        if conflict_sids:
            head = ", ".join(conflict_sids[:10])
            more = f" ...(共 {len(conflict_sids)} 个)" if len(conflict_sids) > 10 else ""
            print(f"  冲突项: {head}{more}")
        return SUCCESS
    results = adapters.apply_link_batch(sids, agents, force=args.force)
    stat, bad = _summarize(results)
    if getattr(args, "json", False):
        jout({"mode": "apply", "sids": sids, "agents": agents,
              "stat": stat, "actions": results})
        return SUCCESS if bad == 0 else ERROR
    print(f"\n执行结果: {_fmt_stat(stat)}")
    return SUCCESS if bad == 0 else ERROR


def cmd_unlink(args) -> int:
    sid = _resolve_sid(args.skill)
    agents = [a for a in args.agents.split(",") if a]
    if not agents:
        print("请用 --agents 指定目标 agent", file=sys.stderr)
        return ERROR
    plan = adapters.plan_unlink(sid, agents)
    if getattr(args, "json", False):
        jout({"mode": "plan" if args.dry_run else "apply", "sid": sid,
              "agents": agents, "actions": plan})
        if args.dry_run:
            return SUCCESS
    else:
        print(f"解除投影计划: {sid}")
        _print_actions(plan)
        if args.dry_run:
            print("\n(未写入。去掉 --dry-run 执行。)")
            return SUCCESS
    results = adapters.apply_unlink(sid, agents)
    if getattr(args, "json", False):
        jout({"mode": "apply", "sid": sid, "agents": agents, "actions": results})
        return SUCCESS
    print("\n执行结果:")
    bad = _print_actions(results)
    return SUCCESS if bad == 0 else ERROR


def cmd_status(args) -> int:
    st = adapters.status()
    rows = []
    for agent, items in st.items():
        if args.agent and agent != args.agent:
            continue
        counts = {"linked": 0, "broken": 0, "conflict": 0, "not_linked": 0}
        for i in items:
            counts[i["state"]] = counts.get(i["state"], 0) + 1
        rows.append({"agent": agent, **counts, "total": len(items)})
        if args.verbose:
            rows[-1]["items"] = [i for i in items if i["state"] != "not_linked"]
    if getattr(args, "json", False):
        jout({"agents": rows})
        return SUCCESS
    for r in rows:
        print(f"{r['agent']:10s} linked={r['linked']:4d} conflict={r['conflict']:4d} "
              f"broken={r['broken']:4d} total={r['total']}")
        if args.verbose:
            for i in r.get("items", []):
                print(f"    {i['state']:9s} {i['name']}")
    return SUCCESS


def cmd_backups(args) -> int:
    bks = adapters.list_backups()
    rows = []
    for b in bks:
        has_conflicts = (b / "conflicts").exists() and any((b / "conflicts").iterdir())
        rows.append({"ts": b.name, "has_conflicts": has_conflicts})
    if getattr(args, "json", False):
        jout({"count": len(rows), "backups": rows,
              "max_backups": adapters.MAX_BACKUPS})
        return SUCCESS
    if not rows:
        print("无备份。")
        return SUCCESS
    print("可用备份:")
    for r in rows:
        tag = "  (含冲突目录)" if r["has_conflicts"] else ""
        print(f"  {r['ts']}{tag}")
    return SUCCESS


def cmd_doctor(args) -> int:
    rep = adapters.doctor()
    if getattr(args, "json", False):
        jout(rep)
        return SUCCESS
    s = rep["summary"]
    print(f"体检: 中央库 {s['skills']} 个 skill")
    print(f"  断链 (中央库副本缺失)   {s['broken']}")
    print(f"  冲突 (目标被别的内容占) {s['conflict']}")
    print(f"  孤儿 store 目录          {s['orphan_store']}")
    print(f"  copy 产物漂移            {s['copy_drift']}")
    print(f"  同名多版本               {s['duplicate_names']}")
    print(f"  agent 目录缺失           {s['missing_dirs']}")
    for key, title in (("broken", "断链"), ("conflict", "冲突"),
                       ("orphan_store", "孤儿 store 目录"),
                       ("copy_drift", "copy 产物漂移")):
        items = rep[key]
        if not items:
            continue
        print(f"\n{title} ({len(items)}):")
        for it in items[:20]:
            if "agent" in it:
                print(f"  [{it['agent']}] {it.get('name') or it.get('sid')}")
            else:
                print(f"  {it.get('sid')}")
        if len(items) > 20:
            print(f"  ...(共 {len(items)} 项, 用 --json 看全部)")
    if rep["duplicate_names"]:
        print(f"\n同名多版本 ({len(rep['duplicate_names'])}):")
        for name, sids in list(rep["duplicate_names"].items())[:10]:
            print(f"  {name}: {', '.join(sids)}")
    if rep["missing_dirs"]:
        print(f"\nagent 目录缺失 ({len(rep['missing_dirs'])}):")
        for it in rep["missing_dirs"]:
            print(f"  [{it['agent']}] {it['dir']}")
    bad = s["broken"] + s["orphan_store"] + s["copy_drift"]
    print("\n✅ 无异常" if bad == 0 else f"\n⚠️  {bad} 项需要处理")
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
    source = getattr(args, "source", None) or "workbuddy"
    try:
        n, definitions, envs, masks = mcp.import_from_agent(source, apply=args.apply)
    except (FileNotFoundError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return ERROR
    print(f"从 {source} 导入 {n} 个 MCP server 定义到中央库:")
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


def cmd_mcp_status(args) -> int:
    from . import mcp
    rep = mcp.status()
    if getattr(args, "json", False):
        jout(rep)
        return SUCCESS
    s = rep["summary"]
    print(f"中央库 {s['total']} 个 MCP server 在各 agent 配置中的存在情况"
          f" (其中 {s['not_in_any_agent']} 个尚未写入任何 agent):")
    for row in rep["servers"]:
        marks = " ".join(f"{a}={'✓' if ok else '·'}" for a, ok in row["agents"].items())
        print(f"  {row['id']:24s} {row['transport']:5s} {marks}")
    return SUCCESS


def _record_from_skill_dir(skill_dir: Path) -> Optional[dict]:
    """从任意 skill 目录构造一条 scan 记录 (复用 scan.py 的解析)。"""
    from . import scan
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    meta = scan._read_frontmatter(md)
    txt = md.read_text(encoding="utf-8", errors="replace")
    return {
        "name": meta.get("name") or skill_dir.name,
        "category": "",
        "path": str(md),
        "md5": scan._md5(md),
        "size": md.stat().st_size,
        "fm_name": meta.get("name", ""),
        "fm_desc": (meta.get("description", "") or "")[:60],
        "risks": scan._risks(txt),
    }


def cmd_export(args) -> int:
    """把中央库的一个 skill 打包成可分发的 zip。"""
    import zipfile
    sid = _resolve_sid(args.skill)
    man = store.get_skill(sid)
    src = store.STORE_DIR / sid
    if not src.exists():
        print(f"中央库副本缺失: {src}", file=sys.stderr)
        return ERROR
    out = Path(args.out) if args.out else Path(f"{man['name']}--{man['md5'][:8]}.zip")
    files = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(src.rglob("*")):
            if f.is_dir() or f.name == ".DS_Store":
                continue
            z.write(f, str(Path(sid) / f.relative_to(src)))
            files.append(str(f.relative_to(src)))
        z.writestr("manifest.json", json.dumps(man, ensure_ascii=False, indent=2))
    print(f"已打包 {sid} ({len(files)} 个文件) → {out}")
    if man.get("risks"):
        print(f"  风险标记: {','.join(man['risks'])}")
    return SUCCESS


def cmd_add(args) -> int:
    """从 zip 或目录导入 skill 到中央库 (默认 dry-run)。"""
    import tempfile
    import zipfile
    from . import scan

    src = Path(args.path).expanduser()
    tmp = None
    if src.is_file() and src.suffix.lower() == ".zip":
        tmp = Path(tempfile.mkdtemp(prefix="skillhub-add-"))
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        cands = sorted({p.parent for p in tmp.rglob("SKILL.md")},
                       key=lambda p: len(p.parts))
        if not cands:
            print(f"zip 里没有找到 SKILL.md: {src}", file=sys.stderr)
            return ERROR
        src = cands[0]
    elif not src.is_dir():
        print(f"路径不存在或格式不支持 (需 zip 或目录): {args.path}", file=sys.stderr)
        return ERROR

    rec = _record_from_skill_dir(src)
    if rec is None:
        print(f"目录里没有 SKILL.md: {src}", file=sys.stderr)
        return ERROR
    # 若带 manifest.json (export 产物), 复用原库的名字与来源, 保证 sid 跨机器一致
    man = None
    for cand in ((tmp / "manifest.json") if tmp else None,
                 src / "manifest.json", src.parent / "manifest.json"):
        if cand and cand.is_file():
            try:
                man = json.loads(cand.read_text(encoding="utf-8"))
                break
            except Exception:
                man = None
    if man and man.get("name") and man.get("md5") == rec["md5"]:
        rec["name"] = man["name"]
    sid = store.import_skill(man["source"] if man and man.get("source") else "external",
                             rec, apply=False)
    existing = store.get_skill(sid)
    action = "已存在(将合并来源)" if existing else "新增"
    print(f"待导入: {sid}")
    print(f"  名称: {rec['name']}   大小: {rec['size']}B")
    print(f"  风险: {','.join(rec['risks']) or '无'}   状态: {action}")
    print(f"  描述: {rec['fm_desc'] or '—'}")
    if not args.apply:
        print("\n(未写入。加 --apply 执行导入。)")
        return SUCCESS
    store.import_skill("external", rec, apply=True)
    print(f"已导入 → {store.STORE_DIR / sid}")
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    return SUCCESS


def cmd_gui(args) -> int:
    from . import webgui
    webgui.serve(port=args.port, open_browser=not args.no_browser)
    return SUCCESS


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="skillhub", description=__doc__)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("scan", help="扫描所有 agent 的 skill")
    add_json_arg(sp)
    sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("import", help="导入扫描结果到中央库 (默认 dry-run)")
    sp.add_argument("--agent", help="只导入指定 agent (逗号分隔)")
    sp.add_argument("--apply", action="store_true", help="实际执行导入")
    sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("list", help="列出中央库 skill")
    sp.add_argument("--risky", action="store_true", help="只显示带风险标记的")
    sp.add_argument("--query", help="按关键词过滤 (名称/描述/id/分类/风险)")
    add_json_arg(sp)
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
    sp.add_argument("--skip-conflicts", action="store_true",
                    help="批量时跳过冲突项, 把其余的做完 (而不是整批停)")
    sp.add_argument("--dry-run", action="store_true", help="只显示计划")
    add_json_arg(sp)
    sp.set_defaults(fn=cmd_link)

    sp = sub.add_parser("unlink", help="解除投影 (默认 dry-run)")
    sp.add_argument("skill", help="skill_id 或名称前缀")
    sp.add_argument("--agents", required=True, help="目标 agent, 逗号分隔")
    sp.add_argument("--dry-run", action="store_true")
    add_json_arg(sp)
    sp.set_defaults(fn=cmd_unlink)

    sp = sub.add_parser("status", help="查看各 agent 投影状态")
    sp.add_argument("--agent", help="只看某个 agent")
    sp.add_argument("--verbose", action="store_true", help="显示明细")
    add_json_arg(sp)
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("doctor", help="体检: 断链/孤儿/漂移/冲突/同名多版本")
    add_json_arg(sp)
    sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("export", help="把中央库 skill 打包为 zip")
    sp.add_argument("skill", help="skill_id 或名称前缀")
    sp.add_argument("--out", help="输出 zip 路径 (默认 ./<name>--<md5前8>.zip)")
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("add", help="从 zip 或目录导入 skill 到中央库 (默认 dry-run)")
    sp.add_argument("path", help="zip 文件或 skill 目录")
    sp.add_argument("--apply", action="store_true", help="实际写入中央库")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("backups", help="列出备份")
    add_json_arg(sp)
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

    msp = msub.add_parser("import", help="导入 MCP server 定义 (默认 dry-run)")
    msp.add_argument("--from", dest="source", default="workbuddy",
                     help="来源 agent: workbuddy/codex/claude/opencode/grok (默认 workbuddy)")
    msp.add_argument("--apply", action="store_true", help="实际写入中央库")
    msp.set_defaults(fn=cmd_mcp_import)

    msp = msub.add_parser("list", help="列出中央库 MCP server 定义")
    add_json_arg(msp)
    msp.set_defaults(fn=cmd_mcp_list)

    msp = msub.add_parser("status", help="各 agent 配置里是否已存在中央库的 server")
    add_json_arg(msp)
    msp.set_defaults(fn=cmd_mcp_status)

    msp = msub.add_parser("generate", help="生成 MCP 配置到 agent (默认 dry-run)")
    msp.add_argument("server", help="server id (skillhub mcp list 查看)")
    msp.add_argument("--agents", required=True, help="目标 agent, 逗号分隔")
    msp.add_argument("--dry-run", action="store_true", help="只显示计划")
    msp.add_argument("--resolve", action="store_true",
                     help="从当前环境变量读真实值注入字面量 (用于不展开 env 的 agent, 如 pi)")
    msp.set_defaults(fn=cmd_mcp_generate)

    sp = sub.add_parser("gui", help="启动本地 Web GUI (只读: agent 概览 / 中央库 / MCP / 备份)")
    sp.add_argument("--port", type=int, default=8317, help="监听端口 (默认 8317, 仅绑定 127.0.0.1)")
    sp.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    sp.set_defaults(fn=cmd_gui)

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
