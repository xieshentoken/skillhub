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

The project requires Python 3.11+ (it uses the standard-library `tomllib` for Codex/Grok configuration).

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
    webgui.py              # local web GUI server (127.0.0.1; view + link/unlink write endpoints)
    gui.html               # GUI single-page frontend
    cli.py                 # command-line entry point
  tests/test_projection.py # integration tests (temp dirs only, never touches real env)

~/.skillhub/               # central store data (user home)
  store/<name>--<md5-8>/   # single authoritative skill copy
  index.json               # skill_id -> manifest (source, version, risk)
  mcp/index.json           # MCP server definitions (secrets as {{env:VAR}} refs, zero plaintext)
  backups/<ts>/            # pre-projection backups
  trash/<entry>/           # per-skill retired versions (default 7 days)
  groups.json              # central grouping relations
  distributions.json       # per-agent group and explicit-skill source selections
  events.jsonl             # redacted audit events
```

## Usage

```bash
cd <repo-root>

# Scan all agents' skill counts
python3 -m skillhub scan

# Import into the central store (preview by default; --apply writes. Copy-only, agent dirs untouched)
python3 -m skillhub import
python3 -m skillhub import --apply

# List the central store
python3 -m skillhub list [--risky]

# Project a skill to agents (preview by default; --apply writes)
python3 -m skillhub link dws --agents pi,codex --dry-run   # preview (compatibility alias)
python3 -m skillhub link dws --agents pi,codex --apply
python3 -m skillhub link dws --agents pi --apply --force   # back up and replace on conflict

# Bulk projection (--all everything / --all-missing only unprojected ones)
python3 -m skillhub link --all --agents workbuddy,codex
python3 -m skillhub link --all-missing --agents workbuddy,codex --apply --force

# Unlink a projection
python3 -m skillhub unlink dws --agents pi --apply

# Show projection status per agent
python3 -m skillhub status [--agent pi] [--verbose]

# Formal / UL version workflow (preview by default; UL is an independent copy)
python3 -m skillhub ul create <formal-sid> --apply
python3 -m skillhub ul edit <ul-sid> --file run.py --text $'print(2)\n' --apply
python3 -m skillhub ul trial <formal-sid> --agents claude --apply
python3 -m skillhub ul publish <ul-sid> --apply

# Per-skill trash and static dependency diagnosis
python3 -m skillhub trash list --json
python3 -m skillhub trash restore <entry> --apply
python3 -m skillhub trash cleanup --apply
python3 -m skillhub diagnose <sid> --json

# Groups and selective distribution
python3 -m skillhub group set release --members <sid1>,<sid2> --apply
python3 -m skillhub group distribute --groups release --agents pi,codex --replace --apply
# Recompute each agent from its saved source selection
python3 -m skillhub group distribute --agents pi,codex --apply

# Discover configured local models (does not call a model)
python3 -m skillhub models --json

# Backups and rollback (preview by default; --apply restores/cleans)
python3 -m skillhub backups
python3 -m skillhub rollback <timestamp> --apply
python3 -m skillhub cleanup --keep 3 --apply

# Local web GUI (view + explicit link/unlink actions)
python3 -m skillhub gui [--port 8317] [--no-browser] [--read-only]
```

`link`/`unlink` accept a full skill_id or a name prefix.

## Web GUI

`skillhub gui` starts a local web console (opens the browser automatically):

- **Agent overview**: per-agent dir, projection mode (symlink/copy/nested), linked/conflict/unprojected counts with ratio bars, MCP support;
- **Central store**: search + filter by agent/state/risk; each row shows per-agent projection state as colored dots; click a row for details (manifest, per-agent target paths, file listing) and **run link / unlink right there** (same backup and conflict protection as the CLI);
- **Import / version workflow**: import a selected agent source, compare formal and UL versions, edit only approved UL text files, trial UL on selected agents, publish with a skill-scoped backup, and restore retired versions from the per-skill trash;
- **Groups / distribution**: drag or checkbox-select skills into manual groups and distribute per-agent saved group/explicit-skill sources; changing a group does not write agents, and the next preview resolves current members while retaining explicit selections. Formal/UL trial projections are deduplicated by target so replace cannot remove the active UL accidentally;
- **Diagnostics / models**: static tool and environment checks block publish on explicit missing or unconfirmed inferred dependencies; configured models are shown, and only a verified existing-agent API or explicit tool-free direct API can be called after an explicit user click;
- **MCP servers**: central definitions (transport, target, env/header variable names, agents);
- **Backups / trash**: snapshot listing and per-skill trash restore; full-store rollback stays on the CLI;
- **Sortable columns + CSV export**: click a column header to sort; export the current filtered view as CSV;
- **Auto refresh**: opt-in periodic refresh, handy when mixing GUI and CLI operations.

Safety: binds `127.0.0.1` only. The process prints a one-time token URL; `GET /` without that
token does not issue a session cookie. GET requests check loopback Host and the session cookie
(same-origin GET fetch omits Origin, so a missing Origin is allowed; a present Origin must match).
POST requests require a matching loopback Origin. Write endpoints require `application/json`, a size
limit, and explicit `apply`. Link/unlink also preview first. The GUI rejects `allow_risky`, MCP
`command`/`args` edits, editor settings, and full-store rollback (use the CLI). Publish, MCP generate,
replace-distribute, and cleanup require a confirmation phrase. `--read-only` disables all POST
handlers. The risk gate still applies and cannot be bypassed from the page. The GUI reads the same
`~/.skillhub` data as the CLI.

## MCP Configuration Layer

Collect each agent's MCP servers into the central store, then render each agent's own config format.

```bash
# Import MCP server definitions from workbuddy (preview by default; --apply writes the store)
python3 -m skillhub mcp import [--apply]

