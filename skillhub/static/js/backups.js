// 备份与回收：快照只读展示（全库回滚走 CLI）；单 skill 回收区可恢复 / 保留 / 清理。
import { S, reload, openSkill } from "./state.js";
import { esc, fmtSize, $ } from "./util.js";
import { toast, action, confirmWrite } from "./ui.js";

export function renderBackups() {
  const d = S.data, el = $("#tab-bk");
  const trash = d.trash || [];
  const plan = Object.fromEntries((d.trash_plan || []).map(x => [x.id, x]));
  const expired = (d.trash_plan || []).filter(x => x.type === "remove").length;
  const backupRows = (d.backups || []).map(b => `<tr>
      <td class="mono"><b>${esc(b.ts)}</b></td><td><span class="badge">${esc(b.kind || "unknown")}</span></td>
      <td class="mono">${fmtSize(b.size)}</td>
      <td>${b.has_conflicts ? `<span class="badge warn">含被替换的冲突目录</span>` : `<span class="badge ok">可验证</span>`}</td>
      <td class="note">${b.kind === "skill_publish" ? "发布备份：旧正式版在下方回收区，可单独恢复" :
        `CLI：<span class="mono">skillhub rollback ${esc(b.ts)} --apply</span>`}</td></tr>`).join("");
  const trashRows = trash.map(t => {
    const p = plan[t.id];
    const status = !t.valid ? `<span class="badge bad">不可验证</span>` : t.retained ? `<span class="badge ok">手动保留</span>` :
      p && p.type === "remove" ? `<span class="badge warn">已到期</span>` : `<span class="badge">未到期</span>`;
    return `<tr><td class="mono">${esc(t.id)}</td>
      <td>${t.sid ? `<a href="#" data-open-sid="${esc(t.sid)}">${esc(t.name || t.sid)}</a>` : esc(t.name || "")}</td>
      <td>${esc(t.reason || "")}</td><td class="mono">${esc(t.retain_until || t.error || "")}</td><td>${status}</td>
      <td>${t.valid ? `<button class="small" data-trash-restore="${esc(t.id)}">恢复</button>
        <button class="small link" data-trash-retain="${esc(t.id)}" data-trash-retained="${t.retained ? "false" : "true"}">${t.retained ? "取消保留" : "保留"}</button>` : ""}</td></tr>`;
  }).join("");
  el.innerHTML = `<div class="section-head"><h2>备份与回收</h2><p>GUI 只处理单 skill 回收区；全库快照回滚请用 CLI。</p></div>
    <div class="panel"><h3>单 skill 回收区 <span class="note">默认保留 ${esc(d.settings?.trash_retention_days || 7)} 天</span></h3>
      ${trash.length ? `<div class="table-wrap"><table><thead><tr><th>回收项</th><th>skill</th><th>原因</th><th>保留至</th><th>状态</th><th></th></tr></thead><tbody>${trashRows}</tbody></table></div>`
        : `<p class="note">回收区为空。</p>`}
      <div class="formrow"><button class="danger-btn" data-trash-cleanup ${expired ? "" : "disabled title=\"没有已到期的回收项\""}>预览并清理到期项（${expired}）</button></div></div>
    <div class="panel"><h3>快照 <span class="note">共 ${esc(d.backup_total ?? (d.backups || []).length)} 份</span></h3>
      ${backupRows ? `<div class="table-wrap"><table><thead><tr><th>时间戳</th><th>类型</th><th>大小</th><th>内容</th><th>恢复方式</th></tr></thead><tbody>${backupRows}</tbody></table></div>`
        : `<p class="empty">还没有备份。</p>`}
      <div class="formrow"><label>保留最近 <input id="backupKeep" type="number" min="1" max="10000" value="3" style="width:80px"> 份</label>
        <button class="danger-btn" data-backup-cleanup>预览清理旧备份</button></div>
      <p class="note">备份目录 <span class="mono">${esc(d.paths.backups)}</span></p></div>`;
}

export function bindBackups() {
  $("#tab-bk").addEventListener("click", e => {
    let b;
    if ((b = e.target.closest("[data-open-sid]"))) { e.preventDefault(); openSkill(b.dataset.openSid); return; }
    if ((b = e.target.closest("[data-trash-restore]"))) return action(b, async () => {
      const ad = await confirmWrite("/api/trash/restore", { entry: b.dataset.trashRestore }, { title: "恢复单个 skill 回收项" });
      if (ad) { toast("已恢复", "ok"); await reload(); }
    });
    if ((b = e.target.closest("[data-trash-retain]"))) return action(b, async () => {
      const retained = b.dataset.trashRetained === "true";
      const ad = await confirmWrite("/api/trash/retain", { entry: b.dataset.trashRetain, retained }, { title: retained ? "手动保留回收项" : "取消回收项保留" });
      if (ad) { toast(retained ? "已保留" : "已取消保留", "ok"); await reload(); }
    });
    if ((b = e.target.closest("[data-trash-cleanup]"))) return action(b, async () => {
      const ad = await confirmWrite("/api/trash/cleanup", { confirm: true }, { title: "清理已到期且未保留的回收项", phrase: "CLEANUP" });
      if (ad) { toast("回收区已清理", "ok"); await reload(); }
    });
    if ((b = e.target.closest("[data-backup-cleanup]"))) return action(b, async () => {
      const keep = Number($("#backupKeep").value);
      if (!Number.isInteger(keep) || keep < 1) { toast("保留份数必须是正整数", "warn"); return; }
      const ad = await confirmWrite("/api/backups/cleanup", { keep }, { title: "清理旧备份快照", phrase: "CLEANUP" });
      if (ad) { toast("旧备份已清理", "ok"); await reload(); }
    });
  });
}
