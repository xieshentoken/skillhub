// 入口：数据加载、hash 路由、主题、自动刷新。
import { S, setHooks } from "./state.js";
import { esc, $, $$ } from "./util.js";
import { getJSON } from "./api.js";
import { modalOpen, toastError } from "./ui.js";
import { renderStatusbar, renderOverview, attentionCount } from "./overview.js";
import { renderSkills, setSkillFilters, bindSkills } from "./skills.js";
import { openSkill, bindDrawer } from "./drawer.js";
import { renderImports, bindImports } from "./imports.js";
import { renderGroups, bindGroups } from "./groups.js";
import { renderMcp, bindMcp } from "./mcp.js";
import { renderBackups, bindBackups } from "./backups.js";
import { renderModels, bindModels } from "./models.js";
import { renderLogs, bindLogs } from "./logs.js";
import { renderGraph } from "./graph.js";

const TABS = ["ov", "sk", "im", "gr", "mc", "bk", "md", "lg"];
// 含未保存表单的页面：后台自动刷新时不重绘，避免丢失输入。
const FORM_TABS = new Set(["gr", "mc", "md", "lg"]);
const RENDER = {
  ov: renderOverview, sk: renderSkillsTab, im: renderImports, gr: renderGroups,
  mc: renderMcp, bk: renderBackups, md: renderModels, lg: renderLogs,
};
let skView = "list";

/* ---------- 数据 ---------- */
async function load({ background = false } = {}) {
  try {
    const data = await getJSON("/api/data");
    const first = !S.data;
    // 扫描来源与模型发现较慢，跨刷新复用（页面上有“重新扫描/重试”）。
    if (S.data) { data.sources = S.data.sources; data.models = S.data.models; }
    S.data = data;
    if (location.search.includes("token=")) history.replaceState({}, "", location.pathname + location.hash);
    renderStatusbar();
    updateNavBadge();
    if (first) route();   // 首次加载走路由，使 #/sk?agent=… 之类的深链接生效
    else if (!(background && FORM_TABS.has(S.tab))) RENDER[S.tab]();
  } catch (err) {
    if (background) return;
    if (!S.data) {
      $("#tab-ov").innerHTML = `<p class="empty">加载失败：${esc(err.message)}<br>请使用终端打印的带 token 的 URL 打开。</p>`;
    } else toastError("刷新失败：" + err.message);
  }
}

function updateNavBadge() {
  const n = attentionCount();
  const link = $('#tabs [data-t="ov"]');
  link.innerHTML = "概览" + (n ? `<span class="count" title="需要处理的事项">${n}</span>` : "");
}

/* ---------- 路由：#/<tab>[/graph][?k=v] ---------- */
function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  if (raw === "graph") return { tab: "sk", sub: "graph", params: null };   // 兼容旧链接
  const [path, query] = raw.split("?");
  const [tab, sub] = path.split("/");
  const params = query ? Object.fromEntries(new URLSearchParams(query)) : null;
  return { tab: TABS.includes(tab) ? tab : "ov", sub: sub || "", params };
}

function navigate(tab, params) {
  const query = params ? "?" + new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v != null && v !== false)) : "";
  const target = "#/" + tab + query;
  if (location.hash === target) route(); else location.hash = target;
}

function route() {
  const { tab, sub, params } = parseHash();
  S.tab = tab;
  $$("#tabs a").forEach(a => {
    const active = a.dataset.t === tab;
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  TABS.forEach(t => { $("#tab-" + t).hidden = t !== tab; });
  if (tab === "sk") skView = sub === "graph" ? "graph" : "list";
  if (!S.data) return;
  if (tab === "sk" && params) {
    renderSkills();
    setSkillFilters({ ...params, risk: params.risk === "true" });
    setSkillView("list");
    return;
  }
  RENDER[tab]();
}

/* ---------- Skills 页（列表 / 关系图） ---------- */
function renderSkillsTab() {
  renderSkills();
  setSkillView(skView);
}

function setSkillView(view) {
  skView = view;
  $("#skListPane").hidden = view !== "list";
  $("#skGraph").hidden = view !== "graph";
  $$("[data-sk-view]").forEach(b => b.classList.toggle("active", b.dataset.skView === view));
  if (view === "graph") requestAnimationFrame(() => renderGraph(true));
}

/* ---------- 主题 ---------- */
const THEMES = { auto: "主题：跟随系统", light: "主题：浅色", dark: "主题：深色" };
function applyTheme() {
  const pref = localStorage.getItem("skillhub.theme") || "auto";
  const dark = pref === "dark" || (pref === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  $("#themeToggle").textContent = THEMES[pref];
}

/* ---------- 启动 ---------- */
setHooks({ reload: () => load(), openSkill, navigate });
bindSkills(); bindDrawer(); bindImports(); bindGroups(); bindMcp(); bindBackups(); bindModels(); bindLogs();

$("#refresh").addEventListener("click", async e => {
  const b = e.currentTarget;
  b.disabled = true;
  if (S.tab === "im" && S.data) S.data.sources = null;
  if (S.tab === "md" && S.data) S.data.models = null;
  try { await load(); } finally { b.disabled = false; }
});
let refreshTimer = null;
$("#autoRefresh").addEventListener("change", e => {
  clearInterval(refreshTimer); refreshTimer = null;
  if (e.target.checked) refreshTimer = setInterval(() => {
    if (!document.hidden && !modalOpen()) load({ background: true });
  }, 15000);
});
$("#themeToggle").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  const cur = localStorage.getItem("skillhub.theme") || "auto";
  localStorage.setItem("skillhub.theme", order[(order.indexOf(cur) + 1) % order.length]);
  applyTheme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
$("#skViewSwitch").addEventListener("click", e => {
  const b = e.target.closest("[data-sk-view]");
  if (!b) return;
  history.replaceState(null, "", b.dataset.skView === "graph" ? "#/sk/graph" : "#/sk");
  setSkillView(b.dataset.skView);
});
window.addEventListener("hashchange", route);

applyTheme();
route();
load();
