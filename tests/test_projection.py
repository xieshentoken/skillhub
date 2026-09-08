"""skillhub 投影功能集成测试 — 用临时目录模拟 agent 与中央库, 不触碰真实环境。

用法: python3 tests/test_projection.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 项目根目录: 从本测试文件动态推导, 不写死绝对路径
WS = Path(__file__).resolve().parents[1]


# 与 skillhub/scan.py 的扫描规则保持一致: 跟随软链目录、跳过隐藏目录与 node_modules,
# 并统计嵌套 skill (如 ima-skill/knowledge-base 这类自带子 SKILL.md 的目录)
SKIP_NAMES = {"node_modules", "__pycache__", ".git", ".venv", "venv"}


def count_skills(root: Path) -> int:
    n = 0
    for p in sorted(root.iterdir()):
        if p.name.startswith(".") or p.name in SKIP_NAMES:
            continue
        if p.is_dir():
            if (p / "SKILL.md").exists():
                n += 1
            n += count_skills(p)
    return n


def main():
    tmp = tempfile.mkdtemp(prefix="skillhub-test-")
    # 复制真实 pi 的 skill 到假目录, 作为唯一的 skill 来源 (导入+投影都指向这里)。
    # 若本机没有 (~/.agents/skills, 如 CI), 造两个最小 skill 兜底, 保证测试可独立运行。
    real_pi = Path.home() / ".agents" / "skills"
    fake_pi = Path(tmp) / "fake-pi-skills"
    if real_pi.is_dir():
        shutil.copytree(real_pi, fake_pi)
    else:
        for name, desc in (("dws", "Demo skill for projection tests"),
                           ("demo-second", "Another demo skill")):
            d = fake_pi / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: {desc}\n---\n# {name}\n",
                encoding="utf-8")
    env = dict(os.environ)
    env["SKILLHUB_HOME"] = str(Path(tmp) / "hub")
    env["SKILLHUB_AGENT_DIR_pi"] = str(fake_pi)
    env["SKILLHUB_AGENT_DIR_workbuddy"] = str(Path(tmp) / "fake-wb")
    env["SKILLHUB_AGENT_DIR_claude"] = str(Path(tmp) / "fake-cl")

    def run(*args):
        r = subprocess.run([sys.executable, "-m", "skillhub", *args], cwd=WS,
                           capture_output=True, text=True, env=env)
        return r

    # 1) 导入真实 pi 的 skill 到临时中央库 (数量动态计算, 不硬编码)
    r = run("import", "--agent", "pi", "--apply")
    assert r.returncode == 0, r.stderr
    expected = count_skills(fake_pi)
    assert f"已导入 {expected} 个" in r.stdout, r.stdout
    print(f"[OK] import --apply ({expected} skills)")

    # 2) 假目录里的 dws 本身就是"原有同名真实目录"(从真实 pi 复制来的), 加标记文件
    target = fake_pi / "dws"  # 投影目标按 manifest.name 命名
    (target / "keep.txt").write_text("original")

    # 3) dry-run: 应报告冲突
    r = run("link", "dws", "--agents", "pi", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "conflict" in r.stdout, r.stdout
    print("[OK] link dry-run detects conflict")

    # 4) 无 --force 执行: 应拒绝且不删除原目录
    r = run("link", "dws", "--agents", "pi")
    assert r.returncode == 0
    assert "未执行" in r.stdout, r.stdout
    assert (target / "keep.txt").exists()
    print("[OK] link without --force refuses")

    # 5) --force: 备份原目录并创建符号链接
    r = run("link", "dws", "--agents", "pi", "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    assert target.is_symlink(), "target should be symlink"
    assert (target / "SKILL.md").exists()
    print("[OK] link --force creates symlink")

    # 6) 重复 link: skip
    r = run("link", "dws", "--agents", "pi")
    assert "skip" in r.stdout, r.stdout
    print("[OK] re-link skips")

    # 7) status 显示 linked
    r = run("status", "--agent", "pi")
    assert "linked=" in r.stdout
    print("[OK] status shows linked")

    # 8) backups 存在
    r = run("backups")
    assert r.returncode == 0 and "20" in r.stdout, r.stdout
    print("[OK] backups listed")

    # 9) unlink 移除投影
    r = run("unlink", "dws", "--agents", "pi")
    assert r.returncode == 0, r.stdout + r.stderr
    assert not target.exists(), "unlink should remove symlink"
    print("[OK] unlink removes projection")

    # 10) rollback: 未知备份报错
    r = run("rollback", "nonexistent-ts")
    assert r.returncode == 1
    print("[OK] rollback rejects unknown backup")

    # 11) 真实备份名 rollback 可执行
    r = run("backups", "--json")
    bks = json.loads(r.stdout)["backups"]
    ts = bks[-1]["ts"]
    r = run("rollback", ts)
    assert r.returncode == 0, r.stdout + r.stderr
    print("[OK] rollback to existing backup")

    # 12) link 到未知 agent 报错
    r = run("link", "dws", "--agents", "nope")
    assert r.returncode == 0
    assert "未知 agent" in r.stdout, r.stdout
    print("[OK] unknown agent reported")

    # 13) list 显示
    r = run("list")
    assert "dws" in r.stdout
    print("[OK] list shows skills")

    # 14) --json: list 可被脚本消费
    r = run("list", "--json")
    data = json.loads(r.stdout)
    assert data["count"] >= 1 and any(s["name"] == "dws" for s in data["skills"])
    print("[OK] list --json")

    # 15) copy 模式: 写投影标记, 重复 link 判 skip 而非冲突
    fake_wb = Path(env["SKILLHUB_AGENT_DIR_workbuddy"])
    r = run("link", "dws", "--agents", "workbuddy", "--force")
    assert r.returncode == 0, r.stdout + r.stderr
    marker = fake_wb / "dws" / ".skillhub-projection.json"
    assert marker.is_file(), "copy projection should carry marker"
    r = run("link", "dws", "--agents", "workbuddy")
    assert "skip" in r.stdout, r.stdout
    print("[OK] copy marker makes re-link idempotent")

    # 16) 风险门禁: 含 sudo 的 skill 默认拦截, --force 放行
    risky = fake_pi / "risky-demo"
    risky.mkdir(exist_ok=True)
    (risky / "SKILL.md").write_text(
        "---\nname: risky-demo\ndescription: contains sudo\n---\nrun `sudo rm -rf /tmp/x`\n",
        encoding="utf-8")
    r = run("import", "--agent", "pi", "--apply")
    r = run("link", "risky-demo", "--agents", "claude")
    assert "blocked" in r.stdout, r.stdout
    assert not (Path(env["SKILLHUB_AGENT_DIR_claude"]) / "risky-demo").exists()
    r = run("link", "risky-demo", "--agents", "claude", "--force")
    assert (Path(env["SKILLHUB_AGENT_DIR_claude"]) / "risky-demo").is_symlink()
    print("[OK] risk gate blocks sudo unless --force")

    # 17) doctor: json 可解析且含 summary
    r = run("doctor", "--json")
    rep = json.loads(r.stdout)
    assert "summary" in rep and "broken" in rep["summary"]
    print("[OK] doctor --json")

    shutil.rmtree(tmp)
    print("\n=== 全部通过 ===")


if __name__ == "__main__":
    main()
