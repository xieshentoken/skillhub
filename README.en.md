# skillhub — Centralized Skill & MCP Management for Local AI Agents

**English** | [简体中文](README.md)

Centrally manage **skills** and **MCP configurations** for multiple agents on one machine (pi / codex / opencode / workbuddy / claude / hermes): a single authoritative copy lives in a central store, and each agent directory only holds projections (symlinks or copies) or generated config fragments.

## Install

```bash
# Local install (editable mode; changes take effect immediately)
pip install -e .
```

> The pip bundled with your system Python may be too old to support editable installs from `pyproject.toml`.
> Upgrade it first: `python3 -m pip install --upgrade pip`

Once installed, use the `skillhub` command directly. Without installing, run `python3 -m skillhub` from the repository root.

## Directory Layout

```
<repo-root>/
  skillhub/                # Python package
    config.py              # agent definitions, central store paths, risk patterns
    scan.py                # scan each agent's SKILL.md
    store.py               # central store: import, dedupe, index
    adapters.py            # projection engine: link/unlink, conflicts, backup/rollback
    mcp.py                 # MCP config layer: central definitions + per-agent renderers
    webgui.py              # local web GUI server (read-only, 127.0.0.1 only)
    gui.html               # GUI single-page frontend
    cli.py                 # command-line entry point
  tests/test_projection.py # integration tests (temp dirs only, never touches real env)

~/.skillhub/               # central store data (user home)
  store/<name>--<md5-8>/   # single authoritative skill copy
  index.json               # skill_id -> manifest (source, version, risk)
  mcp/index.json           # MCP server definitions (secrets as {{env:VAR}} refs, zero plaintext)
  backups/<ts>/            # pre-projection backups
```

## Usage

```bash
cd <repo-root>

# Scan all agents' skill counts
python3 -m skillhub scan

# Import into the central store (dry-run by default; --apply writes. Copy-only, agent dirs untouched)
python3 -m skillhub import --apply

# List the central store
python3 -m skillhub list [--risky]

# Project a skill to agents (--dry-run previews first)
python3 -m skillhub link dws --agents pi,codex --dry-run   # preview
python3 -m skillhub link dws --agents pi,codex              # run if no conflicts
python3 -m skillhub link dws --agents pi --force            # back up and replace on conflict

# Bulk projection (--all everything / --all-missing only unprojected ones)
python3 -m skillhub link --all --agents workbuddy,codex --dry-run
python3 -m skillhub link --all-missing --agents workbuddy,codex --force

# Unlink a projection
python3 -m skillhub unlink dws --agents pi

# Show projection status per agent
python3 -m skillhub status [--agent pi] [--verbose]

# Backups and rollback
python3 -m skillhub backups
python3 -m skillhub rollback <timestamp>

# Local web GUI (read-only: agent overview / central store / MCP / backups)
python3 -m skillhub gui [--port 8317] [--no-browser]
```

`link`/`unlink` accept a full skill_id or a name prefix.

## Web GUI

`skillhub gui` starts a local web console (opens the browser automatically):

- **Agent overview**: per-agent dir, projection mode (symlink/copy/nested), linked/conflict/unprojected counts with ratio bars, MCP support;
- **Central store**: search + filter by agent/state/risk; each row shows per-agent projection state as colored dots; click a row for details (manifest, per-agent target paths, file listing);
- **MCP servers**: central definitions (transport, target, env/header variable names, agents);
- **Backups**: snapshot sizes and whether replaced conflict dirs are included.

Safety: **read-only** — binds `127.0.0.1` only, GET only, no write operations;
projection/import/generation remain CLI-only. The GUI reads the same `~/.skillhub`
data as the CLI; hit refresh for the latest state.

## MCP Configuration Layer

Collect each agent's MCP servers into the central store, then render each agent's own config format.

```bash
# Import MCP server definitions from workbuddy (dry-run by default; --apply writes the store)
python3 -m skillhub mcp import [--apply]

# List central MCP server definitions
python3 -m skillhub mcp list

# Render MCP config for target agents (dry-run by default)
python3 -m skillhub mcp generate qcc-company --agents pi,codex --dry-run   # preview
python3 -m skillhub mcp generate qcc-company --agents pi,codex             # run
python3 -m skillhub mcp generate qcc-company --agents pi --resolve         # inject literal values from env
```

