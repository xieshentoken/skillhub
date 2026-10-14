// 导入：扫描各 agent 目录中的来源 skill，逐个预览导入。
import { S, reload } from "./state.js";
import { esc, fmtSize, color, $ } from "./util.js";
import { getJSON } from "./api.js";
import { toast, action, confirmWrite } from "./ui.js";

const PAGE_SIZE = 100;
const view = { q: "", agent: "", page: 0, loading: false, error: "" };

export function renderImports() {
  const el = $("#tab-im");
  if (!S.data.sources) {
    el.innerHTML = view.error ? `<p class="empty">加载来源失败：${esc(view.error)} <button data-reload-sources>重试</button></p>` : `<p class="empty">正在扫描可导入来源…</p>`;
    if (!view.loading && !view.error) loadSources();
    return;
  }
  const agents = [...new Set(S.data.sources.map(s => s.agent))];
  el.innerHTML = `<div class="section-head"><h2>导入</h2><p>导入只复制进中央库，不修改 agent 原文件；同名不同内容会共存，可在 Skills 详情中比较/改名。</p></div>
    <div class="toolbar">
      <input type="search" id="imQ" placeholder="搜索名称 / 描述 / 路径" value="${esc(view.q)}">
      <select id="imAgent"><option value="">全部来源 agent</option>${agents.map(a => `<option ${a === view.agent ? "selected" : ""}>${esc(a)}</option>`).join("")}</select>
      <span class="grow"></span><span class="note" id="imCount"></span>
      <button data-reload-sources>重新扫描</button>
    </div>
    <div id="imTable"></div>`;
  $("#imQ").addEventListener("input", e => { view.q = e.target.value; view.page = 0; renderImportTable(); });
  $("#imAgent").addEventListener("change", e => { view.agent = e.target.value; view.page = 0; renderImportTable(); });
  renderImportTable();
}

function renderImportTable() {
  const sources = S.data.sources;
  const q = view.q.toLowerCase();
  const rows = sources.filter(s => (!view.agent || s.agent === view.agent) &&
    (!q || (s.name + " " + (s.desc || "") + " " + s.path).toLowerCase().includes(q)));
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  view.page = Math.min(view.page, pages - 1);
  const shown = rows.slice(view.page * PAGE_SIZE, (view.page + 1) * PAGE_SIZE);
  $("#imCount").textContent = `${rows.length} / ${sources.length} 个来源`;
  $("#imTable").innerHTML = `<div class="table-wrap"><table><thead><tr><th>名称</th><th>来源</th><th>大小</th><th>路径</th><th>状态</th><th></th></tr></thead><tbody>
    ${shown.map(s => `<tr><td><b>${esc(s.name)}</b>${s.desc ? `<div class="note">${esc(s.desc)}</div>` : ""}</td>
      <td style="color:${color(s.agent)}">${esc(s.agent)}</td>
      <td class="mono">${s.error ? "" : fmtSize(s.size || 0)}</td>
      <td class="mono wrap dim">${esc(s.path)}</td>
      <td>${statusHtml(s)}
        ${(s.risks || []).map(r => `<span class="risk">${esc(r)}</span>`).join("")}</td>
      <td>${s.error ? `<span class="note danger">${esc(s.error)}</span>` : `<button class="small" data-import-agent="${esc(s.agent)}" data-import-path="${esc(s.path)}">预览导入</button>`}</td></tr>`).join("")
      || `<tr><td colspan="6" class="empty">没有匹配的来源</td></tr>`}
    </tbody></table></div>
    ${pages > 1 ? `<div class="pager"><button class="small" data-im-page="${view.page - 1}" ${view.page ? "" : "disabled"}>上一页</button>
      <span>第 ${view.page + 1} / ${pages} 页</span><button class="small" data-im-page="${view.page + 1}" ${view.page < pages - 1 ? "" : "disabled"}>下一页</button></div>` : ""}`;
}

function statusHtml(source) {
  if (source.error) return `<span class="badge bad">无法导入</span>`;
  const md5 = typeof source.md5 === "string" ? source.md5 : "";
  const identityReady = !!source.name && !!source.path && /^[0-9a-f]{32}$/i.test(md5) && !!source.import_sid;
  if (!identityReady) return `<span class="badge warn" title="需要重新扫描以取得完整摘要和导入身份">待核验</span>`;
  const exact = S.data.skills.some(skill => skill.id === source.import_sid && skill.md5 === md5);
  if (exact) return `<span class="badge ok" title="导入目标 ID 与完整摘要均一致">已在中央库（摘要一致）</span>`;
  const pathKnown = S.data.skills.some(skill => (skill.source_paths || []).includes(source.path));
  if (pathKnown) return `<span class="badge warn" title="同一路径存在历史导入版本，但当前导入身份不同">来源与已导入版本不同</span>`;
  return `<span class="badge">未导入</span>`;
}

async function loadSources() {
  view.loading = true; view.error = "";
  try { S.data.sources = (await getJSON("/api/sources")).sources || []; }
  catch (err) { S.data.sources = null; view.error = err.message; }
  finally { view.loading = false; }
  if (S.tab === "im") renderImports();
}

export function bindImports() {
  $("#tab-im").addEventListener("click", e => {
    let el;
    if ((el = e.target.closest("[data-reload-sources]"))) { S.data.sources = null; view.error = ""; renderImports(); return; }
    if ((el = e.target.closest("[data-im-page]"))) { view.page = Number(el.dataset.imPage); renderImportTable(); return; }
    if ((el = e.target.closest("[data-import-agent]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/skill/import", { agent: el.dataset.importAgent, path: el.dataset.importPath }, { title: "导入来源 skill" });
      if (ad) { toast("已导入：" + (ad.sid || ""), "ok"); S.data.sources = null; await reload(); }
    });
  });
}
