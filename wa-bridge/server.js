// wa-bridge/server.js
// =============================================================================
// JARVIS – bridge HTTP locale per WhatsApp via whatsapp-web.js.
// Esposto su http://127.0.0.1:8765 (override con env WA_PORT / WA_HOST).
//
// PATCH 2026-01 (JARVIS fix v2):
//   * Fuzzy matching sui nomi chat (token-set + Levenshtein normalizzato) con
//     normalizzazione accenti/case, per non fallire piu' su "mamma", "noemy"...
//   * Endpoint /resolveContact -> top-N candidati con score, cosi' JARVIS
//     puo' chiedere conferma vocale se il match esatto non esiste.
//   * Cache in-memory della chat list (refresh ogni 3 min, invalidata dopo
//     un messaggio in ingresso).
//   * Debounce sui messaggi entranti (2 s) per evitare letture doppie quando
//     WhatsApp consegna piu' eventi ravvicinati per la stessa chat.
//   * Blocco della telemetria "dit.whatsapp.net/deidentified_telemetry"
//     (che causa loop CORS + CPU alta nel Chromium headless).
//   * Args puppeteer con WebGL/canvas hw disabilitati -> niente
//     "Too many active WebGL contexts" e meno CPU.
//   * Log dettagliato dello stato quando la risoluzione contatti fallisce.
//
// PATCH 2026-09 (fix "read archived chats" toggle):
//   * BUG: il listener live `client.on("message")` pushava in coda `unread`
//     QUALSIASI messaggio in arrivo, anche da chat archiviate, ignorando
//     completamente il flag `read_archived` scelto dall'utente nel pannello
//     WhatsApp. Il toggle veniva rispettato solo dagli endpoint di lookup
//     (/chats, /resolveContact, /send, /lastFrom) ma NON dal canale in
//     tempo reale usato da JARVIS per leggere/annunciare i nuovi messaggi
//     (actions/check_messages.py -> unread_via_bridge() -> GET /unread).
//   * FIX: il listener ora legge lo stesso config/whatsapp_settings.json
//     usato da Python (single source of truth) e scarta subito i messaggi
//     provenienti da chat archiviate quando `read_archived` e' false.
//
// Endpoint:
//   GET  /status                       -> { ready, qr, online, error }
//   GET  /chats                        -> { chats: [{id,name,isGroup}] }
//   GET  /resolveContact?name=&topN=3  -> { exact, matches:[{name,id,score}] }
//   GET  /unread                       -> { messages:[{id,from,body}] }
//   POST /send   { to|name, text }
//   POST /reply  { name, text, quoteMsgId? }
//   GET  /lastFrom/:name?limit=1
// =============================================================================

const express = require("express");
const qrcode  = require("qrcode-terminal");
const fs      = require("fs");
const path    = require("path");

// --- puppeteer-extra + stealth (whatsapp-web.js require('puppeteer') hook) ---
const puppeteerExtra = require("puppeteer-extra");
const StealthPlugin  = require("puppeteer-extra-plugin-stealth");
puppeteerExtra.use(StealthPlugin());
const realPuppeteerPath = require.resolve("puppeteer");
require.cache[realPuppeteerPath] = {
  id: realPuppeteerPath,
  filename: realPuppeteerPath,
  loaded: true,
  exports: puppeteerExtra,
};

const { Client, LocalAuth } = require("whatsapp-web.js");

// Rete di sicurezza: un reject/errore async isolato (es. una request puppeteer
// gia' gestita) NON deve far terminare il processo del bridge. Ignoriamo pero'
// SOLO gli errori benigni noti di puppeteer/intercettazione: gli altri restano
// fatali cosi' i bug veri non vengono nascosti.
function _isBenignBridgeError(err) {
  const m = (err && err.message) || String(err || "");
  return /Request is already handled|Interception|Target closed|Protocol error|Execution context was destroyed|Session closed|detached Frame/i.test(m);
}
process.on("unhandledRejection", (reason) => {
  console.warn("[wa-bridge] unhandledRejection ignorato:",
    reason && reason.message ? reason.message : reason);
});
process.on("uncaughtException", (err) => {
  if (_isBenignBridgeError(err)) {
    console.warn("[wa-bridge] eccezione puppeteer benigna ignorata:",
      err && err.message ? err.message : err);
    return;
  }
  console.error("[wa-bridge] uncaughtException FATALE:", err);
  throw err;
});

