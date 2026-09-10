"""skillhub 命令行入口。

所有会改动中央库、agent 投影或配置文件的命令默认只预览；只有显式
``--apply`` 才执行。``--dry-run`` 保留为兼容别名，和 ``--apply`` 互斥。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from . import adapters, store
from .config import AGENTS
from .scan import scan_all


SUCCESS, ERROR = 0, 1
MAX_ZIP_FILES = 5000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024
MAX_ZIP_TOTAL_BYTES = 200 * 1024 * 1024
MAX_ZIP_PATH = 512


class CliError(ValueError):
    pass


def add_json_arg(parser) -> None:
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")


def add_modify_flags(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="实际执行写入")
    group.add_argument("--dry-run", action="store_true", help="只预览（兼容别名）")


def jout(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _print_actions(actions: dict) -> int:
    bad_types = {"conflict", "copy_drift", "store_drift", "error", "blocked", "broken"}
    bad = 0
    for agent, rows in actions.items():
        for action in rows:
            typ = action.get("type", "error")
            is_bad = typ in bad_types
            bad += int(is_bad)
            flag = "✗" if is_bad else "✓"
            print(f"  {flag} [{agent}] {typ:10s} {action.get('target', '')}  {action.get('detail', '')}")
    return bad


def _action_blocked(actions: dict) -> bool:
    return any(a.get("type") in {"error", "broken", "conflict", "copy_drift", "store_drift", "blocked"}
               for rows in actions.values() for a in rows)


def _can_apply(actions: dict, *, force: bool, allow_risky: bool) -> bool:
    for rows in actions.values():
        for action in rows:
            typ = action.get("type")
            if typ in {"error", "broken", "store_drift"}:
                return False
            if typ in {"conflict", "copy_drift"} and not force:
                return False
            if typ == "blocked" and not allow_risky:
                return False
    return True


def _agents(raw: str, *, supported=AGENTS) -> list[str]:
    values = [item.strip() for item in raw.split(",") if item.strip()]
    if not values:
        raise CliError("请用 --agents 指定目标 agent")
    unknown = [item for item in values if item not in supported]
    if unknown:
        raise CliError(f"未知 agent: {', '.join(unknown)}")
    return list(dict.fromkeys(values))


def cmd_scan(args) -> int:
    results = scan_all()
    rows, total = [], 0
    for agent in AGENTS:
        records = results[agent]
        total += len(records)
        rows.append({"agent": agent, "skills": len(records),
                     "risky": sum(1 for r in records if r.get("risks")),
                     "errors": sum(1 for r in records if r.get("error"))})
    if args.json:
        jout({"agents": rows, "total": total})
        return SUCCESS
    print(f"{'agent':10s} {'skills':>6s}  {'risky':>6s}  {'errors':>6s}")
    print("-" * 42)
    for row in rows:
        print(f"{row['agent']:10s} {row['skills']:>6d}  {row['risky']:>6d}  {row['errors']:>6d}")
    print("-" * 42)
    print(f"{'total':10s} {total:>6d}")
    return SUCCESS


def cmd_import(args) -> int:
    selected = _agents(args.agent, supported=AGENTS) if args.agent else list(AGENTS)
    scanned = scan_all()
    pending = []
    errors = []
    for agent in selected:
        for record in scanned.get(agent, []):
            if record.get("error"):
                errors.append(f"[{agent}] {record['path']}: {record['error']}")
                continue
            sid = store.import_skill(agent, record, apply=False)
            existing = store.get_skill(sid)
            if existing is None or agent not in existing.get("agents", []):
                pending.append((agent, record, sid))
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return ERROR
    if args.json:
        payload = {"mode": "apply" if args.apply else "plan",
                   "count": len(pending),
                   "skills": [{"agent": a, "sid": sid} for a, _, sid in pending]}
        if args.apply:
            for agent, record, _ in pending:
                store.import_skill(agent, record, apply=True)
        jout(payload)
        return SUCCESS
    print(f"待导入 {len(pending)} 个 skill (中央库已有 {len(store.load_index())} 个):")
    for agent, record, sid in sorted(pending, key=lambda item: item[1].get("name", "")):
        print(f"  {sid:40s} from={agent:10s} risks={','.join(record.get('risks', [])) or '-'}")
    if not args.apply:
        print("\n(预览模式，未写入。加 --apply 执行导入。)")
        return SUCCESS
    for agent, record, _ in pending:
        store.import_skill(agent, record, apply=True)
    print(f"\n已导入 {len(pending)} 个 → {store.STORE_DIR}")
    return SUCCESS


def _filter_skills(skills: list, query: str) -> list:
    q = (query or "").strip().lower()
    if not q:
        return skills
    return [skill for skill in skills if q in " ".join([
        skill.get("name", ""), skill.get("fm_desc", ""), skill.get("id", ""),
        skill.get("category", ""), ",".join(skill.get("risks", []))]).lower()]


def cmd_list(args) -> int:
    skills = _filter_skills(store.list_skills(args.risky), args.query)
    if args.json:
        jout({"count": len(skills), "skills": skills})
        return SUCCESS
    print(f"中央库共 {len(skills)} 个 skill" + (" (仅含风险标记)" if args.risky else "") + ":")
    for skill in skills:
        print(f"  {skill['id']:42s} agents={','.join(skill.get('agents', []))} "
              f"risks={','.join(skill.get('risks', [])) or '-'}")
    return SUCCESS


def _resolve_sid(value: str) -> str:
    try:
        store.safe_component(value)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    index = store.load_index()
    if value in index:
        return value
    matches = [sid for sid in index if sid.startswith(value)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CliError(f"名称 {value!r} 匹配多个 skill: {matches}")
    raise CliError(f"中央库中找不到 skill: {value}")


def _collect_sids(args, agents: list[str]) -> list[str]:
    if args.all and args.all_missing:
        raise CliError("--all 与 --all-missing 只能选一个")
    if (args.all or args.all_missing) and args.skill:
        raise CliError("--all/--all-missing 不能同时指定 skill")
    if args.all:
        return sorted(store.load_index())
    if args.all_missing:
        states = adapters.status()
        return sorted({item["sid"] for agent in agents for item in states.get(agent, [])
                       if item["state"] != "linked"})
    if not args.skill:
        raise CliError("请指定 skill，或用 --all / --all-missing 批量投影")
    return [_resolve_sid(args.skill)]


def _summarize(results: dict):
    stats = {}
    for rows in results.values():
        for action in rows:
            typ = action.get("type", "error")
            stats[typ] = stats.get(typ, 0) + 1
    bad = sum(stats.get(name, 0) for name in
              ("conflict", "copy_drift", "store_drift", "error", "blocked", "broken"))
    return stats, bad


def _fmt_stat(stats: dict) -> str:
    return ", ".join(f"{key} {value}" for key, value in sorted(stats.items())) or "无"


def cmd_link(args) -> int:
    agents = _agents(args.agents)
    sids = _collect_sids(args, agents)
    batch = args.all or args.all_missing
    plans = {}
    conflict_sids = set()
    for sid in sids:
        plan = adapters.plan_link(sid, agents, force=args.force,
                                  allow_risky=args.allow_risky)
        for agent, rows in plan.items():
            plans.setdefault(agent, []).extend(rows)
        if any(row.get("type") in {"conflict", "copy_drift"}
               for rows in plan.values() for row in rows):
            conflict_sids.add(sid)
    if batch and args.skip_conflicts and not args.force:
        sids = [sid for sid in sids if sid not in conflict_sids]
        plans = {}
        for sid in sids:
            plan = adapters.plan_link(sid, agents, force=args.force,
                                      allow_risky=args.allow_risky)
            for agent, rows in plan.items():
                plans.setdefault(agent, []).extend(rows)
    stats, bad = _summarize(plans)
    if not args.apply:
        if args.json:
            jout({"mode": "plan", "sids": sids, "agents": agents,
                  "stat": stats, "actions": plans})
        else:
            print(f"投影计划: {len(sids)} 个 skill → agents={agents}")
            _print_actions(plans)
            print("\n(预览模式，未写入。加 --apply 执行。)")
        return SUCCESS
    if not _can_apply(plans, force=args.force, allow_risky=args.allow_risky):
        if args.json:
            jout({"mode": "apply", "sids": sids, "agents": agents,
                  "stat": stats, "actions": plans, "applied": False})
        else:
            print("存在未获授权或不可恢复的项目，未执行：")
            _print_actions(plans)
            print("冲突需 --force，风险需 --allow-risky；broken 需先修复中央库。")
        return ERROR
    if batch:
        results = adapters.apply_link_batch(sids, agents, force=args.force,
                                            allow_risky=args.allow_risky)
    else:
        results = adapters.apply_link(sids[0], agents, force=args.force,
                                      allow_risky=args.allow_risky)
    stats, bad = _summarize(results)
    if args.json:
        jout({"mode": "apply", "sids": sids, "agents": agents,
              "stat": stats, "actions": results})
    else:
        print("执行结果:")
        _print_actions(results)
    return SUCCESS if bad == 0 else ERROR


def cmd_unlink(args) -> int:
    sid = _resolve_sid(args.skill)
    agents = _agents(args.agents)
    plan = adapters.plan_unlink(sid, agents)
    if not args.apply:
        if args.json:
            jout({"mode": "plan", "sid": sid, "agents": agents, "actions": plan})
        else:
            print(f"解除投影计划: {sid}")
            _print_actions(plan)
            print("\n(预览模式，未写入。加 --apply 执行。)")
        return SUCCESS
    if not _can_apply(plan, force=args.force, allow_risky=True):
        if args.json:
            jout({"mode": "apply", "sid": sid, "agents": agents,
                  "actions": plan, "applied": False})
        else:
            _print_actions(plan)
            print("copy 漂移或冲突内容需 --force，当前未删除。")
        return ERROR
    result = adapters.apply_unlink(sid, agents, force=args.force)
    if args.json:
        jout({"mode": "apply", "sid": sid, "agents": agents, "actions": result})
    else:
        print("执行结果:")
        _print_actions(result)
    return SUCCESS if _summarize(result)[1] == 0 else ERROR


def cmd_status(args) -> int:
    if args.agent and args.agent not in AGENTS:
        raise CliError(f"未知 agent: {args.agent}")
    status = adapters.status()
    rows = []
    for agent, items in status.items():
        if args.agent and agent != args.agent:
            continue
        counts = {}
        for item in items:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        row = {"agent": agent, "total": len(items), **counts}
        if args.verbose:
            row["items"] = [item for item in items if item["state"] != "not_linked"]
        rows.append(row)
    if args.json:
        jout({"agents": rows})
        return SUCCESS
    for row in rows:
        print(f"{row['agent']:10s} linked={row.get('linked', 0):4d} "
              f"conflict={row.get('conflict', 0):4d} broken={row.get('broken', 0):4d} "
              f"store_drift={row.get('store_drift', 0):4d} "
              f"copy_drift={row.get('copy_drift', 0):4d} total={row['total']}")
        if args.verbose:
            for item in row.get("items", []):
                print(f"    {item['state']:11s} {item['name']}")
    return SUCCESS


def cmd_backups(args) -> int:
    rows = [adapters.backup_info(backup) for backup in adapters.list_backups()]
    if args.json:
        jout({"count": len(rows), "backups": rows, "max_backups": adapters.MAX_BACKUPS})
        return SUCCESS
    if not rows:
        print("无备份。")
    else:
        print("可用备份:")
        for row in rows:
            size = "未知" if row["size"] is None else f"{row['size'] / 1024 / 1024:.1f}MB(逻辑大小)"
            print(f"  {row['ts']}  {size}" + ("  (含冲突目录)" if row["has_conflicts"] else ""))
    return SUCCESS


def cmd_doctor(args) -> int:
    report = adapters.doctor()
    summary = report["summary"]
    bad = sum(summary[key] for key in ("broken", "store_drift", "orphan_store", "copy_drift"))
    if args.json:
        jout(report)
        return SUCCESS if bad == 0 else ERROR
    print(f"体检: 中央库 {summary['skills']} 个 skill")
    for key, label in (("broken", "断链"), ("store_drift", "中央库漂移"), ("conflict", "冲突"),
                       ("orphan_store", "孤儿 store 目录"), ("copy_drift", "copy 产物漂移"),
                       ("duplicate_names", "同名多版本"), ("missing_dirs", "agent 目录缺失")):
        print(f"  {label:20s} {summary[key]}")
    print("\n✅ 无异常" if bad == 0 else f"\n⚠️  {bad} 项需要处理")
    return SUCCESS if bad == 0 else ERROR


def cmd_cleanup(args) -> int:
    plan = adapters.plan_cleanup(args.keep)
    if not plan:
        print("无备份可清理。")
        return SUCCESS
    reclaimable = sum(action["size"] for action in plan if action["type"] != "keep")
    if args.json and not args.apply:
        jout({"mode": "plan", "keep": args.keep, "actions": plan,
              "logical_bytes_affected": reclaimable,
              "reclaimable_bytes": None})
        return SUCCESS
    if not args.json:
        print(f"清理计划 (保留最近 {args.keep} 份):")
        for action in plan:
            print(f"  {'✓' if action['type'] == 'keep' else '→'} {action['ts']} "
                  f"{action['type']:12s} {action['size'] / 1024 / 1024:8.1f}MB  {action['detail']}")
        print(f"\n涉及逻辑大小 {reclaimable / 1024 / 1024:.1f}MB；实际释放量取决于文件系统。")
    if not args.apply:
        if not args.json:
            print("(预览模式，未删除。加 --apply 执行。)")
        return SUCCESS
    done = adapters.apply_cleanup(args.keep)
    if args.json:
        jout({"mode": "apply", "keep": args.keep, "actions": done})
    else:
        print(f"已清理 {sum(1 for action in done if action['type'] != 'keep')} 项。")
    return SUCCESS


def cmd_rollback(args) -> int:
    try:
        backup = next(backup for backup in adapters.list_backups() if backup.name == args.backup)
    except StopIteration as exc:
        raise CliError(f"找不到备份 {args.backup!r}") from exc
    info = adapters.backup_info(backup)
    if info.get("kind") == "skill_publish":
        raise CliError("这是 skill 级发布备份，不允许走全库 rollback；请使用 trash restore 恢复旧版本")
    adapters._validate_snapshot(backup)
    if not args.apply:
        if args.json:
            jout({"mode": "plan", "backup": args.backup, "apply": False})
        else:
            print(f"回滚计划: {args.backup}（预览，未写入；加 --apply 执行）")
        return SUCCESS
    adapters.rollback(args.backup)
    if args.json:
        jout({"mode": "apply", "backup": args.backup, "restored": True})
    else:
        print(f"已回滚中央库与备份记录到 {args.backup}")
    return SUCCESS


def cmd_ul(args) -> int:
    if args.ul_cmd == "create":
        sid = _resolve_sid(args.skill)
        result = store.create_ul(sid, apply=args.apply)
    elif args.ul_cmd == "edit":
        sid = _resolve_sid(args.skill)
        if args.text is not None and args.from_file:
            raise CliError("--text 与 --from-file 只能选一个")
        if args.text is None and not args.from_file:
            raise CliError("edit 需要 --text 或 --from-file")
        text = args.text if args.text is not None else Path(args.from_file).read_text(encoding="utf-8")
        result = store.edit_ul(sid, args.file, text, apply=args.apply)
    elif args.ul_cmd == "trial":
        sid = _resolve_sid(args.skill)
        agents = _agents(args.agents)
        result = adapters.plan_trial(sid, agents)
        if args.apply:
            result = {"mode": "apply", "actions": adapters.apply_trial(
                sid, agents, enabled=not args.off, force=args.force)}
    elif args.ul_cmd == "publish":
        sid = _resolve_sid(args.skill)
        result = adapters.publish_ul(sid, apply=args.apply)
    else:  # pragma: no cover - argparse 已限制
        raise CliError("未知 ul 操作")
    if args.json:
        jout(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.apply and args.ul_cmd != "trial":
            print("(预览模式，未写入。加 --apply 执行。)")
    return SUCCESS


def cmd_trash(args) -> int:
    if args.trash_cmd == "list":
        result = {"items": store.list_trash(), "plan": store.plan_trash_cleanup()}
    elif args.trash_cmd == "restore":
        store.safe_component(args.entry)
        result = {"mode": "plan", "entry": args.entry}
        if args.apply:
            result = store.restore_trash(args.entry)
    elif args.trash_cmd == "retain":
        result = {"mode": "plan", "entry": args.entry, "retained": args.retained}
        if args.apply:
            result = store.set_trash_retained(args.entry, args.retained)
    elif args.trash_cmd == "cleanup":
        result = {"mode": "plan", "actions": store.plan_trash_cleanup()}
        if args.apply:
            result = {"mode": "apply", "actions": store.cleanup_trash(authorized=True)}
    else:  # pragma: no cover
        raise CliError("未知 trash 操作")
    if args.json:
        jout(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not args.apply and args.trash_cmd != "list":
            print("(预览模式，未写入。加 --apply 执行。)")
    return SUCCESS


def cmd_group(args) -> int:
    if args.group_cmd == "list":
        result = store.load_groups()
    elif args.group_cmd == "set":
        members = [_resolve_sid(value) for value in args.members.split(",") if value.strip()]
        result = store.update_group(args.group, members, name=args.name or args.group,
                                    kind=args.kind, apply=args.apply)
    elif args.group_cmd == "distribute":
        agents = _agents(args.agents)
        group_ids = args.groups.split(",") if args.groups else None
        sids = [_resolve_sid(value) for value in args.skills.split(",") if value.strip()] if args.skills else None
        replace = args.replace if args.replace else None
        result = adapters.plan_distribution(agents, group_ids=group_ids,
                                            sids=sids, replace=replace, force=args.force,
                                            allow_risky=args.allow_risky)
        if args.apply:
            result = adapters.apply_distribution(agents, group_ids=group_ids,
                                                 sids=sids, replace=replace, force=args.force,
                                                 allow_risky=args.allow_risky)
    else:  # pragma: no cover
        raise CliError("未知 group 操作")
    if args.json:
        jout(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return SUCCESS


def cmd_diagnose(args) -> int:
    sid = _resolve_sid(args.skill)
    if args.confirm:
        keys = [item.strip() for item in args.confirm.split(",") if item.strip()]
        result = store.confirm_diagnostics(sid, keys, note=args.note or "")
    else:
        result = store.diagnose_skill(sid)
    if args.json:
        jout(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return SUCCESS if not result.get("blocking") else ERROR


def cmd_models(args) -> int:
    from .config import discover_models
    result = discover_models()
    if args.json:
        jout(result)
    else:
        for row in result["agents"]:
            print(f"{row['agent']:10s} {'可发现' if row['available'] else '不可用':4s} "
                  f"models={','.join(row['models']) or '-'}  "
                  f"{'可调用' if row.get('callable') else '仅配置'}  {row['reason']}")
        print(result["limitations"])
    return SUCCESS


def cmd_mcp_import(args) -> int:
    from . import mcp
    source = args.source or "workbuddy"
    result = mcp.import_from_agent(source, apply=args.apply)
    imported, definitions, envs, masks = result
    if args.json:
        jout({"mode": "apply" if args.apply else "plan", "count": imported,
              "servers": [{"id": item["id"], "transport": item["transport"]}
                          for item in definitions], "envs": envs, "masks": masks})
        return SUCCESS
    print(f"从 {source} 导入 {imported} 个 MCP server 定义:")
    for server in definitions:
        print(f"  {server['id']:24s} {server['transport']:8s} "
              f"url={server.get('url', '-')} headers={','.join(server.get('headers', {})) or '-'}")
    if envs:
        print("检测到凭证，已改为环境变量引用:")
        for name, masked in zip(envs, masks):
            print(f"  {name} = {masked}")
    print("\n已写入中央库。" if args.apply else "\n(预览模式，未写入。加 --apply 执行导入。)")
    return SUCCESS


def _resolve_values(definition: dict) -> dict:
    from . import mcp
    variables = mcp.required_env_vars(definition)
    missing = [name for name in variables if not os.environ.get(name)]
    if missing:
        raise CliError(f"--resolve 缺少环境变量: {', '.join(missing)}")
    return {name: os.environ[name] for name in variables}


def cmd_mcp_list(args) -> int:
    from . import mcp
    servers = mcp.list_servers()
    if args.json:
        jout({"count": len(servers), "servers": mcp.redact_for_output(servers)})
        return SUCCESS
    print(f"中央库 MCP server 定义共 {len(servers)} 个:")
    for server in servers:
        print(f"  {server['id']:24s} {server['transport']:14s} "
              f"agents={','.join(server.get('agents', []))}")
    return SUCCESS


def cmd_mcp_generate(args) -> int:
    from . import mcp
    sid = args.server
    definition = mcp.get_server(sid)
    if definition is None:
        raise CliError(f"中央库中不存在 MCP server: {sid}")
    agents = _agents(args.agents, supported=mcp.MCP_TARGETS)
    resolve = _resolve_values(definition) if args.resolve else None
    plan = mcp.plan_generate(sid, agents)
    if not args.apply:
        if args.json:
            jout({"mode": "plan", "server": sid, "agents": agents, "actions": plan})
        else:
            print(f"MCP 生成计划: {sid}")
            _print_actions(plan)
            print("\n(预览模式，未写入。加 --apply 执行。)")
        return SUCCESS
    result = mcp.apply_generate(sid, agents, resolve=resolve)
    if args.json:
        jout({"mode": "apply", "server": sid, "agents": agents, "actions": result})
    else:
        print("执行结果:")
        _print_actions(result)
    return SUCCESS if _summarize(result)[1] == 0 else ERROR


def _record_from_skill_dir(skill_dir: Path) -> Optional[dict]:
    from . import scan
    md = skill_dir / "SKILL.md"
    if not md.is_file():
        return None
    meta = scan._read_frontmatter(md)
    summary = store.file_summary(skill_dir)
    return {"name": meta.get("name") or skill_dir.name, "category": "",
            "path": str(md), "md5": summary["md5"], "size": summary["size"],
            "file_count": summary["file_count"], "files": summary["files"],
            "fm_name": meta.get("name", ""),
            "fm_desc": (meta.get("description", "") or "")[:60],
            "risks": scan.risks_for_skill(skill_dir)}


def _zip_member_safe(info: zipfile.ZipInfo) -> None:
    name = info.filename
    if not name or len(name) > MAX_ZIP_PATH:
        raise CliError("zip 成员路径过长或为空")
    pure = PurePosixPath(name)
    if pure.is_absolute() or "\\" in name or any(part in {"", ".", ".."} for part in pure.parts):
        raise CliError(f"zip 成员路径不安全: {name}")
    if info.file_size > MAX_ZIP_MEMBER_BYTES:
        raise CliError(f"zip 成员过大: {name}")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise CliError(f"zip 不允许软链接成员: {name}")


def _extract_zip(path: Path, destination: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_ZIP_FILES:
            raise CliError("zip 成员数量超过限制")
        total = 0
        names = set()
        for info in infos:
            _zip_member_safe(info)
            if info.filename in names:
                raise CliError(f"zip 含重复成员: {info.filename}")
            names.add(info.filename)
            total += info.file_size
        if total > MAX_ZIP_TOTAL_BYTES:
            raise CliError("zip 解压总大小超过限制")
        for info in infos:
            target = destination / PurePosixPath(info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, open(target, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            # store.file_summary intentionally includes executable bits in its
            # digest.  ZIP extraction never restores special permissions, but
            # it does preserve ordinary user/group/other execute bits so an
            # export -> add round trip remains content-identical.
            execute_bits = (info.external_attr >> 16) & 0o111
            if execute_bits:
                target.chmod((target.stat().st_mode & 0o666) | execute_bits)


def _find_skill_root(root: Path) -> Path:
    if (root / "SKILL.md").is_file():
        return root
    candidates = sorted({path.parent for path in root.rglob("SKILL.md")})
    if not candidates:
        raise CliError("包里没有找到 SKILL.md")
    top = [candidate for candidate in candidates
           if not any(candidate != other and other in candidate.parents for other in candidates)]
    if len(top) != 1:
        raise CliError("输入包含多个 skill，请一次指定一个 skill 包")
    return top[0]


def _read_manifest(candidates: list[Path]) -> Optional[dict]:
    for path in candidates:
        if not path or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CliError(f"manifest.json 无法解析: {path}") from exc
        if not isinstance(value, dict):
            raise CliError("manifest.json 必须是对象")
        return value
    return None


def cmd_export(args) -> int:
    sid = _resolve_sid(args.skill)
    manifest = store.get_skill(sid)
    source = store.safe_path(store.STORE_DIR, sid)
    if not source.is_dir() or source.is_symlink():
        raise CliError(f"中央库副本缺失: {source}")
    files = list(store.skill_files(source))
    output = Path(args.out).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_name(f".{output.name}.skillhub-tmp-{os.getpid()}")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in files:
                archive.write(item, str(Path(sid) / item.relative_to(source)))
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        os.replace(temp, output)
    finally:
        temp.unlink(missing_ok=True)
    print(f"已打包 {sid} ({len(files)} 个文件) → {output}")
    return SUCCESS


def cmd_add(args) -> int:
    source = Path(args.path).expanduser()
    if not source.exists():
        raise CliError(f"路径不存在: {args.path}")
    temporary = None
    try:
        if source.is_file() and source.suffix.lower() == ".zip":
            temporary = tempfile.TemporaryDirectory(prefix="skillhub-add-")
            extracted = Path(temporary.name)
            _extract_zip(source, extracted)
            root = _find_skill_root(extracted)
            manifest = _read_manifest([extracted / "manifest.json",
                                       root / "manifest.json", root.parent / "manifest.json"])
        elif source.is_dir():
            root = _find_skill_root(source)
            manifest = _read_manifest([root / "manifest.json", root.parent / "manifest.json"])
        else:
            raise CliError("路径不存在或格式不支持（需 zip 或目录）")
        record = _record_from_skill_dir(root)
        if record is None:
            raise CliError("目录里没有 SKILL.md")
        if manifest is not None:
            if manifest.get("name") != record["name"] or manifest.get("md5") != record["md5"]:
                raise CliError("manifest 与完整 skill 文件集不一致")
            if manifest.get("id") and manifest["id"] != store.skill_id_for(record["name"], record["md5"]):
                raise CliError("manifest id 与完整 skill 文件集不一致")
        sid = store.import_skill("external", record, apply=False)
        existing = store.get_skill(sid)
        payload = {"mode": "apply" if args.apply else "plan", "sid": sid,
                   "name": record["name"], "size": record["size"],
                   "action": "existing" if existing else "new"}
        if args.json:
            if args.apply:
                store.import_skill("external", record, apply=True)
            jout(payload)
        else:
            print(f"待导入: {sid}\n  名称: {record['name']}  大小: {record['size']}B")
            if not args.apply:
                print("(预览模式，未写入。加 --apply 执行导入。)")
            else:
                store.import_skill("external", record, apply=True)
                print(f"已导入 → {store.STORE_DIR / sid}")
        return SUCCESS
    finally:
        if temporary is not None:
            temporary.cleanup()


def cmd_gui(args) -> int:
    from . import webgui
    webgui.serve(port=args.port, open_browser=not args.no_browser,
                 read_only=args.read_only)
    return SUCCESS


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="skillhub", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")

    sp = sub.add_parser("scan", help="扫描所有 agent 的 skill")
    add_json_arg(sp); sp.set_defaults(fn=cmd_scan)

    sp = sub.add_parser("import", help="导入 skill 到中央库（默认预览）")
    sp.add_argument("--agent", help="只导入指定 agent（逗号分隔）")
    add_modify_flags(sp); add_json_arg(sp); sp.set_defaults(fn=cmd_import)

    sp = sub.add_parser("list", help="列出中央库 skill")
    sp.add_argument("--risky", action="store_true"); sp.add_argument("--query")
    add_json_arg(sp); sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("link", help="投影 skill 到 agent（默认预览）")
    sp.add_argument("skill", nargs="?"); sp.add_argument("--agents", required=True)
    sp.add_argument("--all", action="store_true"); sp.add_argument("--all-missing", action="store_true")
    sp.add_argument("--force", action="store_true", help="冲突时备份后替换")
    sp.add_argument("--allow-risky", action="store_true", help="显式放行风险 skill")
    sp.add_argument("--skip-conflicts", action="store_true")
    add_modify_flags(sp); add_json_arg(sp); sp.set_defaults(fn=cmd_link)

    sp = sub.add_parser("unlink", help="解除投影（默认预览）")
    sp.add_argument("skill"); sp.add_argument("--agents", required=True); sp.add_argument("--force", action="store_true")
    add_modify_flags(sp); add_json_arg(sp); sp.set_defaults(fn=cmd_unlink)

    sp = sub.add_parser("status", help="查看投影状态")
    sp.add_argument("--agent"); sp.add_argument("--verbose", action="store_true")
    add_json_arg(sp); sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("doctor", help="检查断链、漂移、冲突和索引")
    add_json_arg(sp); sp.set_defaults(fn=cmd_doctor)

    sp = sub.add_parser("export", help="把单个 skill 输出为 zip（必须显式 --out）")
    sp.add_argument("skill"); sp.add_argument("--out", required=True)
    sp.set_defaults(fn=cmd_export)

    sp = sub.add_parser("add", help="从 zip 或目录导入 skill（默认预览）")
    sp.add_argument("path"); add_modify_flags(sp); add_json_arg(sp); sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("backups", help="列出备份")
    add_json_arg(sp); sp.set_defaults(fn=cmd_backups)

    sp = sub.add_parser("cleanup", help="清理旧备份（默认预览）")
    sp.add_argument("--keep", type=int, default=3); add_modify_flags(sp); add_json_arg(sp); sp.set_defaults(fn=cmd_cleanup)

    sp = sub.add_parser("rollback", help="回滚中央库和已记录目标（默认预览）")
    sp.add_argument("backup"); add_modify_flags(sp); add_json_arg(sp); sp.set_defaults(fn=cmd_rollback)

    sp = sub.add_parser("ul", help="正式/试用版本管理（默认预览）")
    usub = sp.add_subparsers(dest="ul_cmd", required=True)
    usp = usub.add_parser("create", help="创建独立 ul 副本")
    usp.add_argument("skill"); add_modify_flags(usp); add_json_arg(usp); usp.set_defaults(fn=cmd_ul)
    usp = usub.add_parser("edit", help="编辑 ul 文本文件")
    usp.add_argument("skill"); usp.add_argument("--file", required=True)
    edit_group = usp.add_mutually_exclusive_group(); edit_group.add_argument("--text"); edit_group.add_argument("--from-file")
    add_modify_flags(usp); add_json_arg(usp); usp.set_defaults(fn=cmd_ul)
    usp = usub.add_parser("trial", help="指定 agent 使用 ul")
    usp.add_argument("skill"); usp.add_argument("--agents", required=True); usp.add_argument("--off", action="store_true")
    usp.add_argument("--force", action="store_true"); add_modify_flags(usp); add_json_arg(usp); usp.set_defaults(fn=cmd_ul)
    usp = usub.add_parser("publish", help="发布 ul 为正式版")
    usp.add_argument("skill"); add_modify_flags(usp); add_json_arg(usp); usp.set_defaults(fn=cmd_ul)

    sp = sub.add_parser("trash", help="单 skill 回收区（默认预览）")
    tsub = sp.add_subparsers(dest="trash_cmd", required=True)
    tsp = tsub.add_parser("list"); add_json_arg(tsp); tsp.set_defaults(fn=cmd_trash)
    tsp = tsub.add_parser("restore"); tsp.add_argument("entry"); add_modify_flags(tsp); add_json_arg(tsp); tsp.set_defaults(fn=cmd_trash)
    tsp = tsub.add_parser("retain"); tsp.add_argument("entry"); tsp.add_argument("--release", dest="retained", action="store_false"); tsp.set_defaults(retained=True); add_modify_flags(tsp); add_json_arg(tsp); tsp.set_defaults(fn=cmd_trash)
    tsp = tsub.add_parser("cleanup"); add_modify_flags(tsp); add_json_arg(tsp); tsp.set_defaults(fn=cmd_trash)

    sp = sub.add_parser("group", help="分组与分发（默认预览）")
    gsub = sp.add_subparsers(dest="group_cmd", required=True)
    gsp = gsub.add_parser("list"); add_json_arg(gsp); gsp.set_defaults(fn=cmd_group)
    gsp = gsub.add_parser("set"); gsp.add_argument("group"); gsp.add_argument("--members", required=True); gsp.add_argument("--name"); gsp.add_argument("--kind", choices=["manual", "model"], default="manual"); add_modify_flags(gsp); add_json_arg(gsp); gsp.set_defaults(fn=cmd_group)
    gsp = gsub.add_parser("distribute"); gsp.add_argument("--agents", required=True); gsp.add_argument("--groups", default=""); gsp.add_argument("--skills", default=""); gsp.add_argument("--replace", action="store_true"); gsp.add_argument("--force", action="store_true"); gsp.add_argument("--allow-risky", action="store_true"); add_modify_flags(gsp); add_json_arg(gsp); gsp.set_defaults(fn=cmd_group)

    sp = sub.add_parser("diagnose", help="静态依赖/缺项诊断，不执行 skill")
    sp.add_argument("skill"); sp.add_argument("--confirm", help="逗号分隔的待人工确认项"); sp.add_argument("--note"); add_json_arg(sp); sp.set_defaults(fn=cmd_diagnose)

    sp = sub.add_parser("models", help="发现本机实际配置的模型名（只读）")
    add_json_arg(sp); sp.set_defaults(fn=cmd_models)

    sp = sub.add_parser("mcp", help="MCP 配置层")
    msub = sp.add_subparsers(dest="mcp_cmd", required=True)
    msp = msub.add_parser("import", help="导入 MCP 定义（默认预览）")
    msp.add_argument("--from", dest="source", default="workbuddy"); add_modify_flags(msp); add_json_arg(msp); msp.set_defaults(fn=cmd_mcp_import)
    msp = msub.add_parser("list", help="列出中央库 MCP 定义")
    add_json_arg(msp); msp.set_defaults(fn=cmd_mcp_list)
    msp = msub.add_parser("status", help="比较实际 MCP 配置内容")
    add_json_arg(msp); msp.set_defaults(fn=lambda args: _cmd_mcp_status(args))
    msp = msub.add_parser("generate", help="生成 MCP 配置（默认预览）")
    msp.add_argument("server"); msp.add_argument("--agents", required=True); msp.add_argument("--resolve", action="store_true")
    add_modify_flags(msp); add_json_arg(msp); msp.set_defaults(fn=cmd_mcp_generate)

    sp = sub.add_parser("gui", help="启动本地 Web GUI")
    sp.add_argument("--port", type=int, default=8317); sp.add_argument("--no-browser", action="store_true")
    sp.add_argument("--read-only", action="store_true", help="只读：禁止全部写接口")
    sp.set_defaults(fn=cmd_gui)

    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help(); return SUCCESS
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\n中断。", file=sys.stderr); return 130
    except (CliError, store.CorruptIndexError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return ERROR


def _cmd_mcp_status(args) -> int:
    from . import mcp
    report = mcp.status()
    if args.json:
        jout(report); return SUCCESS
    summary = report["summary"]
    print(f"中央库 {summary['total']} 个 MCP server，未对齐 {summary['not_in_any_agent']} 个:")
    for row in report["servers"]:
        print(f"  {row['id']:24s} {row['transport']:14s} " +
              " ".join(f"{agent}={'✓' if data['match'] else '✗'}"
                       for agent, data in row["agents"].items()))
    return SUCCESS


if __name__ == "__main__":
    sys.exit(main())
