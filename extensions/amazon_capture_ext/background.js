// background.js (MV3 service worker)
// Mantiene lease/heartbeat aunque cierres el popup.
// Ejemplo (FastAPI):
// fetch("http://127.0.0.1:8080/captures", {
//   method: "POST",
//   headers: { "Content-Type": "application/json", "X-API-Key": "<API_KEY>" },
//   body: JSON.stringify({ data: capture })
// });

const DEFAULT_API_BASE = "http://127.0.0.1:8765";
const DEFAULT_API_V2_BASE = "http://127.0.0.1:8080";

const CFG_KEYS = ["apiBase", "apiBaseV2", "userName", "apiKey"];

let cfg = {
  apiBase: DEFAULT_API_BASE,
  apiBaseV2: DEFAULT_API_V2_BASE,
  userName: "PC",
  apiKey: ""
};

let state = {
  currentItem: null,
  leaseId: null,
  leaseExpiresAt: null,
  stats: null,
  lastHeartbeatAt: null,
  lastError: null
};

const ALARM_HEARTBEAT = "amazon_lease_heartbeat";
const ALARM_STATS = "amazon_stats_refresh";

// ---------- utils ----------
function cleanServerUrl(u, fallback) {
  u = (u || "").trim();
  if (!u) return fallback || DEFAULT_API_BASE;
  return u.replace(/\/+$/, "");
}
function normIsbn(s) {
  return (s || "").toUpperCase().replace(/[^0-9X]/g, "");
}
function authHeaders(extra = {}) {
  const h = { ...extra };
  if (cfg.userName) h["X-User"] = cfg.userName;
  if (cfg.apiKey) h["X-API-Key"] = cfg.apiKey;
  return h;
}
async function apiFetch(path, opts = {}) {
  const url = `${cfg.apiBase}${path}`;
  const headers = Object.assign({ "Content-Type": "application/json" }, authHeaders(opts.headers || {}));
  return fetch(url, Object.assign({}, opts, { headers }));
}

async function apiFetchV2(path, opts = {}) {
  const url = `${cfg.apiBaseV2}${path}`;
  const headers = Object.assign({ "Content-Type": "application/json" }, authHeaders(opts.headers || {}));
  return fetch(url, Object.assign({}, opts, { headers }));
}

function persistState() {
  return chrome.storage.local.set({
    bg_state_v1: state,
    // compat (si alguna parte vieja lo lee)
    currentItem: state.currentItem,
    currentLeaseId: state.leaseId,
    currentLeaseExpiresAt: state.leaseExpiresAt,
    stickyApiBase: cfg.apiBase
  });
}

async function loadCfgFromStorage() {
  const got = await chrome.storage.local.get(CFG_KEYS);
  cfg.apiBase = cleanServerUrl(got.apiBase || DEFAULT_API_BASE, DEFAULT_API_BASE);
  cfg.apiBaseV2 = cleanServerUrl(got.apiBaseV2 || DEFAULT_API_V2_BASE, DEFAULT_API_V2_BASE);
  cfg.userName = (got.userName || "PC").trim() || "PC";
  cfg.apiKey = (got.apiKey || "").trim();
}

async function loadStateFromStorage() {
  const got = await chrome.storage.local.get(["bg_state_v1"]);
  if (got.bg_state_v1 && typeof got.bg_state_v1 === "object") {
    state = Object.assign(state, got.bg_state_v1);
  }
}

function ensureAlarms() {
  // Stats: cada 60s (ligero)
  chrome.alarms.create(ALARM_STATS, { periodInMinutes: 1 });

  // Heartbeat: cada 1 min; si no hay lease, el handler no hace nada.
  chrome.alarms.create(ALARM_HEARTBEAT, { periodInMinutes: 1 });
}

function clearLease() {
  state.currentItem = null;
  state.leaseId = null;
  state.leaseExpiresAt = null;
}

