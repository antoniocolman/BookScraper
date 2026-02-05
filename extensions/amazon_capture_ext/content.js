function text(el) {
  if (!el) return "";
  return (el.textContent || "").replace(/\s+/g, " ").trim();
}

function stripTags(html) {
  const div = document.createElement("div");
  div.innerHTML = html || "";
  return (div.textContent || "").replace(/\s+\n/g, "\n").trim();
}

function canonicalUrl() {
  const u = new URL(window.location.href);
  // recorta todo a ".../dp/ASIN"
  const p = u.pathname;
  const idx = p.toLowerCase().indexOf("/dp/");
  if (idx !== -1 && p.length >= idx + 14) {
    const asin = p.slice(idx + 4, idx + 14);
    u.pathname = p.slice(0, idx + 4) + asin;
  }
  u.search = "";
  u.hash = "";
  return u.toString();
}

function cleanValue(s) {
  return (s || "")
    .replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069\u00ad\uFEFF]/g, "")
    .replace(/[‏‎]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function normKey(s) {
  return cleanValue(s)
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "") // sin tildes
    .replace(/[:：]\s*$/, "")        // saca ':' final
    .replace(/[^a-z0-9]+/g, " ")     // tokens simples
    .replace(/\s+/g, " ")
    .trim();
}

function pickByKeysNorm(kvNorm, keys) {
  for (const k of keys) {
    const kk = normKey(k);
    if (kvNorm[kk]) return kvNorm[kk];
  }
  return "";
}

function normalizeIsbn(s) {
  return (s || "").toUpperCase().replace(/[^0-9X]/g, "");
}

function getCoverUrl() {
  const landing = document.querySelector("#landingImage");
  if (landing) {
    const hires = landing.getAttribute("data-old-hires");
    if (hires) return hires.trim();
    const src = landing.getAttribute("src");
    if (src) return src.trim();
  }
  const og = document.querySelector('meta[property="og:image"]');
  if (og?.content) return og.content.trim();
  return "";
}

function getTitle() {
  return text(document.querySelector("#productTitle"));
}

function getBindingAndDate() {
  // Ej: "Tapa blanda – 7 Enero 2025"
  const sub = text(document.querySelector("#productSubtitle"));
  if (!sub) return { binding: "", pubDateRaw: "" };

  const parts = sub.split("–").map(s => s.trim());
  if (parts.length >= 2) return { binding: parts[0], pubDateRaw: parts.slice(1).join(" – ") };
  return { binding: sub, pubDateRaw: "" };
}

function getAuthors() {
  const a1 = document.querySelectorAll("span.author a.a-link-normal");
  if (a1 && a1.length) return Array.from(a1).map(a => text(a)).filter(Boolean);

  const by = document.querySelector("#bylineInfo");
  if (by) {
    const links = by.querySelectorAll("a.a-link-normal");
    const names = Array.from(links).map(a => text(a)).filter(Boolean);
    return names.slice(0, 3);
  }
  return [];
}

function getDetailBulletsKV() {
  const rawKV = {};
  const normKV = {};

  const root = document.querySelector("#detailBullets_feature_div");
  if (!root) return { rawKV, normKV };

  function extractValueFromLi(li) {
    // Clonar para limpiar sin tocar el DOM real
    const clone = li.cloneNode(true);

    // Quitar basura que mete ruido
    clone.querySelectorAll("script,style,ul,ol,noscript").forEach(n => n.remove());

    // Quitar label bold para que no se repita
    const bold = clone.querySelector("span.a-text-bold");
    if (bold) bold.remove();

    // Texto final
    let v = cleanValue(clone.textContent || "");
    v = v.replace(/^[:：]\s*/, "");     // quita ":" al inicio
    v = v.replace(/\s*[:：]\s*$/, "");  // quita ":" al final
    return v.trim();
  }

  const lis = root.querySelectorAll("li");
  lis.forEach(li => {
    const labelEl = li.querySelector("span.a-text-bold");
    if (!labelEl) return;

    const labelRaw = cleanValue(labelEl.textContent || "")
      .replace(/[:：]\s*$/, "")
      .trim();

    let value = "";
    const next = labelEl.nextElementSibling;
    if (next && next.tagName === "SPAN") {
      value = cleanValue(next.textContent || "");
    } else {
      value = extractValueFromLi(li);
    }

    if (!labelRaw || !value) return;

    rawKV[labelRaw] = value;
    normKV[normKey(labelRaw)] = value;
  });

  return { rawKV, normKV };
}

function getSynopsis() {
  const root = document.querySelector("#bookDescription_feature_div");
  if (!root) return "";

  const expanded = root.querySelector('div[data-expanded="true"].a-expander-content');
  if (expanded) {
    const html = (expanded.innerHTML || "")
      .replaceAll("</p>", "\n\n")
      .replaceAll("<br>", "\n")
      .replaceAll("<br/>", "\n");
    return stripTags(html);
  }

  const any = root.querySelector(".a-expander-content");
  if (any) return stripTags(any.innerHTML || "");
  return "";
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!["CAPTURE_AMAZON_BOOK","CAPTURE_AMAZON"].includes(msg?.type)) return;

  if (!/\/dp\//i.test(window.location.pathname)) {
    sendResponse({ ok: false, error: "No estás en una página de producto (/dp/...)." });
    return true;
  }

  const title = getTitle();
  const { binding, pubDateRaw } = getBindingAndDate();
  const authors = getAuthors();
  const { rawKV, normKV } = getDetailBulletsKV();
  const synopsis = getSynopsis();

  const isbn13 = normalizeIsbn(pickByKeysNorm(normKV, ["ISBN-13", "ISBN13"]));
  const isbn10 = normalizeIsbn(pickByKeysNorm(normKV, ["ISBN-10", "ISBN10"]));

  const data = {
    SITE: "amazon",
    URL: canonicalUrl(),
    "URL PORTADA": getCoverUrl(),

    ISBN: isbn13 || "",
    ISBN10: isbn10 || "",

    TITULO: title || "",
    AUTOR: authors[0] || "",

    ENCUADERNACION: binding || "",
    "FECHA PUBLICACION": pubDateRaw || "",

    EDITORIAL: pickByKeysNorm(normKV, ["Editorial", "Publisher", "Imprint"]) || "",
    IDIOMA: pickByKeysNorm(normKV, ["Idioma", "Language"]) || "",
    PAGINAS: pickByKeysNorm(normKV, ["Número de páginas", "Number of pages", "Print length"]) || "",
    DIMENSIONES: pickByKeysNorm(normKV, ["Dimensiones", "Dimensions"]) || "",

    SINOPSIS: synopsis || "",

    _amazon_detail_kv: rawKV,
    _amazon_authors: authors
  };

  sendResponse({ ok: true, data });
  return true;
});
