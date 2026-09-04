"""扫描各 agent 的 skill 目录, 输出结构化记录。"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Dict, List

from .config import AGENTS, RISK_PATTERNS


def _md5(p: Path) -> str:
    h = hashlib.md5()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_frontmatter(p: Path) -> Dict[str, str]:
    """读取 SKILL.md 的 YAML frontmatter (name/description)。"""
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return {}
    if not txt.startswith("---"):
        return {}
    end = txt.find("\n---", 3)
    if end < 0:
        return {}
    meta = {}
    for line in txt[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w]*)\s*:\s*(.*)$", line)
        if m:
            meta[m.group(1)] = m.group(2).strip().strip("'\"")
    return meta


def _risks(txt: str) -> List[str]:
    return [k for k, pat in RISK_PATTERNS.items() if re.search(pat, txt)]


def scan_agent(agent: str) -> List[dict]:
    """扫描单个 agent 的所有 SKILL.md, 返回记录列表。

    记录: {agent, name, category, path, md5, size, fm_name, fm_desc, risks}
    """
    cfg = AGENTS[agent]
    root = cfg["skill_dir"]
    if not root.exists():
        return []
    records = []
    for md in sorted(root.rglob("SKILL.md")):
        try:
            parts = md.relative_to(root).parts
            if len(parts) == 2:
                name, category = parts[0], ""
            else:
                # hermes 分类层级: <category>/<skill>/SKILL.md
                category, name = parts[0], parts[-2]
        except ValueError:
            name, category = md.parent.name, ""
        meta = _read_frontmatter(md)
        try:
            txt = md.read_text(encoding="utf-8", errors="replace")
        except Exception:
            txt = ""
        records.append({
            "agent": agent,
            "name": name,
            "category": category,
            "path": str(md),
            "md5": _md5(md),
            "size": md.stat().st_size,
            "fm_name": meta.get("name", ""),
            "fm_desc": (meta.get("description", "") or "")[:60],
            "risks": _risks(txt),
        })
    return records


def scan_all() -> Dict[str, List[dict]]:
    return {agent: scan_agent(agent) for agent in AGENTS}
