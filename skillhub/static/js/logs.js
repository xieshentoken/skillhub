// 日志与设置：回收保留天数、审计日志、数据路径。
import { S, reload, openSkill } from "./state.js";
import { esc, $ } from "./util.js";
import { toast, action, confirmWrite } from "./ui.js";

const view = { q: "" };

export function renderLogs() {
  const d = S.data, el = $("#tab-lg");
  el.innerHTML = `<div class="section-head"><h2>日志与设置</h2></div>
    <div class="panel"><h3>设置</h3>
      <div class="formrow"><label>回收保留天数 <input id="retentionDays" type="number" min="1" value="${esc(d.settings?.trash_retention_days || 7)}" style="width:90px"></label>
        <button data-save-settings>预览并保存</button></div>
      <p class="note">保存是明确写操作；GUI 启动/刷新不会自动清理回收区。外部编辑器只能通过环境变量 VISUAL / EDITOR 配置。</p></div>
    <div class="panel"><h3>审计日志 <span class="note">最近 ${(d.logs || []).length} 条</span></h3>
      <div class="toolbar"><input type="search" id="logQ" placeholder="按事件 / 状态 / skill 过滤" value="${esc(view.q)}"></div>
      <div id="logTable"></div></div>
    <div class="panel"><h3>数据位置</h3><dl class="kv">
      ${Object.entries(d.paths).map(([k, v]) => `<dt>${esc(k)}</dt><dd class="mono">${esc(v)}</dd>`).join("")}</dl></div>`;
  $("#logQ").addEventListener("input", e => { view.q = e.target.value; renderLogTable(); });
  renderLogTable();
}

function renderLogTable() {
  const q = view.q.trim().toLowerCase();
  const logs = (S.data.logs || []).slice().reverse()
    .filter(x => !q || [x.event, x.status, x.sid, x.at].join(" ").toLowerCase().includes(q));
  $("#logTable").innerHTML = `<div class="table-wrap"><table><thead><tr><th>时间</th><th>事件</th><th>状态</th><th>skill</th></tr></thead><tbody>
    ${logs.map(x => `<tr><td class="mono">${esc(x.at)}</td><td>${esc(x.event)}</td>
      <td>${x.status ? `<span class="badge ${/ok|success|apply/i.test(x.status) ? "ok" : /fail|error/i.test(x.status) ? "bad" : ""}">${esc(x.status)}</span>` : ""}</td>
      <td class="mono">${x.sid ? `<a href="#" data-open-sid="${esc(x.sid)}">${esc(x.sid)}</a>` : ""}</td></tr>`).join("")
      || `<tr><td colspan="4" class="empty">暂无日志</td></tr>`}</tbody></table></div>`;
}

export function bindLogs() {
  $("#tab-lg").addEventListener("click", e => {
    let b;
    if ((b = e.target.closest("[data-open-sid]"))) {
      e.preventDefault();
      if ((S.data.skills || []).some(s => s.id === b.dataset.openSid)) openSkill(b.dataset.openSid);
      else toast("该 skill 已不在中央库", "warn");
      return;
    }
    if ((b = e.target.closest("[data-save-settings]"))) return action(b, async () => {
      const ad = await confirmWrite("/api/settings", { settings: { trash_retention_days: Number($("#retentionDays").value) } }, { title: "保存 skillhub 设置" });
      if (ad) { toast("设置已保存", "ok"); await reload(); }
    });
  });
}
