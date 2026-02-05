// popup.js
const DEFAULT_API_BASE = "http://127.0.0.1:8765";

let apiBase = DEFAULT_API_BASE;
let userName = "PC";
let apiKey = "";

// FIFO state (fuente de verdad: background.js)
let currentItem = null;
let currentLeaseId = null;
let currentLeaseExpiresAt = null;
let stats = null;

// Captura (último JSON capturado)
let lastData = null;

const $ = (id) => document.getElementById(id);

// ------------------ helpers ------------------
function cleanServerUrl(u) {
  u = (u || "").trim();
  if (!u) return DEFAULT_API_BASE;
  return u.replace(/\/+$/, "");
}
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
  $("btnSend").disabled = !lastData;
  $("btnEnrichSingle").disabled = !lastData;
  $("btnCopy").disabled = !lastData;
  $("btnDownload").disabled = !lastData;

  // Match indicador (si hay FIFO + captura)
  if (hasItem && lastData) {
    if (canEnrichWithFIFO()) setQStatus("Captura coincide con FIFO ✅", "ok");
    else setQStatus("Captura NO coincide con FIFO ⚠️", "warn");
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
  const cfg = await chrome.storage.local.get(["apiBase", "userName", "apiKey", "lastData"]);
  apiBase = cleanServerUrl(cfg.apiBase || DEFAULT_API_BASE);
  userName = (cfg.userName || "PC").trim() || "PC";
  apiKey = (cfg.apiKey || "").trim();
  lastData = cfg.lastData || null;

  $("cfgServer").value = apiBase;
  $("cfgUser").value = userName;
  $("cfgKey").value = apiKey;
}

async function saveCfg() {
  apiBase = cleanServerUrl($("cfgServer").value);
  userName = ($("cfgUser").value || "PC").trim() || "PC";
  apiKey = ($("cfgKey").value || "").trim();

  await chrome.storage.local.set({ apiBase, userName, apiKey });
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
  if (!r.ok) return setQStatus(`❌ Claim falló: ${r.error || ""}`, "err");
  if (r.stats) setCounts(r.stats);
  await syncFromBG();

  if (r.done) return setQStatus("✅ Cola terminada", "ok");
  if (currentItem?.isbn) setQStatus(`Item listo ✅ (${currentItem.isbn})`, "ok");
  refreshUI();
}

async function skipItem() {
  if (!currentLeaseId) return;
  setQStatus("Skipping...");
  const r = await bgSend("SKIP");
  if (!r.ok) return setQStatus(`❌ Skip falló: ${r.error || ""}`, "err");
  await syncFromBG();
  await fetchStats();
  refreshUI();
  setQStatus("✅ Skipped", "ok");
}

async function enrichFifo() {
  if (!lastData || !currentItem || !currentLeaseId) return;
  if (!canEnrichWithFIFO()) return setQStatus("❌ No coincide con FIFO", "err");

  setQStatus("Enriqueciendo + DONE...");
  const r = await bgSend("ENRICH_AND_DONE", { data: lastData, targetIsbn: currentItem.isbn });
  if (!r.ok) return setQStatus(`❌ Enrich falló: ${r.error || ""}`, "err");

  await syncFromBG();
  await fetchStats();
  refreshUI();
  setQStatus(`✅ Enriquecido (rows=${r.enrich?.enriched_rows ?? "?"})`, "ok");
}

async function sendToDb() {
  if (!lastData) return;
  setStatus("Enviando a DB...");
  const r = await bgSend("INGEST", { data: lastData });
  if (!r.ok) return setStatus(`❌ Ingest falló: ${r.error || ""}`, "err");
  setStatus(`✅ DB OK (ins=${r.resp?.inserted ?? 0}, upd=${r.resp?.updated ?? 0})`, "ok");
}

