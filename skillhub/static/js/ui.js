// 通用 UI 组件：toast、按钮忙碌态、模态框、写操作预览确认、选择器。
import { S } from "./state.js";
import { esc, fmtSize, skillById } from "./util.js";
import { postJSON } from "./api.js";

/* ---------- toast ---------- */
export function toast(message, kind = "info", ms = 5000) {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.setAttribute("role", kind === "err" ? "alert" : "status");
  el.innerHTML = `<div class="msg"></div><button aria-label="关闭">×</button>`;
  el.querySelector(".msg").textContent = message;
  const close = () => el.remove();
  el.querySelector("button").addEventListener("click", close);
  box.appendChild(el);
  if (ms) setTimeout(close, kind === "err" ? ms * 2 : ms);
}
export const toastError = err => toast(err instanceof Error ? err.message : String(err), "err");

/** 运行异步操作期间禁用按钮并显示转圈，防止重复提交。 */
export async function withBusy(button, fn) {
  if (button && button.disabled) return;
  if (button) { button.disabled = true; button.classList.add("busy"); }
  try { return await fn(); }
  finally { if (button) { button.disabled = false; button.classList.remove("busy"); } }
}

/** 包装事件处理：忙碌态 + 错误 toast。 */
export function action(button, fn) {
  return withBusy(button, async () => {
    try { await fn(); } catch (err) { toastError(err); }
  });
}

/* ---------- modal ---------- */
export const modalOpen = () => !!document.querySelector(".modal-mask");

/**
 * 打开模态框，返回在 close(value) 时 resolve 的 Promise（Esc/取消为 null）。
 * buttons: [{label, key, className, value | onClick({close, button, body, els})}]。
 */
