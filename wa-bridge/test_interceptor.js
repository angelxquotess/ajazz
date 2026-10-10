// Test mirato del request interceptor del bridge: verifica che NON crashi piu'
// con "Request is already handled!" (che chiudeva il processo su Node 22).
// Estrae l'handler REALE da server.js ed esegue scenari critici.
const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.join(__dirname, "server.js"), "utf8");

// Estrai il corpo del callback passato a page.on("request", (req) => { ... });
const m = src.match(/page\.on\("request",\s*\(req\)\s*=>\s*\{([\s\S]*?)\n {4}\}\);/);
if (!m) {
  console.error("FAIL: handler request non trovato in server.js");
  process.exit(1);
}
const handler = new Function("req", m[1]);

let failures = 0;
let sawUnhandled = false;
process.on("unhandledRejection", () => { sawUnhandled = true; });

function makeReq({ handled, url, continueRejects, abortRejects }) {
  const calls = { continue: 0, abort: 0 };
  return {
    calls,
    isInterceptResolutionHandled: () => handled,
    url: () => url,
    continue: () => {
      calls.continue++;
      return continueRejects
        ? Promise.reject(new Error("Request is already handled!"))
        : Promise.resolve();
    },
    abort: () => {
      calls.abort++;
      return abortRejects
        ? Promise.reject(new Error("Request is already handled!"))
        : Promise.resolve();
    },
  };
}

function assert(cond, name) {
  if (cond) { console.log("PASS:", name); }
  else { console.error("FAIL:", name); failures++; }
}

// 1) request GIA' gestita -> non deve chiamare continue/abort ne' lanciare
try {
  const r = makeReq({ handled: true, url: "https://web.whatsapp.com/x" });
  handler(r);
  assert(r.calls.continue === 0 && r.calls.abort === 0, "gia' gestita: nessun continue/abort");
} catch (e) { assert(false, "gia' gestita: nessuna eccezione (" + e.message + ")"); }

// 2) telemetria -> abort, non crasha
try {
  const r = makeReq({ handled: false, url: "https://dit.whatsapp.net/deidentified_telemetry" });
  handler(r);
  assert(r.calls.abort === 1 && r.calls.continue === 0, "telemetria: chiama abort");
} catch (e) { assert(false, "telemetria: nessuna eccezione (" + e.message + ")"); }

// 3) request normale -> continue, non crasha
try {
  const r = makeReq({ handled: false, url: "https://web.whatsapp.com/app.js" });
  handler(r);
  assert(r.calls.continue === 1 && r.calls.abort === 0, "normale: chiama continue");
} catch (e) { assert(false, "normale: nessuna eccezione (" + e.message + ")"); }

// 4) continue() che RIFIUTA -> la promise deve essere swallowed (niente crash)
try {
  const r = makeReq({ handled: false, url: "https://web.whatsapp.com/x", continueRejects: true });
  handler(r);
  assert(true, "continue rejecting: handler non lancia in sincrono");
} catch (e) { assert(false, "continue rejecting: nessuna eccezione (" + e.message + ")"); }

// 5) abort() che RIFIUTA -> swallowed
try {
  const r = makeReq({ handled: false, url: "https://dit.whatsapp.net/telemetry", abortRejects: true });
  handler(r);
  assert(true, "abort rejecting: handler non lancia in sincrono");
} catch (e) { assert(false, "abort rejecting: nessuna eccezione (" + e.message + ")"); }

// Verifica finale: nessuna unhandledRejection dopo il microtask flush
setTimeout(() => {
  assert(!sawUnhandled, "nessuna unhandledRejection (il bridge non si chiuderebbe)");
  if (failures === 0) { console.log("\nALL TESTS PASSED"); process.exit(0); }
  else { console.error("\n" + failures + " TEST FALLITI"); process.exit(1); }
}, 100);