const HOST = process.env.WA_HOST || "127.0.0.1";
const PORT = parseInt(process.env.WA_PORT || "8765", 10);

function findSystemBrowser() {
  if (process.env.CHROME_PATH && fs.existsSync(process.env.CHROME_PATH)) {
    return process.env.CHROME_PATH;
  }
  const candidates = [];
  if (process.platform === "win32") {
    const PF   = process.env["ProgramFiles"]        || "C:\\Program Files";
    const PFX  = process.env["ProgramFiles(x86)"]   || "C:\\Program Files (x86)";
    const LAD  = process.env["LOCALAPPDATA"]        || "";
    candidates.push(
      path.join(PF,  "Google\\Chrome\\Application\\chrome.exe"),
      path.join(PFX, "Google\\Chrome\\Application\\chrome.exe"),
      LAD && path.join(LAD, "Google\\Chrome\\Application\\chrome.exe"),
      path.join(PF,  "Microsoft\\Edge\\Application\\msedge.exe"),
      path.join(PFX, "Microsoft\\Edge\\Application\\msedge.exe"),
    );
  } else if (process.platform === "darwin") {
    candidates.push(
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    );
  } else {
    candidates.push(
      "/usr/bin/google-chrome",
      "/usr/bin/chromium",
      "/usr/bin/chromium-browser",
      "/usr/bin/microsoft-edge",
    );
  }
  for (const p of candidates.filter(Boolean)) {
    try { if (fs.existsSync(p)) return p; } catch (_) {}
  }
  return null;
}

const SYS_BROWSER = findSystemBrowser();
if (SYS_BROWSER) {
  console.log(`[wa-bridge] usero' il browser di sistema: ${SYS_BROWSER}`);
} else {
  console.warn("[wa-bridge] Chrome/Edge non trovato: usero' il Chromium bundled.");
}

