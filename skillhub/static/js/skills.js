// Skills 列表：筛选、排序、分页、键盘导航、CSV 导出。
import { S, openSkill } from "./state.js";
import { esc, fmtSize, color, STATES, stateChip, $ } from "./util.js";

const PAGE_SIZE = 100;
const view = { sort: { key: null, dir: 1 }, page: 0 };

function filters() {
  return {
    q: $("#q").value.trim().toLowerCase(),
    agent: $("#fAgent").value,
    state: $("#fState").value,
    risk: $("#fRisk").checked,
  };
}

export function filteredSkills() {
  const f = filters();
  let rows = S.data.skills.filter(s => {
    if (f.risk && !s.risks.length) return false;
    if (f.state) {
      if (f.agent) { if (s.states[f.agent] !== f.state) return false; }
      else if (!Object.values(s.states).includes(f.state)) return false;
    }
    if (f.agent && !f.state && s.states[f.agent] === undefined) return false;
    if (f.q && !((s.name + " " + s.desc + " " + s.id + " " + s.category).toLowerCase().includes(f.q))) return false;
    return true;
  });
  const { key, dir } = view.sort;
  if (key) {
    rows = [...rows].sort((a, b) => {
      if (key === "risks") return (a.risks.length - b.risks.length) * dir;
      if (key === "size") return ((a.size || 0) - (b.size || 0)) * dir;
      const va = (a[key] || "").toLowerCase(), vb = (b[key] || "").toLowerCase();
      return va < vb ? -dir : va > vb ? dir : 0;
    });
  }
  return rows;
}

/** 首次渲染时填充下拉框并绑定事件；之后只刷新表格。 */
export function renderSkills() {
  const sel = $("#fAgent"), cur = sel.value;
  sel.innerHTML = `<option value="">全部 agent</option>` + S.data.agents.map(a => `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
  sel.value = cur;
  const st = $("#fState");
  if (st.options.length <= 1) st.innerHTML = `<option value="">任意状态</option>` +
    Object.entries(STATES).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join("");
  $("#skLegend").innerHTML = `<b>图例</b>` + Object.entries(STATES).map(([k, v]) => `<span>${stateChip(k)} ${esc(v.label)}</span>`).join("") +
    `<span>空白 = 不适用</span><span>点击行或按 Enter 打开详情，↑↓ 移动</span>`;
  renderSkillTable();
}

export function setSkillFilters(params = {}) {
  $("#q").value = params.q || "";
  $("#fAgent").value = params.agent || "";
  $("#fState").value = params.state || "";
  $("#fRisk").checked = !!params.risk;
  view.page = 0;
  renderSkillTable();
}

export function renderSkillTable() {
  if (!S.data) return;
  const rows = filteredSkills();
  const pages = Math.max(1, Math.ceil(rows.length / PAGE_SIZE));
  view.page = Math.min(view.page, pages - 1);
  const pageRows = rows.slice(view.page * PAGE_SIZE, (view.page + 1) * PAGE_SIZE);
  const caret = k => view.sort.key === k ? (view.sort.dir > 0 ? " ▲" : " ▼") : "";
  const agents = S.data.agents;
  $("#skCount").textContent = `${rows.length} / ${S.data.skills.length} 个 skill`;
  $("#skTable").innerHTML = `<div class="table-wrap"><table><thead><tr>
      <th data-sort="name">名称${caret("name")}</th>
      <th data-sort="category">分类${caret("category")}</th>
      <th>描述</th>
      <th data-sort="risks">风险${caret("risks")}</th>
      <th data-sort="size">大小${caret("size")}</th>
      <th>来源</th>
      ${agents.map(a => `<th class="agent-col" title="${esc(a.name)} 投影状态"><span class="agent-dot" style="background:${color(a.name)}"></span><br>${esc(a.name)}</th>`).join("")}
    </tr></thead><tbody>` +
    (pageRows.map(s => `<tr class="clickable" tabindex="0" data-sid="${esc(s.id)}">
      <td><span class="mono">${esc(s.name)}</span>${s.channel === "ul" ? ` <span class="badge cyan">ul</span>` : ""}${s.ul_sid && s.channel !== "ul" ? ` <span class="badge" title="有 ul 试用版">+ul</span>` : ""}</td>
      <td class="dim">${esc(s.category) || "—"}</td>
      <td class="desc" title="${esc(s.desc)}">${esc(s.desc) || "—"}</td>
      <td>${s.risks.length ? s.risks.map(r => `<span class="risk">${esc(r)}</span>`).join("") : `<span class="dim">—</span>`}</td>
      <td class="mono">${fmtSize(s.size || 0)}</td>
      <td>${s.source_agents.map(a => `<span style="color:${color(a)};font-size:12px">${esc(a)}</span>`).join(" ")}</td>
      ${agents.map(a => `<td class="state-cell">${s.states[a.name] ? stateChip(s.states[a.name], a.name) : ""}</td>`).join("")}
    </tr>`).join("") || `<tr><td colspan="${6 + agents.length}" class="empty">没有匹配的 skill</td></tr>`) +
    `</tbody></table></div>`;
  $("#skPager").innerHTML = pages > 1
    ? `<button class="small" data-page="${view.page - 1}" ${view.page ? "" : "disabled"}>上一页</button>
       <span>第 ${view.page + 1} / ${pages} 页（每页 ${PAGE_SIZE}）</span>
       <button class="small" data-page="${view.page + 1}" ${view.page < pages - 1 ? "" : "disabled"}>下一页</button>` : "";
}

function exportCsv() {
  const agents = S.data.agents.map(a => a.name);
  const head = ["id", "name", "category", "risks", "source_agents", ...agents.map(a => a + "_state")];
  const lines = [head.join(",")];
  filteredSkills().forEach(s => {
    const row = [s.id, s.name, s.category, s.risks.join("|"), s.source_agents.join("|"), ...agents.map(a => s.states[a] || "")];
    lines.push(row.map(v => '"' + String(v).replace(/"/g, '""') + '"').join(","));
  });
  const blob = new Blob(["\ufeff" + lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "skillhub-skills.csv";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function bindSkills() {
  ["q", "fAgent", "fState", "fRisk"].forEach(id => document.getElementById(id).addEventListener("input", () => { view.page = 0; renderSkillTable(); }));
  $("#clearFilters").addEventListener("click", () => setSkillFilters({}));
  $("#exportCsv").addEventListener("click", exportCsv);
  $("#skPager").addEventListener("click", e => {
    const b = e.target.closest("[data-page]");
    if (b) { view.page = Number(b.dataset.page); renderSkillTable(); $("#skTable tr.clickable")?.focus(); }
  });
  const table = $("#skTable");
  table.addEventListener("click", e => {
    const header = e.target.closest("[data-sort]");
    if (header) {
      const k = header.dataset.sort;
      view.sort = view.sort.key === k ? { key: k, dir: -view.sort.dir } : { key: k, dir: 1 };
      renderSkillTable();
      return;
    }
    const row = e.target.closest("[data-sid]");
    if (row) openSkill(row.dataset.sid);
  });
  table.addEventListener("keydown", e => {
    const row = e.target.closest("tr[data-sid]");
    if (!row) return;
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openSkill(row.dataset.sid); }
    else if (e.key === "ArrowDown") { e.preventDefault(); row.nextElementSibling?.focus(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); row.previousElementSibling?.focus(); }
  });
}
