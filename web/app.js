/* 智答 frontend — vanilla JS single-page app. */

const $ = (sel) => document.querySelector(sel);
const messagesEl = $("#messages");
const sessionInfoEl = $("#session-info");
const docListEl = $("#doc-list");
const uploadStatusEl = $("#upload-status");

let sessionId = null;
let documents = [];
let selectedDocIds = new Set();
const citationCache = new Map(); // "docId:ord" -> {title, snippet, ...}

// ---------- documents ----------

async function loadDocuments() {
  docListEl.innerHTML = '<div class="p-4 text-sm text-slate-400">加载中…</div>';
  try {
    const r = await fetch("/api/v1/documents");
    documents = await r.json();
    renderDocuments();
  } catch (e) {
    docListEl.innerHTML = `<div class="p-4 text-sm text-red-500">加载失败: ${e}</div>`;
  }
}

function renderDocuments() {
  if (documents.length === 0) {
    docListEl.innerHTML = '<div class="p-4 text-sm text-slate-400">暂无文档，请上传</div>';
    return;
  }
  docListEl.innerHTML = "";
  for (const d of documents) {
    const row = document.createElement("label");
    row.className = "doc-row hover:bg-slate-50 cursor-pointer";
    row.dataset.docId = d.id;
    const checked = selectedDocIds.has(d.id) ? "checked" : "";
    row.innerHTML = `
      <input type="checkbox" class="mt-1" data-doc-id="${d.id}" ${checked} />
      <div class="flex-1 min-w-0">
        <div class="text-sm font-medium truncate">${escapeHtml(d.title)}</div>
        <div class="text-xs text-slate-500 mt-0.5">
          ${d.mime_type} · ${d.chunk_count} 块 · ${formatBytes(d.byte_size)}
        </div>
      </div>
    `;
    row.querySelector("input").addEventListener("change", (e) => {
      const id = parseInt(e.target.dataset.docId);
      if (e.target.checked) selectedDocIds.add(id);
      else selectedDocIds.delete(id);
    });
    docListEl.appendChild(row);
  }
}

async function uploadFiles(files) {
  for (const file of files) {
    uploadStatusEl.textContent = `上传中: ${file.name}…`;
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await fetch("/api/v1/ingest", { method: "POST", body: fd });
      const j = await r.json();
      if (!r.ok) throw new Error(j.detail || "上传失败");
      uploadStatusEl.textContent = j.duplicated
        ? `${file.name} 已存在（去重）`
        : `${file.name} 上传成功 · ${j.chunk_count} 块`;
    } catch (e) {
      uploadStatusEl.textContent = `❌ ${file.name}: ${e.message || e}`;
    }
  }
  await loadDocuments();
}

// ---------- chat ----------

function appendUserBubble(text) {
  const wrap = document.createElement("div");
  wrap.className = "flex justify-end";
  wrap.innerHTML = `<div class="bubble-user">${escapeHtml(text)}</div>`;
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function appendAssistantBubble() {
  const wrap = document.createElement("div");
  wrap.className = "flex justify-start flex-col items-start gap-1";
  wrap.innerHTML = `
    <div class="tool-bar flex flex-wrap gap-1"></div>
    <div class="bubble-assistant"><span class="text"></span></div>
  `;
  messagesEl.appendChild(wrap);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return wrap;
}

function renderCitationsInPlace(textEl) {
  const html = escapeHtml(textEl.textContent).replace(
    /\[doc:(\d+)#chunk:(\d+)\]/g,
    (_m, doc, ord) => {
      const key = `${doc}:${ord}`;
      const cit = citationCache.get(key);
      const tip = cit ? `${cit.title} — ${cit.snippet}` : `doc ${doc} chunk ${ord}`;
      return `<sup class="cite" data-doc="${doc}" data-ord="${ord}" title="${escapeAttr(tip)}">[${doc}·${ord}]</sup>`;
    }
  );
  textEl.innerHTML = html;
}

async function sendChat(message) {
  appendUserBubble(message);
  const bubbleWrap = appendAssistantBubble();
  const toolBar = bubbleWrap.querySelector(".tool-bar");
  const textEl = bubbleWrap.querySelector(".text");
  $("#send-btn").disabled = true;

  const body = {
    session_id: sessionId,
    message,
    document_ids: selectedDocIds.size ? [...selectedDocIds] : null,
  };

  try {
    const res = await fetch("/api/v1/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const ev = parseSSE(frame);
        if (!ev) continue;
        handleEvent(ev, { textEl, toolBar });
      }
    }
    renderCitationsInPlace(textEl);
  } catch (e) {
    textEl.textContent = `\n[错误] ${e.message || e}`;
  } finally {
    $("#send-btn").disabled = false;
  }
}

function handleEvent(ev, ctx) {
  switch (ev.event) {
    case "session":
      sessionId = ev.data.session_id;
      sessionInfoEl.textContent = `会话 ${sessionId.slice(0, 8)}…`;
      break;
    case "text":
      ctx.textEl.textContent += ev.data.delta || "";
      messagesEl.scrollTop = messagesEl.scrollHeight;
      break;
    case "tool_use": {
      const badge = document.createElement("span");
      badge.className = "tool-badge";
      const q = ev.data.input?.query || "";
      badge.textContent = `🔍 ${ev.data.name}${q ? `: ${q}` : ""}`;
      ctx.toolBar.appendChild(badge);
      break;
    }
    case "citation": {
      const key = `${ev.data.document_id}:${ev.data.ord}`;
      citationCache.set(key, ev.data);
      break;
    }
    case "done":
      break;
    case "error":
      ctx.textEl.textContent += `\n[错误] ${ev.data.message || ""}`;
      break;
  }
}

function parseSSE(frame) {
  let event = "message";
  let data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return { event, data: {} };
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return { event, data: { raw: data } };
  }
}

// ---------- helpers ----------

function escapeHtml(s) {
  return String(s)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
function escapeAttr(s) {
  return escapeHtml(s).replaceAll('"', "&quot;");
}
function formatBytes(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(2)} MB`;
}

// ---------- bootstrap ----------

$("#file-input").addEventListener("change", (e) => {
  uploadFiles([...e.target.files]);
  e.target.value = "";
});
$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("#chat-input").value.trim();
  if (!text) return;
  $("#chat-input").value = "";
  sendChat(text);
});
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    $("#chat-form").dispatchEvent(new Event("submit"));
  }
});
$("#new-session").addEventListener("click", () => {
  sessionId = null;
  sessionInfoEl.textContent = "尚未开始会话";
  messagesEl.innerHTML = "";
  citationCache.clear();
});

loadDocuments();
