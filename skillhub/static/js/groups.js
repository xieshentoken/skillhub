// 分组与分发：分组关系只写中央库；写 agent 只发生在确认分发之后。
import { S, reload } from "./state.js";
import { esc, skillName, $, $$ } from "./util.js";
import { toast, action, confirmWrite, confirmEditSwitch, createPicker, skillItems } from "./ui.js";

// isNew 为真时编辑器处于“新建分组”模式（用独立标志，避免与任何合法分组 id 冲突）。
const G = {
  selected: null, isNew: false, picker: null, editor: null,
  switchPending: false, writePending: null,
};
const D = { groupPicker: null, sidPicker: null, dirty: false, conflict: false };

const uniqueMembers = members => [...new Set((members || []).filter(x => typeof x === "string"))];
const memberKey = members => uniqueMembers(members).sort().join("\u0000");
const contextKey = (isNew, gid) => isNew ? "new" : "group:" + gid;
const currentContext = () => contextKey(G.isNew, G.selected);

function newEditorState() {
  const group = G.isNew ? { name: "", members: [] } : (S.data.groups?.[G.selected] || {});
  const name = G.isNew ? "" : (group.name || G.selected);
  const members = uniqueMembers(group.members);
  return {
    context: currentContext(),
    baseline: { name, members: [...members] },
    name, members: [...members], query: "",
  };
}

function ensureEditorState() {
  if (!G.editor || G.editor.context !== currentContext()) G.editor = newEditorState();
  return G.editor;
}

function captureEditor() {
  if (!G.editor || !G.picker || !$("#groupName")) return G.editor;
  G.editor.name = $("#groupName").value;
  G.editor.members = uniqueMembers([...G.picker.get()]);
  G.editor.query = G.picker.getQuery ? G.picker.getQuery() : ($("#groupPicker input")?.value || "");
  return G.editor;
}

function editorDirty(state = G.editor) {
  return !!state && (state.name !== state.baseline.name ||
    memberKey(state.members) !== memberKey(state.baseline.members));
}

function updateDirty() {
  const note = $("#groupDirty");
  if (note) note.textContent = editorDirty(captureEditor()) ? "名称或成员已修改，尚未保存。" : "";
}

function clearEditorFor(context) {
  if (G.editor?.context !== context) return;
  G.editor = null; G.picker = null;
}

function markEditorClean(context, name, members) {
  if (G.editor?.context !== context) return;
  const cleanMembers = uniqueMembers(members);
  G.editor = {
    context, baseline: { name, members: [...cleanMembers] },
    name, members: [...cleanMembers], query: G.editor.query || "",
  };
  const nameInput = $("#groupName");
  if (nameInput) nameInput.value = name;
  updateDirty();
}

/** 写操作从预览开始到结果返回期间冻结当前编辑器；重绘后由 renderGroupEditor 再次应用。 */
function setEditorWriteLocked(locked) {
  const box = $("#groupEditor");
  if (!box) return;
  if (locked) {
    box.setAttribute("aria-busy", "true");
    box.setAttribute("inert", "");
  } else {
    box.removeAttribute("aria-busy");
    box.removeAttribute("inert");
  }
  box.querySelectorAll("input, button, textarea, select").forEach(control => { control.disabled = locked; });
}

export function renderGroups() {
  captureEditor();
  const el = $("#tab-gr");
  const groups = S.data.groups || {};
  if (!G.isNew && !(G.selected in groups)) {
    G.selected = Object.keys(groups)[0] || null;
    G.isNew = G.selected === null;
    G.editor = null;
    G.picker = null;
  }
  if (G.editor && G.editor.context !== currentContext()) {
    G.editor = null;
    G.picker = null;
  }
  el.innerHTML = `<div class="section-head"><h2>分组与分发</h2><p>分组关系保存在中央库；修改分组不会写 agent，下一次分发预览按当前成员重算。</p></div>
    <div class="panel"><h3>分组</h3>
      <div class="split">
        <div><div class="group-list" id="groupList"></div>
          <p class="note">可把右侧候选 skill 拖到左侧分组上直接加入（会先预览）。</p></div>
        <div id="groupEditor" class="dropzone"></div>
      </div>
    </div>
    <div class="panel" id="distPanel"></div>`;
  renderGroupList();
  renderGroupEditor();
  renderDistribution();
}

