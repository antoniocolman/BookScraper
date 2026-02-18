// popup.js
const DEFAULT_API_BASE = "http://127.0.0.1:8765";\nconst DEFAULT_API_V2_BASE = "http://127.0.0.1:8080";

let apiBase = DEFAULT_API_BASE;\nlet apiBaseV2 = DEFAULT_API_V2_BASE;
let userName = "PC";
let apiKey = "";

// FIFO state (fuente de verdad: background.js)
let currentItem = null;
let currentLeaseId = null;
let currentLeaseExpiresAt = null;
let stats = null;

// Captura (Ãºltimo JSON capturado)
let lastData = null;

const $ = (id) => document.getElementById(id);

// ------------------ helpers ------------------
function cleanServerUrl(u, fallback) {\n  u = (u || "").trim();\n  if (!u) return fallback || DEFAULT_API_BASE;\n  return u.replace(/\\/+$/, "");\n}
function normIsbn(s) {
  return (s || "").toUpperCase().replace(/[^0-9X]/g, "");
}
function authHeaders(extra = {}) {
  const h = { ...extra };
  if (userName) h["X-User"] = userName;
  if (apiKey) h["X-API-Key"] = apiKey;
  return h;
}
async function apiFetch(path, opts = {}) {
  const url = `${apiBase}${path}`;
  const headers = Object.assign({ "Content-Type": "application/json" }, authHeaders(opts.headers || {}));
  return fetch(url, Object.assign({}, opts, { headers }));
}

async function apiFetchV2(path, opts = {}) {
  const url = `${apiBaseV2}${path}`;
  const headers = Object.assign({ "Content-Type": "application/json" }, authHeaders(opts.headers || {}));
  return fetch(url, Object.assign({}, opts, { headers }));
}

function bgSend(type, payload = {}) {
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type, payload }, (resp) => {
      const err = chrome.runtime.lastError;
      if (err) return resolve({ ok: false, error: String(err.message || err) });
      resolve(resp || { ok: false, error: "no_response" });
    });
  });
}

// ------------------ UI ------------------
function setConnLine(txt, cls = "") {
  $("connLine").className = "small " + cls;
  $("connLine").textContent = txt;
}
function setApiConnLine(txt, cls = "") {
  $("apiConnLine").className = "small " + cls;
  $("apiConnLine").textContent = txt;
}
function setCfgMsg(txt, cls = "") {
  $("cfgMsg").className = "small " + cls;
  $("cfgMsg").textContent = txt;
}
function setStatus(txt, cls = "") {
  $("status").className = "small " + cls;
  $("status").textContent = txt;
}
function setQStatus(txt, cls = "") {
  $("qStatus").className = "small " + cls;
  $("qStatus").textContent = txt;
}
function setCounts(s) {
  stats = s || null;
  const total = s?.total ?? "?";
  const remaining = s?.remaining ?? "?";
  const done = s?.done ?? "?";
  const skipped = s?.skipped ?? "?";
  const inprog = s?.in_progress ?? "?";
  $("qCounts").textContent = `Remaining: ${remaining} | Done: ${done} | Skip: ${skipped} | InProg: ${inprog} | Total: ${total}`;
}

function refreshUI() {
  // FIFO
  const hasItem = !!(currentItem && currentLeaseId);
  $("qItem").textContent = hasItem ? JSON.stringify(currentItem, null, 2) : "(sin item)";
  $("btnCopyIsbn").disabled = !hasItem;
  $("btnOpenSearch").disabled = !hasItem;
  $("btnSkip").disabled = !hasItem;
  $("btnEnrichFifo").disabled = !hasItem || !lastData;

  // DB preview card visible solo si hay item
  const box = $("dbPreview");
  if (box) box.style.display = hasItem ? "block" : "none";

  // Captura
  $("out").textContent = lastData ? JSON.stringify(lastData, null, 2) : "(sin captura)";
  $("btnSend").disabled = !lastData;\n  $("btnSendV2").disabled = !lastData;
  $("btnEnrichSingle").disabled = !lastData;\n  $("btnFetchMissingItem").disabled = !hasItem;
  $("btnCopy").disabled = !lastData;
  $("btnDownload").disabled = !lastData;

  // Match indicador (si hay FIFO + captura)
  if (hasItem && lastData) {
    if (canEnrichWithFIFO()) setQStatus("Captura coincide con FIFO âœ…", "ok");
    else setQStatus("Captura NO coincide con FIFO âš ï¸", "warn");
  }
}