// -----------------------------------------------------------------------------
// Fuzzy matching (nessuna nuova dipendenza)
// -----------------------------------------------------------------------------
function normalize(s) {
  return (s || "")
    .toString()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")   // rimuove accenti
    .toLowerCase()
    .replace(/[^a-z0-9\s]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function levenshtein(a, b) {
  if (a === b) return 0;
  const al = a.length, bl = b.length;
  if (!al) return bl;
  if (!bl) return al;
  const v0 = new Array(bl + 1);
  const v1 = new Array(bl + 1);
  for (let i = 0; i <= bl; i++) v0[i] = i;
  for (let i = 0; i < al; i++) {
    v1[0] = i + 1;
    for (let j = 0; j < bl; j++) {
      const cost = a[i] === b[j] ? 0 : 1;
      v1[j + 1] = Math.min(v1[j] + 1, v0[j + 1] + 1, v0[j] + cost);
    }
    for (let j = 0; j <= bl; j++) v0[j] = v1[j];
  }
  return v1[bl];
}

// token_set_ratio simile a rapidfuzz: 0..100
function tokenSetRatio(a, b) {
  const A = normalize(a), B = normalize(b);
  if (!A || !B) return 0;
  const ta = new Set(A.split(" "));
  const tb = new Set(B.split(" "));
  const inter = [...ta].filter((x) => tb.has(x)).sort().join(" ");
  const restA = [...ta].filter((x) => !tb.has(x)).sort().join(" ");
  const restB = [...tb].filter((x) => !ta.has(x)).sort().join(" ");
  const t0 = inter;
  const t1 = (inter + " " + restA).trim();
  const t2 = (inter + " " + restB).trim();
  const combos = [
    [t0, t1], [t0, t2], [t1, t2],
    [A, B],
  ];
  let best = 0;
  for (const [x, y] of combos) {
    const len = Math.max(x.length, y.length);
    if (!len) continue;
    const dist = levenshtein(x, y);
    const score = Math.round((1 - dist / len) * 100);
    if (score > best) best = score;
  }
  // bonus se una stringa contiene l'altra
  if (A.includes(B) || B.includes(A)) best = Math.max(best, 85);
  return best;
}

// -----------------------------------------------------------------------------
// App state
// -----------------------------------------------------------------------------
const app = express();
app.use(express.json({ limit: "1mb" }));

let lastQR    = null;
let isReady   = false;
let initError = null;
const unread  = [];
const seenIds = new Set();

// WhatsApp Web (lug 2026) ha rinominato la proprieta' `_serialized` delle
// chiavi messaggio in `$1`. Questo helper legge l'id in modo tollerante cosi'
// il bridge continua a funzionare con qualsiasi build.
function msgKeyId(id) {
  if (!id) return "";
  return id._serialized || id.$1 || (typeof id === "string" ? id : "") || "";
}

// cache chat list
let _chatCache = { at: 0, list: [] };
const CHAT_CACHE_TTL_MS = 3 * 60 * 1000;
function invalidateChatCache() { _chatCache.at = 0; }

// debounce per notifiche duplicate stessa chat/testo
const _recentNotifs = new Map(); // key -> lastTs
const NOTIF_DEBOUNCE_MS = 2000;

const DESKTOP_UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

const puppeteerOpts = {
  headless: "new",
  args: [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-webgl",
    "--disable-webgl2",
    "--disable-accelerated-2d-canvas",
    "--disable-features=WebGL,WebGL2,Accelerated2dCanvas",
    "--disable-extensions",
    "--no-first-run",
    "--no-default-browser-check",
    "--mute-audio",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-translate",
  ],
};
if (SYS_BROWSER) puppeteerOpts.executablePath = SYS_BROWSER;

const client = new Client({
  authStrategy: new LocalAuth({ clientId: "jarvis" }),
  puppeteer: puppeteerOpts,
  userAgent: DESKTOP_UA,
});

// --- Blocco telemetria WhatsApp (dit.whatsapp.net/deidentified_telemetry) ---
// Whatsapp-web.js espone `client.pupPage` dopo il ready. Attacchiamo un
// requestInterception che aborta la telemetria: risparmia CPU/rete e ferma
// il loop di errori CORS visibile nei log.
async function installRequestInterceptor() {
  try {
    const page = client.pupPage;
    if (!page) return;
    await page.setRequestInterception(true);
    page.on("request", (req) => {
      // Con piu' handler di intercettazione (la libreria ne registra uno suo)
      // bisogna SEMPRE verificare che la request non sia gia' stata gestita,
      // altrimenti puppeteer lancia "Request is already handled!" e la promise
      // rifiutata non gestita fa CRASHARE Node -> il bridge si chiude.
      try {
        if (typeof req.isInterceptResolutionHandled === "function"
            && req.isInterceptResolutionHandled()) {
          return;
        }
        const url = req.url() || "";
        const isTelemetry =
          url.includes("dit.whatsapp.net") ||
          url.includes("deidentified_telemetry") ||
          url.includes("/telemetry");
        const p = isTelemetry ? req.abort() : req.continue();
        // le API ritornano una promise: swallow del reject per non crashare.
        if (p && typeof p.catch === "function") p.catch(() => {});
      } catch (_) { /* request gia' gestita: ignora */ }
    });
    console.log("[wa-bridge] telemetry interceptor attivo.");
  } catch (e) {
    console.warn("[wa-bridge] interceptor non installato:", e.message);
  }
}

// -----------------------------------------------------------------------------
// PATCH runtime per il rename `_serialized` -> `$1` di WhatsApp Web (lug 2026).
// whatsapp-web.js <= 1.34.7 chiama Msg.get(key._serialized): con `$1` diventa
// Msg.get(undefined) e getChats() torna VUOTO -> invio "contatto non trovato",
// nessun messaggio non letto, nessun annuncio. Qui riscriviamo le funzioni
// iniettate DENTRO la pagina (via pupPage) dopo il ready: non tocchiamo i file
// in node_modules, quindi funziona su qualunque installazione gia' presente.
async function installWWebJSPatches() {
  try {
    const page = client.pupPage;
    if (!page) return;
    await page.evaluate(() => {
      if (!window.WWebJS) return;
      // helper tollerante
      window.WWebJS.getMsgKeyId = (key) =>
        (key && (key._serialized ?? key.$1)) ?? undefined;

      const origGetChatModel = window.WWebJS.getChatModel;
      // getChats: itera chat per chat e non scarta tutto se una fallisce;
      // ripristina `_serialized` su lastReceivedKey cosi' l'originale non crasha.
      window.WWebJS.getChats = async () => {
        const chats = window
          .require("WAWebCollections")
          .Chat.getModelsArray();
        const results = [];
        for (const chat of chats) {
          try {
            const lrk = chat.lastReceivedKey;
            if (lrk && lrk._serialized == null && lrk.$1 != null) {
              try { lrk._serialized = lrk.$1; } catch (_) {}
            }
            const model = await origGetChatModel(chat);
            if (model) results.push(model);
          } catch (_) { /* salta la singola chat problematica */ }
        }
        return results;
      };

      // getMessageModel: normalizza l'id del messaggio (`$1` -> `_serialized`)
      // cosi' l'evento `message` in arrivo ha un id valido e non si autoscarta.
      const origGetMessageModel = window.WWebJS.getMessageModel;
      if (origGetMessageModel) {
        window.WWebJS.getMessageModel = (message) => {
          try {
            if (message && message.id && message.id._serialized == null
                && message.id.$1 != null) {
              message.id._serialized = message.id.$1;
            }
          } catch (_) {}
          const m = origGetMessageModel(message);
          try {
            if (m && m.id && typeof m.id === "object"
                && m.id._serialized == null) {
              const sid = window.WWebJS.getMsgKeyId(m.id);
              if (sid) m.id = Object.assign({}, m.id, { _serialized: sid });
            }
          } catch (_) {}
          return m;
        };
      }
    });
    console.log("[wa-bridge] patch WWebJS ($1 rename) applicata.");
  } catch (e) {
    console.warn("[wa-bridge] patch WWebJS non applicata:", e.message);
  }
}

// ---------------------------------------------------------------------------
// Settings condivisi con Python (config/whatsapp_settings.json)
// ---------------------------------------------------------------------------
// Stessa fonte di verita' usata da actions/whatsapp_settings.py (load_settings
// / get_read_archived). Cache breve per non fare I/O su disco a ogni evento
// "message" che arriva (possono essere molto ravvicinati).
const _SETTINGS_FILE = path.join(__dirname, "..", "config", "whatsapp_settings.json");
const _SETTINGS_CACHE_TTL_MS = 2000;
let _settingsCache = { at: 0, readArchived: false };

function isArchivedReadAllowed() {
  const now = Date.now();
  if (now - _settingsCache.at < _SETTINGS_CACHE_TTL_MS) {
    return _settingsCache.readArchived;
  }
  let readArchived = false;
  try {
    const raw = fs.readFileSync(_SETTINGS_FILE, "utf-8");
    const data = JSON.parse(raw);
    readArchived = !!data.read_archived;
  } catch (_) {
    // File assente/non leggibile/non ancora scritto -> default sicuro: OFF.
    readArchived = false;
  }
  _settingsCache = { at: now, readArchived };
  return readArchived;
}

client.on("qr", (qr) => {
  lastQR = qr;
  console.log("\n[wa-bridge] Scansiona QR (WhatsApp > Dispositivi collegati):\n");
  qrcode.generate(qr, { small: true });
});
client.on("ready", async () => {
  isReady = true; lastQR = null; initError = null;
  console.log("[wa-bridge] WhatsApp pronto.");
  await installWWebJSPatches();
  installRequestInterceptor();
});
client.on("auth_failure", (m) => { initError = "auth_failure: " + m; console.error(initError); });
client.on("disconnected", (r) => { isReady = false; console.warn("[wa-bridge] disconnesso", r); });

client.on("message", async (msg) => {
  try {
    if (msg.fromMe) return;
    const mid = msgKeyId(msg.id);
    if (mid && seenIds.has(mid)) return;
    if (mid) seenIds.add(mid);
    if (seenIds.size > 1000) {
      const arr = Array.from(seenIds).slice(-500);
      seenIds.clear();
      arr.forEach((x) => seenIds.add(x));
    }

    let from = msg.from;
    let chatObj = null;
    try {
      chatObj = await msg.getChat();
      from = (chatObj && chatObj.name) ? chatObj.name : from;
    } catch (_) {}

    // FIX: rispetta il toggle "READ ARCHIVED CHATS". Se la chat e'
    // archiviata e l'utente NON ha abilitato la lettura degli archiviati,
    // il messaggio non entra nemmeno in coda: niente notifica/lettura da
    // parte di JARVIS. Prima questo controllo mancava del tutto qui.
    if (chatObj && chatObj.archived && !isArchivedReadAllowed()) {
      return;
    }

    // Debounce 2s per stessa chat+body: evita letture doppie.
    const dkey = (from || "?") + "|" + ((msg.body || "").slice(0, 60));
    const now = Date.now();
    const prev = _recentNotifs.get(dkey) || 0;
    if (now - prev < NOTIF_DEBOUNCE_MS) return;
    _recentNotifs.set(dkey, now);
    if (_recentNotifs.size > 200) {
      // GC leggero
      for (const [k, ts] of _recentNotifs) {
        if (now - ts > 60_000) _recentNotifs.delete(k);
      }
    }

    unread.push({ id: mid, from, body: msg.body || "", type: msg.type || "chat" });
    if (unread.length > 500) unread.splice(0, unread.length - 250);
    invalidateChatCache();
  } catch (e) {
    console.error("[wa-bridge] err msg", e);
  }
});

client.initialize().catch((e) => {
  initError = String(e && e.message || e);
  console.error("[wa-bridge] init err", e);
});

// ---------------- API ----------------
// Pagina di stato + QR scansionabile. Prima "GET /" dava "Cannot GET /" e
// sembrava rotto; inoltre l'utente non aveva modo di collegare il bridge
// (che e' una sessione whatsapp-web.js SEPARATA dalla tendina/webview).
// Ora questa pagina mostra lo stato in tempo reale e, se serve, il QR da
// scansionare (WhatsApp > Dispositivi collegati).
app.get("/", (_req, res) => {
  res
    .status(200)
    .type("html")
    .send(`<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>JARVIS wa-bridge</title>
<style>
  *{box-sizing:border-box}
  body{background:#000;color:#eee;font-family:system-ui,Segoe UI,Arial,sans-serif;
       margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center}
  .card{max-width:520px;width:92%;padding:28px 32px;border:1px solid #222;border-radius:16px;background:#0b0b0b;text-align:center}
  h1{font-size:20px;margin:0 0 6px;letter-spacing:.5px}
  .sub{color:#888;font-size:13px;margin-bottom:18px}
  .s{font-size:16px;margin:14px 0;padding:12px 16px;background:#111;border-radius:10px}
  #qrbox{display:none;margin:18px auto 6px;padding:16px;background:#fff;border-radius:12px;width:max-content}
  #qrbox canvas,#qrbox img{display:block}
  .hint{color:#9ad;font-size:13px;margin-top:10px;line-height:1.6}
  .err{color:#f66;margin-top:10px;font-size:13px}
  code{color:#8fd}
  .steps{text-align:left;color:#bbb;font-size:13px;line-height:1.8;margin-top:14px}
</style></head><body><div class="card">
  <h1>JARVIS · wa-bridge</h1>
  <div class="sub">connessione WhatsApp per JARVIS (sessione dedicata)</div>
  <div id="status" class="s">Carico lo stato…</div>
  <div id="qrbox"></div>
  <div id="hint" class="hint"></div>
  <div id="err" class="err"></div>
  <div class="steps" id="steps"></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js"></script>
<script>
  var lastQr = null;
  var elS = document.getElementById('status');
  var elQ = document.getElementById('qrbox');
  var elH = document.getElementById('hint');
  var elE = document.getElementById('err');
  var elSteps = document.getElementById('steps');

  function renderQR(qr){
    if(qr === lastQr) return;
    lastQr = qr;
    elQ.innerHTML = '';
    elQ.style.display = 'block';
    if(window.QRCode && QRCode.toCanvas){
      var c = document.createElement('canvas');
      elQ.appendChild(c);
      QRCode.toCanvas(c, qr, {width:260, margin:1}, function(e){
        if(e){ elQ.innerHTML = '<div style="color:#000;font-size:12px;word-break:break-all">'+qr+'</div>'; }
      });
    } else {
      elQ.innerHTML = '<div style="color:#000;font-size:12px;word-break:break-all">'+qr+'</div>';
    }
  }

  function poll(){
    fetch('/status').then(function(r){return r.json()}).then(function(d){
      elE.textContent = d.error ? ('Errore: '+d.error) : '';
      if(d.ready){
        elS.textContent = '🟢 CONNESSO — WhatsApp pronto. Puoi tornare a JARVIS.';
        elQ.style.display = 'none';
        elH.textContent = '';
        elSteps.innerHTML = '';
      } else if(d.qr){
        elS.textContent = '🟡 SCANSIONA IL QR per collegare WhatsApp';
        renderQR(d.qr);
        elH.textContent = '';
        elSteps.innerHTML = 'Sul telefono: <b>WhatsApp → Impostazioni → Dispositivi collegati → Collega un dispositivo</b>, poi inquadra il QR qui sopra.';
      } else {
        elS.textContent = '🟠 AVVIO IN CORSO — attendo WhatsApp (il QR comparirà tra pochi secondi)…';
        elQ.style.display = 'none';
        elSteps.innerHTML = '';
      }
    }).catch(function(){
      elS.textContent = '🔴 Bridge non raggiungibile su questa porta.';
    });
  }
  poll();
  setInterval(poll, 3000);
</script>
</body></html>`);
});

app.get("/health", (_req, res) =>
  res.json({ ok: true, ready: isReady, waitingQR: !!lastQR })
);

app.get("/status", (_req, res) =>
  res.json({ ready: isReady, qr: lastQR, online: true, error: initError })
);

async function getChatsCached(includeArchived = false) {
  if (!isReady) return [];
  const now = Date.now();
  // Due cache separate: archiviate incluse vs escluse.
  const cacheKey = includeArchived ? "with_archived" : "no_archived";
  if (
    _chatCache.list.length &&
    _chatCache.key === cacheKey &&
    now - _chatCache.at < CHAT_CACHE_TTL_MS
  ) {
    return _chatCache.list;
  }
  const chats = await client.getChats();
  const filtered = includeArchived
    ? chats
    : chats.filter((c) => !c.archived);
  const mapped = filtered.map((c) => ({
    _raw: c,
    id:   c.id && c.id._serialized ? c.id._serialized : String(c.id || ""),
    name: c.name || (c.formattedTitle) || (c.id && c.id.user) || "(chat)",
    isGroup: !!c.isGroup,
    isArchived: !!c.archived,
  }));
  _chatCache = { at: now, key: cacheKey, list: mapped };
  return mapped;
}

app.get("/chats", async (req, res) => {
  if (!isReady) return res.json({ chats: [], ready: false, error: initError });
  try {
    const includeArchived =
      req.query.includeArchived === "1" ||
      req.query.includeArchived === "true";
    const chats = await getChatsCached(includeArchived);
    res.json({
      chats: chats.map((c) => ({
        id: c.id, name: c.name, isGroup: c.isGroup, isArchived: c.isArchived,
      })),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.get("/unread", (_req, res) => {
  const out = unread.splice(0, unread.length);
  res.json({ messages: out });
});

// ---- Risoluzione contatti con top-N fuzzy match ----------------------------
async function resolveContactTopN(needleRaw, topN = 3, threshold = 65,
                                  includeArchived = false) {
  const needle = normalize(needleRaw);
  if (!needle) return { exact: null, matches: [] };
  const chats = await getChatsCached(includeArchived);

  // 1) exact (normalizzato)
  let exact = chats.find((c) => normalize(c.name) === needle);

  // 2) score fuzzy per tutti
  const scored = chats
    .map((c) => ({
      name: c.name,
      id: c.id,
      isGroup: c.isGroup,
      isArchived: c.isArchived,
      _raw: c._raw,
      score: tokenSetRatio(needle, c.name),
    }))
    .filter((c) => c.score >= threshold)
    .sort((a, b) => b.score - a.score);

  if (!exact && scored.length && scored[0].score >= 92) {
    exact = scored[0];
  }

  const top = scored.slice(0, topN);
  return { exact, matches: top };
}

async function resolveChatByName(needleRaw, includeArchived = false) {
  const { exact, matches } = await resolveContactTopN(
    needleRaw, 1, 60, includeArchived);
  const pick = exact || matches[0] || null;
  if (!pick) {
    // Log dettagliato per debug: quali nomi ha visto il bridge?
    try {
      const chats = await getChatsCached(includeArchived);
      const preview = chats.slice(0, 15).map((c) => c.name).join(" | ");
      console.warn(
        `[wa-bridge] chat NON risolta per "${needleRaw}". Chat viste (` +
        `${chats.length}, archived=${includeArchived}): ${preview}` +
        `${chats.length > 15 ? " ..." : ""}`
      );
    } catch (_) {}
    return null;
  }
  return pick._raw || pick;
}

app.get("/resolveContact", async (req, res) => {
  if (!isReady) return res.status(503).json({ ready: false, error: initError });
  try {
    const name  = (req.query.name || "").toString();
    const topN  = Math.max(1, Math.min(10, parseInt(req.query.topN || "3", 10)));
    const thr   = Math.max(0, Math.min(100, parseInt(req.query.threshold || "65", 10)));
    const inclA =
      req.query.includeArchived === "1" || req.query.includeArchived === "true";
    const { exact, matches } = await resolveContactTopN(name, topN, thr, inclA);
    res.json({
      query: name,
      threshold: thr,
      exact: exact ? { name: exact.name, id: exact.id, isGroup: !!exact.isGroup, isArchived: !!exact.isArchived, score: 100 } : null,
      matches: matches.map((m) => ({
        name: m.name, id: m.id, isGroup: !!m.isGroup,
        isArchived: !!m.isArchived, score: m.score,
      })),
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

app.post("/send", async (req, res) => {
  if (!isReady) return res.status(503).json({ ok: false, error: "WhatsApp non pronto, scansiona il QR." });
  const { to, name, text, includeArchived } = req.body || {};
  const inclA = !!includeArchived;
  const message = (text || "").toString();
  if (!message) return res.status(400).json({ ok: false, error: "text mancante" });
  try {
    let target = (to || "").toString();
    let chatObj = null;
    if (!target.includes("@")) {
      chatObj = await resolveChatByName(name || to, inclA);
      if (!chatObj) {
        // Ritorna anche i top candidati cosi' il chiamante puo' chiedere
        // conferma vocale senza fare una seconda round-trip.
        const { matches } = await resolveContactTopN(name || to, 3, 60, inclA);
        return res.status(404).json({
          ok: false,
          error: "chat non trovata",
          suggestions: matches.map((m) => ({ name: m.name, id: m.id, score: m.score })),
        });
      }
      target = chatObj.id._serialized || chatObj.id;
    }
    await client.sendMessage(target, message);
    res.json({ ok: true, to: (chatObj && chatObj.name) || target });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.get("/lastFrom/:name", async (req, res) => {
  if (!isReady) return res.status(503).json({ messages: [], error: "WhatsApp non pronto." });
  const rawName = decodeURIComponent(req.params.name || "");
  const limit = Math.max(1, Math.min(20, parseInt(req.query.limit || "1", 10)));
  const inclA =
    req.query.includeArchived === "1" || req.query.includeArchived === "true";
  try {
    const chat = await resolveChatByName(rawName, inclA);
    if (!chat) {
      const { matches } = await resolveContactTopN(rawName, 3, 60, inclA);
      return res.status(404).json({
        messages: [],
        error: "chat non trovata",
        suggestions: matches.map((m) => ({ name: m.name, id: m.id, score: m.score })),
      });
    }
    const raw = await chat.fetchMessages({ limit: limit + 20 });
    const incoming = raw.filter((m) => !m.fromMe).slice(-limit).reverse();
    res.json({
      chat: chat.name || (chat.id && chat.id._serialized) || "",
      messages: incoming.map((m) => ({
        id:   msgKeyId(m.id) || String(m.id || ""),
        from: chat.name || (m.author || m.from || ""),
        body: m.body || "",
        type: m.type || "chat",
        timestamp: m.timestamp || 0,
      })),
    });
  } catch (e) {
    res.status(500).json({ messages: [], error: String(e) });
  }
});

app.post("/reply", async (req, res) => {
  if (!isReady) return res.status(503).json({ ok: false, error: "WhatsApp non pronto." });
  // FIX: come /send, la risoluzione del contatto per la risposta include
  // sempre gli archiviati (di default true), a prescindere dal toggle
  // "read_archived" che riguarda solo la lettura dei messaggi in arrivo.
  const { name, text, quoteMsgId, includeArchived } = req.body || {};
  const inclA = includeArchived === undefined ? true : !!includeArchived;
  const message = (text || "").toString();
  if (!message) return res.status(400).json({ ok: false, error: "text mancante" });
  try {
    const chat = await resolveChatByName(name, inclA);
    if (!chat) {
      const { matches } = await resolveContactTopN(name, 3, 60, inclA);
      return res.status(404).json({
        ok: false, error: "chat non trovata",
        suggestions: matches.map((m) => ({ name: m.name, id: m.id, score: m.score })),
      });
    }

    let quoted = null;
    if (quoteMsgId) {
      try {
        const hist = await chat.fetchMessages({ limit: 50 });
        quoted = hist.find((m) => m.id && m.id._serialized === quoteMsgId) || null;
      } catch (_) { quoted = null; }
    }

    if (quoted) {
      await chat.sendMessage(message, { quotedMessageId: quoted.id._serialized });
    } else {
      await chat.sendMessage(message);
    }
    res.json({ ok: true, quoted: !!quoted });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// --- Voice addon (sendVoice + /media/:msgId) ---
try {
  require("./voice_addon")(app, client);
  console.log("[wa-bridge] voice_addon attivo (POST /sendVoice, GET /media/:id)");
} catch (e) {
  console.warn("[wa-bridge] voice_addon non caricato:", e.message);
}

app.listen(PORT, HOST, () => {
  console.log(`[wa-bridge] listening on http://${HOST}:${PORT}`);
});