async function enrichSingle() {
  if (!lastData) return;
  setStatus("Enriqueciendo (última)...");
  const r = await bgSend("ENRICH_ONLY", { data: lastData, targetIsbn: normIsbn(lastData.ISBN) });
  if (!r.ok) return setStatus(`❌ Enrich falló: ${r.error || ""}`, "err");
  setStatus(`✅ Enrich OK (rows=${r.enrich?.enriched_rows ?? "?"})`, "ok");
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
  const missingTxt = missing.length ? `Faltan: ${missing.join(", ")}` : "Completo ✅";

  box.innerHTML = `
    <div style="display:flex; gap:10px; align-items:flex-start;">
      ${cover ? `<img class="cover" src="${cover}" alt="cover">` : ""}
      <div style="flex:1">
        <div style="font-weight:700; font-size:13px;">${title || "(sin título)"}</div>
        <div class="muted">${author || ""}</div>
        <div class="kv"><b>Editorial:</b> ${publisher || "—"}</div>
        <div class="kv"><b>Idioma:</b> ${lang || "—"} <b>•</b> <b>Páginas:</b> ${pages || "—"}</div>
        <div class="kv"><b>Encuad.:</b> ${bind || "—"} <b>•</b> <b>Fecha:</b> ${date || "—"}</div>
        <div class="kv"><b>${missingTxt}</b></div>
      </div>
    </div>
  `;
}

// ------------------ capture (content script) ------------------
async function captureCurrentPage() {
  const tab = await getActiveTab();
  if (!tab?.id) return setStatus("❌ No tab", "err");

  try {
    const resp = await new Promise((resolve) => {
      chrome.tabs.sendMessage(tab.id, { type: "CAPTURE_AMAZON_BOOK" }, (r) => {
        const err = chrome.runtime.lastError;
        if (err) return resolve({ ok: false, error: String(err.message || err) });
        resolve(r || { ok: false, error: "no_response" });
      });
    });

    if (!resp.ok || !resp.data) {
      setStatus("⚠️ No pude capturar. ¿Estás en /dp/...?", "warn");
      return;
    }

    lastData = resp.data;
    await saveLastData();
    setStatus("✅ Capturado", "ok");
    refreshUI();
  } catch {
    setStatus("❌ Error capturando (ver consola)", "err");
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
      setConnLine("❌ No conecta", "err");
      return false;
    }
    $("modePill").textContent = `mode: ${j.mode}`;
    setConnLine(`✅ ${new URL(apiBase).host} | user=${userName}`, "ok");
    return true;
  } catch {
    setConnLine("❌ No conecta", "err");
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
    if (!r.ok || !j.ok) return setCfgMsg("❌ No pude cambiar modo (¿admin/key?)", "warn");
    setCfgMsg(`✅ Modo: ${j.mode}`, "ok");
    await refreshModePill();
  } catch {
    setCfgMsg("❌ Error cambiando modo", "warn");
  }
}

// ------------------ events ------------------
$("btnNext").addEventListener("click", claimNext);
$("btnSkip").addEventListener("click", skipItem);
$("btnEnrichFifo").addEventListener("click", enrichFifo);

$("btnCopyIsbn").addEventListener("click", async () => {
  if (!currentItem?.isbn) return;
  const ok = await copyText(currentItem.isbn);
  setQStatus(ok ? "✅ ISBN copiado" : "❌ No pude copiar", ok ? "ok" : "err");
});

$("btnOpenSearch").addEventListener("click", async () => {
  if (!currentItem?.isbn) return;
  await searchOnAmazonSameTab(currentItem.isbn);
});

$("btnCapture").addEventListener("click", captureCurrentPage);
$("btnSend").addEventListener("click", sendToDb);
$("btnEnrichSingle").addEventListener("click", enrichSingle);

$("btnCopy").addEventListener("click", async () => {
  if (!lastData) return;
  const ok = await copyText(JSON.stringify(lastData, null, 2));
  setStatus(ok ? "✅ Copiado" : "❌ No pude copiar", ok ? "ok" : "err");
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
  setCfgMsg("✅ Guardado", "ok");
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
  if (!r.ok || !j.ok) return setCfgMsg("❌ No pude resetear (¿admin/key?)", "warn");
  await bgSend("FETCH_STATS");
  await syncFromBG();
  refreshUI();
  setCfgMsg("✅ Estado reseteado", "ok");
});

$("btnUploadQueue").addEventListener("click", async () => {
  const f = $("queueFile").files?.[0];
  if (!f) return setCfgMsg("Elegí un .txt o .csv primero", "warn");
  const text = await f.text();

  const r = await apiFetch("/admin/upload_queue", {
    method: "POST",
    body: JSON.stringify({ text, reset_state: true }),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) return setCfgMsg("❌ Upload falló (¿admin/key?)", "warn");

  await bgSend("FETCH_STATS");
  await syncFromBG();
  refreshUI();
  setCfgMsg(`✅ Cola cargada: items=${j.items}`, "ok");
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
