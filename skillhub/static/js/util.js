import { S } from "./state.js";

export const AGENT_COLORS = {pi:"#5e5ce6", codex:"#34c759", opencode:"#ff9f0a", workbuddy:"#64d2ff",
  claude:"#bf5af2", grok:"#8e8e93", hermes:"#30d158"};

/** 投影状态：label 为中文名，glyph 用于非颜色区分，order 用于图例顺序。 */
export const STATES = {
  linked:      {label:"已投影",     glyph:"✓"},
  trial:       {label:"试用 UL",    glyph:"T"},
  conflict:    {label:"冲突",       glyph:"!"},
  copy_drift:  {label:"copy 漂移",  glyph:"≈"},
  store_drift: {label:"中央库漂移", glyph:"~"},
  broken:      {label:"断链",       glyph:"✕"},
  copy_broken: {label:"copy 断链",  glyph:"✕"},
  not_linked:  {label:"未投影",     glyph:"·"},
};
export const stateLabel = st => (STATES[st] || {}).label || st;

export const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;"}[c]));
export const fmtSize = n => n < 0 ? "" : n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB";
export const color = a => AGENT_COLORS[a] || "#94a3b8";

export function stateChip(st, agent) {
  const info = STATES[st] || {label: st, glyph: "?"};
  const title = agent ? `${agent}: ${info.label}` : info.label;
  return `<span class="st st-${esc(st)}" title="${esc(title)}" aria-label="${esc(title)}">${info.glyph}</span>`;
}

export function agentName(name) {
  return `<span class="agent-dot" style="background:${color(name)}"></span> ${esc(name)}`;
}

export function skillById(sid) { return (S.data?.skills || []).find(s => s.id === sid); }
export function skillName(sid) { return skillById(sid)?.name || sid; }

/** 风险门禁命中的风险标签（GUI 不能放行这些 skill）。 */
export function gatedRisks(risks) {
  const gate = S.data?.risk_gate || [];
  return (risks || []).filter(r => gate.includes(r));
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** 逗号分隔输入 → 去空白的数组。 */
export const splitList = value => String(value || "").split(",").map(x => x.trim()).filter(Boolean);
