// Skill 详情抽屉：信息 / 投影 / 版本 / 依赖 / 文件。
import { S, reload } from "./state.js";
import { esc, fmtSize, color, stateChip, stateLabel, gatedRisks, splitList, $, $$ } from "./util.js";
import { getJSON } from "./api.js";
import { toast, action, confirmWrite, modalOpen } from "./ui.js";

const SECTIONS = [["info", "信息"], ["proj", "投影"], ["ver", "版本"], ["deps", "依赖"], ["files", "文件"]];
const cur = { sid: null, detail: null, section: "info", diag: null, returnFocus: null };
const enc = encodeURIComponent;

export async function openSkill(sid, section = "info") {
  const drawer = $("#drawer");
  if (!drawer.classList.contains("open")) cur.returnFocus = document.activeElement;
  $("#mask").hidden = false;
  drawer.classList.add("open");
  drawer.setAttribute("aria-hidden", "false");
  cur.sid = sid; cur.section = section; cur.detail = null; cur.diag = null;
  $("#dTitle").innerHTML = esc(sid);
  $("#dTabs").innerHTML = "";
  $("#dBody").innerHTML = `<p class="empty">加载中…</p>`;
  $("#closeDrawer").focus();
  try {
    const detail = await getJSON("/api/skill/" + enc(sid));
    if (cur.sid !== sid) return;
    cur.detail = detail;
    renderDrawer();
    loadDiagnosis(sid);
  } catch (err) { $("#dBody").innerHTML = `<p class="empty">加载失败：${esc(err.message)}</p>`; }
}

export function closeDrawer() {
  const drawer = $("#drawer");
  if (!drawer.classList.contains("open")) return;
  drawer.classList.remove("open");
  drawer.setAttribute("aria-hidden", "true");
  $("#mask").hidden = true;
  cur.sid = null;
  if (cur.returnFocus && cur.returnFocus.focus) cur.returnFocus.focus();
}

function renderDrawer() {
  const m = cur.detail.manifest;
  $("#dTitle").innerHTML = `${esc(m.name)} ${m.channel === "ul" ? `<span class="badge cyan">ul</span>` : ""}<span class="sub">${esc(cur.sid)}</span>`;
  $("#dTabs").innerHTML = SECTIONS.map(([k, label]) =>
    `<button role="tab" data-dsec="${k}" aria-selected="${k === cur.section}" class="${k === cur.section ? "active" : ""}">${label}${k === "files" ? ` (${cur.detail.files.length})` : ""}</button>`).join("");
  const body = $("#dBody");
  body.innerHTML = ({ info: infoHtml, proj: projHtml, ver: versionHtml, deps: depsHtml, files: filesHtml })[cur.section]();
  if (cur.section === "deps") renderDiagnosis();
}

/* ---------- 各分区 ---------- */
function infoHtml() {
  const d = cur.detail, m = d.manifest;
  return `<dl class="kv">
    <dt>名称</dt><dd><b>${esc(m.name)}</b></dd>
    <dt>分类</dt><dd>${esc(m.category) || "—"}</dd>
    <dt>描述</dt><dd>${esc(m.fm_desc) || "—"}</dd>
    <dt>风险</dt><dd>${(m.risks || []).length ? m.risks.map(x => `<span class="risk">${esc(x)}</span>`).join("") : "无标记"}
      ${gatedRisks(m.risks).length ? `<div class="note">命中风险门禁：GUI 不能投影，请用 CLI <span class="mono">--allow-risky</span></div>` : ""}</dd>
    <dt>大小</dt><dd>${fmtSize(m.size || 0)}</dd>
    <dt>导入时间</dt><dd>${esc(m.imported_at || "—")}</dd>
    <dt>来源</dt><dd>${(m.sources || []).map(s => `${esc(s.agent)} <span class="mono dim">${esc(s.path)}</span>`).join("<br>") || "—"}</dd>
    <dt>中央库</dt><dd class="mono dim">${esc(d.store_dir)}</dd>
    <dt>投影</dt><dd>${S.data.agents.map(a => { const st = d.projections[a.name]?.state; return st ? `<span style="margin-right:8px">${stateChip(st, a.name)} ${esc(a.name)}</span>` : ""; }).join("")}
      <div><button class="link small" data-goto="proj">管理投影 →</button></div></dd>
  </dl>`;
}

