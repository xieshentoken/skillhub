// 与后端的 HTTP 交互。所有请求同源，会话由 HttpOnly cookie 承载。

async function parse(response, fallback) {
  let body;
  try { body = await response.json(); }
  catch { throw new Error(`${fallback}（HTTP ${response.status}）`); }
  if (!response.ok || body.error) throw new Error(body.error || `${fallback}（HTTP ${response.status}）`);
  return body;
}

export async function getJSON(url) {
  const response = await fetch(url, {credentials: "same-origin"});
  return parse(response, "请求失败");
}

export async function postJSON(url, payload) {
  const response = await fetch(url, {
    method: "POST", credentials: "same-origin",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload),
  });
  return parse(response, "请求失败");
}
