// MCP：中央定义只存环境引用；command/args 只能用 CLI 修改。
import { S, reload } from "./state.js";
import { esc, color, $ } from "./util.js";
import { toast, action, confirmWrite } from "./ui.js";

const REF_KEY = /^[A-Za-z_][A-Za-z0-9_.-]*$/;
const REF_VALUE = /^(?:\{\{env:[A-Za-z_][A-Za-z0-9_]*\}\}|\$\{[A-Za-z_][A-Za-z0-9_]*\}|\{env:[A-Za-z_][A-Za-z0-9_]*\}|Bearer\s+\{\{env:[A-Za-z_][A-Za-z0-9_]*\}\})$/;
const open = new Set(); // 刷新后保持展开状态

const refLines = value => Object.entries(value || {}).map(([k, v]) => k + "=" + v).join("\n");

/** 解析“每行 NAME=引用”，只允许完整的环境变量引用。 */
function parseRefs(text, label) {
  const output = {};
  for (const line of text.split("\n")) {
    const value = line.trim();
    if (!value) continue;
    const pos = value.indexOf("=");
    if (pos < 1) throw new Error(label + " 必须使用 NAME=环境引用 格式");
    const key = value.slice(0, pos).trim(), ref = value.slice(pos + 1).trim();
    if (!REF_KEY.test(key) || !REF_VALUE.test(ref)) throw new Error(label + " 只能填写完整的环境变量引用：" + value);
    output[key] = ref;
  }
  return output;
}

export function renderMcp() {
  const el = $("#tab-mc");
  const rows = S.data.mcp_servers;
  const sources = S.data.agents.filter(a => a.mcp_supported && a.name !== "pi");
  const targets = S.data.agents.filter(a => a.mcp_supported);
  const sourceList = sources.length ? sources : [{ name: "workbuddy" }];
  const preferred = sourceList.find(a => a.name === "workbuddy") || sourceList[0];
  const servers = rows.map(s => `<details class="mcp" data-mcp-row="${esc(s.id)}" ${open.has(s.id) ? "open" : ""}>
    <summary><b class="mono">${esc(s.id)}</b>${s.label && s.label !== s.id ? `<span class="dim">${esc(s.label)}</span>` : ""}
      <span class="badge info">${esc(s.transport)}</span>
      ${s.enabled ? `<span class="badge ok">启用</span>` : `<span class="badge">停用</span>`}
      <span class="grow" style="flex:1"></span>
      ${(s.agents || []).map(a => `<span style="color:${color(a)};font-size:12px">${esc(a)}</span>`).join(" ")}</summary>
    <div class="mcp-body">
      <div class="formrow"><label>标签 <input type="text" data-mcp-field="label" value="${esc(s.label || "")}" style="width:220px"></label>
        <label class="ck"><input type="checkbox" data-mcp-field="enabled" ${s.enabled ? "checked" : ""}>启用</label></div>
      ${s.transport === "http"
        ? `<div class="formrow"><label class="field">URL<input type="text" data-mcp-field="url" value="${esc(s.url || "")}"></label></div>`
        : `<div class="note">命令（只读，改 command/args 请用 CLI）</div><pre class="args">${esc(s.command || "")}${(s.args || []).length ? "\n" + esc(s.args.join("\n")) : ""}</pre>`}
      <div class="formrow">
        <label class="field">环境引用（每行 <span class="mono">NAME={{env:VAR}}</span>）<textarea data-mcp-field="env_refs">${esc(refLines(s.env_refs))}</textarea></label>
        <label class="field">请求头引用（每行 <span class="mono">Header={{env:VAR}}</span>）<textarea data-mcp-field="header_refs">${esc(refLines(s.header_refs))}</textarea></label>
      </div>
      <div class="formrow"><button data-mcp-save="${esc(s.id)}">预览保存字段</button></div>
      <div class="sect">生成到 agent 配置</div>
      <div class="formrow">${targets.map(a => `<label class="ck"><input type="checkbox" data-mcp-target data-agent="${esc(a.name)}" ${(s.agents || []).includes(a.name) ? "checked" : ""}>${esc(a.name)}</label>`).join("") || `<span class="note">没有可用目标 agent</span>`}
        <button class="danger-btn" data-mcp-generate="${esc(s.id)}">预览生成</button></div>
      <p class="note">明文密钥不会回显；这里只能追加/更新环境变量引用。生成需要输入 GENERATE 确认。</p>
    </div></details>`).join("");
  el.innerHTML = `<div class="section-head"><h2>MCP</h2><p>中央定义只存环境引用。command 与 args 请用 CLI 修改。</p></div>
    <div class="panel"><h3>导入 MCP 定义</h3>
      <div class="formrow"><label>来源 agent <select id="mcpImportSource">${sourceList.map(a => `<option ${a.name === preferred.name ? "selected" : ""}>${esc(a.name)}</option>`).join("")}</select></label>
        <button data-mcp-import>预览导入</button></div>
      <p class="note">只读取所选 agent 的现有配置；解析失败会停止，不覆盖中央库。</p></div>
    <div class="section-head"><h2 style="font-size:16px">Server（${rows.length}）</h2>
      ${rows.length ? `<button class="link small" data-mcp-expand>全部展开</button><button class="link small" data-mcp-collapse>全部折叠</button>` : ""}</div>
    ${servers || `<p class="empty">中央库还没有 MCP server 定义。</p>`}`;
}