function renderGroupList() {
  const groups = S.data.groups || {};
  $("#groupList").innerHTML = Object.entries(groups).map(([gid, g]) =>
    `<button data-group="${esc(gid)}" data-drop-group="${esc(gid)}" class="${!G.isNew && gid === G.selected ? "active" : ""}">
      <span>${esc(g.name || gid)}</span><span class="badge">${(g.members || []).length}</span></button>`).join("") +
    `<button data-new-group class="${G.isNew ? "active" : ""}">＋ 新建分组</button>`;
}

function renderGroupEditor() {
  const box = $("#groupEditor");
  const isNew = G.isNew;
  const state = ensureEditorState();
  const g = isNew ? { name: state.name, members: state.members, kind: "manual" } :
    { ...S.data.groups[G.selected], name: state.name, members: state.members };
  box.innerHTML = `<div class="formrow">
      <label>名称 <input type="text" id="groupName" value="${esc(g.name)}" placeholder="新分组名称" style="width:220px"></label>
      ${isNew ? "" : `<span class="badge">${esc(g.kind || "manual")}</span><span class="note mono">${esc(G.selected)}</span>`}
      <span class="grow"></span>
      ${isNew ? `<button class="primary" data-create-group>预览创建</button>`
        : `<button class="primary" data-save-group>预览保存</button><button class="danger-btn" data-delete-group>预览删除</button>`}
    </div>
    <div class="note" id="groupDirty"></div>
    <div id="groupPicker"></div>`;
  G.picker = createPicker($("#groupPicker"), {
    items: skillItems, selected: g.members || [], placeholder: "搜索 skill 名称或描述，点击加入", draggable: true,
    emptyText: "空组：在下方搜索并点击 skill 加入",
    onChange: sel => {
      state.members = uniqueMembers([...sel]);
      updateDirty();
    },
  });
  G.picker.setQuery(state.query);
  $("#groupName").addEventListener("input", e => { state.name = e.target.value; updateDirty(); });
  $("#groupPicker input").addEventListener("input", e => { state.query = e.target.value; });
  updateDirty();
  setEditorWriteLocked(!!G.writePending);
}

/* ---------- 分发 ---------- */
function savedSource(agent) {
  const raw = (S.data.distribution_sources || {})[agent] || {};
  return { groups: Array.isArray(raw.groups) ? raw.groups : [], sids: Array.isArray(raw.sids) ? raw.sids : [], replace: !!raw.replace };
}
const sourceKey = s => JSON.stringify({ groups: [...(s.groups || [])].sort(), sids: [...(s.sids || [])].sort(), replace: !!s.replace });
const distAgents = () => $$("[data-dist-agent]:checked").map(x => x.dataset.distAgent);
const groupItems = () => Object.entries(S.data.groups || {}).map(([gid, g]) => ({ id: gid, label: g.name || gid, meta: `${(g.members || []).length} 个` }));

function renderDistribution() {
  $("#distPanel").innerHTML = `<h3>分发 <span class="note">仅在明确确认后写 agent</span></h3>
    <div class="sect">1. 目标 agent</div>
    <div class="formrow">${S.data.agents.map(a => `<label class="ck"><input type="checkbox" data-dist-agent="${esc(a.name)}">${esc(a.name)}</label>`).join("")}</div>
    <div class="note" id="distSaved">选择 agent 后自动回填其已保存的来源选择。</div>
    <div class="sect">2. 来源：分组</div><div id="distGroups"></div>
    <div class="sect">3. 来源：单个 skill</div><div id="distSkills"></div>
    <div class="formrow"><label class="ck"><input type="checkbox" id="distReplace">按选择移除不再需要的本库投影（需输入 REPLACE 确认）</label>
      <span class="grow"></span><button class="primary" data-distribute>预览分发</button></div>
    <div class="note" id="distProjection"></div>
    <p class="note">去重按投影目标；多个 agent 的已保存来源不同时不会自动合并，需手动选出一套共同来源。非 skillhub 管理项不会被移除。</p>`;
  const markDirty = () => { D.dirty = true; updateSummary(); };
  D.groupPicker = createPicker($("#distGroups"), { items: groupItems, placeholder: "搜索分组", emptyText: "未选择分组", onChange: markDirty });
  D.sidPicker = createPicker($("#distSkills"), { items: skillItems, placeholder: "搜索 skill 名称或描述", emptyText: "未选择单个 skill", onChange: markDirty });
  $("#distReplace").addEventListener("change", markDirty);
  syncSelection();
}