function projHtml() {
  const d = cur.detail, m = d.manifest;
  const rows = S.data.agents.map(a => {
    const p = d.projections[a.name], st = p.state;
    const isOurs = st === "linked" || st === "copy" || st === "broken";
    const act = isOurs ? "unlink" : "link";
    const needForce = act === "link" && st === "conflict";
    const gated = !isOurs && gatedRisks(m.risks).length > 0;
    const label = isOurs ? "解除" : needForce ? "投影（替换）" : "投影";
    const btn = S.data.read_only ? "" : gated ? `<span class="note">风险门禁，请用 CLI</span>` :
      `<button class="small ${isOurs ? "" : needForce ? "danger-btn" : "primary"}" data-proj-agent="${esc(a.name)}" data-proj-state="${esc(st)}">${label}</button>`;
    return `<div class="projline">${stateChip(st, a.name)}
      <span class="agent" style="color:${color(a.name)}">${esc(a.name)}</span>
      <span class="mono" title="${esc(stateLabel(st))}">${esc(p.target)}</span>${btn}</div>`;
  }).join("");
  return `${rows}<p class="note">投影先预览再确认；冲突项会备份后替换；GUI 不能绕过风险门禁；断链需先修复中央库。</p>`;
}

function versionHtml() {
  const m = cur.detail.manifest;
  const other = m.channel === "ul" ? m.formal_sid : m.ul_sid;
  const actions = m.channel === "ul"
    ? `<button data-refresh-ul>刷新外部编辑摘要</button><button class="danger-btn" data-publish>预览并发布为正式版</button>`
    : `<button data-create-ul ${m.ul_sid ? "disabled title=\"已存在 ul\"" : ""}>创建独立 ul 试用版</button>`;
  const trial = m.channel === "formal" && m.ul_sid
    ? `<div class="sect">试用 agent</div><div class="formrow">${S.data.agents.map(a =>
        `<label class="ck"><input type="checkbox" data-trial-agent="${esc(a.name)}" ${(m.trial_agents || []).includes(a.name) ? "checked" : ""}>${esc(a.name)}</label>`).join("")}
        <button data-trial>预览切换</button></div><p class="note">只切换勾选的 agent 到 ul，其它 agent 保持正式版。</p>` : "";
  return `<dl class="kv"><dt>通道</dt><dd><span class="badge ${m.channel === "ul" ? "cyan" : ""}">${esc(m.channel || "formal")}</span></dd>
      <dt>${m.channel === "ul" ? "正式版" : "ul 版"}</dt><dd>${other ? `<a href="#" data-open-sid="${esc(other)}" class="mono">${esc(other)}</a>` : "—"}</dd></dl>
    <div class="formrow">${actions}${other ? `<button data-diff="${esc(other)}">查看版本差异</button>` : ""}</div>
    ${m.channel === "ul" ? `<p class="note">ul 是独立副本，只能编辑文本文件；外部编辑后先刷新摘要，再发布。发布需要依赖诊断无阻断项。</p>`
      : `<p class="note">正式版可创建一个独立 ul；正式版与 ul 的关系各只保留一个。</p>`}
    ${trial}
    <div class="sect">改名</div>
    <div class="formrow"><input type="text" id="renameInput" value="${esc(m.name)}" style="width:240px"><button data-rename>预览改名</button></div>
    <div id="diffView"></div>`;
}

function depsHtml() {
  const m = cur.detail.manifest;
  return `<div id="diagView" class="note">加载诊断中…</div>
    <div class="sect">显式依赖声明</div>
    <div class="formrow"><label class="field">必需工具（逗号分隔）<input type="text" id="depTools" placeholder="ffmpeg,git" value="${esc((m.dependencies?.tools || []).join(","))}"></label>
      <label class="field">必需环境变量（逗号分隔）<input type="text" id="depEnv" placeholder="TOKEN" value="${esc((m.dependencies?.env || []).join(","))}"></label></div>
    <div class="formrow"><button data-save-deps>预览保存依赖声明</button></div>
    <p class="note">诊断只做静态检查（文件文本、which、环境变量是否存在），不执行 skill。</p>`;
}

function filesHtml() {
  const files = cur.detail.files;
  return files.length ? `<div class="filelist">${files.map(f => f.size < 0 && f.path.startsWith("...")
      ? `<div class="picker-more">${esc(f.path)}</div>`
      : `<button data-file="${esc(f.path)}"><span>${esc(f.path)}</span><span>${fmtSize(f.size)}</span></button>`).join("")}</div>
    <p class="note">${cur.detail.manifest.channel === "ul" ? "ul 文本文件可编辑，保存前会预览。" : "正式版只读；如需修改请先创建 ul。"}</p>`
    : `<p class="empty">中央库目录为空</p>`;
}

/* ---------- 诊断 ---------- */
async function loadDiagnosis(sid) {
  try { cur.diag = await getJSON("/api/skill/" + enc(sid) + "/diagnose"); }
  catch (err) { cur.diag = { error: err.message }; }
  if (cur.sid === sid && cur.section === "deps") renderDiagnosis();
}