// ---------- server ops ----------
async function fetchStats() {
  try {
    const r = await apiFetch("/queue/stats", { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (r.ok && j.ok) {
      state.stats = j.stats || null;
      state.lastError = null;
      await persistState();
      return { ok: true, stats: state.stats };
    }
    state.lastError = j.error || "stats_failed";
    await persistState();
    return { ok: false, error: state.lastError };
  } catch (e) {
    state.lastError = String(e?.message || e);
    await persistState();
    return { ok: false, error: state.lastError };
  }
}

async function claimNext() {
  try {
    const r = await apiFetch("/queue/claim", {
      method: "POST",
      body: JSON.stringify({ worker: cfg.userName })
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      state.lastError = j.error || "claim_failed";
      await persistState();
      return { ok: false, error: state.lastError };
    }

    // stats
    if (j.stats) state.stats = j.stats;

    if (j.done) {
      clearLease();
      state.lastError = null;
      await persistState();
      return { ok: true, done: true, item: null, lease_id: null, stats: state.stats };
    }

    state.currentItem = j.item || null;
    state.leaseId = j.lease_id || null;
    state.leaseExpiresAt = j.lease_expires_at || null;
    state.lastError = null;

    await persistState();
    return { ok: true, done: false, item: state.currentItem, lease_id: state.leaseId, lease_expires_at: state.leaseExpiresAt, stats: state.stats };
  } catch (e) {
    state.lastError = String(e?.message || e);
    await persistState();
    return { ok: false, error: state.lastError };
  }
}

async function heartbeat() {
  if (!state.leaseId) return { ok: true, skipped: true };

  try {
    const r = await apiFetch("/queue/heartbeat", {
      method: "POST",
      body: JSON.stringify({ lease_id: state.leaseId })
    });
    const j = await r.json().catch(() => ({}));

    if (!r.ok || !j.ok) {
      // Lease perdido -> limpiar
      state.lastError = j.error || "heartbeat_failed";
      clearLease();
      await persistState();
      return { ok: false, error: state.lastError };
    }

    state.lastHeartbeatAt = Date.now();
    // opcionalmente podríamos actualizar expires_at si el server lo devolviera
    state.lastError = null;
    await persistState();
    return { ok: true, heartbeat: true };
  } catch (e) {
    state.lastError = String(e?.message || e);
    await persistState();
    return { ok: false, error: state.lastError };
  }
}

async function finish(action) {
  if (!state.leaseId) return { ok: false, error: "no_active_lease" };
  const path = action === "skip" ? "/queue/skip" : "/queue/done";
  try {
    const r = await apiFetch(path, {
      method: "POST",
      body: JSON.stringify({ lease_id: state.leaseId })
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      state.lastError = j.error || "finish_failed";
      await persistState();
      return { ok: false, error: state.lastError };
    }
    clearLease();
    state.lastError = null;
    await persistState();
    // refrescar stats (best-effort)
    await fetchStats();
    return { ok: true, action, isbn: j.isbn || null, stats: state.stats };
  } catch (e) {
    state.lastError = String(e?.message || e);
    await persistState();
    return { ok: false, error: state.lastError };
  }
}

async function enrichAndMaybeDone({ data, targetIsbn, markDone }) {
  const target = normIsbn(targetIsbn) || normIsbn(state.currentItem?.isbn);
  try {
    const r = await apiFetch("/enrich", {
      method: "POST",
      body: JSON.stringify({ data, target_isbn: target })
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) {
      state.lastError = j.error || "enrich_failed";
      await persistState();
      return { ok: false, error: state.lastError, enrich: j };
    }

    let finishResp = null;
    if (markDone) finishResp = await finish("done");

    return { ok: true, enrich: j, finish: finishResp, stats: state.stats };
  } catch (e) {
    state.lastError = String(e?.message || e);
    await persistState();
    return { ok: false, error: state.lastError };
  }
}

async function ingest({ data }) {
  try {
    const r = await apiFetch("/ingest", { method: "POST", body: JSON.stringify({ data }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) return { ok: false, error: j.error || "ingest_failed", resp: j };
    return { ok: true, resp: j };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
}

async function captureV2({ data }) {
  try {
    const r = await apiFetchV2("/captures", { method: "POST", body: JSON.stringify({ data }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) return { ok: false, error: j.detail || j.error || "capture_failed", resp: j };
    return { ok: true, resp: j };
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
}

async function dbLookup(isbn) {
  const s = normIsbn(isbn);
  if (!s) return { ok: false, error: "missing_isbn" };
  try {
    const r = await apiFetch(`/db/lookup?isbn=${encodeURIComponent(s)}`, { method: "GET" });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) return { ok: false, error: j.error || "db_lookup_failed", resp: j };
    return j;
  } catch (e) {
    return { ok: false, error: String(e?.message || e) };
  }
}

// ---------- lifecycle ----------
async function init() {
  await loadCfgFromStorage();
  await loadStateFromStorage();
  ensureAlarms();
  await persistState();
}

chrome.runtime.onInstalled.addListener(() => { init(); });
chrome.runtime.onStartup.addListener(() => { init(); });

// storage sync
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  let touch = false;
  for (const k of CFG_KEYS) {
    if (k in changes) touch = true;
  }
  if (touch) {
    // recargar config (async)
    loadCfgFromStorage().then(() => persistState());
  }
});

// alarms
chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name === ALARM_HEARTBEAT) {
    await heartbeat();
  }
  if (alarm.name === ALARM_STATS) {
    await fetchStats();
  }
});

// messages (popup)
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    const type = msg?.type || "";
    const payload = msg?.payload || {};

    if (type === "GET_STATE") {
      await loadCfgFromStorage();
      await loadStateFromStorage();
      sendResponse({ ok: true, cfg, state });
      return;
    }

    if (type === "FETCH_STATS") {
      const out = await fetchStats();
      sendResponse(out);
      return;
    }

    if (type === "CLAIM_NEXT") {
      const out = await claimNext();
      sendResponse(out);
      return;
    }

    if (type === "HEARTBEAT_NOW") {
      const out = await heartbeat();
      sendResponse(out);
      return;
    }

    if (type === "DONE") {
      const out = await finish("done");
      sendResponse(out);
      return;
    }

    if (type === "SKIP") {
      const out = await finish("skip");
      sendResponse(out);
      return;
    }

    if (type === "RELEASE") {
      // Release por ahora = marcar skip? (si querés endpoint /queue/release, lo agregamos)
      sendResponse({ ok: false, error: "not_implemented" });
      return;
    }

    if (type === "ENRICH_ONLY") {
      const out = await enrichAndMaybeDone({ data: payload.data, targetIsbn: payload.targetIsbn, markDone: false });
      sendResponse(out);
      return;
    }

    if (type === "ENRICH_AND_DONE") {
      const out = await enrichAndMaybeDone({ data: payload.data, targetIsbn: payload.targetIsbn, markDone: true });
      sendResponse(out);
      return;
    }

    if (type === "INGEST") {
      const out = await ingest({ data: payload.data });
      sendResponse(out);
      return;
    }

    if (type === "CAPTURE_V2") {
      const out = await captureV2({ data: payload.data });
      sendResponse(out);
      return;
    }

    if (type === "DB_LOOKUP") {
      const out = await dbLookup(payload.isbn);
      sendResponse(out);
      return;
    }

    sendResponse({ ok: false, error: "unknown_message" });
  })();

  // keep channel open for async
  return true;
});

init();