/** 根据所选 agent 回填已保存来源；多个 agent 来源不同则清空并提示。 */
function syncSelection() {
  const sources = distAgents().map(savedSource);
  D.conflict = sources.length > 1 && sources.some(s => sourceKey(s) !== sourceKey(sources[0]));
  const source = (!D.conflict && sources[0]) || { groups: [], sids: [], replace: false };
  D.groupPicker.set(source.groups);
  D.sidPicker.set(source.sids);
  $("#distReplace").checked = source.replace;
  D.dirty = false;
  updateSummary();
}

function updateSummary() {
  const agents = distAgents();
  const saved = $("#distSaved"), projection = $("#distProjection");
  if (!agents.length) saved.textContent = "选择 agent 后自动回填其已保存的来源选择。";
  else if (D.conflict) saved.innerHTML = `<span class="danger">所选 agent 的已保存来源不同，未自动合并；请手动选出共同来源后再预览。</span>`;
  else saved.textContent = "已保存来源：" + agents.map(agent => {
    const s = savedSource(agent);
    const groups = s.groups.map(gid => (S.data.groups[gid] || {}).name || `已删除分组:${gid}`);
    const sids = s.sids.map(sid => skillName(sid) === sid ? `已删除 skill:${sid}` : skillName(sid));
    return `${agent} [分组:${groups.join("、") || "无"}; skill:${sids.join("、") || "无"}; replace:${s.replace ? "是" : "否"}]`;
  }).join("；");
  if (D.dirty && agents.length) saved.innerHTML += ` <b>（当前选择已修改，确认后会覆盖所选 agent 的来源）</b>`;
  const linked = S.data.skills.filter(s => agents.some(a => ["linked", "trial"].includes(s.states?.[a]))).map(s => s.name);
  projection.textContent = agents.length ? `当前已投影 ${linked.length} 个：${linked.slice(0, 40).join("、")}${linked.length > 40 ? " …" : ""}` : "";
}

async function distribute() {
  const agents = distAgents();
  const groups = [...D.groupPicker.get()], sids = [...D.sidPicker.get()];
  if (!agents.length) { toast("请先选择至少一个目标 agent", "warn"); return; }
  if (D.conflict && !D.dirty) { toast("所选 agent 的已保存来源不同，请先手动选出一套共同来源", "warn"); return; }
  if (!groups.length && !sids.length && !D.dirty) { toast("请至少选择一个分组或单个 skill；若要清空来源，请先移除已选项再预览", "warn"); return; }
  const replace = $("#distReplace").checked;
  const ad = await confirmWrite("/api/distribute", { agents, groups, sids, replace }, {
    title: replace ? "分发并移除未选中的本库投影" : "分发已选择的 skill", phrase: replace ? "REPLACE" : undefined });
  if (ad) { toast("分发完成", "ok"); await reload(); }
}

/* ---------- 编辑器切换与写操作 ---------- */
async function switchContext(next) {
  const nextKey = contextKey(next.isNew, next.gid);
  if (nextKey === currentContext()) return;
  if (G.writePending) {
    toast("当前分组写操作尚未完成，请先等待或取消预览。", "warn");
    return;
  }
  if (G.switchPending) return;
  captureEditor();
  if (editorDirty()) {
    G.switchPending = true;
    try {
      if (!await confirmEditSwitch({ title: "切换分组" })) return;
    } finally {
      G.switchPending = false;
    }
  }
  G.selected = next.isNew ? null : next.gid;
  G.isNew = !!next.isNew;
  G.editor = null;
  G.picker = null;
  renderGroupList();
  renderGroupEditor();
}

async function confirmDirtyLeave(title) {
  if (G.writePending || G.switchPending) return false;
  captureEditor();
  if (!editorDirty()) return true;
  G.switchPending = true;
  try {
    return !!(await confirmEditSwitch({ title }));
  } finally {
    G.switchPending = false;
  }
}

