"use strict";
const $ = (selector, parent = document) => parent.querySelector(selector);
const icons = {
  chat: '<path d="M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9H13a8.5 8.5 0 0 1 8 8v.5Z"/>',
  book: '<path d="M4 3h6a3 3 0 0 1 3 3v15a4 4 0 0 0-4-2H4z"/><path d="M20 3h-4a3 3 0 0 0-3 3v15a4 4 0 0 1 4-2h3z"/>',
  database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6z"/><path d="m8 12 3 3 5-6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/>',
  settings: '<path d="m9 3-1 3-3 1-2 3 2 2-1 3 3 2 3-1 2 3 3-1 1-3 3-1 2-3-2-2 1-3-3-2-3 1-2-3z"/><circle cx="12" cy="12" r="3"/>',
  export: '<path d="M12 16V3m-4 4 4-4 4 4M5 13v7h14v-7"/>',
  code: '<path d="m8 6-6 6 6 6m8-12 6 6-6 6m-3-16-2 20"/>',
  layers: '<path d="m12 3 10 6-10 6L2 9zM2 13l10 6 10-6M2 17l10 6 10-6"/>',
  pulse: '<path d="M2 12h4l3-8 5 16 3-8h5"/>',
  paperclip: '<path d="m8 12 6-6a3 3 0 0 1 4 4l-8 8a5 5 0 0 1-7-7l9-9a4 4 0 0 1 6 6l-9 9a2 2 0 0 1-3-3l8-8"/>',
  "arrow-up": '<path d="M12 20V4m-6 6 6-6 6 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  upload: '<path d="M12 16V3m-4 4 4-4 4 4M4 15v6h16v-6"/>',
};
function icon(name) { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.chat}</svg>`; }
function hydrateIcons(parent = document) { parent.querySelectorAll("[data-icon]").forEach(el => { el.innerHTML = icon(el.dataset.icon); }); }
hydrateIcons();
const state = { session: null, sessions: [], attachments: [], busy: false, uploading: false, controller: null, status: null, panel: null };
let token = sessionStorage.getItem("chatdb-token") || "";
let filePurpose = "attachment", toastTimer, codeCounter = 0;
const codeRegistry = new Map();
const resultRegistry = new Map();
const agentNames = { auto: "智能路由", sql: "SQL 编码", knowledge: "知识问答", design: "结构设计", diagnosis: "故障诊断" };
const esc = value => String(value == null ? "" : value).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
function toast(message) { const el = $("#toast"); el.textContent = message; el.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; }, 4500); }
async function api(path, options = {}) {
  const headers = { ...(options.body && !(options.body instanceof FormData) ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...options.headers };
  const response = await fetch(path, { ...options, headers });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { const data = await response.json(); message = typeof data.detail === "string" ? data.detail : message; } catch (_) {}
    if (response.status === 401 && state.panel !== "settings") openPanel("settings");
    throw new Error(message);
  }
  return response;
}
async function json(path, options) { return (await api(path, options)).json(); }
function download(content, filename, type) {
  const url = URL.createObjectURL(content instanceof Blob ? content : new Blob([content], { type }));
  const link = document.createElement("a"); link.href = url; link.download = filename; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("已复制"); } catch (_) { toast("浏览器不允许复制，请手动选择文本"); }
}
function inline(text) { return esc(text).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>"); }
function highlightSQL(sql) {
  const regex = /'(?:''|[^'])*'|\b(?:SELECT|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|ON|AS|AND|OR|NOT|GROUP|BY|ORDER|DESC|ASC|LIMIT|OFFSET|WITH|UNION|ALL|HAVING|CASE|WHEN|THEN|ELSE|END|DISTINCT|COUNT|SUM|AVG|ROUND|CREATE|TABLE|PRIMARY|KEY|INTEGER|TEXT|NUMERIC|NULL|IS|BETWEEN|IN)\b|\b\d+(?:\.\d+)?\b/gi;
  let html = "", position = 0;
  for (const match of sql.matchAll(regex)) {
    html += esc(sql.slice(position, match.index));
    const type = match[0].startsWith("'") ? "string" : /^\d/.test(match[0]) ? "number" : "keyword";
    html += `<span class="sql-${type}">${esc(match[0])}</span>`; position = match.index + match[0].length;
  }
  return html + esc(sql.slice(position));
}
function prose(text) {
  const lines = text.split("\n"); let html = "", listType = null;
  const closeList = () => { if (listType) { html += `</${listType}>`; listType = null; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trim();
    if (!line) { closeList(); continue; }
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      closeList();
      const cells = value => value.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|");
      html += "<div class=\"table-scroll\"><table><thead><tr>" + cells(line).map(v => `<th>${inline(v.trim())}</th>`).join("") + "</tr></thead><tbody>"; i++;
      while (i + 1 < lines.length && lines[i + 1].includes("|")) {
        html += "<tr>" + cells(lines[++i]).map(v => `<td>${inline(v.trim())}</td>`).join("") + "</tr>";
      }
      html += "</tbody></table></div>"; continue;
    }
    const list = line.match(/^([-*]|\d+\.)\s+(.+)/);
    if (list) {
      const type = /^\d/.test(list[1]) ? "ol" : "ul";
      if (listType !== type) { closeList(); html += `<${type}>`; listType = type; }
      html += `<li>${inline(list[2])}</li>`; continue;
    }
    closeList();
    const heading = line.match(/^(#{1,3})\s+(.+)/);
    if (heading) html += `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`;
    else if (line.startsWith(">")) html += `<blockquote>${inline(line.slice(1).trim())}</blockquote>`;
    else html += `<p>${inline(line)}</p>`;
  }
  closeList(); return html;
}
function markdown(text) {
  const parts = text.split(/```([a-zA-Z0-9_-]*)[ \t]*\n([\s\S]*?)```/g);
  let output = "";
  for (let i = 0; i < parts.length; i += 3) {
    output += prose(parts[i]);
    if (i + 2 < parts.length) {
      const language = parts[i + 1].toLowerCase(), code = parts[i + 2].trimEnd(), id = String(++codeCounter);
      codeRegistry.set(id, code);
      const isSQL = language === "sql";
      output += `<div class="code-block"><div class="code-header"><span>${esc(language || "code").toUpperCase()}</span><button type="button" data-copy-code="${id}">复制代码</button></div><pre><code>${isSQL ? highlightSQL(code) : esc(code)}</code></pre>${isSQL ? `<div class="code-actions"><button class="small-button primary" data-run-code="${id}">执行查询</button><button class="small-button" data-explain-code="${id}">查看计划</button><span style="font-size:9px;color:#93a381;align-self:center">仅允许只读 SELECT</span></div>` : ""}<div class="code-result"></div></div>`;
    }
  }
  return output;
}
function traceHTML(meta) {
  if (!meta.traces?.length) return "";
  return `<details class="trace"><summary>工具活动 · ${meta.traces.length} 步已完成</summary><div class="trace-content">${meta.traces.map(t => `<div class="trace-step">${esc(t)}</div>`).join("")}</div></details>`;
}
function referencesHTML(refs = []) {
  if (!refs.length) return "";
  return `<div class="references"><span class="references-title">参考来源 · ${refs.length} 个检索片段</span>${refs.map((r, i) => `<details class="reference"><summary>[${i + 1}] ${esc(r.name)} · 片段 ${esc(r.position)}</summary><p>${esc(r.content)}</p>${/^https?:\/\//.test(r.source) ? `<a href="${esc(r.source)}" target="_blank" rel="noopener noreferrer">${esc(r.source)} ↗</a>` : `<span>${esc(r.source)}</span>`}</details>`).join("")}</div>`;
}
function scrollBottom() { const el = $("#conversation"); el.scrollTop = el.scrollHeight; }
function addMessage(role, content, meta = {}, messageId = "") {
  $("#welcome").hidden = true; $("#suggestions").hidden = true;
  const el = document.createElement("article"); el.className = `message ${role}`; el.dataset.id = messageId;
  if (role === "user") {
    el.innerHTML = `<div class="user-bubble">${esc(content)}${meta.attachments?.length ? `<span class="user-files">附件：${meta.attachments.map(a => esc(a.name)).join("、")}</span>` : ""}</div>`;
  } else {
    el.innerHTML = `<div class="assistant-header"><span class="assistant-logo">✦</span>ChatDB<span class="agent-tag">${esc(agentNames[meta.agent] || "正在处理")}</span></div><div class="trace-container">${traceHTML(meta)}</div><div class="markdown">${content ? markdown(content) : '<span class="typing">● ● ●</span>'}</div><div class="reference-container">${referencesHTML(meta.references)}</div><div class="message-footer"></div>`;
    el._content = content;
    finishMessage(el, meta);
  }
  $("#messages").append(el);
  return el;
}
function finishMessage(el, meta) {
  if (!el || meta.status === "streaming" || !meta.status) return;
  $(".message-footer", el).innerHTML = `<button class="copy-answer">复制回答</button><span>${meta.status === "interrupted" ? "生成已停止" : meta.status === "error" ? "生成遇到错误" : meta.mode === "demo" ? "本地演示" : "回答完成"}</span>`;
}
function setBusy(busy) {
  state.busy = busy;
  $("#send").classList.toggle("stop", busy);
  $("#send").innerHTML = busy ? '<span style="width:10px;height:10px;background:currentColor;border-radius:2px"></span>' : icon("arrow-up");
  $("#send").setAttribute("aria-label", busy ? "停止生成" : "发送消息");
  $("#prompt").disabled = busy; $("#agent").disabled = busy; $("#attach").disabled = busy || state.uploading;
}
async function refreshSessions() {
  state.sessions = await json("/api/sessions");
  renderHistory();
}
function renderHistory() {
  const filter = $("#history-search").value.toLowerCase();
  const groups = new Map();
  for (const item of state.sessions.filter(s => s.title.toLowerCase().includes(filter))) {
    const today = new Date(); today.setHours(0, 0, 0, 0);
    const timestamp = new Date(item.updated_at);
    const days = (today - timestamp) / 86400000;
    const group = timestamp >= today ? "今天" : days < 7 ? "近 7 天" : days < 30 ? "近 30 天" : "更早";
    if (!groups.has(group)) groups.set(group, []); groups.get(group).push(item);
  }
  $("#history-list").innerHTML = groups.size ? [...groups].map(([label, items]) => `<div class="history-group">${label}</div>${items.map(item => `<div class="history-item ${item.id === state.session ? "selected" : ""}"><button data-session="${item.id}" title="${esc(item.title)}">${icon("chat")}${esc(item.title)}</button><button class="history-delete" data-delete-session="${item.id}" aria-label="删除会话">×</button></div>`).join("")}`).join("") : `<div class="history-empty">${filter ? "没有匹配的对话" : "还没有对话<br>从一个数据库问题开始"}</div>`;
}
function resetConversation() {
  state.session = null; state.attachments = []; $("#messages").innerHTML = ""; codeRegistry.clear(); resultRegistry.clear();
  $("#welcome").hidden = false; $("#suggestions").hidden = false; $("#page-title").textContent = "智能对话"; $("#prompt").value = ""; $("#prompt").style.height = "";
  renderAttachments(); renderHistory(); $("#prompt").focus(); $("#sidebar").classList.remove("open");
}
async function selectSession(id) {
  if (state.busy) return toast("请先停止当前回答");
  try {
    const detail = await json(`/api/sessions/${id}`);
    state.session = id; state.attachments = []; renderAttachments(); $("#messages").innerHTML = ""; codeRegistry.clear(); resultRegistry.clear();
    $("#welcome").hidden = detail.messages.length > 0; $("#suggestions").hidden = detail.messages.length > 0;
    detail.messages.forEach(m => addMessage(m.role, m.content, m.metadata, m.id));
    $("#page-title").textContent = detail.session.title; renderHistory(); scrollBottom(); $("#sidebar").classList.remove("open");
  } catch (error) { toast(error.message); }
}
async function submitChat(requestedAgent) {
  if (state.busy) { state.controller?.abort(); return; }
  if (state.uploading) return toast("附件正在上传，请稍候");
  const content = $("#prompt").value.trim(); if (!content) return $("#prompt").focus();
  const selectedAgent = requestedAgent || $("#agent").value || "auto";
  setBusy(true); const attachments = [...state.attachments]; let assistant, answer = "", meta = { status: "streaming" };
  try {
    if (!state.session) { const item = await json("/api/sessions", { method: "POST" }); state.session = item.id; }
    state.controller = new AbortController();
    const response = await api(`/api/sessions/${state.session}/chat`, { method: "POST", signal: state.controller.signal,
      body: JSON.stringify({ content, agent: agentNames[selectedAgent] ? selectedAgent : "auto", attachment_ids: attachments.map(a => a.id) }) });
    addMessage("user", content, { attachments });
    assistant = addMessage("assistant", "", meta);
    $("#prompt").value = ""; $("#prompt").style.height = ""; state.attachments = []; renderAttachments();
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = "", lastRender = 0;
    const render = () => {
      const nearBottom = $("#conversation").scrollHeight - $("#conversation").scrollTop - $("#conversation").clientHeight < 150;
      $(".markdown", assistant).innerHTML = markdown(answer); assistant._content = answer;
      if (nearBottom) scrollBottom(); lastRender = Date.now();
    };
    scrollBottom();
    while (true) {
      const { value, done } = await reader.read(); if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let split;
      while ((split = buffer.indexOf("\n\n")) !== -1) {
        const block = buffer.slice(0, split); buffer = buffer.slice(split + 2);
        const event = block.split("\n").find(l => l.startsWith("event:"))?.slice(6).trim();
        const data = block.split("\n").filter(l => l.startsWith("data:")).map(l => l.slice(5).trim()).join("\n");
        if (!data) continue; const payload = JSON.parse(data);
        if (event === "start") assistant.dataset.id = payload.message_id;
        if (event === "context") { meta = payload; $(".agent-tag", assistant).textContent = agentNames[meta.agent]; $(".trace-container", assistant).innerHTML = traceHTML(meta); }
        if (event === "delta") { answer += payload.text; if (Date.now() - lastRender > 75) render(); }
        if (event === "done") { meta = payload.metadata; render(); $(".reference-container", assistant).innerHTML = referencesHTML(meta.references); finishMessage(assistant, meta); }
        if (event === "error") { meta.status = "error"; answer += "\n\n" + payload.message; render(); toast(payload.message); }
      }
    }
    if (meta.status === "streaming") meta.status = "interrupted";
    render(); finishMessage(assistant, meta);
  } catch (error) {
    if (error.name === "AbortError") { meta.status = "interrupted"; toast("已停止生成"); }
    else { meta.status = "error"; toast(error.message); }
    if (assistant) {
      if (!answer) answer = error.name === "AbortError" ? "生成已停止。" : error.message;
      $(".markdown", assistant).innerHTML = markdown(answer); assistant._content = answer; finishMessage(assistant, meta);
    }
  } finally {
    setBusy(false); state.controller = null; $("#prompt").focus();
    try { await refreshSessions(); $("#page-title").textContent = state.sessions.find(s => s.id === state.session)?.title || "智能对话"; } catch (_) {}
  }
}
function renderAttachments() {
  $("#attachment-list").innerHTML = state.attachments.map(a => `<span class="attachment-chip">${icon("paperclip")}${esc(a.name)}<button type="button" data-remove-attachment="${a.id}" aria-label="移除附件">×</button></span>`).join("");
}
async function uploadFile(file, purpose) {
  if (!file) return;
  if (state.busy) return toast("请在回答完成后上传");
  if (state.uploading) return toast("已有文件正在上传");
  if (file.size > 10 * 1024 * 1024) return toast("文件不能超过 10MB");
  if (purpose === "attachment" && state.attachments.length >= 3) return toast("每条消息最多附加 3 个文件");
  state.uploading = true; $("#attach").disabled = true; toast("正在解析文档…");
  const form = new FormData(); form.append("file", file);
  try {
    const result = await json(purpose === "knowledge" ? "/api/knowledge" : "/api/attachments", { method: "POST", body: form });
    if (purpose === "knowledge") { if (state.panel === "knowledge") await openPanel("knowledge"); toast("文档已入库，可在问答中检索"); }
    else { state.attachments.push(result); renderAttachments(); toast("附件已解析，将随下一条消息发送"); }
  } catch (error) { toast(error.message); }
  finally { state.uploading = false; $("#attach").disabled = state.busy; }
}
function resultHTML(result) {
  const id = String(++codeCounter); resultRegistry.set(id, result);
  return `<div class="result"><div class="result-info"><span>${result.explain ? "执行计划" : `${result.rows.length} 行结果`} · ${result.elapsed_ms} ms${result.truncated ? ` · 已截断，最多 ${result.limit} 行` : ""}</span><button class="small-button" data-export-result="${id}">导出 CSV</button></div><div class="table-scroll"><table><thead><tr>${result.columns.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead><tbody>${result.rows.map(row => `<tr>${row.map(v => `<td>${v === null ? '<span style="color:#a0ad91">NULL</span>' : esc(typeof v === "object" ? JSON.stringify(v) : v)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>${!result.rows.length ? '<p style="text-align:center;color:#97a888;font-size:11px">查询成功，没有匹配的数据。</p>' : ""}</div>`;
}
async function runQuery(sql, explain, target, button) {
  button.disabled = true; target.innerHTML = '<div class="trace">正在进行只读校验与查询…</div>';
  try {
    const result = await json("/api/database/query", { method: "POST", body: JSON.stringify({ sql, explain, session_id: state.session }) });
    target.innerHTML = resultHTML(result);
  } catch (error) { target.innerHTML = `<div class="result-error">${esc(error.message)}</div>`; }
  finally { button.disabled = false; }
}
function closePanel() { $("#panel").hidden = true; $("#panel-backdrop").hidden = true; state.panel = null; $("#prompt").focus(); }
function connectionForm(config) {
  const c = config.connection;
  return `<form class="settings-card" id="connection-form" autocomplete="off">
    <h3>数据库连接</h3><p>填写连接参数，测试成功后可保存并立即切换。</p>
    <label class="field-label" for="connection-type">数据库类型</label>
    <select class="token-input" id="connection-type">${config.types.map(t => `<option value="${t.type}" ${t.type === c.type ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</select>
    <div class="connection-grid" id="network-fields">
      <label class="field-label">主机 IP / 地址<input class="token-input" id="connection-host" value="${esc(c.host)}" placeholder="127.0.0.1"></label>
      <label class="field-label">端口<input class="token-input" id="connection-port" type="number" min="1" max="65535" value="${c.port || ""}"></label>
      <label class="field-label">用户名<input class="token-input" id="connection-user" value="${esc(c.username)}" autocomplete="off"></label>
      <label class="field-label">密码<input class="token-input" id="connection-password" type="password" autocomplete="new-password" placeholder="输入数据库密码"></label>
    </div>
    <label id="reuse-password-label" class="field-label"><input type="checkbox" id="reuse-password" ${c.has_password ? "checked" : ""}> 沿用当前连接已保存的密码</label>
    <label class="field-label" for="connection-database" id="database-field-label">数据库名</label>
    <input class="token-input" id="connection-database" value="${esc(c.database)}">
    <div id="oracle-fields" hidden><label class="field-label" for="oracle-mode">Oracle 连接方式</label><select class="token-input" id="oracle-mode"><option value="service_name">服务名（Service Name）</option><option value="sid">SID</option></select></div>
    <div id="schema-fields"><label class="field-label" for="connection-schemas">允许访问的 Schema</label><input class="token-input" id="connection-schemas" value="${esc(c.schemas)}" placeholder="多个 Schema 用逗号分隔"></div>
    <p id="connection-help"></p><div class="panel-buttons"><button type="button" class="small-button" id="test-connection">测试连接</button><button type="submit" class="small-button primary" id="save-connection">保存并连接</button></div>
    <p id="connection-feedback" role="status" aria-live="polite"></p>
  </form>`;
}
function bindConnectionForm(config) {
  const c = config.connection;
  $("#oracle-mode").value = c.oracle_mode || "service_name";
  function updateReuse() {
    const t = config.types.find(t => t.type === $("#connection-type").value);
    const same = c.has_password && t.type === c.type && $("#connection-host").value.trim() === c.host &&
      $("#connection-user").value.trim() === c.username && Number($("#connection-port").value || t.port) === Number(c.port || t.port);
    $("#reuse-password").disabled = !same;
    if (!same || $("#connection-password").value) $("#reuse-password").checked = false;
  }
  function fields(changed = false) {
    const t = config.types.find(t => t.type === $("#connection-type").value);
    const sqlite = t.type === "sqlite", mysql = ["mysql", "mariadb"].includes(t.type), oracle = t.type === "oracle";
    $("#network-fields").hidden = sqlite;
    $("#reuse-password-label").hidden = sqlite;
    $("#oracle-fields").hidden = !oracle;
    $("#schema-fields").hidden = sqlite || mysql;
    $("#database-field-label").textContent = sqlite ? "SQLite 文件路径（服务器本地）" : oracle ? "服务名 / SID" : "数据库名";
    $("#connection-database").placeholder = sqlite ? "D:\\data\\business.sqlite3" : mysql ? "可留空，连接后选择业务库" : "业务数据库";
    $("#connection-help").textContent = sqlite ? "只读打开已有文件。路径相对于项目目录，不会创建业务库。" :
      oracle ? "使用 Thin 模式，无需 Oracle 客户端。Schema 默认使用用户名；执行计划暂不支持。" :
      t.type === "mssql" ? "使用 SQL Server 账号认证，无需 ODBC。请使用只读账号；查看计划还需 SHOWPLAN 权限。" :
      "支持域名、IPv4 和 IPv6。密码中的特殊字符会自动处理，请使用只读账号。";
    if (changed) {
      $("#connection-port").value = t.port || "";
      $("#connection-schemas").value = t.schema || "";
      $("#connection-password").value = "";
      $("#connection-database").value = "";
      $("#reuse-password").checked = false;
      $("#connection-feedback").textContent = "";
      if (oracle) $("#oracle-mode").value = "service_name";
    }
    updateReuse();
  }
  $("#connection-type").onchange = () => fields(true);
  ["connection-host", "connection-port", "connection-user", "connection-password"].forEach(id => {
    $("#" + id).addEventListener("input", updateReuse);
  });
  fields();
  async function submit(save) {
    if (!$("#connection-form").reportValidity()) return;
    if (save && state.busy) return toast("请先停止当前回答");
    const body = {
      type: $("#connection-type").value, host: $("#connection-host").value.trim(),
      port: $("#connection-port").value ? Number($("#connection-port").value) : null,
      username: $("#connection-user").value.trim(), password: $("#connection-password").value,
      reuse_password: $("#reuse-password").checked && !$("#reuse-password").disabled,
      database: $("#connection-database").value.trim(), schemas: $("#connection-schemas").value.trim(),
      oracle_mode: $("#oracle-mode").value,
    };
    const buttons = [$("#test-connection"), $("#save-connection")], feedback = $("#connection-feedback");
    buttons.forEach(b => b.disabled = true); feedback.textContent = "正在连接并检查表结构…";
    try {
      const result = await json("/api/database/connection" + (save ? "" : "/test"), { method: "POST", body: JSON.stringify(body) });
      if (save) {
        state.status = await json("/api/status"); $("#db-label").textContent = state.status.database;
        if (state.panel === "database") await openPanel("database");
        toast("连接已保存，共读取 " + result.table_count + " 张表");
      } else feedback.textContent = "连接成功，共读取 " + result.table_count + " 张表。";
    } catch (error) { feedback.textContent = error.message; }
    finally { buttons.forEach(b => b.disabled = false); body.password = ""; }
  }
  $("#test-connection").onclick = () => submit(false);
  $("#connection-form").onsubmit = event => { event.preventDefault(); submit(true); };
}
async function openPanel(name) {
  state.panel = name; $("#panel").hidden = false; $("#panel-backdrop").hidden = false;
  $("#panel-title").textContent = ({ knowledge: "知识库", database: "数据库与查询", audit: "操作审计", settings: "工作空间设置" })[name];
  const content = $("#panel-content"); content.innerHTML = '<p class="panel-description">正在加载…</p>';
  $("#close-panel").focus();
  try {
    if (name === "knowledge") {
      const documents = await json("/api/knowledge"); if (state.panel !== name) return;
      content.innerHTML = `<p class="panel-description">上传业务口径、设计规范或技术资料。文档会自动切片，回答时检索相关内容并展示引用。当前使用本地关键词检索。</p><button class="upload-zone" style="width:100%" id="upload-knowledge">${icon("upload")}点击上传知识文档<small>TXT / MD / PDF / DOCX / HTML 等 · 最大 10MB</small></button><p class="field-label">${documents.length} 份文档</p>${documents.map(d => `<div class="document-card"><div class="document-heading"><span>${icon("book")} ${esc(d.name)}</span><button class="icon-button" data-delete-document="${d.id}" aria-label="删除知识文档">${icon("close")}</button></div><p>${d.chunks} 个片段 · ${d.characters.toLocaleString()} 字符</p><p>来源：${esc(d.source)}</p></div>`).join("")}`;
      $("#upload-knowledge").onclick = () => { filePurpose = "knowledge"; $("#file-input").click(); };
    } else if (name === "database") {
      const config = await json("/api/database/connection"); if (state.panel !== name) return;
      content.innerHTML = connectionForm(config); bindConnectionForm(config);
      let schema;
      try { schema = await json("/api/database/schema"); }
      catch (error) { if (state.panel === name) content.insertAdjacentHTML("beforeend", `<div class="result-error">${esc(error.message)} 可在上方修改连接配置后重试。</div>`); return; }
      if (state.panel !== name) return;
      content.insertAdjacentHTML("beforeend", `<p class="panel-description">${esc(state.status?.database || schema.dialect)}<br>仅允许访问下方表。查询限制 ${state.status?.row_limit || 200} 行；禁止写入、多语句、越权访问和危险函数。</p>${schema.tables.map(t => `<details class="schema-table"><summary>${icon("database")} ${esc(t.schema)}.${esc(t.name)} <span style="float:right;color:#96a982;font-size:10px">${t.columns.length} 字段</span></summary><table>${t.columns.map(c => `<tr><td>${esc(c.name)} ${c.primary_key ? "⌑" : ""}</td><td>${esc(c.type)}</td><td>${c.nullable ? "可空" : "非空"}</td></tr>`).join("")}</table><div class="schema-index">索引：${t.indexes.map(i => esc(i.definition || i.name)).join("<br>") || "无显式索引"}</div></details>`).join("")}<label class="field-label" for="query-editor">只读查询控制台</label><textarea class="query-editor" id="query-editor" spellcheck="false">${schema.demo ? "SELECT * FROM sales LIMIT 10;" : "-- 在此输入 SELECT 查询"}</textarea><div class="panel-buttons"><button class="small-button primary" id="run-query">执行查询</button><button class="small-button" id="explain-query" ${schema.explain_supported ? "" : "disabled title='Oracle 只读执行计划暂不支持'"}>查看计划</button></div><div id="query-result"></div>`);
      $("#run-query").onclick = event => runQuery($("#query-editor").value, false, $("#query-result"), event.currentTarget);
      $("#explain-query").onclick = event => runQuery($("#query-editor").value, true, $("#query-result"), event.currentTarget);
      if (schema.dialect === "mysql") {
        content.insertAdjacentHTML("beforeend", `<div class="settings-card"><h3>选择业务数据库</h3><p>当前：${esc(schema.database || "尚未选择")}。仅查询选中库中的表，切换会保存到 .env。</p><label class="field-label" for="mysql-database">MySQL 数据库</label><select class="token-input" id="mysql-database"><option value="">请选择数据库</option>${schema.databases.map(db => `<option value="${esc(db)}" ${db === schema.database ? "selected" : ""}>${esc(db)}</option>`).join("")}</select><div class="panel-buttons"><button class="small-button primary" id="select-database">连接此数据库</button></div></div>`);
        $("#select-database").onclick = async event => {
          const name = $("#mysql-database").value;
          if (!name) return toast("请选择数据库");
          if (state.busy) return toast("请先停止当前回答");
          const button = event.currentTarget; button.disabled = true;
          try {
            await json("/api/database/select", { method: "POST", body: JSON.stringify({ database: name }) });
            state.status = await json("/api/status"); $("#db-label").textContent = state.status.database;
            await openPanel("database"); toast("已连接 " + name);
          } catch (error) { toast(error.message); button.disabled = false; }
        };
        if (!schema.database) {
          $("#run-query").disabled = true; $("#explain-query").disabled = true;
        } else if (schema.tables.length === 0) {
          $("#query-editor").value = "SELECT 1 AS connection_ok;";
        }
      }
    } else if (name === "audit") {
      const rows = await json("/api/audit"); if (state.panel !== name) return;
      const labels = { question: "用户提问", answer: "回答已完成", query_requested: "提交查询", query: "只读查询", explain: "读取执行计划", query_blocked: "查询已拦截", query_failed: "查询失败", knowledge_uploaded: "知识文档入库", attachment_uploaded: "附件已上传", session_deleted: "删除会话", knowledge_deleted: "删除知识文档", answer_interrupted: "生成已中断", answer_error: "生成失败" };
      content.innerHTML = `<p class="panel-description">最近 100 条操作记录。保存提问、生成 SQL、工具结果及拦截记录；删除会话不会删除审计。当前工作空间中的内容对持有访问令牌的人可见。</p>${rows.length ? rows.map(row => `<div class="audit-entry"><strong>${esc(labels[row.action] || row.action)}</strong><br><time>${new Date(row.created_at).toLocaleString("zh-CN")}</time><details><summary>查看记录</summary><pre>${esc(JSON.stringify(row.payload, null, 2))}</pre></details></div>`).join("") : '<p class="panel-description">暂无操作记录。</p>'}`;
    } else if (name === "settings") {
      content.innerHTML = `<p class="panel-description">应用默认运行在本机工作空间。数据库可在网页配置并立即生效；模型配置从项目根目录的 .env 加载，手动修改后需重启服务。</p><div class="settings-card"><h3>模型连接</h3><p>在 .env 中填写兼容 OpenAI 的接口：</p><code>LLM_API_KEY=你的密钥\nLLM_BASE_URL=https://api.openai.com/v1\nLLM_MODEL=gpt-4o-mini</code><p>未配置时可体验模板回答、示例查询与知识检索。</p></div><div class="settings-card"><h3>访问令牌</h3><p>如果服务端配置了 APP_ACCESS_TOKEN，在此填写对应令牌。只保存于当前浏览器会话。</p><input class="token-input" id="token-input" type="password" autocomplete="off" placeholder="输入访问令牌" value="${esc(token)}"><div class="panel-buttons"><button class="small-button primary" id="save-token">保存并连接</button></div></div><p class="panel-description">当前版本：FastAPI + LangGraph + LangChain 工具；SQLite 持久化和文本检索。图片 OCR、pgvector、Redis、多租户权限属于后续扩展。</p>`;
      $("#save-token").onclick = async () => { token = $("#token-input").value.trim(); sessionStorage.setItem("chatdb-token", token); await initialize(); };
      content.insertAdjacentHTML("afterbegin", `<div class="settings-card"><h3>数据库连接</h3><p>支持 MySQL、MariaDB、PostgreSQL、SQL Server、Oracle 和 SQLite。填写主机、端口、账号、密码后测试并保存，立即生效。</p><button class="small-button primary" id="open-connections">配置数据库连接</button></div>`);
      $("#open-connections").onclick = () => openPanel("database");
    }
  } catch (error) { if (state.panel === name) content.innerHTML = `<div class="result-error">${esc(error.message)}</div>`; }
}
async function initialize() {
  try {
    state.status = await json("/api/status");
    $("#agent").innerHTML = Object.entries(agentNames).map(([value, name]) => `<option value="${value}">${name}</option>`).join("");
    $("#model-label").textContent = state.status.model;
    $("#db-label").textContent = state.status.database;
    $("#mode-note").textContent = state.status.mode === "demo" ? "本地演示使用内置模板。连接模型后可进行通用 AI 对话。" : "AI 生成内容请结合业务验证。查询始终通过只读工具执行。";
    await refreshSessions();
    if (state.panel === "settings" && token) { closePanel(); toast("已连接工作空间"); }
  } catch (error) { toast(error.message); }
}
$("#chat-form").addEventListener("submit", event => { event.preventDefault(); submitChat(); });
$("#prompt").addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitChat(); } });
$("#prompt").addEventListener("input", event => { event.target.style.height = "auto"; event.target.style.height = Math.min(160, event.target.scrollHeight) + "px"; });
$("#new-chat").onclick = () => { if (state.busy) return toast("请先停止当前回答"); closePanel(); resetConversation(); };
$("#nav-chat").onclick = closePanel;
$("#history-search").addEventListener("input", renderHistory);
$("#mobile-menu").onclick = () => $("#sidebar").classList.toggle("open");
$("#close-panel").onclick = closePanel; $("#panel-backdrop").onclick = closePanel;
$("#attach").onclick = () => { filePurpose = "attachment"; $("#file-input").click(); };
$("#file-input").onchange = async event => { const file = event.target.files[0], purpose = filePurpose; event.target.value = ""; await uploadFile(file, purpose); };
$("#export").onclick = async () => { if (!state.session) return toast("先开始一个对话"); try { const response = await api(`/api/sessions/${state.session}/export`); download(await response.blob(), "ChatDB-对话.md", "text/markdown"); } catch (error) { toast(error.message); } };
document.addEventListener("keydown", event => {
  if (event.key === "Escape") { closePanel(); $("#sidebar").classList.remove("open"); }
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") { event.preventDefault(); $("#new-chat").click(); }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("button"); if (!button) return;
  if (button.dataset.panel) openPanel(button.dataset.panel);
  if (button.dataset.prompt) { if (state.busy) return toast("请先停止当前回答"); $("#prompt").value = button.dataset.prompt; $("#agent").value = button.dataset.agent || "auto"; submitChat(button.dataset.agent || "auto"); }
  if (button.dataset.session) selectSession(button.dataset.session);
  if (button.dataset.deleteSession) {
    if (state.busy) return toast("请先停止当前回答");
    if (!confirm("删除这条对话？审计记录会保留。")) return;
    try { await json(`/api/sessions/${button.dataset.deleteSession}`, { method: "DELETE" }); if (state.session === button.dataset.deleteSession) resetConversation(); await refreshSessions(); } catch (error) { toast(error.message); }
  }
  if (button.dataset.removeAttachment) { state.attachments = state.attachments.filter(a => a.id !== button.dataset.removeAttachment); renderAttachments(); }
  if (button.dataset.copyCode) copy(codeRegistry.get(button.dataset.copyCode));
  if (button.classList.contains("copy-answer")) copy(button.closest(".message")._content);
  const sqlId = button.dataset.runCode || button.dataset.explainCode;
  if (sqlId) runQuery(codeRegistry.get(sqlId), !!button.dataset.explainCode, $(".code-result", button.closest(".code-block")), button);
  if (button.dataset.exportResult) {
    const result = resultRegistry.get(button.dataset.exportResult);
    const cell = value => { let text = String(value == null ? "" : typeof value === "object" ? JSON.stringify(value) : value); if (/^[=+\-@\t\r]/.test(text)) text = "'" + text; return '"' + text.replace(/"/g, '""') + '"'; };
    const csv = "\uFEFF" + [result.columns, ...result.rows].map(row => row.map(cell).join(",")).join("\r\n");
    download(csv, "ChatDB-查询结果.csv", "text/csv;charset=utf-8");
  }
  if (button.dataset.deleteDocument) {
    if (!confirm("删除该知识文档及所有片段？")) return;
    try { await json(`/api/knowledge/${button.dataset.deleteDocument}`, { method: "DELETE" }); await openPanel("knowledge"); toast("知识文档已删除"); } catch (error) { toast(error.message); }
  }
});
const composer = $(".composer");
composer.addEventListener("dragover", event => { event.preventDefault(); composer.classList.add("drag-over"); });
composer.addEventListener("dragleave", () => composer.classList.remove("drag-over"));
composer.addEventListener("drop", event => { event.preventDefault(); composer.classList.remove("drag-over"); uploadFile(event.dataTransfer.files[0], "attachment"); });
initialize();
