// 概览：状态条、需要处理的事项、各 agent 卡片。
import { S, navigate } from "./state.js";
import { esc, color, stateLabel, gatedRisks } from "./util.js";

const PROBLEM_STATES = ["conflict", "broken", "copy_broken", "store_drift", "copy_drift"];

export function renderStatusbar() {
  const d = S.data, s = d.summary;
  document.getElementById("statusbar").innerHTML =
    `<span class="stat">skills<b>${s.skills}</b></span>` +
    `<span class="stat ${s.risky ? "warn" : ""}">风险<b>${s.risky}</b></span>` +
    `<span class="stat">agents<b>${s.agents}</b></span>` +
    `<span class="stat">投影<b>${s.linked_total}</b></span>` +
    `<span class="stat">MCP<b>${s.mcp_servers}</b></span>` +
    `<span class="stat">备份<b>${s.backups}</b></span>` +
    (d.read_only ? `<span class="stat ro" title="skillhub gui --read-only：所有写操作已禁用">只读模式</span>` : "") +
    `<span class="spacer"></span><span>数据生成于 ${esc(d.generated_at)}</span>`;
}

/** 需要处理的事项，每项带跳转目标。 */
export function attentionItems() {
  const d = S.data, items = [];
  for (const agent of d.agents) {
    for (const st of PROBLEM_STATES) {
      const n = d.skills.filter(s => s.states[agent.name] === st).length;
      if (n) items.push({ level: st === "conflict" || st === "copy_drift" ? "warn" : "bad",
        text: `${agent.name}：${n} 个${stateLabel(st)}`, tab: "sk", params: { agent: agent.name, state: st } });
    }
  }
  const gated = d.skills.filter(s => gatedRisks(s.risks).length);
  if (gated.length) items.push({ level: "warn", text: `${gated.length} 个 skill 命中风险门禁（GUI 不能投影，需 CLI --allow-risky）`,
    tab: "sk", params: { risk: true } });
  const plan = d.trash_plan || [];
  const expired = plan.filter(x => x.type === "remove").length;
  const invalid = plan.filter(x => x.type === "invalid").length;
  if (expired) items.push({ level: "info", text: `${expired} 个回收项已到期，可清理`, tab: "bk" });
  if (invalid) items.push({ level: "bad", text: `${invalid} 个回收项无法验证`, tab: "bk" });
  return items;
}

export function renderOverview() {
  const d = S.data;
  const el = document.getElementById("tab-ov");
  const items = attentionItems();
  const attn = items.length
    ? `<div class="attn">${items.map((x, i) => `<div class="attn-item"><span class="badge ${x.level}">${x.level === "bad" ? "问题" : x.level === "warn" ? "注意" : "提示"}</span>
        <span class="msg">${esc(x.text)}</span><button class="link" data-attn="${i}">查看 →</button></div>`).join("")}</div>`
    : `<div class="attn-ok">✓ 没有冲突、断链、漂移或到期回收项。</div>`;
  const cards = d.agents.map(a => {
    const tot = Math.max(a.total_in_store, 1);
    const pct = n => (n / tot * 100).toFixed(1) + "%";
    const problems = a.broken + a.store_drift + a.copy_drift;
    return `<div class="card">
      <h3><span class="agent-dot" style="background:${color(a.name)}"></span>${esc(a.name)}
        <span class="badge ${a.mode === "copy" ? "warn" : ""}">${esc(a.mode)}${a.nested ? " · nested" : ""}</span>
        ${a.dir_exists ? "" : `<span class="badge" title="首次投影时创建">目录未创建</span>`}</h3>
      <div class="dir">${esc(a.skill_dir)}</div>
      <div class="nums">
        <span><b>${a.linked}</b>已投影</span>
        <span class="${a.conflict ? "hot" : ""}"><b>${a.conflict}</b>冲突</span>
        <span class="${problems ? "bad" : ""}"><b>${problems}</b>断链/漂移</span>
        <span><b>${a.not_linked}</b>未投影</span>
      </div>
      <div class="bar" title="已投影 ${a.linked} / 冲突 ${a.conflict} / 中央库 ${a.total_in_store}">
        <i style="width:${pct(a.linked)};background:var(--green)"></i>
        <i style="width:${pct(a.conflict)};background:var(--amber)"></i>
        <i style="width:${pct(problems)};background:var(--red)"></i>
      </div>
      <div class="meta">${a.mcp_supported ? `<span class="badge info" title="${esc(a.mcp_target)}">MCP 支持</span>` : `<span class="badge">MCP 不支持</span>`}
        <button class="link small" data-agent-skills="${esc(a.name)}">查看 skills →</button></div>
    </div>`;
  }).join("");
  el.innerHTML = `<div class="panel"><h3>需要处理 ${items.length ? `<span class="badge bad">${items.length}</span>` : ""}</h3>${attn}</div>
    <div class="section-head"><h2>Agents</h2><p>数字是当前中央库的实时状态，不是扫描缓存。</p></div>
    <div class="cards">${cards}</div>
    <p class="note">写操作都先预览再确认；GUI 不能 --allow-risky，全库 rollback 请用 CLI。只读模式：<span class="mono">skillhub gui --read-only</span>。
      中央库 <span class="mono">${esc(d.paths.store)}</span></p>`;
  el.onclick = e => {
    const a = e.target.closest("[data-attn]");
    if (a) { const item = items[Number(a.dataset.attn)]; navigate(item.tab, item.params); return; }
    const s = e.target.closest("[data-agent-skills]");
    if (s) navigate("sk", { agent: s.dataset.agentSkills });
  };
}

/** 导航上的问题计数。 */
export function attentionCount() { return attentionItems().filter(x => x.level !== "info").length; }