function renderDiagnosis() {
  const el = $("#diagView");
  if (!el || !cur.diag) return;
  if (cur.diag.error) { el.textContent = "诊断失败：" + cur.diag.error; return; }
  const missing = cur.diag.missing || [], manual = cur.diag.needs_manual || [];
  const items = [...missing, ...manual];
  const key = x => (x.kind || "") + ":" + (x.name || "");
  el.className = "";
  el.innerHTML = (items.length
      ? `<div class="banner bad">发布阻断：${items.map(x => esc(key(x)) + " " + esc(x.status)).join("、")}</div>`
      : `<div class="banner info">未发现静态缺项</div>`) +
    (missing.length ? `<p class="note">缺失项不能在 GUI 中标记为已满足，请先补齐后重新诊断。</p>` : "") +
    (manual.length ? `<div class="sect">待人工核验</div><p class="note">勾选本次确实核验过的项（可部分确认）：</p>
      ${manual.map(x => `<div><label class="ck"><input type="checkbox" data-diag-key="${esc(key(x))}">${esc(key(x))} <span class="note">${esc(x.source || x.certainty || x.status || "")}</span></label></div>`).join("")}
      <div class="formrow"><input type="text" id="diagNote" placeholder="核验备注（可选）" style="min-width:260px"><button data-diag-confirm>确认选中项</button></div>` : "");
}

/* ---------- 文件 / 差异 ---------- */
async function openFile(path) {
  const d = await getJSON("/api/skill/" + enc(cur.sid) + "/file?path=" + enc(path));
  const writable = d.channel === "ul" && d.editable && !S.data.read_only;
  $("#dBody").innerHTML = `<div class="formrow"><button data-back>← 返回文件列表</button><b class="mono">${esc(path)}</b>
      <span class="badge ${writable ? "cyan" : ""}">${writable ? "可编辑" : "只读"}</span></div>
    <textarea id="editorText" ${writable ? "" : "readonly"} spellcheck="false"></textarea>
    ${writable ? `<div class="formrow"><button class="primary" data-save-file="${esc(path)}">预览保存</button>
      <button data-open-editor="${esc(path)}">打开外部编辑器</button></div>` : ""}
    <p class="note">${writable ? "保存先预览，确认后才写入；外部编辑后请到“版本”刷新摘要。" : "只有 ul 的文本文件可以编辑。"}</p>`;
  $("#editorText").value = d.text;
}

function diffHtml(d) {
  const changes = d.changes || [];
  if (!changes.length) return `<p class="note">两个版本内容一致。</p>`;
  const line = l => {
    const cls = l.startsWith("@@") ? "hunk" : l.startsWith("+") && !l.startsWith("+++") ? "add" : l.startsWith("-") && !l.startsWith("---") ? "del" : "";
    return `<span class="${cls}">${esc(l) || " "}</span>`;
  };
  return `<div class="sect">${esc(d.left)} ↔ ${esc(d.right)}</div>` + changes.map(x =>
    `<div class="diff"><div class="file">${esc(x.path)} <span class="badge ${x.kind === "added" ? "ok" : x.kind === "removed" ? "bad" : "warn"}">${esc(x.kind)}</span></div>
      ${x.diff ? `<pre>${x.diff.replace(/\n$/, "").split("\n").map(line).join("")}</pre>` : `<pre><span class="dim">（无文本差异可展示）</span></pre>`}</div>`).join("");
}

/* ---------- 写操作 ---------- */
async function proj(agent, state) {
  const sid = cur.sid;
  const isOurs = ["linked", "broken", "copy_broken"].includes(state);
  const act = isOurs ? "unlink" : "link";
  if (act === "link" && ["broken", "copy_broken"].includes(state)) {
    toast("中央库副本已缺失（悬空链接），请先 import 或用 CLI rollback 修复中央库。", "warn"); return;
  }
  if (state === "store_drift") { toast("中央库副本与索引摘要不一致，请先恢复或重新导入中央库。", "warn"); return; }
  const force = act === "link" && (state === "conflict" || state === "copy_drift");
  const ad = await confirmWrite("/api/" + act, { sid, agents: [agent], force }, {
    title: act === "unlink" ? `解除 ${agent} 上的投影（只移除投影，不动中央库）`
      : force ? `投影到 ${agent}（备份后替换已有非本库内容）` : `投影到 ${agent}` });
  if (!ad) return;
  const acts = (ad.actions && ad.actions[agent]) || [];
  const bad = acts.filter(a => ["error", "conflict", "copy_drift", "store_drift", "blocked", "broken"].includes(a.type));
  if (bad.length) toast("未完成：" + bad.map(a => a.detail).join("; "), "err");
  else toast(act === "unlink" ? `已解除 ${agent} 投影` : `已投影到 ${agent}`, "ok");
  await refreshAfter(sid, "proj");
}