export function bindMcp() {
  const el = $("#tab-mc");
  el.addEventListener("toggle", e => {
    const row = e.target.closest?.("[data-mcp-row]");
    if (row) { if (row.open) open.add(row.dataset.mcpRow); else open.delete(row.dataset.mcpRow); }
  }, true);
  el.addEventListener("click", e => {
    let b;
    if (e.target.closest("[data-mcp-expand]")) { el.querySelectorAll("details.mcp").forEach(d => { d.open = true; }); return; }
    if (e.target.closest("[data-mcp-collapse]")) { el.querySelectorAll("details.mcp").forEach(d => { d.open = false; }); return; }
    if ((b = e.target.closest("[data-mcp-import]"))) return action(b, async () => {
      const source = $("#mcpImportSource").value;
      const ad = await confirmWrite("/api/mcp/import", { source }, { title: "导入 MCP 定义" });
      if (ad) {
        toast(`已导入 ${ad.count ?? ""} 个 MCP 定义` + ((ad.envs || []).length ? `；请设置环境变量：${ad.envs.join(", ")}` : ""), "ok", 10000);
        await reload();
      }
    });
    if ((b = e.target.closest("[data-mcp-save]"))) return action(b, async () => {
      const row = b.closest("[data-mcp-row]");
      const field = name => row.querySelector(`[data-mcp-field="${name}"]`);
      const s = S.data.mcp_servers.find(item => item.id === b.dataset.mcpSave);
      const changes = { label: field("label").value, enabled: field("enabled").checked };
      if (s.transport === "http") changes.url = field("url").value;
      const env = parseRefs(field("env_refs").value, "环境引用");
      const headers = parseRefs(field("header_refs").value, "请求头引用");
      if (Object.keys(env).length) changes.env_refs = env;
      if (Object.keys(headers).length) changes.header_refs = headers;
      const ad = await confirmWrite("/api/mcp/edit", { sid: b.dataset.mcpSave, changes }, { title: "保存 MCP 字段" });
      if (ad) { toast("MCP 字段已保存", "ok"); await reload(); }
    });
    if ((b = e.target.closest("[data-mcp-generate]"))) return action(b, async () => {
      const row = b.closest("[data-mcp-row]");
      const agents = [...row.querySelectorAll("[data-mcp-target]:checked")].map(x => x.dataset.agent);
      if (!agents.length) { toast("请先选择至少一个目标 agent", "warn"); return; }
      const ad = await confirmWrite("/api/mcp/generate", { sid: b.dataset.mcpGenerate, agents }, { title: "生成 MCP 配置到所选 agent", phrase: "GENERATE" });
      if (ad) { toast("MCP 配置已生成", "ok"); await reload(); }
    });
  });
}