async function runGroupWrite(context, work) {
  if (G.writePending) {
    toast("当前分组写操作尚未完成，请先等待或取消预览。", "warn");
    return null;
  }
  G.writePending = context;
  setEditorWriteLocked(true);
  try {
    return await work();
  } finally {
    if (G.writePending === context) {
      G.writePending = null;
      setEditorWriteLocked(false);
    }
  }
}

async function saveGroup(gid, members, name) {
  const old = S.data.groups[gid] || {};
  return confirmWrite("/api/groups", { group_id: gid, members, name, kind: old.kind || "manual", description: old.description || "" },
    { title: "保存分组成员与名称" });
}

async function createCurrentGroup() {
  const context = currentContext();
  captureEditor();
  const state = G.editor;
  const gid = (state?.name || "").trim();
  if (!gid) { toast("请填写分组名称", "warn"); return; }
  await runGroupWrite(context, async () => {
    const ad = await confirmWrite("/api/groups",
      { group_id: gid, members: uniqueMembers(state.members), name: gid, kind: "manual" },
      { title: "创建分组关系" });
    if (!ad) return;
    G.selected = gid;
    G.isNew = false;
    G.editor = null;
    G.picker = null;
    toast("分组已创建", "ok");
    await reload();
  });
}

async function saveCurrentGroup() {
  const context = currentContext();
  const gid = G.selected;
  captureEditor();
  const state = G.editor;
  const name = (state?.name || "").trim() || gid;
  const members = uniqueMembers(state.members);
  await runGroupWrite(context, async () => {
    const ad = await saveGroup(gid, members, name);
    if (!ad) return;
    markEditorClean(context, name, members);
    toast("分组已保存", "ok");
    await reload();
  });
}

async function deleteCurrentGroup() {
  const context = currentContext();
  const gid = G.selected;
  if (!await confirmDirtyLeave("删除分组")) return;
  await runGroupWrite(context, async () => {
    const ad = await confirmWrite("/api/groups/delete", { group_id: gid },
      { title: "删除分组（不删除 skill）" });
    if (!ad) return;
    clearEditorFor(context);
    G.selected = null;
    toast("分组已删除", "ok");
    await reload();
  });
}

export function bindGroups() {
  const el = $("#tab-gr");
  el.addEventListener("click", e => {
    let b;
    if ((b = e.target.closest("[data-group]"))) {
      return action(b, () => switchContext({ gid: b.dataset.group, isNew: false }));
    }
    if ((b = e.target.closest("[data-new-group]"))) {
      return action(b, () => switchContext({ gid: null, isNew: true }));
    }
    if ((b = e.target.closest("[data-create-group]"))) return action(b, createCurrentGroup);
    if ((b = e.target.closest("[data-save-group]"))) return action(b, saveCurrentGroup);
    if ((b = e.target.closest("[data-delete-group]"))) return action(b, deleteCurrentGroup);
    if ((b = e.target.closest("[data-distribute]"))) return action(b, distribute);
  });
  el.addEventListener("change", e => { if (e.target.matches("[data-dist-agent]")) syncSelection(); });
  // 拖拽：候选 skill → 左侧分组按钮
  el.addEventListener("dragover", e => {
    const t = e.target.closest("[data-drop-group]");
    if (t) { e.preventDefault(); t.classList.add("over"); }
  });
  el.addEventListener("dragleave", e => { e.target.closest?.("[data-drop-group]")?.classList.remove("over"); });
  el.addEventListener("drop", e => {
    const t = e.target.closest("[data-drop-group]");
    if (!t) return;
    e.preventDefault(); t.classList.remove("over");
    const sid = e.dataTransfer.getData("text/plain"), gid = t.dataset.dropGroup, old = S.data.groups[gid] || {};
    if (!sid || (old.members || []).includes(sid)) return;
    action(null, async () => {
      if (!await confirmDirtyLeave("拖拽加入分组")) return;
      const context = currentContext();
      await runGroupWrite(context, async () => {
        const ad = await saveGroup(gid, [...(old.members || []), sid], old.name || gid);
        if (!ad) return;
        clearEditorFor(context);
        toast("已将 " + skillName(sid) + " 加入 " + (old.name || gid), "ok");
        await reload();
      });
    });
  });
}