async function refreshAfter(sid, section) {
  await reload();
  if (sid) openSkill(sid, section);
}

export function bindDrawer() {
  $("#mask").addEventListener("click", closeDrawer);
  $("#closeDrawer").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !modalOpen()) closeDrawer(); });
  $("#dTabs").addEventListener("click", e => {
    const b = e.target.closest("[data-dsec]");
    if (b && cur.detail) { cur.section = b.dataset.dsec; renderDrawer(); }
  });
  $("#dBody").addEventListener("click", e => {
    const t = e.target, sid = cur.sid;
    const on = sel => t.closest(sel);
    let el;
    if ((el = on("[data-goto]"))) { cur.section = el.dataset.goto; renderDrawer(); return; }
    if ((el = on("[data-open-sid]"))) { e.preventDefault(); openSkill(el.dataset.openSid, "ver"); return; }
    if ((el = on("[data-proj-agent]"))) return action(el, () => proj(el.dataset.projAgent, el.dataset.projState));
    if ((el = on("[data-file]"))) return action(el, () => openFile(el.dataset.file));
    if (on("[data-back]")) { renderDrawer(); return; }
    if ((el = on("[data-diff]"))) return action(el, async () => {
      const d = await getJSON("/api/skill/" + enc(sid) + "/diff?with=" + enc(el.dataset.diff));
      $("#diffView").innerHTML = diffHtml(d);
    });
    if ((el = on("[data-save-file]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/skill/edit", { sid, path: el.dataset.saveFile, text: $("#editorText").value }, { title: "保存 ul 文件" });
      if (ad) { toast("已保存", "ok"); await refreshAfter(sid, "files"); }
    });
    if ((el = on("[data-open-editor]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/editor/open", { sid, path: el.dataset.openEditor }, { title: "打开外部编辑器" });
      if (ad) toast("已打开：" + ad.path, "ok");
    });
    if ((el = on("[data-create-ul]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/skill/create-ul", { sid }, { title: "创建独立 ul" });
      if (ad) { toast("已创建 ul", "ok"); await refreshAfter(ad.ul_sid || sid, "ver"); }
    });
    if ((el = on("[data-refresh-ul]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/skill/refresh", { sid }, { title: "刷新外部编辑后的 ul 摘要" });
      if (ad) { toast("摘要已刷新", "ok"); await refreshAfter(ad.sid || sid, "ver"); }
    });
    if ((el = on("[data-publish]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/skill/publish", { sid }, { title: "发布 ul 为正式版", phrase: "PUBLISH" });
      if (ad) { toast("已发布为正式版", "ok"); await refreshAfter(ad.new_formal_sid || sid, "ver"); }
    });
    if ((el = on("[data-rename]"))) return action(el, async () => {
      const ad = await confirmWrite("/api/skill/rename", { sid, name: $("#renameInput").value }, { title: "改名并保留来源" });
      if (ad) { toast("已改名", "ok"); await refreshAfter(ad.sid, "ver"); }
    });
    if ((el = on("[data-trial]"))) return action(el, async () => {
      const agents = $$("[data-trial-agent]:checked").map(x => x.dataset.trialAgent);
      const ad = await confirmWrite("/api/skill/trial", { sid, agents, enable: true }, { title: "切换试用 agent" });
      if (ad) { toast("试用 agent 已切换", "ok"); await refreshAfter(sid, "ver"); }
    });
    if ((el = on("[data-diag-confirm]"))) return action(el, async () => {
      const keys = $$("[data-diag-key]:checked").map(x => x.dataset.diagKey);
      if (!keys.length) { toast("请先勾选实际核验过的待验证项", "warn"); return; }
      const ad = await confirmWrite("/api/diagnose/confirm", { sid, keys, note: $("#diagNote")?.value || "" }, { title: "记录人工诊断确认" });
      if (ad) { toast("已记录确认", "ok"); cur.diag = null; await loadDiagnosis(sid); }
    });
    if ((el = on("[data-save-deps]"))) return action(el, async () => {
      const dependencies = { tools: splitList($("#depTools").value), env: splitList($("#depEnv").value) };
      const ad = await confirmWrite("/api/diagnose/dependencies", { sid, dependencies }, { title: "保存显式依赖声明" });
      if (ad) { toast("依赖声明已保存", "ok"); await refreshAfter(sid, "deps"); }
    });
  });
}