function setupTabs() {
  document.querySelectorAll(".tabbtn").forEach((b) => {
    b.addEventListener("click", () => {
      document.querySelectorAll(".tabbtn").forEach((x) => x.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      b.classList.add("active");
      $("tab-" + b.dataset.tab).classList.add("active");
    });
  });
}

// ------------------ config + persistence ------------------
async function loadCfg() {
  const cfg = await chrome.storage.local.get(["apiBase", "apiBaseV2", "userName", "apiKey", "lastData"]);
  apiBase = cleanServerUrl(cfg.apiBase || DEFAULT_API_BASE, DEFAULT_API_BASE);\n  apiBaseV2 = cleanServerUrl(cfg.apiBaseV2 || DEFAULT_API_V2_BASE, DEFAULT_API_V2_BASE);
  userName = (cfg.userName || "PC").trim() || "PC";
  apiKey = (cfg.apiKey || "").trim();
  lastData = cfg.lastData || null;

  $("cfgServer").value = apiBase;\n  $("cfgServerV2").value = apiBaseV2;
  $("cfgUser").value = userName;
  $("cfgKey").value = apiKey;
}

async function saveCfg() {
  apiBase = cleanServerUrl($("cfgServer").value, DEFAULT_API_BASE);\n  apiBaseV2 = cleanServerUrl($("cfgServerV2").value, DEFAULT_API_V2_BASE);
  userName = ($("cfgUser").value || "PC").trim() || "PC";
  apiKey = ($("cfgKey").value || "").trim();

  await chrome.storage.local.set({ apiBase, apiBaseV2, userName, apiKey });
  // background escucha storage.onChanged, pero igual forzamos un refresh de state
  await bgSend("GET_STATE");
}

async function saveLastData() {
  await chrome.storage.local.set({ lastData });
}

// ------------------ background sync ------------------
async function syncFromBG() {
  const r = await bgSend("GET_STATE");
  if (!r.ok) return;

  const st = r.state || {};
  currentItem = st.currentItem || null;
  currentLeaseId = st.leaseId || null;
  currentLeaseExpiresAt = st.leaseExpiresAt || null;
  if (st.stats) setCounts(st.stats);

  // DB preview (best-effort)
  if (currentItem?.isbn) {
    const dbr = await bgSend("DB_LOOKUP", { isbn: currentItem.isbn });
    renderDbPreview(dbr);
  } else {
    renderDbPreview(null);
  }
}

async function fetchStats() {
  const r = await bgSend("FETCH_STATS");
  if (r.ok && r.stats) setCounts(r.stats);
}

// ------------------ logic ------------------
function canEnrichWithFIFO() {
  if (!currentItem || !lastData) return false;
  const target = normIsbn(currentItem.isbn);
  const got13 = normIsbn(lastData.ISBN);
  const got10 = normIsbn(lastData.ISBN10);
  return !!target && (target === got13 || target === got10);
}

async function claimNext() {
  setQStatus("Claiming...");
  const r = await bgSend("CLAIM_NEXT");
  if (!r.ok) return setQStatus(`âŒ Claim fallÃ³: ${r.error || ""}`, "err");
  if (r.stats) setCounts(r.stats);
  await syncFromBG();

  if (r.done) return setQStatus("âœ… Cola terminada", "ok");
  if (currentItem?.isbn) setQStatus(`Item listo âœ… (${currentItem.isbn})`, "ok");
  refreshUI();
}

async function skipItem() {
  if (!currentLeaseId) return;
  setQStatus("Skipping...");
  const r = await bgSend("SKIP");
  if (!r.ok) return setQStatus(`âŒ Skip fallÃ³: ${r.error || ""}`, "err");
  await syncFromBG();
  await fetchStats();
  refreshUI();
  setQStatus("âœ… Skipped", "ok");
}

async function enrichFifo() {
  if (!lastData || !currentItem || !currentLeaseId) return;
  if (!canEnrichWithFIFO()) return setQStatus("âŒ No coincide con FIFO", "err");

  setQStatus("Enriqueciendo + DONE...");
  const r = await bgSend("ENRICH_AND_DONE", { data: lastData, targetIsbn: currentItem.isbn });
  if (!r.ok) return setQStatus(`âŒ Enrich fallÃ³: ${r.error || ""}`, "err");

  await syncFromBG();
  await fetchStats();
  refreshUI();
  setQStatus(`âœ… Enriquecido (rows=${r.enrich?.enriched_rows ?? "?"})`, "ok");
}

async function sendToDb() {
  if (!lastData) return;
  setStatus("Enviando a DB...");
  const r = await bgSend("INGEST", { data: lastData });
  if (!r.ok) return setStatus(`âŒ Ingest fallÃ³: ${r.error || ""}`, "err");
  setStatus(`âœ… DB OK (ins=${r.resp?.inserted ?? 0}, upd=${r.resp?.updated ?? 0})`, "ok");
}

async function sendToApiV2() {
  if (!lastData) return;
  setStatus("Enviando a API...");
  const r = await bgSend("CAPTURE_V2", { data: lastData });
  if (!r.ok) return setStatus(`âŒ API fallÃ³: ${r.error || ""}`, "err");
  setStatus(`âœ… API OK (ins=${r.resp?.inserted ?? 0})`, "ok");
}

async function enrichSingle() {
  if (!lastData) return;
  setStatus("Enriqueciendo (Ãºltima)...");
  const r = await bgSend("ENRICH_ONLY", { data: lastData, targetIsbn: normIsbn(lastData.ISBN) });
  if (!r.ok) return setStatus(`âŒ Enrich fallÃ³: ${r.error || ""}`, "err");
  setStatus(`âœ… Enrich OK (rows=${r.enrich?.enriched_rows ?? "?"})`, "ok");
}

// ------------------ Amazon helpers (same tab) ------------------
async function getActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}

