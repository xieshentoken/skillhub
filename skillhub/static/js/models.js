// 模型：只读发现本机配置的模型；分组建议只发送 skill 名称和描述，结果需再次确认才写入分组。
import { S, reload } from "./state.js";
import { esc, skillName, $ } from "./util.js";
import { getJSON, postJSON } from "./api.js";
import { toast, action, confirmWrite, createPicker, skillItems, promptText } from "./ui.js";

const M = { suggestions: [], picker: null, loading: false, error: "" };

export function renderModels() {
  const el = $("#tab-md");
  if (!S.data.models) {
    el.innerHTML = M.error ? `<p class="empty">加载模型失败：${esc(M.error)} <button data-models-retry>重试</button></p>` : `<p class="empty">正在发现本机模型配置…</p>`;
    if (!M.loading && !M.error) loadModels();
    return;
  }
  const agents = S.data.models.agents || [];
  const callable = agents.filter(m => m.callable && m.models?.length);
  const rows = agents.map(m => `<tr><td><b>${esc(m.agent)}</b></td>
    <td>${m.available ? esc(m.models.join(", ")) : "—"}</td>
    <td>${m.callable ? `<span class="badge ok">可调用</span>` : `<span class="badge">不可调用</span>`}</td>
    <td class="note">${esc(m.reason)}</td></tr>`).join("");
  el.innerHTML = `<div class="section-head"><h2>模型</h2><p>只读取实际配置，不执行配置命令；密钥不会出现在页面上。</p></div>
    <div class="panel"><h3>分组建议</h3>
      <p class="note">调用只在点击后发生，发送字段严格为 skill 名称和描述；结果先展示，接受前还需确认。人工分组不会被覆盖，模型失败不会改组。</p>
      <div class="formrow"><label>agent <select id="modelAgent">${callable.map(m => `<option value="${esc(m.agent)}">${esc(m.agent)}</option>`).join("") || `<option value="">没有可安全调用的本机模型</option>`}</select></label>
        <label>模型 <select id="modelName"></select></label></div>
      <div class="sect">要分组的 skill</div>
      <div id="modelPicker"></div>
      <div class="formrow"><button class="primary" data-model-suggest ${callable.length && !S.data.read_only ? "" : "disabled"}>生成分组建议</button></div>
      <div id="modelSuggestions"></div></div>
    <div class="panel"><h3>发现结果</h3><div class="table-wrap"><table><thead><tr><th>agent</th><th>模型</th><th>调用</th><th>说明 / 限制</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="4" class="empty">无</td></tr>`}</tbody></table></div></div>`;
  const agentSelect = $("#modelAgent");
  const updateModels = () => {
    const selected = callable.find(m => m.agent === agentSelect.value);
    $("#modelName").innerHTML = (selected?.callable_models || selected?.models || []).map(x => `<option>${esc(x)}</option>`).join("");
  };
  agentSelect.addEventListener("change", updateModels);
  updateModels();
  const previous = M.picker ? [...M.picker.get()] : [];
  M.picker = createPicker($("#modelPicker"), { items: skillItems, selected: previous, placeholder: "搜索 skill 并点击加入", emptyText: "尚未选择 skill" });
  renderSuggestions();
}

async function loadModels() {
  M.loading = true; M.error = "";
  try { S.data.models = await getJSON("/api/models"); }
  catch (err) { M.error = err.message; }
  finally { M.loading = false; }
  if (S.tab === "md") renderModels();
}

function renderSuggestions() {
  const el = $("#modelSuggestions");
  if (!el) return;
  if (!M.suggestions.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="sect">待确认建议（${M.suggestions.length}）</div>
    <div class="formrow"><button class="primary" data-model-accept-all>确认接受全部建议</button></div>` +
    M.suggestions.map((s, i) => `<div class="attn-item"><div class="msg"><b>${esc(s.name)}</b>
        <div class="chips">${(s.members || []).map(id => `<span class="chip static">${esc(skillName(id))}</span>`).join("")}</div></div>
      <button class="small" data-model-accept="${i}">接受此组</button><button class="small link" data-model-adjust="${i}">改名后接受</button></div>`).join("");
}

async function acceptSuggestions(list, title, onDone) {
  const ad = await confirmWrite("/api/model-groups", { suggestions: list }, { title });
  if (ad) { onDone(); renderSuggestions(); toast("分组建议已应用", "ok"); await reload(); }
}

export function bindModels() {
  $("#tab-md").addEventListener("click", e => {
    let b;
    if (e.target.closest("[data-models-retry]")) { M.error = ""; renderModels(); return; }
    if ((b = e.target.closest("[data-model-suggest]"))) return action(b, async () => {
      const sids = [...M.picker.get()];
      if (!sids.length) { toast("先选择要分组的 skill", "warn"); return; }
      try {
        const d = await postJSON("/api/models/suggest", { agent: $("#modelAgent").value, model: $("#modelName").value, sids });
        M.suggestions = d.suggestions || [];
        renderSuggestions();
        if (!M.suggestions.length) toast("模型没有返回分组建议", "warn");
      } catch (err) { throw new Error("模型建议未应用：" + err.message); }
    });
    if ((b = e.target.closest("[data-model-accept-all]"))) return action(b, () =>
      acceptSuggestions(M.suggestions, "批量接受模型分组建议", () => { M.suggestions = []; }));
    if ((b = e.target.closest("[data-model-accept]"))) return action(b, () => {
      const i = Number(b.dataset.modelAccept);
      return acceptSuggestions([M.suggestions[i]], "接受单个模型分组建议", () => { M.suggestions.splice(i, 1); });
    });
    if ((b = e.target.closest("[data-model-adjust]"))) return action(b, async () => {
      const i = Number(b.dataset.modelAdjust);
      const name = await promptText({ title: "调整组名", label: "分组名称", value: M.suggestions[i].name });
      if (name === null) return;
      await acceptSuggestions([{ ...M.suggestions[i], name: name.trim() || M.suggestions[i].name }], "确认应用调整后的模型分组",
        () => { M.suggestions.splice(i, 1); });
    });
  });
}