# List central MCP server definitions
python3 -m skillhub mcp list

# Render MCP config for target agents (preview by default)
python3 -m skillhub mcp generate qcc-company --agents pi,codex             # preview
python3 -m skillhub mcp generate qcc-company --agents pi,codex --apply     # run
python3 -m skillhub mcp generate qcc-company --agents pi --resolve         # inject literal values from env
```

**Secrets (zero plaintext by default)**: secrets detected at import time (Bearer tokens etc.) are never written to the store; they become `{{env:QCC_COMPANY_TOKEN}}` placeholders, and the import output tells you which environment variables to set. In rendered agent configs:
- Claude uses the officially supported `${VAR}` references;
- Codex/Grok use `${VAR}`, and Pi/WorkBuddy retain `${VAR}`;
- OpenCode uses the current direct `mcp.<id>` server map: stdio is `type=local` with a `command` array and `environment`; HTTP is `type=remote` with `url`/`headers`, and environment references use `{env:VAR}`.
- **if a target agent does not expand environment variables** (e.g. pi's host-core treats headers as literal),
  `export QCC_COMPANY_TOKEN='...'` in your shell first, then render with `--resolve` to inject literal values.

Rendered target formats:

| agent | target file | write mode |
|---|---|---|
| pi | `~/.agents/servers/<id>.json` | one McpConfig file per server |
| codex | `~/.codex/config.toml` | write `[mcp_servers.<id>]`; HTTP uses `http_headers` / `env_http_headers` / `bearer_token_env_var` |
| grok | `~/.grok/config.toml` | same `[mcp_servers.<id>]` structure as Codex |
| workbuddy | `~/.workbuddy/mcp.json` | merge into `mcpServers` without forcing a `type` |
| claude | `~/.claude.json` | merge into `mcpServers` (`stdio`/`http`, `${VAR}` references) |
| opencode | `~/.config/opencode/opencode.jsonc` | merge into the current official direct `mcp` map (`local`/`remote`) |
| hermes | — | no MCP config support found, skipped |

## Security Design

- **Import only copies**: skills are copied into the central store; agent source dirs are never touched.
- **All writes default to preview**: `import`, `link`, `unlink`, `add`, `cleanup`, `rollback`, and MCP import/generate only show a plan unless `--apply` is explicit; `--dry-run` is a compatibility alias and is mutually exclusive with `--apply`. `export` requires an explicit `--out`.
- **Backup before every projection**: `backups/<timestamp>/` saves store + index + replaced conflict dirs, restorable via `rollback --apply`; store snapshots are independent copies and do not share inodes with the live store, so editing a projection cannot silently change a backup. Listed sizes are logical sizes; actual reclaim depends on the filesystem. Auto-pruning keeps the latest `SKILLHUB_MAX_BACKUPS` (default 10).
- **Bulk projections back up once**: `link --all` / `--all-missing` take a single full backup before the batch starts, then each skill skips its own backup. Per-skill backups would copy the whole store hundreds of times (measured 196 × 59M ≈ 11GB).
- **Bulk stops on any conflict / or skips**: a batch writes nothing while any conflict exists; add `--force` explicitly, or use `--skip-conflicts` to skip conflicted items and continue with the rest — the output lists conflicts and skipped items separately.
- **Copy-mode artifacts carry a marker**: a `.skillhub-projection.json` marker (sid + md5) is written inside copy projections, so re-running `link` recognizes "this is our copy" and skips idempotently instead of misreporting a conflict; only a tampered/missing marker is treated as a conflict.
- **Broken-link detection**: once a symlink projection's store source is deleted, `status` honestly reports `broken` (instead of lying "linked"), and `doctor` lists all broken projections with fix suggestions (re-link or unlink).
- **Risk gate**: skills flagged high-risk (default pattern: docs/scripts containing `sudo`; tunable via `SKILLHUB_RISK_GATE`) are blocked at link time unless `--allow-risky` is given; `--force` only replaces conflicts. Blocked items are flagged in `status`/`doctor`/GUI.
- **Drift is refused**: if live store files no longer match the index digest, the state is `store_drift` and link/unlink will not project or delete anything; modified WorkBuddy-style copies are `copy_drift` and are not deleted by default.
- **MCP rollback**: MCP import/generate records original paths, missing state, and independent copies in `backups/<ts>/mcp/targets.json`; the same `rollback <ts> --apply` restores MCP files, while parse failures leave the original untouched.
- **Formal / UL versions**: a UL is an independent copy with an explicit relation to one formal version; trial switches only selected agents, and publish backs up only the related versions, projections, and index relations. A publish backup is not accepted by the full-library rollback path, so unrelated skills cannot be overwritten accidentally.
- **Per-skill trash**: replacing or publishing a version moves the old copy into `trash/<entry>/` with integrity metadata and a retention deadline. Restore is scoped to the same logical role; cleanup requires explicit confirmation.
- **Static diagnosis**: only local file text, `shutil.which`, and environment presence are inspected. No skill, script, service, shell command, or network action is executed. Explicitly missing or inferred/unconfirmed dependencies block publish until a user confirms them.
- **Model suggestions**: model names are discovered from configured files without executing commands. `codex exec --json --sandbox read-only` is not treated as tool-free because its sandbox can still expose files, shell, MCP, or automatic context. Suggestions are callable only through verified existing-agent API config (currently Claude messages, OpenCode/Pi OpenAI-compatible or Anthropic custom providers) or an explicitly configured HTTPS/loopback endpoint; requests carry no tools, secrets are never returned to the GUI, and results are validated against selected names before a second explicit apply confirmation. Failed model calls never modify groups.
- **Dedupe**: skill_id = `<name>--<md5-8>`; same name with different content does not collide; identical content merges source-agent records.
- **Scan follows symlinks**: `Path.rglob` does not descend into symlinked directories, and symlink-mode agents (pi / claude / grok) store each skill as a symlink into the central store — plain `rglob` always returns 0 there. `scan` now walks manually (with realpath cycle protection) and skips hidden dirs (e.g. codex's `.system` built-ins) and third-party dirs like `node_modules`.
- **MCP secrets zero plaintext**: the store's `mcp/index.json` only holds `{{env:VAR}}` references; import checks sensitive fields, URL query parameters, and args; generation writes references by default; `--resolve` is required for literal values, and the store remains plaintext-free. Parse failures stop before the original config is overwritten.

## Configurable Environment Variables

- `SKILLHUB_HOME`: central store location, default `~/.skillhub`
- `SKILLHUB_AGENT_DIR_<agent>`: override an agent's skill dir (testing/custom)
- `SKILLHUB_MCP_FILE_<agent>`: override an agent's MCP config file (testing)
- `SKILLHUB_MCP_PI_DIR`: override pi's servers dir (testing)
- `SKILLHUB_RISK_GATE`: risk-gate keywords (comma-separated), default `sudo`
- `SKILLHUB_MAX_BACKUPS`: number of backups to keep, default `10`
- `SKILLHUB_TRASH_RETENTION_DAYS`: default per-skill trash retention, default `7`
- `SKILLHUB_MODEL_FILE_<agent>`: read-only model config override; configuration commands are never executed
- `SKILLHUB_MODEL_API_ENDPOINT_<agent>` / `SKILLHUB_MODEL_API_KEY_ENV_<agent>`: explicit tool-free OpenAI-compatible `/chat/completions` endpoint and credential environment-variable name (HTTPS or loopback HTTP only)

For example: `export SKILLHUB_MODEL_API_ENDPOINT_codex=https://api.example.com/v1/chat/completions` and
`export SKILLHUB_MODEL_API_KEY_ENV_codex=OPENAI_API_KEY`. The model name must still be discovered from
`SKILLHUB_MODEL_FILE_codex` (default `~/.codex/config.toml`); skillhub never reads a plaintext key from
the agent config.

By default it also reads OpenCode `~/.config/opencode/opencode.jsonc`
(`provider.*.options.baseURL/apiKey/models`) and Pi `~/.pi/agent/models.json`
(`providers.*.baseUrl/api/apiKey/models`). Claude reuses only its existing
Anthropic-compatible `env` settings from `~/.claude/settings.json`. Credentials stay in backend
request memory and are never returned to the GUI.

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
python3 tests/test_projection.py   # temp dirs only, never touches the real environment
```