async function searchOnAmazonSameTab(isbn) {
  const tab = await getActiveTab();
  if (!tab?.id) return;

  const s = encodeURIComponent(isbn);
  const isAmazon = (tab.url || "").includes("amazon.com");

  if (!isAmazon) {
    await chrome.tabs.update(tab.id, { url: `https://www.amazon.com/s?k=${s}&i=stripbooks` });
    return;
  }

  await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    args: [isbn],
    func: (isbnArg) => {
      try {
        const box = document.querySelector("#twotabsearchtextbox");
        if (box) {
          box.focus();
          box.value = isbnArg;
          box.dispatchEvent(new Event("input", { bubbles: true }));
        }
        const sel = document.querySelector("#searchDropdownBox");
        if (sel) {
          const want = "search-alias=stripbooks";
          if (sel.value !== want) {
            sel.value = want;
            sel.dispatchEvent(new Event("change", { bubbles: true }));
          }
        }
        const form = document.querySelector("#nav-search-bar-form");
        if (form) form.submit();
      } catch {}
    },
  });
}

// ------------------ DB preview render ------------------
function renderDbPreview(resp) {
  const box = $("dbPreview");
  if (!box) return;

  if (!resp?.ok || !resp.found) {
    box.innerHTML = `<div class="muted">DB preview: sin datos para este ISBN.</div>`;
    return;
  }

  const b = resp.best || {};
  const cover = (b["URL PORTADA"] || "").trim();
  const title = (b["TITULO"] || "").trim();
  const author = (b["AUTOR"] || "").trim();
  const publisher = (b["EDITORIAL"] || "").trim();
  const lang = (b["IDIOMA"] || "").trim();
  const pages = (b["PAGINAS"] || "").trim();
  const bind = (b["ENCUADERNACION"] || "").trim();
  const date = (b["FECHA PUBLICACION"] || "").trim();

  const missing = Array.isArray(resp.missing_fields) ? resp.missing_fields : [];
  const missingTxt = missing.length ? `Faltan: ${missing.join(", ")}` : "Completo âœ…";

  box.innerHTML = `
    <div style="display:flex; gap:10px; align-items:flex-start;">
      ${cover ? `<img class="cover" src="${cover}" alt="cover">` : ""}
      <div style="flex:1">
        <div style="font-weight:700; font-size:13px;">${title || "(sin tÃ­tulo)"}</div>
        <div class="muted">${author || ""}</div>
        <div class="kv"><b>Editorial:</b> ${publisher || "â€”"}</div>
        <div class="kv"><b>Idioma:</b> ${lang || "â€”"} <b>â€¢</b> <b>PÃ¡ginas:</b> ${pages || "â€”"}</div>
        <div class="kv"><b>Encuad.:</b> ${bind || "â€”"} <b>â€¢</b> <b>Fecha:</b> ${date || "â€”"}</div>
        <div class="kv"><b>${missingTxt}</b></div>
      </div>
    </div>
  `;
}
function renderApiMissing(label, items) {
  const box = $("apiMissingBox");
  if (!box) return;
  if (!items || !items.length) {
    box.textContent = `${label}: (sin faltantes)`;
    return;
  }
  const list = items.join(", ");
  box.textContent = `${label}: ${list}`;
}

