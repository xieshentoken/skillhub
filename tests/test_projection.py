"""Deterministic integration/security tests for skillhub.

The fixture is created below a temporary directory.  It never copies or reads
the user's real ``~/.agents`` tree.
"""
from __future__ import annotations

import json
import http.server
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import zipfile
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path


WS = Path(__file__).resolve().parents[1]


class SkillhubFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="skillhub-test-"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.hub = self.tmp / "hub"
        self.pi = self.tmp / "pi-skills"
        self.workbuddy = self.tmp / "workbuddy-skills"
        self.claude = self.tmp / "claude-skills"
        for path in (self.pi, self.workbuddy, self.claude):
            path.mkdir()
        self.env = dict(os.environ)
        self.env.update({
            "HOME": str(self.home),
            "SKILLHUB_HOME": str(self.hub),
            "SKILLHUB_AGENT_DIR_pi": str(self.pi),
            "SKILLHUB_AGENT_DIR_workbuddy": str(self.workbuddy),
            "SKILLHUB_AGENT_DIR_claude": str(self.claude),
            "SKILLHUB_AGENT_DIR_codex": str(self.tmp / "codex-skills"),
            "SKILLHUB_AGENT_DIR_opencode": str(self.tmp / "opencode-skills"),
            "SKILLHUB_AGENT_DIR_grok": str(self.tmp / "grok-skills"),
            "SKILLHUB_AGENT_DIR_hermes": str(self.tmp / "hermes-skills"),
            "SKILLHUB_MCP_FILE_workbuddy": str(self.tmp / "mcp.json"),
            "SKILLHUB_MCP_FILE_claude": str(self.tmp / "claude.json"),
            "SKILLHUB_MCP_FILE_opencode": str(self.tmp / "opencode.jsonc"),
            "SKILLHUB_MCP_FILE_codex": str(self.tmp / "config.toml"),
            "SKILLHUB_MCP_FILE_grok": str(self.tmp / "grok.toml"),
            "SKILLHUB_MCP_PI_DIR": str(self.tmp / "pi-servers"),
        })

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "skillhub", *args],
            cwd=WS,
            env=self.env,
            text=True,
            capture_output=True,
        )

    def add_skill(self, name: str = "demo", *, script: str = "print(1)\n",
                  category: str = "") -> Path:
        root = self.pi / name
        if category:
            root = self.pi / category / name
        root.mkdir(parents=True)
        (root / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: deterministic fixture\n---\n# {name}\n",
            encoding="utf-8",
        )
        (root / "run.py").write_text(script, encoding="utf-8")
        return root

    def import_skill(self) -> str:
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index), 1)
        return next(iter(index))