function openModal({ title, body, buttons, onMount }) {
  return new Promise(resolve => {
    const previous = document.activeElement;
    const mask = document.createElement("div");
    mask.className = "modal-mask";
    mask.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
      <header><h3 id="modalTitle"></h3></header><div class="mbody"></div><footer><span class="grow"></span></footer></div>`;
    mask.querySelector("#modalTitle").textContent = title;
    const mbody = mask.querySelector(".mbody");
    if (typeof body === "string") mbody.innerHTML = body; else if (body) mbody.appendChild(body);
    const footer = mask.querySelector("footer");
    let done = false;
    const close = value => {
      if (done) return; done = true;
      mask.remove();
      document.removeEventListener("keydown", onKey, true);
      if (previous && previous.focus) previous.focus();
      resolve(value);
    };
    const onKey = e => {
      if (e.key === "Escape") { e.stopPropagation(); e.preventDefault(); close(null); return; }
      if (e.key === "Tab") {
        const items = [...mask.querySelectorAll("button,input,select,textarea,summary,[tabindex]")].filter(x => !x.disabled);
        if (!items.length) return;
        const first = items[0], last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    };
    document.addEventListener("keydown", onKey, true);
    const els = {};
    for (const b of buttons) {
      const el = document.createElement("button");
      el.textContent = b.label;
      if (b.className) el.className = b.className;
      el.addEventListener("click", () => b.onClick ? b.onClick({ close, button: el, body: mbody, els }) : close(b.value ?? null));
      footer.appendChild(el);
      els[b.key || b.label] = el;
    }
    document.body.appendChild(mask);
    // 默认聚焦“取消”，避免误按回车直接执行写操作。
    const focusTarget = mbody.querySelector("[autofocus]") || footer.querySelector("button");
    if (focusTarget) focusTarget.focus();
    if (onMount) onMount({ close, body: mbody, els });
  });
}

export function promptText({ title, label, value = "" }) {
  const body = document.createElement("div");
  body.innerHTML = `<label class="field">${esc(label)}<input type="text" autofocus></label>`;
  const input = body.querySelector("input");
  input.value = value;
  return openModal({ title, body, buttons: [
    { label: "取消", value: null },
    { label: "确定", key: "confirm", className: "primary", onClick: ({ close }) => close(input.value) },
  ]});
}

/** 有未保存编辑时，确认是否放弃当前编辑并离开。Esc 等同于继续编辑。 */
export function confirmEditSwitch({ title = "放弃未保存修改？" } = {}) {
  const body = document.createElement("p");
  body.textContent = "当前分组有未保存的名称、成员或搜索条件。";
  return openModal({ title, body, buttons: [
    { label: "继续编辑", key: "continue", value: false },
    { label: "放弃修改并切换", key: "discard", className: "danger-btn", value: true },
  ]});
}

/* ---------- 写操作计划渲染 ---------- */
const GOOD = new Set(["link", "write", "merge", "create", "restore", "trial", "import", "add", "update", "rename", "publish"]);
const REMOVE = new Set(["unlink", "remove", "prune_store", "delete", "trash"]);
const BAD = new Set(["conflict", "copy_drift", "store_drift", "broken", "copy_broken", "blocked", "error", "invalid"]);
const TYPE_LABEL = {link: "投影", unlink: "解除", skip: "跳过", write: "写入", merge: "合并", remove: "移除",
  keep: "保留", conflict: "冲突", blocked: "门禁拦截", error: "错误", broken: "断链", store_drift: "中央库漂移",
  copy_drift: "copy 漂移", copy_broken: "copy 断链", trial: "试用", invalid: "无效", prune_store: "精简"};
const SEGMENT_LABEL = {add: "将投影", remove: "将移除", desired: "目标集合", linked: "已投影", servers: "MCP server",
  envs: "需设置的环境变量", members: "成员", sids: "skill", groups: "分组", keep: "保留份数"};
const SKIP_SEGMENTS = new Set(["actions", "agents", "plan", "result"]);

function typeClass(type) {
  if (BAD.has(type)) return "bad";
  if (REMOVE.has(type)) return "warn";
  if (GOOD.has(type)) return "ok";
  return "";
}
function labelPath(path) {
  const parts = path.filter(p => !SKIP_SEGMENTS.has(p));
  return parts.map(p => SEGMENT_LABEL[p] || p).join(" · ") || "操作";
}
function isAction(x) { return x && typeof x === "object" && !Array.isArray(x) && typeof x.type === "string"; }

function collect(value, path, out, depth) {
  if (Array.isArray(value)) {
    if (value.length && value.every(isAction)) out.actions.push({ path, items: value });
    else if (value.length && value.every(x => typeof x === "string")) out.lists.push({ path, items: value });
    return;
  }
  if (value && typeof value === "object" && depth < 4) {
    for (const [k, v] of Object.entries(value)) collect(v, [...path, k], out, depth + 1);
  }
}

/** 把预览结果渲染为可读的动作列表；原始 JSON 折叠在下方。 */
export function renderPlan(plan) {
  const out = { actions: [], lists: [] };
  collect(plan, [], out, 0);
  const scalars = Object.entries(plan || {}).filter(([k, v]) => k !== "mode" && (typeof v !== "object" || v === null));
  const allActions = out.actions.flatMap(g => g.items);
  const bad = allActions.filter(a => BAD.has(a.type));
  let html = "";
  if (bad.length) html += `<div class="banner warn">有 ${bad.length} 项不会按预期执行（冲突 / 门禁 / 漂移 / 错误），详见下方。</div>`;
  if (!allActions.length && !out.lists.length && !scalars.length) html += `<div class="banner info">预览没有返回可展示的变更。</div>`;
  if (scalars.length) {
    html += `<dl class="kv">${scalars.map(([k, v]) => `<dt>${esc(SEGMENT_LABEL[k] || k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`;
  }
  for (const group of out.actions) {
    html += `<div class="plan-group"><h4>${esc(labelPath(group.path))} <span class="badge">${group.items.length}</span></h4>` +
      group.items.map(a => {
        const target = a.target || a.ts || a.id || a.sid || "";
        const detail = a.detail || (a.size != null ? fmtSize(a.size) : "");
        return `<div class="plan-row"><span class="badge ${typeClass(a.type)}">${esc(TYPE_LABEL[a.type] || a.type)}</span>
          <div>${esc(detail)}${target ? `<div class="t">${esc(target)}</div>` : ""}</div></div>`;
      }).join("") + `</div>`;
  }
  for (const list of out.lists) {
    const shown = list.items.slice(0, 60);
    html += `<div class="plan-group"><h4>${esc(labelPath(list.path))} <span class="badge">${list.items.length}</span></h4>
      <div class="chips">${shown.map(x => `<span class="chip static">${esc(skillById(x)?.name || x)}</span>`).join("")}
      ${list.items.length > shown.length ? `<span class="note">另 ${list.items.length - shown.length} 项</span>` : ""}</div></div>`;
  }
  const raw = JSON.stringify(plan, null, 2);
  html += `<details class="raw"><summary>原始预览 JSON</summary><pre>${esc(raw.length > 40000 ? raw.slice(0, 40000) + "\n…" : raw)}</pre></details>`;
  return html;
}

/**
 * 写操作统一流程：先 apply:false 预览 → 模态框展示计划 → 用户确认（高风险需输入确认短语）
 * → apply:true 执行。用户取消返回 null；预览失败抛出异常；执行失败在模态框内显示并允许重试/取消。
 */
export async function confirmWrite(url, payload, { title, phrase, confirmLabel = "确认执行" } = {}) {
  if (S.data && S.data.read_only) { toast("GUI 处于只读模式，写操作已禁用", "warn"); return null; }
  const preview = await postJSON(url, { ...payload, apply: false });
  const body = document.createElement("div");
  body.innerHTML = renderPlan(preview) +
    (phrase ? `<div class="phrase">高风险写操作，请输入 <b class="mono">${esc(phrase)}</b> 确认：<input type="text" autocomplete="off" spellcheck="false"></div>` : "") +
    `<div class="apply-error"></div>`;
  const phraseInput = body.querySelector(".phrase input");
  const buttons = [
    { label: "取消", value: null },
    { label: confirmLabel, key: "confirm", className: phrase ? "danger-btn" : "primary", onClick: async ({ close, button }) => {
      if (phrase && phraseInput.value !== phrase) return;
      const request = { ...payload, apply: true, confirm: true };
      if (phrase) request.confirm_phrase = phrase;
      const errorBox = body.querySelector(".apply-error");
      errorBox.innerHTML = "";
      await withBusy(button, async () => {
        try { close(await postJSON(url, request)); }
        catch (err) { errorBox.innerHTML = `<div class="banner bad">执行失败：${esc(err.message)}</div>`; }
      });
    } },
  ];
  const onMount = ({ els }) => {
    if (!phraseInput) return;
    els.confirm.disabled = true;
    phraseInput.addEventListener("input", () => { els.confirm.disabled = phraseInput.value !== phrase; });
    phraseInput.addEventListener("keydown", e => { if (e.key === "Enter" && !els.confirm.disabled) els.confirm.click(); });
    phraseInput.focus();
  };
  return openModal({ title: "预览：" + title, body, buttons, onMount });
}

/* ---------- 搜索选择器 ---------- */
/**
 * 可搜索的多选器：上方为已选 chips（可移除），下方为搜索框和候选列表。
 * options.items(): [{id, label, desc, meta}]；options.onChange(selectedSet)。
 * options.draggable：候选项可拖拽（dataTransfer 为 id）。
 */
export function createPicker(container, { items, selected = [], onChange, placeholder = "搜索…", limit = 80, draggable = false, emptyText = "未选择" }) {
  const state = { selected: new Set(selected), q: "" };
  container.innerHTML = `<div class="chips" data-chips></div>
    <div class="picker"><input type="search" placeholder="${esc(placeholder)}"><div class="picker-list" data-list></div></div>`;
  const chipsEl = container.querySelector("[data-chips]");
  const listEl = container.querySelector("[data-list]");
  const input = container.querySelector("input");
  const lookup = () => new Map(items().map(x => [x.id, x]));

  function renderChips() {
    const map = lookup();
    chipsEl.innerHTML = state.selected.size
      ? [...state.selected].map(id => `<span class="chip">${esc(map.get(id)?.label || id)}${map.has(id) ? "" : " (已删除)"}<button type="button" data-remove="${esc(id)}" aria-label="移除 ${esc(map.get(id)?.label || id)}">×</button></span>`).join("")
      : `<span class="note">${esc(emptyText)}</span>`;
  }
  function renderList() {
    const q = state.q.toLowerCase();
    const matches = items().filter(x => !state.selected.has(x.id) &&
      (!q || (x.label + " " + (x.desc || "") + " " + x.id).toLowerCase().includes(q)));
    const shown = matches.slice(0, limit);
    listEl.innerHTML = shown.map(x => `<div class="picker-item" role="button" tabindex="0" data-add="${esc(x.id)}" ${draggable ? `draggable="true" data-drag-sid="${esc(x.id)}"` : ""}>
        <span>＋ ${esc(x.label)}</span>${x.meta ? `<span class="badge">${esc(x.meta)}</span>` : ""}<span class="d">${esc(x.desc || "")}</span></div>`).join("") +
      (matches.length > shown.length ? `<div class="picker-more">还有 ${matches.length - shown.length} 项，输入关键字缩小范围</div>` : "") +
      (!matches.length ? `<div class="picker-more">没有匹配项</div>` : "");
  }
  const changed = () => { renderChips(); renderList(); if (onChange) onChange(new Set(state.selected)); };
  input.addEventListener("input", () => { state.q = input.value.trim(); renderList(); });
  container.addEventListener("click", e => {
    const add = e.target.closest("[data-add]");
    if (add) { state.selected.add(add.dataset.add); changed(); return; }
    const rm = e.target.closest("[data-remove]");
    if (rm) { state.selected.delete(rm.dataset.remove); changed(); }
  });
  container.addEventListener("keydown", e => {
    const add = e.target.closest("[data-add]");
    if (add && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); state.selected.add(add.dataset.add); changed(); }
  });
  if (draggable) container.addEventListener("dragstart", e => {
    const item = e.target.closest("[data-drag-sid]");
    if (item) e.dataTransfer.setData("text/plain", item.dataset.dragSid);
  });
  renderChips(); renderList();
  return {
    get: () => new Set(state.selected),
    getQuery: () => input.value,
    set(ids) { state.selected = new Set(ids); renderChips(); renderList(); },
    setQuery(value) { input.value = String(value || ""); state.q = input.value.trim(); renderList(); },
    add(id) { state.selected.add(id); changed(); },
  };
}

/** Skill 候选项（供选择器使用）。 */
export function skillItems() {
  return (S.data?.skills || []).map(s => ({ id: s.id, label: s.name, desc: s.desc, meta: s.channel === "ul" ? "ul" : "" }));
}