**Secrets (zero plaintext by default)**: secrets detected at import time (Bearer tokens etc.) are never written to the store; they become `{{env:QCC_COMPANY_TOKEN}}` placeholders, and the import output tells you which environment variables to set. In rendered agent configs:
- claude uses `{env:VAR}` (officially expanded)
- codex / opencode / pi / workbuddy use `${VAR}` references
- **if a target agent does not expand environment variables** (e.g. pi's host-core treats headers as literal),
  `export QCC_COMPANY_TOKEN='...'` in your shell first, then render with `--resolve` to inject literal values.

Rendered target formats:

| agent | target file | write mode |
|---|---|---|
| pi | `~/.agents/servers/<id>.json` | one McpConfig file per server |
| codex | `~/.codex/config.toml` | append `[[mcp_servers.<id>]]` section (skip if exists) |
| grok | `~/.grok/config.toml` | append `[[mcp_servers.<id>]]` section (same shape as codex) |
| workbuddy | `~/.workbuddy/mcp.json` | merge into `mcpServers` (original format, no type) |
| claude | `~/.claude.json` | merge into `mcpServers` (type=http) |
| opencode | `~/.config/opencode/opencode.jsonc` | merge into `mcp` (type=remote) |
| hermes | — | no MCP config support found, skipped |

## Security Design

- **Import only copies**: skills are copied into the central store; agent source dirs are never touched.
- **Projections are previewable**: `link`/`unlink` with `--dry-run` shows the plan only; on execution, conflicts (an existing non-store dir at the target) are refused unless `--force`.
- **Backup before every projection**: `backups/<timestamp>/` saves store + index + replaced conflict dirs, restorable via `rollback`.
- **Bulk projections back up once**: `link --all` / `--all-missing` take a single full backup before the batch starts, then each skill skips its own backup. Per-skill backups would copy the whole store hundreds of times (measured 196 × 59M ≈ 11GB).
- **Bulk stops on any conflict**: a batch writes nothing while any conflict exists; add `--force` explicitly. Conflicting skills are listed so they can be handled individually.
- **Copy mode (workbuddy) re-projection counts as conflict**: a copied artifact cannot be verified as a store projection (unlike a symlink), so re-running `link` on the same skill reports a conflict and needs `--force` to back up and replace.
- **Dedupe**: skill_id = `<name>--<md5-8>`; same name with different content does not collide; identical content merges source-agent records.
- **Scan follows symlinks**: `Path.rglob` does not descend into symlinked directories, and symlink-mode agents (pi / claude / grok) store each skill as a symlink into the central store — plain `rglob` always returns 0 there. `scan` now walks manually (with realpath cycle protection) and skips hidden dirs (e.g. codex's `.system` built-ins) and third-party dirs like `node_modules`.
- **MCP secrets zero plaintext**: the store's `mcp/index.json` only holds `{{env:VAR}}` references; generation also writes references by default; `--resolve` injects literal values only for agents that cannot expand env vars, and the store stays plaintext-free.

## Configurable Environment Variables

- `SKILLHUB_HOME`: central store location, default `~/.skillhub`
- `SKILLHUB_AGENT_DIR_<agent>`: override an agent's skill dir (testing/custom)
- `SKILLHUB_MCP_FILE_<agent>`: override an agent's MCP config file (testing)
- `SKILLHUB_MCP_PI_DIR`: override pi's servers dir (testing)

## Agent Adapters

| agent | dir | projection | notes |
|---|---|---|---|
| pi | `~/.agents/skills` | symlink | |
| codex | `~/.codex/skills` | symlink | |
| opencode | `~/.config/opencode/skills` | symlink | |
| workbuddy | `~/.workbuddy/skills` | copy | for agents that don't trust symlinks |
| claude | `~/.claude/skills` | symlink | dir doesn't exist by default, created on first link |
| grok | `~/.grok/skills` | symlink | dir doesn't exist by default; grok also compat-scans `~/.agents/skills` and `~/.claude/skills` |
| hermes | App Support hermes-home/skills | symlink | categorized layout `<category>/<skill>` |

### Grok compat scanning and precedence (verified on v1.0.13)

Besides `~/.grok/skills` (plus project `./.grok/skills` and `[skills] paths` entries), Grok
automatically reads `~/.agents/skills`, `~/.claude/skills`, `~/.cursor`, etc.
(disable per vendor with `[compat.claude] skills = false`). Verified behavior:

- **Same-named skills register once** — no duplicates (the `grok inspect --json` count stays the same).
- **`~/.grok/skills` wins**: on a name clash it overrides the compat dir, and `inspect` reports
  `source.path` pointing at it.

So Grok can already read skillhub's pi projection through `~/.agents/skills` without any setup,
but projecting explicitly into `~/.grok/skills` removes the dependency on pi
(unlinking pi then no longer affects Grok). Recommended: `skillhub link --all --agents grok`.

## Tests

```bash
python3 tests/test_projection.py   # 12 cases, temp dirs only, never touches the real environment
```