class SecurityRegressionTests(SkillhubFixture):
    def test_link_defaults_to_preview_and_apply_is_explicit(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        target = self.claude / "demo"

        preview = self.run_cli("link", sid, "--agents", "pi")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertFalse(target.exists())
        self.assertIn("预览", preview.stdout)

        applied = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertTrue(target.is_symlink())

    def test_risk_gate_requires_allow_risky_separately_from_force(self) -> None:
        self.add_skill(script="sudo echo unsafe\n")
        sid = self.import_skill()
        result = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.claude / "demo").exists())
        result = self.run_cli("link", sid, "--agents", "claude", "--apply", "--allow-risky")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_external_name_cannot_escape_store_or_projection_root(self) -> None:
        self.add_skill("safe")
        self.add_skill("other")
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)

        index_path = self.hub / "index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        sid = next(iter(index))
        index[sid]["name"] = "../escaped"
        index_path.write_text(json.dumps(index), encoding="utf-8")
        result = self.run_cli("link", sid, "--agents", "pi", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "escaped").exists())

    def test_corrupt_index_is_not_treated_as_empty_or_overwritten(self) -> None:
        self.hub.mkdir()
        index_path = self.hub / "index.json"
        index_path.write_text("{broken", encoding="utf-8")
        result = self.run_cli("list", "--json")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(index_path.read_text(encoding="utf-8"), "{broken")

    def test_parent_symlink_cannot_escape_nested_agent_root(self) -> None:
        self.add_skill("demo", category="category")
        sid = self.import_skill()
        outside = self.tmp / "outside"
        outside.mkdir()
        nested_parent = self.tmp / "hermes-skills" / "category"
        nested_parent.parent.mkdir(parents=True, exist_ok=True)
        nested_parent.symlink_to(outside, target_is_directory=True)
        result = self.run_cli("link", sid, "--agents", "hermes", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((outside / "demo").exists())

    def test_complete_file_set_digest_keeps_script_variants_distinct(self) -> None:
        self.add_skill("same", script="print('one')\n")
        self.add_skill("same-copy", script="print('two')\n")
        first_md = (self.pi / "same" / "SKILL.md").read_text(encoding="utf-8")
        (self.pi / "same-copy" / "SKILL.md").write_text(first_md.replace("same-copy", "same"), encoding="utf-8")
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(len(index), 2)

    def test_copy_drift_is_reported_and_unlink_does_not_delete_it(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        result = self.run_cli("link", sid, "--agents", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        copied = self.workbuddy / "demo" / "run.py"
        copied.write_text("modified\n", encoding="utf-8")

        status = self.run_cli("status", "--agent", "workbuddy", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("copy_drift", status.stdout)

        unlink = self.run_cli("unlink", sid, "--agents", "workbuddy", "--apply")
        self.assertNotEqual(unlink.returncode, 0)
        self.assertTrue(copied.exists())

    def test_backup_is_independent_and_rollback_restores_conflict(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        target = self.claude / "demo"
        target.mkdir()
        (target / "keep.txt").write_text("original\n", encoding="utf-8")
        result = self.run_cli("link", sid, "--agents", "claude", "--apply", "--force")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = json.loads(self.run_cli("backups", "--json").stdout)["backups"]
        self.assertTrue(backups)
        backup = self.hub / "backups" / backups[-1]["ts"]
        saved = list((backup / "targets").rglob("keep.txt"))
        self.assertTrue(saved)
        self.assertNotEqual(os.stat(saved[0]).st_ino, os.stat(self.hub / "store" / sid / "SKILL.md").st_ino)

        rollback = self.run_cli("rollback", backups[-1]["ts"], "--apply")
        self.assertEqual(rollback.returncode, 0, rollback.stdout + rollback.stderr)
        self.assertTrue(target.is_dir())
        self.assertEqual((target / "keep.txt").read_text(encoding="utf-8"), "original\n")

    def test_rollback_rejects_saved_path_escape_before_deleting_current_state(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        result = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        backups = json.loads(self.run_cli("backups", "--json").stdout)["backups"]
        backup = self.hub / "backups" / backups[-1]["ts"]
        journal_path = backup / "targets.json"
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        journal[0]["kind"] = "file"
        journal[0]["saved"] = "../../escape"
        journal_path.write_text(json.dumps(journal), encoding="utf-8")
        before = (self.claude / "demo").readlink()
        result = self.run_cli("rollback", backups[-1]["ts"], "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.claude / "demo").readlink(), before)
        self.assertFalse((self.hub / "escape").exists())

    def test_rollback_rejects_corrupt_store_snapshot_before_live_changes(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        result = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.claude / "demo"
        backups = json.loads(self.run_cli("backups", "--json").stdout)["backups"]
        backup = self.hub / "backups" / backups[0]["ts"]
        (backup / "store" / sid / "run.py").unlink()
        before = target.readlink()
        result = self.run_cli("rollback", backups[0]["ts"], "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.readlink(), before)
        self.assertTrue((target / "run.py").exists())

    def test_internal_skill_symlink_is_rejected_without_copying_external_file(self) -> None:
        root = self.add_skill()
        secret = self.tmp / "secret.txt"
        secret.write_text("do not copy", encoding="utf-8")
        (root / "secret.txt").symlink_to(secret)
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.hub.exists())

    def test_modified_central_copy_is_rejected_and_reported(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        result = self.run_cli("link", sid, "--agents", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        copied = self.workbuddy / "demo" / "run.py"
        central_script = self.hub / "store" / sid / "run.py"
        central_script.write_text("tampered\n", encoding="utf-8")
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertNotEqual(result.returncode, 0)
        status = self.run_cli("status", "--agent", "workbuddy", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("store_drift", status.stdout)
        result = self.run_cli("unlink", sid, "--agents", "workbuddy", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(copied.exists())
        result = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertNotEqual(result.returncode, 0)
        doctor = self.run_cli("doctor", "--json")
        self.assertNotEqual(doctor.returncode, 0)
        report = json.loads(doctor.stdout)
        self.assertGreaterEqual(report["summary"].get("store_drift", 0), 1)

    def test_mcp_parse_failure_does_not_overwrite_and_resolve_requires_env(self) -> None:
        self.add_skill()
        server_file = self.tmp / "mcp.json"
        server_file.write_text("{broken", encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(server_file.read_text(encoding="utf-8"), "{broken")

    def test_mcp_import_redacts_short_credentials_and_preserves_unknown_fields(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "stdio": {
                "command": "node",
                "args": ["--token", "short-secret"],
                "env": {"PLAIN": "value"},
                "enabled": False,
                "unknown_field": {"keep": True},
            },
            "remote": {
                "url": "https://example.test/mcp?api_key=short-url-secret",
                "headers": {"Authorization": "Bearer x"},
            },
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index_text = (self.hub / "mcp" / "index.json").read_text(encoding="utf-8")
        self.assertNotIn("short-secret", index_text)
        self.assertNotIn("short-url-secret", index_text)
        index = json.loads(index_text)
        self.assertEqual(index["stdio"]["fields"]["unknown_field"], {"keep": True})
        self.assertFalse(index["stdio"]["enabled"])

    def test_mcp_generation_uses_agent_transport_formats_and_atomic_update(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "stdio": {"command": "node", "args": ["server.js"], "env": {"TOKEN": "short"}},
            "remote": {"url": "https://example.test/mcp", "headers": {"X-Key": "short"}},
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('"short"', (self.hub / "mcp" / "index.json").read_text(encoding="utf-8"))

        result = self.run_cli("mcp", "generate", "stdio", "--agents", "codex,opencode,claude,workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        codex = tomllib.loads((self.tmp / "config.toml").read_text(encoding="utf-8"))
        self.assertIn("stdio", codex["mcp_servers"])
        self.assertEqual(codex["mcp_servers"]["stdio"]["command"], "node")
        self.assertNotIn("url", codex["mcp_servers"]["stdio"])
        self.assertNotIn("[[mcp_servers.stdio]]", (self.tmp / "config.toml").read_text(encoding="utf-8"))
        opencode = json.loads((self.tmp / "opencode.jsonc").read_text(encoding="utf-8"))
        block = opencode["mcp"]["stdio"]
        self.assertEqual(block["type"], "local")
        self.assertIsInstance(block["command"], list)
        self.assertTrue(block["enabled"])
        claude = json.loads((self.tmp / "claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["mcpServers"]["stdio"]["type"], "stdio")
        self.assertNotIn("type", json.loads((self.tmp / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["stdio"])

        mode = stat.S_IMODE((self.tmp / "config.toml").stat().st_mode)
        self.assertEqual(mode, 0o600)
        before = (self.tmp / "opencode.jsonc").read_text(encoding="utf-8")
        (self.tmp / "opencode.jsonc").write_text("{invalid", encoding="utf-8")
        result = self.run_cli("mcp", "generate", "remote", "--agents", "opencode", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.tmp / "opencode.jsonc").read_text(encoding="utf-8"), "{invalid")

    def test_codex_standard_http_table_round_trips_env_headers_and_enabled(self) -> None:
        config = self.tmp / "config.toml"
        config.write_text(
            "[mcp_servers.remote]\n"
            "url = \"https://example.test/mcp\"\n"
            "enabled = false\n"
            "[mcp_servers.remote.env_http_headers]\n"
            "Authorization = \"AUTH_TOKEN\"\n",
            encoding="utf-8",
        )
        result = self.run_cli("mcp", "import", "--from", "codex", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "mcp" / "index.json").read_text(encoding="utf-8"))
        self.assertFalse(index["remote"]["enabled"])
        self.assertEqual(index["remote"]["fields"]["env_http_headers"]["Authorization"], "AUTH_TOKEN")
        result = self.run_cli("mcp", "generate", "remote", "--agents", "opencode", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        opencode = json.loads((self.tmp / "opencode.jsonc").read_text(encoding="utf-8"))
        self.assertEqual(opencode["mcp"]["remote"]["headers"]["Authorization"], "{env:AUTH_TOKEN}")
        self.assertFalse(opencode["mcp"]["remote"]["enabled"])
        result = self.run_cli("mcp", "generate", "remote", "--agents", "claude", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "claude.json").exists())
        self.assertTrue(self.run_cli("mcp", "status", "--json").returncode == 0)

    def test_mcp_stdio_command_args_are_normalized_for_json_targets_and_pi(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "stdio": {"command": "node", "args": ["--token", "short"]},
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(
            "mcp", "generate", "stdio", "--agents", "opencode,claude,pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        opencode = json.loads((self.tmp / "opencode.jsonc").read_text(encoding="utf-8"))
        op_block = opencode["mcp"]["stdio"]
        self.assertEqual(op_block["command"][0:2], ["node", "--token"])
        self.assertNotIn("args", op_block)
        self.assertNotIn("{{env:", json.dumps(op_block))

        claude = json.loads((self.tmp / "claude.json").read_text(encoding="utf-8"))
        cl_block = claude["mcpServers"]["stdio"]
        self.assertEqual(cl_block["command"], "node")
        self.assertEqual(cl_block["args"][0], "--token")
        self.assertNotIn("{{env:", json.dumps(cl_block))

        pi = json.loads((self.tmp / "pi-servers" / "stdio.json").read_text(encoding="utf-8"))
        self.assertEqual(pi["id"], "stdio")
        self.assertEqual(pi["transport"], "stdio")
        self.assertNotIn("type", pi)
        self.assertEqual(pi["command"], "node")
        self.assertEqual(pi["args"][0], "--token")
        self.assertNotIn("{{env:", json.dumps(pi))

    def test_opencode_source_converts_to_claude_and_codex(self) -> None:
        source = self.tmp / "opencode.jsonc"
        source.write_text(json.dumps({"mcp": {
            "stdio": {
                "type": "local",
                "command": ["node", "--token", "short"],
                "enabled": True,
            },
            "remote": {
                "type": "remote",
                "url": "https://example.invalid/mcp",
                "headers": {"Authorization": "Bearer remote-secret"},
                "enabled": True,
            },
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "opencode", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(
            "mcp", "generate", "stdio", "--agents", "claude,codex", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        claude = json.loads((self.tmp / "claude.json").read_text(encoding="utf-8"))
        self.assertEqual(claude["mcpServers"]["stdio"]["command"], "node")
        self.assertEqual(claude["mcpServers"]["stdio"]["args"][0], "--token")
        self.assertNotIn("{{env:", json.dumps(claude["mcpServers"]["stdio"]))
        config = tomllib.loads((self.tmp / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["mcp_servers"]["stdio"]["command"], "node")
        self.assertEqual(config["mcp_servers"]["stdio"]["args"][0], "--token")

        result = self.run_cli(
            "mcp", "generate", "remote", "--agents", "claude,codex", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        claude = json.loads((self.tmp / "claude.json").read_text(encoding="utf-8"))
        self.assertNotIn("disabled", claude["mcpServers"]["remote"])
        config = tomllib.loads((self.tmp / "config.toml").read_text(encoding="utf-8"))
        self.assertTrue(config["mcp_servers"]["remote"]["enabled"])
        self.assertIn("bearer_token_env_var", config["mcp_servers"]["remote"])

    def test_mcp_url_components_keep_refs_and_resolve_with_encoding(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "remote": {
                "url": (
                    "https://user:pass@example.invalid/mcp?"
                    "existing={{env:REMOTE_API_KEY}}&api_key=short&token=second"
                ),
            },
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "mcp" / "index.json").read_text(encoding="utf-8"))
        url = index["remote"]["url"]
        self.assertIn("{{env:", url)
        self.assertNotIn("%7B%7Benv%3A", url)
        self.assertNotIn("pass", url)
        self.assertNotIn("short", url)
        self.assertNotIn("second", url)

        from skillhub import mcp
        variables = mcp.required_env_vars(index["remote"])
        self.assertIn("REMOTE_API_KEY", variables)
        self.assertIn("REMOTE_API_KEY_2", variables)
        for variable in variables:
            self.env[variable] = f"value/{variable}&part"
        result = self.run_cli("mcp", "generate", "remote", "--agents", "opencode", "--apply", "--resolve")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads((self.tmp / "opencode.jsonc").read_text(encoding="utf-8"))
        rendered = output["mcp"]["remote"]["url"]
        self.assertNotIn("{env:", rendered)
        self.assertNotIn("{{env:", rendered)
        self.assertIn("%26part", rendered)
        self.assertIn("%2F", rendered)

    def test_codex_maps_imported_secret_headers_to_env_fields(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "remote": {
                "url": "https://example.invalid/mcp",
                "headers": {
                    "Authorization": "Bearer short-token",
                    "X-API-Key": "another-secret",
                    "X-Client": "plain",
                },
            },
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("mcp", "generate", "remote", "--agents", "codex", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = tomllib.loads((self.tmp / "config.toml").read_text(encoding="utf-8"))
        block = config["mcp_servers"]["remote"]
        self.assertRegex(block["bearer_token_env_var"], r"^[A-Za-z_][A-Za-z0-9_]*$")
        self.assertEqual(block["env_http_headers"]["X-API-Key"], "REMOTE_X_API_KEY")
        self.assertNotIn("Authorization", block.get("http_headers", {}))
        self.assertEqual(block["http_headers"]["X-Client"], "plain")
        self.assertNotIn("${", (self.tmp / "config.toml").read_text(encoding="utf-8"))

    def test_codex_rejects_compound_header_ref_without_resolve(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "remote": {
                "url": "https://example.invalid/mcp",
                "headers": {"X-Value": "prefix-${EXISTING}-suffix"},
            },
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("mcp", "generate", "remote", "--agents", "codex", "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "config.toml").exists())
        self.env["EXISTING"] = "resolved&value"
        result = self.run_cli(
            "mcp", "generate", "remote", "--agents", "codex", "--apply", "--resolve")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = tomllib.loads((self.tmp / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["mcp_servers"]["remote"]["http_headers"]["X-Value"],
                         "prefix-resolved&value-suffix")

    def test_toml_and_json_generation_preserve_custom_fields_and_replace_transport(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "demo": {"command": "node", "args": ["server.js"]},
        }}), encoding="utf-8")
        (self.tmp / "config.toml").write_text(
            "[mcp_servers.\"demo\"] # normal comment\n"
            "url = \"https://old.invalid\"\n"
            "custom_timeout = 30\n"
            "[mcp_servers.\"demo\".legacy]\n"
            "keep = true\n"
            "[other]\nvalue = 1\n",
            encoding="utf-8",
        )
        (self.tmp / "claude.json").write_text(json.dumps({"mcpServers": {
            "demo": {
                "url": "https://old.invalid",
                "headers": {"Authorization": "old-secret"},
                "custom_timeout": 30,
            },
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("mcp", "generate", "demo", "--agents", "codex,claude", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = tomllib.loads((self.tmp / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["mcp_servers"]["demo"]["command"], "node")
        self.assertEqual(config["mcp_servers"]["demo"]["custom_timeout"], 30)
        self.assertTrue(config["mcp_servers"]["demo"]["legacy"]["keep"])
        self.assertNotIn("url", config["mcp_servers"]["demo"])
        claude = json.loads((self.tmp / "claude.json").read_text(encoding="utf-8"))
        block = claude["mcpServers"]["demo"]
        self.assertEqual(block["command"], "node")
        self.assertEqual(block["custom_timeout"], 30)
        self.assertNotIn("url", block)
        self.assertNotIn("headers", block)

    def test_disabled_pi_and_claude_targets_are_rejected_without_writes(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "disabled": {"command": "node", "enabled": False},
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        for agent, target in (("pi", self.tmp / "pi-servers" / "disabled.json"),
                              ("claude", self.tmp / "claude.json")):
            result = self.run_cli("mcp", "generate", "disabled", "--agents", agent, "--apply")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(target.exists())

    def test_mcp_generate_then_status_matches_all_supported_targets(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "demo": {"command": "node"},
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli(
            "mcp", "generate", "demo",
            "--agents", "pi,codex,grok,workbuddy,claude,opencode", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        status = self.run_cli("mcp", "status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        row = next(item for item in json.loads(status.stdout)["servers"] if item["id"] == "demo")
        for agent in ("pi", "codex", "grok", "workbuddy", "claude", "opencode"):
            self.assertTrue(row["agents"][agent]["match"], row["agents"][agent])
        claude = json.loads((self.tmp / "claude.json").read_text(encoding="utf-8"))
        claude["mcpServers"]["demo"]["command"] = "other"
        (self.tmp / "claude.json").write_text(json.dumps(claude), encoding="utf-8")
        status = self.run_cli("mcp", "status", "--json")
        row = next(item for item in json.loads(status.stdout)["servers"] if item["id"] == "demo")
        self.assertEqual(row["agents"]["claude"]["state"], "drift")

    def test_opencode_direct_server_id_named_servers_is_not_treated_as_container(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "servers": {"command": "node", "args": ["server.js"]},
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("mcp", "generate", "servers", "--agents", "opencode", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = json.loads((self.tmp / "opencode.jsonc").read_text(encoding="utf-8"))
        self.assertEqual(output["mcp"]["servers"]["type"], "local")
        self.assertTrue(output["mcp"]["servers"]["enabled"])

    def test_mcp_backup_is_rollbackable_and_restores_original_file(self) -> None:
        server_file = self.tmp / "mcp.json"
        original = json.dumps({"mcpServers": {"stdio": {"command": "node"}}}, indent=2)
        server_file.write_text(original, encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("mcp", "generate", "stdio", "--agents", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotEqual(server_file.read_text(encoding="utf-8"), original)
        backups = json.loads(self.run_cli("backups", "--json").stdout)["backups"]
        mcp_backups = [item for item in backups if item.get("kind") == "mcp"]
        self.assertTrue(mcp_backups)
        result = self.run_cli("rollback", mcp_backups[0]["ts"], "--apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(server_file.read_text(encoding="utf-8"), original)

    def test_resolve_missing_environment_variable_stops_before_write(self) -> None:
        server_file = self.tmp / "mcp.json"
        server_file.write_text(json.dumps({"mcpServers": {
            "remote": {"url": "https://example.test/mcp", "headers": {"Authorization": "Bearer x"}},
        }}), encoding="utf-8")
        result = self.run_cli("mcp", "import", "--from", "workbuddy", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        target = self.tmp / "pi-servers" / "remote.json"
        result = self.run_cli("mcp", "generate", "remote", "--agents", "pi", "--apply", "--resolve")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(target.exists())

    def test_gui_requires_loopback_session_json_and_explicit_apply(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        import importlib
        sys.path.insert(0, str(WS))
        old_env = dict(os.environ)
        os.environ.update(self.env)
        webgui = importlib.import_module("skillhub.webgui")
        httpd = webgui.ThreadingHTTPServer(("127.0.0.1", 0), webgui._Handler)
        httpd.skillhub_token = "test-session"
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"

        def request(path: str, *, data=None, content_type=None, cookie="test-session", origin=None):
            headers = {"Host": f"127.0.0.1:{httpd.server_address[1]}"}
            if cookie:
                headers["Cookie"] = f"skillhub_session={cookie}"
            if origin:
                headers["Origin"] = origin
            if content_type:
                headers["Content-Type"] = content_type
            req = urllib.request.Request(base + path, data=data, headers=headers, method="POST" if data is not None else "GET")
            try:
                with urllib.request.urlopen(req) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

        status, _ = request("/api/health", cookie="wrong")
        self.assertEqual(status, 403)
        status, _ = request("/api/health", origin="http://evil.test")
        self.assertEqual(status, 403)
        self.add_skill("中文 名'\"")
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "index.json").read_text(encoding="utf-8"))
        special_sid = next(sid for sid, item in index.items()
                           if item["name"] == "中文 名'\"")
        status, body = request("/api/skill/" + quote(special_sid, safe=""))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["manifest"]["id"], special_sid)
        payload = json.dumps({"sid": sid, "agents": ["claude"]}).encode()
        status, _ = request("/api/link", data=payload, content_type="text/plain")
        self.assertEqual(status, 415)
        status, _ = request("/api/link", data=json.dumps({"sid": sid, "agents": "claude"}).encode(), content_type="application/json")
        self.assertEqual(status, 400)
        status, body = request("/api/link", data=payload, content_type="application/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["mode"], "plan")
        self.assertFalse((self.claude / "demo").exists())
        payload = json.dumps({"sid": sid, "agents": ["claude"], "apply": True}).encode()
        status, _ = request("/api/link", data=payload, content_type="application/json")
        self.assertEqual(status, 200)
        self.assertTrue((self.claude / "demo").is_symlink())
        httpd.shutdown(); thread.join(timeout=2); httpd.server_close()
        os.environ.clear(); os.environ.update(old_env)

    def test_zip_cleanup_and_manifest_validation(self) -> None:
        self.add_skill()
        (self.pi / "demo" / "run.py").chmod(0o755)
        sid = self.import_skill()
        out = self.tmp / "demo.zip"
        result = self.run_cli("export", sid, "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_cli("add", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        with zipfile.ZipFile(out, "a") as zf:
            zf.writestr("../outside.txt", "must not extract")
        result = self.run_cli("add", str(out), "--apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.tmp / "outside.txt").exists())

    def test_ul_edit_trial_publish_and_single_skill_trash(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        created = self.run_cli("ul", "create", sid, "--apply", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        ul_sid = json.loads(created.stdout)["ul_sid"]
        self.assertNotEqual(ul_sid, sid)
        self.assertTrue((self.hub / "store" / ul_sid).is_dir())
        self.assertNotEqual(os.stat(self.hub / "store" / sid / "run.py").st_ino,
                            os.stat(self.hub / "store" / ul_sid / "run.py").st_ino)

        edited = self.run_cli("ul", "edit", ul_sid, "--file", "run.py",
                              "--text", "print(2)\n", "--apply", "--json")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        edited_sid = json.loads(edited.stdout)["sid"]
        self.assertNotEqual(edited_sid, ul_sid)
        self.assertFalse((self.hub / "store" / ul_sid).exists())
        self.assertEqual((self.hub / "store" / edited_sid / "run.py").read_text(), "print(2)\n")

        linked = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertEqual(linked.returncode, 0, linked.stderr)
        trial = self.run_cli("ul", "trial", sid, "--agents", "claude", "--apply", "--json")
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertTrue((self.claude / "demo").is_symlink())
        self.assertEqual(os.path.realpath(self.claude / "demo"),
                         os.path.realpath(self.hub / "store" / edited_sid))

        published = self.run_cli("ul", "publish", edited_sid, "--apply", "--json")
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        new_formal = json.loads(published.stdout)["new_formal_sid"]
        index = json.loads((self.hub / "index.json").read_text())
        self.assertEqual(list(index), [new_formal])
        self.assertEqual(index[new_formal]["channel"], "formal")
        self.assertEqual(os.path.realpath(self.claude / "demo"),
                         os.path.realpath(self.hub / "store" / new_formal))
        trash = json.loads(self.run_cli("trash", "list", "--json").stdout)
        self.assertTrue(any(item["sid"] == sid and item["valid"] for item in trash["items"]))

    def test_formal_link_and_unlink_follow_active_ul_trial(self) -> None:
        self.add_skill()
        sid = self.import_skill()
        created = self.run_cli("ul", "create", sid, "--apply", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        ul_sid = json.loads(created.stdout)["ul_sid"]

        linked = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertEqual(linked.returncode, 0, linked.stderr)
        trial = self.run_cli("ul", "trial", sid, "--agents", "claude", "--apply")
        self.assertEqual(trial.returncode, 0, trial.stderr)
        self.assertEqual(os.path.realpath(self.claude / "demo"),
                         os.path.realpath(self.hub / "store" / ul_sid))

        relink = self.run_cli("link", sid, "--agents", "claude", "--apply")
        self.assertEqual(relink.returncode, 0, relink.stdout + relink.stderr)
        unlink = self.run_cli("unlink", sid, "--agents", "claude", "--apply")
        self.assertEqual(unlink.returncode, 0, unlink.stdout + unlink.stderr)
        self.assertFalse((self.claude / "demo").exists())

    def test_single_trash_restore_does_not_replace_unrelated_skill(self) -> None:
        self.add_skill("one")
        self.add_skill("two")
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "index.json").read_text())
        one = next(sid for sid, item in index.items() if item["name"] == "one")
        two = next(sid for sid, item in index.items() if item["name"] == "two")
        self.run_cli("ul", "create", one, "--apply")
        ul = next(sid for sid, item in json.loads((self.hub / "index.json").read_text()).items()
                  if item["name"] == "one" and item.get("channel") == "ul")
        edited = self.run_cli("ul", "edit", ul, "--file", "run.py", "--text", "changed\n", "--apply", "--json")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        published = self.run_cli("ul", "publish", json.loads(edited.stdout)["sid"], "--apply", "--json")
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        current = json.loads((self.hub / "index.json").read_text())
        new_one = next(sid for sid, item in current.items() if item["name"] == "one")
        trash = json.loads(self.run_cli("trash", "list", "--json").stdout)["items"]
        old_entry = next(item["id"] for item in trash if item.get("sid") == one)
        restored = self.run_cli("trash", "restore", old_entry, "--apply", "--json")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        after = json.loads((self.hub / "index.json").read_text())
        self.assertIn(two, after)
        self.assertIn(one, after)
        self.assertNotIn(new_one, after)

    def test_external_ul_refresh_retargets_active_trial_and_publish(self) -> None:
        self.add_skill()
        formal_sid = self.import_skill()
        created = self.run_cli("ul", "create", formal_sid, "--apply", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        old_ul_sid = json.loads(created.stdout)["ul_sid"]
        linked = self.run_cli("link", formal_sid, "--agents", "claude", "--apply")
        self.assertEqual(linked.returncode, 0, linked.stderr)
        trial = self.run_cli("ul", "trial", formal_sid, "--agents", "claude", "--apply")
        self.assertEqual(trial.returncode, 0, trial.stderr)

        old_ul_root = self.hub / "store" / old_ul_sid
        (old_ul_root / "run.py").write_text("print(2)\n", encoding="utf-8")
        code = ("from skillhub import store; import json; "
                f"print(json.dumps(store.refresh_ul({old_ul_sid!r}, apply=True)))")
        refreshed = subprocess.run([sys.executable, "-c", code], cwd=WS,
                                    env=self.env, text=True, capture_output=True)
        self.assertEqual(refreshed.returncode, 0, refreshed.stderr)
        refresh_result = json.loads(refreshed.stdout)
        new_ul_sid = refresh_result["sid"]
        self.assertNotEqual(new_ul_sid, old_ul_sid)
        self.assertFalse(old_ul_root.exists())
        self.assertTrue((self.hub / "store" / new_ul_sid).is_dir())
        self.assertEqual((self.claude / "demo").resolve(),
                         (self.hub / "store" / new_ul_sid).resolve())

        published = self.run_cli("ul", "publish", new_ul_sid, "--apply", "--json")
        self.assertEqual(published.returncode, 0, published.stderr)
        self.assertEqual(json.loads(published.stdout)["new_formal_sid"],
                         new_ul_sid.removesuffix("--ul"))

    def test_publish_and_restore_migrate_groups_and_distribution(self) -> None:
        self.add_skill("one")
        old_sid = self.import_skill()
        group = self.run_cli("group", "set", "release", "--members", old_sid, "--apply")
        self.assertEqual(group.returncode, 0, group.stderr)
        saved_distribution = self.run_cli("group", "distribute", "--groups", "release",
                                          "--skills", old_sid, "--agents", "claude",
                                          "--replace", "--apply", "--json")
        self.assertEqual(saved_distribution.returncode, 0,
                         saved_distribution.stdout + saved_distribution.stderr)
        created = self.run_cli("ul", "create", old_sid, "--apply", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        ul_sid = json.loads(created.stdout)["ul_sid"]
        edited = self.run_cli("ul", "edit", ul_sid, "--file", "run.py",
                              "--text", "changed\n", "--apply", "--json")
        self.assertEqual(edited.returncode, 0, edited.stderr)
        published = self.run_cli("ul", "publish", json.loads(edited.stdout)["sid"],
                                 "--apply", "--json")
        self.assertEqual(published.returncode, 0, published.stdout + published.stderr)
        current_sid = json.loads(published.stdout)["new_formal_sid"]
        groups = json.loads((self.hub / "groups.json").read_text(encoding="utf-8"))
        self.assertEqual(groups["groups"]["release"]["members"], [current_sid])
        distributions = json.loads((self.hub / "distributions.json").read_text(encoding="utf-8"))
        self.assertEqual(distributions["agents"]["claude"]["sids"], [current_sid])

        linked = self.run_cli("link", current_sid, "--agents", "claude", "--apply")
        self.assertEqual(linked.returncode, 0, linked.stderr)
        entry = next(item["id"] for item in json.loads(
            self.run_cli("trash", "list", "--json").stdout)["items"]
                     if item.get("sid") == old_sid)
        restored = self.run_cli("trash", "restore", entry, "--apply", "--json")
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        after = json.loads((self.hub / "index.json").read_text(encoding="utf-8"))
        self.assertIn(old_sid, after)
        self.assertNotIn(current_sid, after)
        self.assertEqual(os.path.realpath(self.claude / "one"),
                         os.path.realpath(self.hub / "store" / old_sid))
        groups = json.loads((self.hub / "groups.json").read_text(encoding="utf-8"))
        self.assertEqual(groups["groups"]["release"]["members"], [old_sid])
        distributions = json.loads((self.hub / "distributions.json").read_text(encoding="utf-8"))
        self.assertEqual(distributions["agents"]["claude"]["sids"], [old_sid])
        plan = self.run_cli("group", "distribute", "--groups", "release",
                            "--agents", "claude", "--replace", "--json")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        payload = json.loads(plan.stdout)
        self.assertEqual(payload["desired"], [old_sid])
        self.assertEqual(payload["agents"]["claude"]["remove"], [])

    def test_restore_transaction_rolls_back_copy_index_and_second_projection_failure(self) -> None:
        code = r'''
import json, os, pathlib, tempfile

tmp = pathlib.Path(tempfile.mkdtemp(prefix="skillhub-restore-tx-"))
home = tmp / "home"; home.mkdir()
hub = tmp / "hub"
paths = {name: tmp / (name + "-skills") for name in ("pi", "codex", "opencode", "workbuddy", "claude", "grok", "hermes")}
for path in paths.values(): path.mkdir()
os.environ.update({"HOME": str(home), "SKILLHUB_HOME": str(hub), **{
    "SKILLHUB_AGENT_DIR_" + name: str(path) for name, path in paths.items()
}})
from skillhub import adapters, scan, store

root = paths["pi"] / "demo"; root.mkdir()
(root / "SKILL.md").write_text("---\nname: demo\ndescription: tx\n---\n# demo\n")
(root / "run.py").write_text("print(1)\n")
old = store.import_skill("pi", scan.scan_agent("pi")[0], apply=True)
ul = store.create_ul(old, apply=True)["ul_sid"]
edited = store.edit_ul(ul, "run.py", "print(2)\n", apply=True)["sid"]
current = adapters.publish_ul(edited, apply=True)["new_formal_sid"]
adapters.apply_link(current, ["claude", "workbuddy"], allow_risky=True)
entry = next(item["id"] for item in store.list_trash() if item.get("sid") == old)
before_index = json.dumps(store.load_index(), sort_keys=True)

real_copy = store._copy_plain_tree
calls = {"copy": 0}
def fail_copy(src, dst):
    calls["copy"] += 1
    if calls["copy"] == 2:
        raise RuntimeError("copy injection")
    return real_copy(src, dst)
store._copy_plain_tree = fail_copy
try:
    store.restore_trash(entry)
except RuntimeError:
    pass
else:
    raise AssertionError("copy failure was not propagated")
finally:
    store._copy_plain_tree = real_copy
assert json.dumps(store.load_index(), sort_keys=True) == before_index
assert (hub / "store" / current).is_dir() and not (hub / "store" / old).exists()

real_save = store.save_index
calls = {"save": 0}
def fail_save(index):
    calls["save"] += 1
    if calls["save"] == 2:
        raise RuntimeError("index injection")
    return real_save(index)
store.save_index = fail_save
try:
    store.restore_trash(entry)
except RuntimeError:
    pass
else:
    raise AssertionError("index failure was not propagated")
finally:
    store.save_index = real_save
assert json.dumps(store.load_index(), sort_keys=True) == before_index
assert os.path.realpath(paths["claude"] / "demo") == os.path.realpath(hub / "store" / current)
assert (paths["workbuddy"] / "demo" / "run.py").read_text() == "print(2)\n"

real_install = adapters._install_projection
calls = {"install": 0}
def fail_install(*args, **kwargs):
    calls["install"] += 1
    if calls["install"] == 2:
        raise RuntimeError("projection injection")
    return real_install(*args, **kwargs)
adapters._install_projection = fail_install
try:
    store.restore_trash(entry)
except RuntimeError:
    pass
else:
    raise AssertionError("projection failure was not propagated")
finally:
    adapters._install_projection = real_install
assert json.dumps(store.load_index(), sort_keys=True) == before_index
assert os.path.realpath(paths["claude"] / "demo") == os.path.realpath(hub / "store" / current)
assert (paths["workbuddy"] / "demo" / "run.py").read_text() == "print(2)\n"
print("ok")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=WS, env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_groups_deduplicate_distribution_and_preserve_unmanaged_target(self) -> None:
        self.add_skill("one")
        self.add_skill("two")
        result = self.run_cli("import", "--agent", "pi", "--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        index = json.loads((self.hub / "index.json").read_text())
        one = next(sid for sid, item in index.items() if item["name"] == "one")
        two = next(sid for sid, item in index.items() if item["name"] == "two")
        group = self.run_cli("group", "set", "work", "--members", f"{one},{one},{two}", "--apply", "--json")
        self.assertEqual(group.returncode, 0, group.stderr)
        groups = json.loads((self.hub / "groups.json").read_text())
        self.assertEqual(groups["groups"]["work"]["members"], [one, two])
        plan = self.run_cli("group", "distribute", "--groups", "work", "--agents", "claude", "--json")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(plan.stdout.count('"desired"'), 1)
        applied = self.run_cli("group", "distribute", "--groups", "work", "--agents", "claude", "--apply")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertTrue((self.claude / "one").is_symlink())
        self.assertTrue((self.claude / "two").is_symlink())
        reduced = self.run_cli("group", "distribute", "--skills", two, "--agents", "claude", "--replace", "--apply")
        self.assertEqual(reduced.returncode, 0, reduced.stdout + reduced.stderr)
        self.assertFalse((self.claude / "one").exists())
        self.assertTrue((self.claude / "two").is_symlink())

    def test_saved_distribution_sources_survive_group_changes_and_trial_replace(self) -> None:
        self.add_skill("one")
        sid = self.import_skill()
        group = self.run_cli("group", "set", "release", "--members", sid, "--apply")
        self.assertEqual(group.returncode, 0, group.stderr)

        # Save both a group selection and an explicit individual selection.
        applied = self.run_cli("group", "distribute", "--groups", "release",
                               "--skills", sid, "--agents", "claude", "--replace",
                               "--apply", "--json")
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        sources = json.loads((self.hub / "distributions.json").read_text(encoding="utf-8"))
        self.assertEqual(sources["agents"]["claude"],
                         {"groups": ["release"], "sids": [sid], "replace": True})

        # A new CLI process resolves the saved source, rather than inferring
        # it from whichever projection happens to exist right now.
        reduced = self.run_cli("group", "set", "release", "--members", "", "--apply")
        self.assertEqual(reduced.returncode, 0, reduced.stderr)
        self.assertTrue((self.claude / "one").is_symlink())
        replay = self.run_cli("group", "distribute", "--agents", "claude", "--apply", "--json")
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        replay_payload = json.loads(replay.stdout)
        self.assertEqual(replay_payload["desired_by_agent"]["claude"], [sid])
        self.assertEqual(replay_payload["plan"]["agents"]["claude"]["remove"], [])
        self.assertTrue((self.claude / "one").is_symlink())

        # Create an UL and put this agent into trial.  A saved formal/group
        # request must protect the actual UL owner during replace.
        created = self.run_cli("ul", "create", sid, "--apply", "--json")
        self.assertEqual(created.returncode, 0, created.stderr)
        trial = self.run_cli("ul", "trial", sid, "--agents", "claude", "--apply", "--json")
        self.assertEqual(trial.returncode, 0, trial.stdout + trial.stderr)
        ul_sid = json.loads(created.stdout)["ul_sid"]
        self.assertEqual(os.path.realpath(self.claude / "one"),
                         os.path.realpath(self.hub / "store" / ul_sid))
        trial_replay = self.run_cli("group", "distribute", "--agents", "claude",
                                    "--apply", "--json")
        self.assertEqual(trial_replay.returncode, 0,
                         trial_replay.stdout + trial_replay.stderr)
        trial_payload = json.loads(trial_replay.stdout)
        self.assertEqual(trial_payload["plan"]["agents"]["claude"]["remove"], [])
        self.assertEqual(os.path.realpath(self.claude / "one"),
                         os.path.realpath(self.hub / "store" / ul_sid))

        # Removing only the explicit selection makes the now-empty group the
        # complete desired source; replace may then remove the projection.
        cleared = self.run_cli("group", "distribute", "--groups", "release",
                               "--agents", "claude", "--replace", "--apply", "--json")
        self.assertEqual(cleared.returncode, 0, cleared.stdout + cleared.stderr)
        cleared_payload = json.loads(cleared.stdout)
        self.assertEqual(cleared_payload["desired_by_agent"]["claude"], [])
        self.assertFalse((self.claude / "one").exists())
        sources = json.loads((self.hub / "distributions.json").read_text(encoding="utf-8"))
        self.assertEqual(sources["agents"]["claude"],
                         {"groups": ["release"], "sids": [], "replace": True})

    def test_diagnosis_blocks_explicit_missing_without_running_skill(self) -> None:
        self.add_skill(script="print('not executed')\n")
        sid = self.import_skill()
        index_path = self.hub / "index.json"
        index = json.loads(index_path.read_text())
        index[sid]["dependencies"] = {"tools": ["skillhub-tool-that-does-not-exist"]}
        index_path.write_text(json.dumps(index), encoding="utf-8")
        diagnosis = self.run_cli("diagnose", sid, "--json")
        self.assertEqual(diagnosis.returncode, 1)
        payload = json.loads(diagnosis.stdout)
        self.assertTrue(payload["blocking"])
        self.assertTrue(any(item["status"] == "missing" for item in payload["missing"]))
        create = self.run_cli("ul", "create", sid, "--apply")
        self.assertEqual(create.returncode, 0, create.stderr)
        ul_sid = next(key for key, item in json.loads(index_path.read_text()).items()
                      if item.get("channel") == "ul")
        publish = self.run_cli("ul", "publish", ul_sid, "--apply")
        self.assertEqual(publish.returncode, 1)
        self.assertIn(sid, json.loads(index_path.read_text()))

    def test_model_grouping_mock_sends_only_name_description_and_rejects_failure(self) -> None:
        code = r'''
import json
from skillhub.config import suggest_model_groups
items = [{"sid":"alpha--11111111","name":"alpha","description":"first"},
         {"sid":"beta--22222222","name":"beta","description":"second"}]
def runner(argv, prompt):
    assert "alpha" in prompt and "beta" in prompt
    assert "alpha--11111111" not in prompt and "beta--22222222" not in prompt
    return json.dumps({"groups":[{"name":"mock","members":["alpha","beta"]}]})
result = suggest_model_groups(items, "codex", "mock", runner=runner)
assert result["sent_fields"] == ["name", "description"]
try:
    suggest_model_groups(items, "codex", "mock", runner=lambda argv, prompt: '{"groups":[{"name":"bad","members":["not-input"]}]}')
except ValueError:
    pass
else:
    raise AssertionError("untrusted model member accepted")
print("ok")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=WS, env=self.env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_codex_exec_is_not_a_safe_model_protocol(self) -> None:
        config = self.tmp / "codex-model.toml"
        config.write_text('model = "fixture-model"\n', encoding="utf-8")
        env = dict(self.env)
        env.update({"SKILLHUB_MODEL_FILE_codex": str(config), "PATH": str(self.tmp / "empty-path")})
        code = r'''
from skillhub.config import discover_models, suggest_model_groups
row = next(x for x in discover_models()["agents"] if x["agent"] == "codex")
assert row["models"] == ["fixture-model"]
assert not row["callable"] and not row["safe_to_call"]
assert "无工具" in row["reason"]
try:
    suggest_model_groups([{"sid":"x--11111111","name":"x","description":"d"}], "codex", "fixture-model")
except ValueError:
    pass
else:
    raise AssertionError("unsafe codex exec path was callable")
print("ok")
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=WS, env=env,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(result.stdout.strip(), "ok")

    def test_direct_model_api_has_no_tools_and_only_sends_summaries(self) -> None:
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.headers.get("Authorization"), json.loads(self.rfile.read(length))))
                payload = {"choices": [{"message": {"content": json.dumps(
                    {"groups": [{"name": "fixture", "members": ["alpha"]}]}
                )}}]}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions"
            config = self.tmp / "codex-model.toml"
            config.write_text(
                'model = "fixture-model"\n'
                "[skillhub_model_api]\n"
                f'endpoint = "{endpoint}"\n'
                'api_key_env = "SKILLHUB_TEST_MODEL_KEY"\n',
                encoding="utf-8",
            )
            env = dict(self.env)
            env.update({"SKILLHUB_MODEL_FILE_codex": str(config),
                        "SKILLHUB_TEST_MODEL_KEY": "fixture-secret"})
            code = r'''
from skillhub.config import discover_models, suggest_model_groups
row = next(x for x in discover_models()["agents"] if x["agent"] == "codex")
assert row["callable"] and row["protocol"] == "openai-chat-json"
result = suggest_model_groups([{"sid":"alpha--11111111","name":"alpha","description":"first"}], "codex", "fixture-model")
assert result["suggestions"][0]["members"] == ["alpha--11111111"]
print("ok")
'''
            result = subprocess.run([sys.executable, "-c", code], cwd=WS, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(result.stdout.strip(), "ok")
            self.assertEqual(len(received), 1)
            authorization, body = received[0]
            self.assertEqual(authorization, "Bearer fixture-secret")
            self.assertEqual(body["tools"], [])
            self.assertEqual(body["tool_choice"], "none")
            self.assertEqual(body["model"], "fixture-model")
            self.assertIn("alpha", body["messages"][0]["content"])
            self.assertNotIn("alpha--11111111", body["messages"][0]["content"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_opencode_existing_provider_is_reused_without_secret_exposure(self) -> None:
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.headers.get("Authorization"), json.loads(self.rfile.read(length))))
                payload = {"choices": [{"message": {"content": json.dumps(
                    {"groups": [{"name": "fixture", "members": ["alpha"]}]}
                )}}]}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
            config = self.tmp / "opencode.jsonc"
            config.write_text(json.dumps({"provider": {"fixture": {
                "npm": "@ai-sdk/openai-compatible",
                "options": {"baseURL": base_url, "apiKey": "{env:SKILLHUB_PROVIDER_KEY}"},
                "models": {"fixture-model": {"name": "Fixture"}},
            }}}), encoding="utf-8")
            env = dict(self.env)
            env.update({"SKILLHUB_MODEL_FILE_opencode": str(config),
                        "SKILLHUB_PROVIDER_KEY": "fixture-secret"})
            code = r'''
import json
from skillhub.config import discover_models, suggest_model_groups
row = next(x for x in discover_models()["agents"] if x["agent"] == "opencode")
assert row["models"] == ["fixture-model"] and row["callable_models"] == ["fixture-model"]
assert row["callable"] and "fixture-secret" not in json.dumps(row)
assert "api_key" not in row
result = suggest_model_groups([{"sid":"alpha--11111111","name":"alpha","description":"first"}], "opencode", "fixture-model")
assert result["protocol"] == "openai-chat-json"
print("ok")
'''
            result = subprocess.run([sys.executable, "-c", code], cwd=WS, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(result.stdout.strip(), "ok")
            self.assertEqual(len(received), 1)
            authorization, body = received[0]
            self.assertEqual(authorization, "Bearer fixture-secret")
            self.assertEqual(body["tools"], [])
            self.assertEqual(body["tool_choice"], "none")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_claude_existing_settings_are_reused_without_secret_exposure(self) -> None:
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib handler API
                length = int(self.headers.get("Content-Length", "0"))
                received.append((self.headers.get("x-api-key"), json.loads(self.rfile.read(length))))
                payload = {"content": [{"type": "text", "text": json.dumps(
                    {"groups": [{"name": "fixture", "members": ["alpha"]}]}
                )}]}
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args):
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}/anthropic"
            config = self.tmp / "claude-settings.json"
            config.write_text(json.dumps({"env": {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": "fixture-secret",
                "ANTHROPIC_MODEL": "fixture-model",
            }}), encoding="utf-8")
            env = dict(self.env)
            env["SKILLHUB_MODEL_FILE_claude"] = str(config)
            code = r'''
import json
from skillhub.config import discover_models, suggest_model_groups
row = next(x for x in discover_models()["agents"] if x["agent"] == "claude")
assert row["models"] == ["fixture-model"] and row["callable_models"] == ["fixture-model"]
assert row["callable"] and "fixture-secret" not in json.dumps(row)
result = suggest_model_groups([{"sid":"alpha--11111111","name":"alpha","description":"first"}], "claude", "fixture-model")
assert result["protocol"] == "anthropic-messages-json"
print("ok")
'''
            result = subprocess.run([sys.executable, "-c", code], cwd=WS, env=env,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(result.stdout.strip(), "ok")
            self.assertEqual(len(received), 1)
            api_key, body = received[0]
            self.assertEqual(api_key, "fixture-secret")
            self.assertEqual(body["tools"], [])
            self.assertNotIn("alpha--11111111", body["messages"][0]["content"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
