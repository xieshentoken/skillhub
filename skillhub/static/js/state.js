// 全局共享状态与少量跨模块钩子（避免模块间循环依赖）。
export const S = {
  data: null,   // /api/data 最近一次结果（另缓存 sources / models）
  tab: "ov",
};

const hooks = { reload: null, openSkill: null, navigate: null };

export function setHooks(values) { Object.assign(hooks, values); }

/** 重新拉取 /api/data 并重绘当前页。 */
export function reload() { return hooks.reload ? hooks.reload() : Promise.resolve(); }
/** 打开 skill 详情抽屉。 */
export function openSkill(sid, section) { if (hooks.openSkill) hooks.openSkill(sid, section); }
/** 切换页面，可带参数（例如 Skills 筛选）。 */
export function navigate(tab, params) { if (hooks.navigate) hooks.navigate(tab, params); }