async function fetchApiMissing(limit = 50) {
  try {
    const r = await apiFetchV2(`/missing?limit=${limit}`, { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      renderApiMissing("API missing", []);
      return false;
    }
    const rows = j.rows || [];
    if (!rows.length) {
      renderApiMissing("API missing", []);
      return true;
    }
    const first = rows[0] || {};
    const miss = first.missing_fields || [];
    const label = `API missing (1er item ${first.isbn || ""} ${first.site || ""})`.trim();
    renderApiMissing(label, miss);
    return true;
  } catch {
    renderApiMissing("API missing", []);
    return false;
  }
}

async function fetchApiMissingForIsbn(isbn) {
  const box = $("apiMissingBox");
  if (!box) return false;
  if (!isbn) {
    renderApiMissing("API missing", []);
    return false;
  }
  try {
    const r = await apiFetchV2(`/isbn/${encodeURIComponent(isbn)}`, { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok || !j.rows || !j.rows.length) {
      renderApiMissing(`API missing (${isbn})`, []);
      return false;
    }
    const miss = j.rows[0].missing_fields || [];
    renderApiMissing(`API missing (${isbn})`, miss);
    return true;
  } catch {
    renderApiMissing(`API missing (${isbn})`, []);
    return false;
  }
}

// ------------------ capture (content script) ------------------
async function captureCurrentPage() {
  const tab = await getActiveTab();
  if (!tab?.id) return setStatus("âŒ No tab", "err");

  try {
    const resp = await new Promise((resolve) => {
      chrome.tabs.sendMessage(tab.id, { type: "CAPTURE_AMAZON_BOOK" }, (r) => {
        const err = chrome.runtime.lastError;
        if (err) return resolve({ ok: false, error: String(err.message || err) });
        resolve(r || { ok: false, error: "no_response" });
      });
    });

    if (!resp.ok || !resp.data) {
      setStatus("âš ï¸ No pude capturar. Â¿EstÃ¡s en /dp/...?", "warn");
      return;
    }

    lastData = resp.data;
    await saveLastData();
    setStatus("âœ… Capturado", "ok");
    refreshUI();
  } catch {
    setStatus("âŒ Error capturando (ver consola)", "err");
  }
}


// ------------------ clipboard/download ------------------
async function copyText(txt) {
  try {
    await navigator.clipboard.writeText(txt);
    return true;
  } catch {
    return false;
  }
}
function downloadJson(obj, filename = "amazon_capture.json") {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  chrome.downloads.download({ url, filename, saveAs: true }, () => {
    URL.revokeObjectURL(url);
  });
}

// ------------------ connection / mode ------------------
async function testConnection() {
  try {
    const r = await apiFetch("/health", { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      setConnLine("âŒ No conecta", "err");
      return false;
    }
    $("modePill").textContent = `mode: ${j.mode}`;
    setConnLine(`âœ… ${new URL(apiBase).host} | user=${userName}`, "ok");
    return true;
  } catch {
    setConnLine("âŒ No conecta", "err");
    return false;
  }
}
async function testApiV2() {
  try {
    const r = await apiFetchV2("/health", { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      setApiConnLine("❌ API no conecta", "err");
      return false;
    }
    setApiConnLine(`✅ API OK (${new URL(apiBaseV2).host})`, "ok");
    return true;
  } catch {
    setApiConnLine("❌ API no conecta", "err");
    return false;
  }
}

async function refreshModePill() {
  try {
    const r = await apiFetch("/admin/mode", { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok) $("modePill").textContent = `mode: ${j.mode}`;
  } catch {}
}

async function setServerMode(mode) {
  try {
    const r = await apiFetch("/admin/mode", { method: "POST", body: JSON.stringify({ mode }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) return setCfgMsg("âŒ No pude cambiar modo (Â¿admin/key?)", "warn");
    setCfgMsg(`âœ… Modo: ${j.mode}`, "ok");
    await refreshModePill();
  } catch {
    setCfgMsg("âŒ Error cambiando modo", "warn");
  }
}

// ------------------ events ------------------
$("btnNext").addEventListener("click", claimNext);
$("btnSkip").addEventListener("click", skipItem);
$("btnEnrichFifo").addEventListener("click", enrichFifo);\n\n$("btnFetchMissing").addEventListener("click", async () => {\n  await fetchApiMissing(50);\n});\n$("btnFetchMissingItem").addEventListener("click", async () => {\n  await fetchApiMissingForIsbn(currentItem?.isbn);\n});

$("btnCopyIsbn").addEventListener("click", async () => {
  if (!currentItem?.isbn) return;
  const ok = await copyText(currentItem.isbn);
  setQStatus(ok ? "âœ… ISBN copiado" : "âŒ No pude copiar", ok ? "ok" : "err");
});

$("btnOpenSearch").addEventListener("click", async () => {
  if (!currentItem?.isbn) return;
  await searchOnAmazonSameTab(currentItem.isbn);
});

$("btnCapture").addEventListener("click", captureCurrentPage);
$("btnSend").addEventListener("click", sendToDb);\n$("btnSendV2").addEventListener("click", sendToApiV2);
$("btnEnrichSingle").addEventListener("click", enrichSingle);

$("btnCopy").addEventListener("click", async () => {
  if (!lastData) return;
  const ok = await copyText(JSON.stringify(lastData, null, 2));
  setStatus(ok ? "âœ… Copiado" : "âŒ No pude copiar", ok ? "ok" : "err");
});
$("btnDownload").addEventListener("click", () => {
  if (lastData) downloadJson(lastData, "amazon_last.json");
});

$("btnSaveCfg").addEventListener("click", async () => {
  await saveCfg();
  await loadCfg();
  await testConnection();
  await syncFromBG();
  await fetchStats();
  refreshUI();
  setCfgMsg("âœ… Guardado", "ok");
});

$("btnTestCfg").addEventListener("click", async () => {
  await loadCfg();
  const ok = await testConnection();
  if (ok) await fetchStats();
});

$("btnModeLocal").addEventListener("click", () => setServerMode("local"));
$("btnModeLan").addEventListener("click", () => setServerMode("lan"));

$("btnResetState").addEventListener("click", async () => {
  const r = await apiFetch("/admin/reset_state", { method: "POST", body: JSON.stringify({ confirm: true }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) return setCfgMsg("âŒ No pude resetear (Â¿admin/key?)", "warn");
  await bgSend("FETCH_STATS");
  await syncFromBG();
  refreshUI();
  setCfgMsg("âœ… Estado reseteado", "ok");
});

$("btnUploadQueue").addEventListener("click", async () => {
  const f = $("queueFile").files?.[0];
  if (!f) return setCfgMsg("ElegÃ­ un .txt o .csv primero", "warn");
  const text = await f.text();

  const r = await apiFetch("/admin/upload_queue", {
    method: "POST",
    body: JSON.stringify({ text, reset_state: true }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) return setCfgMsg("âŒ Upload fallÃ³ (Â¿admin/key?)", "warn");

  await bgSend("FETCH_STATS");
  await syncFromBG();
  refreshUI();
  setCfgMsg(`âœ… Cola cargada: items=${j.items}`, "ok");
});

// ------------------ init ------------------
(async function init() {
  setupTabs();
  await loadCfg();
  refreshUI();
  await testConnection();
  await refreshModePill();
  await syncFromBG();
  await fetchStats();
})();









