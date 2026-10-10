// wa-bridge/call-bridge/call_bridge.js
// =============================================================================
// JARVIS – call-bridge: sessione WPPConnect DEDICATA per chiamate voce/video
// native su WhatsApp Web (Early Access), separata dal wa-bridge principale
// (che resta su whatsapp-web.js, solo testo/media).
//
// Perche' un processo/sessione separati:
//   - whatsapp-web.js (bridge principale) e WPPConnect (questo bridge) sono
//     due librerie diverse: non vanno fatte girare sulla stessa pagina/sessione.
//   - Un secondo "dispositivo collegato" evita che un crash o un logout su
//     questo bridge butti giu' anche messaggi/HUD del bridge principale.
//
// COSA FA:
//   1) Apre una sessione WPPConnect (QR alla prima esecuzione, poi persistita
//      in tokens/).
//   2) Aggancia lo stack VoIP nativo via wa-js (WPP.call.*) dentro la pagina
//      reale di WhatsApp Web.
//   3) Espone un'API HTTP locale (default :8767) che il resto di JARVIS
//      (Python) chiama per accept/reject/hangup/status, e un canale SSE per
//      notificare la HUD in tempo reale (chiamata in arrivo, terminata...).
//
// COSA NON FA (di proposito):
//   - Non fa STT/TTS qui dentro. Espone solo gli HOOK POINT (vedi sotto)
//     dove collegare la tua pipeline vocale esistente (voice_io.py / Gemini
//     Live) via i device audio virtuali instradati nel browser.
//   - Non implementa "mute" via API WhatsApp (non confermata/documentata in
//     modo stabile in wa-js). Il muto e' realizzato a livello di routing
//     audio: si smette di alimentare il "microfono virtuale" del browser
//     (vedi actions/call_bridge_client.py -> /call/mute). E' piu' robusto
//     di un'API nativa non verificata.
//
// API REALE DI WPP.call (verificata contro node_modules/@wppconnect/wa-js
// inclusa in questo progetto, v4.6.0 - vedi dist/call/functions/*.d.ts):
//   WPP.call.accept(callId?)              -> accetta una chiamata IN ARRIVO
//   WPP.call.reject(callId?)              -> rifiuta/chiude una chiamata IN ARRIVO
//                                             (lancia call_is_not_incoming_ring
//                                             se la chiamata e' gia' stata accettata!)
//   WPP.call.end()                        -> chiude la chiamata ATTIVA (nessun
//                                             argomento: agisce sempre su
//                                             CallStore.activeCall)
//   WPP.call.offer(jid, {isVideo})        -> avvia una chiamata in uscita
//   WPP.call.enableCallInterface()        -> inizializza lo stack VoIP nativo
//                                             (patcha i flag A/B che abilitano
//                                             le chiamate e chiama
//                                             WAWebVoipInit.initWAWebVoip()).
//                                             offer() la chiama da sola per le
//                                             chiamate in uscita, ma NON viene
//                                             mai chiamata per le chiamate in
//                                             ARRIVO: senza di lei lo stack VoIP
//                                             nativo puo' non essere mai
//                                             inizializzato, il che spiega sia
//                                             l'audio muto in ricezione sia
//                                             chiamate che non vengono notificate.
//   NON esistono WPP.call.hangup / hangUp / endCall: e' per questo che
//   "riattacca" non ha mai funzionato (il vecchio codice provava quei nomi e
//   ripiegava su reject(), che pero' fallisce su una chiamata gia' accettata).
//   NON esiste nessuna WPP.call.mute/unmute: il muto e' implementato qui
//   disabilitando davvero la MediaStreamTrack del microfono catturata da
//   getUserMedia (vedi hookNativeCallStack), non solo un flag di stato.
//
// REQUISITI (non installati/testati in questo scaffold):
//   npm install @wppconnect-team/wppconnect express
//   Un browser reale (headful) o Xvfb su Linux, + device audio virtuale
//   configurato PRIMA di avviare questo processo (vedi ../../scripts/).
// =============================================================================

const express = require("express");
const path = require("path");
const { spawn } = require("child_process");

const CALL_PORT = process.env.CALL_BRIDGE_PORT || 8767;
const CALL_HOST = process.env.CALL_BRIDGE_HOST || "127.0.0.1";
const SESSION_NAME = process.env.CALL_BRIDGE_SESSION || "jarvis-calls";

// Nome dei device audio virtuali (vedi scripts/setup_virtual_audio_linux.sh).
// Il browser deve vedere QUESTI come default mic/speaker di sistema PRIMA
// di partire: su Linux lo si fa impostando pactl set-default-source/sink,
// su Windows selezionando VB-Cable come device di default in Impostazioni
// suono prima di lanciare il processo (o via script nircmd/soundvolumeview).
const VIRTUAL_MIC_LABEL = process.env.CALL_BRIDGE_VMIC || "jarvis_call_mic";
const VIRTUAL_SPK_LABEL = process.env.CALL_BRIDGE_VSPK || "jarvis_call_speaker";

let wppconnect;
try {
  wppconnect = require("@wppconnect-team/wppconnect");
} catch (e) {
  console.error(
    "[call-bridge] Manca @wppconnect-team/wppconnect. Esegui:\n" +
    "  cd wa-bridge/call-bridge && npm install\n" +
    "Errore originale:", e.message
  );
  process.exit(1);
}

// --- Stato in-memory -------------------------------------------------------
const state = {
  ready: false,
  qr: null,
  error: null,
  activeCall: null,      // { id, from, isVideo, startedAt }
  muted: false,          // controllato SOLO lato routing audio, non WhatsApp
  lastEvent: null,
  lastVideoFrame: null,  // data URL JPEG dell'ultimo frame video REMOTO (null se nessuna videochiamata attiva)
  lastVideoFrameAt: 0,
};

// Client SSE connessi (per aggiornare la HUD in tempo reale).
const sseClients = new Set();
function broadcast(event) {
  state.lastEvent = event;
  const payload = `data: ${JSON.stringify(event)}\n\n`;
  for (const res of sseClients) {
    try { res.write(payload); } catch (_) { /* client morto, verra' ripulito */ }
  }
}

// --- Bootstrap sessione ------------------------------------------------------

let client = null;

async function start() {
  client = await wppconnect.create({
    session: SESSION_NAME,
    folderNameToken: path.join(__dirname, "tokens"),
    catchQR: (base64Qr, asciiQR) => {
      state.qr = base64Qr;
      console.log("[call-bridge] Scansiona il QR (nuovo dispositivo collegato dedicato alle chiamate):");
      console.log(asciiQR);
    },
    statusFind: (status) => {
      console.log("[call-bridge] status:", status);
    },
    // FIX "non sento l'altra persona": la modalita' headless di Chromium
    // (quella usata finora di default, vedi sotto) NON ha un vero output
    // audio verso il sistema operativo - e' un limite noto di
    // Puppeteer/Chromium, non solo di questa pagina: un <audio>/<video> puo'
    // risultare "in play" secondo l'API HTMLMediaElement (motivo per cui
    // l'hook su srcObject qui sotto sembrava gia' a posto), ma nessun
    // campione arriva mai davvero al device audio virtuale. Il microfono
    // invece "funzionava" comunque perche' catturare/abilitare una
    // MediaStreamTrack via getUserMedia e' pura manipolazione JS e non
    // richiede un vero output audio. Ora il browser e' REALE (headful) di
    // default - unica modalita' in cui l'audio remoto arriva davvero al
    // device audio virtuale - ma con la finestra spostata fuori
    // dall'area visibile dello schermo (mai minimizzata: una finestra
    // minimizzata puo' far scattare il "background tab throttling" di
    // Chrome e mettere in pausa/rallentare proprio l'audio), cosi' resta
    // "in background" esattamente come prima, senza comparire mai
    // sullo schermo. Per tornare al vecchio comportamento (sconsigliato:
    // niente audio in ricezione) impostare CALL_BRIDGE_HEADLESS=true.
    headless: process.env.CALL_BRIDGE_HEADLESS === "true", // false di default (headful, necessario per l'audio reale); su Linux serve comunque un display reale o Xvfb se la macchina non ne ha uno
    puppeteerOptions: {
      args: [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        // NON usare --use-fake-device-for-media-stream: genererebbe un mic
        // finto silenzioso, ignorando il device audio virtuale reale.
        // Usiamo invece --use-fake-ui-for-media-stream per autoaccettare il
        // prompt "consenti microfono", MA con il device audio virtuale
        // impostato come default di sistema, cosi' il flusso reale passa
        // davvero dal nostro sink/source.
        "--use-fake-ui-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        // Finestra reale ma invisibile: la spostiamo fuori dallo schermo
        // invece di minimizzarla, cosi' per Chrome resta "visibile" (Page
        // Visibility API) e continua a riprodurre l'audio normalmente.
        "--window-position=-32000,-32000",
        "--window-size=1000,700",
        // Le ottimizzazioni "risparmio risorse" di Chrome per finestre in
        // background/occluse si applicano anche a una finestra fuori
        // schermo come questa: disabilitarle evita che l'audio remoto
        // venga rallentato o silenziato mentre resta nascosta.
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
      ],
    },
  });

  state.ready = true;
  state.qr = null;
  console.log(`[call-bridge] Sessione pronta (mic virtuale atteso: "${VIRTUAL_MIC_LABEL}", speaker: "${VIRTUAL_SPK_LABEL}")`);

  await hideFromTaskbarWindows();
  await hookNativeCallStack();
}

// --- Nascondi la finestra dalla barra delle applicazioni (solo Windows) ----
// Il browser gira headful (serve per l'audio reale, vedi sopra) e fuori
// schermo, ma Windows mostra comunque un pulsante in taskbar per QUALSIASI
// finestra reale, anche se posizionata fuori dall'area visibile: non e' una
// cosa che si possa evitare con un flag di Chromium (il browser non ha
// un'opzione "nascondimi dalla taskbar" da riga di comando), e' proprio lo
// stile della finestra a livello di sistema operativo. Lo sistemiamo qui
// cambiando lo stile della finestra da "app window" a "tool window"
// (WS_EX_TOOLWINDOW) via una chiamata a user32.dll: e' la stessa tecnica
// usata da molti programmi che girano in background per restare invisibili
// in taskbar senza essere minimizzati (minimizzato resterebbe comunque
// visibile in taskbar, ed e' proprio quello che vogliamo evitare).
async function hideFromTaskbarWindows() {
  if (process.platform !== "win32") return; // rilevante solo su Windows
  try {
    const browser = client && client.page && typeof client.page.browser === "function"
      ? client.page.browser()
      : null;
    const proc = browser && typeof browser.process === "function" ? browser.process() : null;
    const pid = proc && proc.pid;
    if (!pid) {
      console.log("[call-bridge] Nascondi da taskbar: PID del browser non disponibile, salto.");
      return;
    }
    const psScript = [
      'Add-Type @\'',
      'using System;',
      'using System.Runtime.InteropServices;',
      'public class JarvisWin {',
      '  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr hWnd, int nIndex);',
      '  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr hWnd, int nIndex, int dwNewLong);',
      '  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);',
      '}',
      '\'@',
      `$p = Get-Process -Id ${pid} -ErrorAction SilentlyContinue`,
      'if ($p -and $p.MainWindowHandle -ne 0) {',
      '  $h = $p.MainWindowHandle',
      '  $style = [JarvisWin]::GetWindowLong($h, -20)',                 // GWL_EXSTYLE
      '  $style = ($style -bor 0x80) -band (-bnot 0x40000)',            // + WS_EX_TOOLWINDOW, - WS_EX_APPWINDOW
      '  [JarvisWin]::SetWindowLong($h, -20, $style) | Out-Null',
      '  [JarvisWin]::SetWindowPos($h, [IntPtr]::Zero, 0,0,0,0, 0x27) | Out-Null', // NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
      '}',
    ].join("\n");

    // -WindowStyle Hidden + windowsHide: evitano che spawnare PowerShell
    // apra a sua volta una finestra di console visibile.
    const child = spawn(
      "powershell",
      ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", psScript],
      { windowsHide: true }
    );
    child.on("error", (e) => console.log("[call-bridge] Nascondi da taskbar fallito:", e.message));
  } catch (e) {
    console.log("[call-bridge] Nascondi da taskbar fallito:", e && e.message ? e.message : String(e));
  }
}

async function hookNativeCallStack() {
  const page = client.page || (client.waPage) || null;
  if (!page) {
    state.error = "Impossibile ottenere la Puppeteer Page dal client WPPConnect (API cambiata? controlla client.page).";
    console.error("[call-bridge]", state.error);
    return;
  }

  // Bridge Node <- pagina: la pagina chiama window.__jarvisCallEvent(evt)
  // ogni volta che WPP emette un evento di chiamata.
  await page.exposeFunction("__jarvisCallEvent", (evt) => {
    handleCallEvent(evt);
  });

  // Bridge Node <- pagina per i frame video REMOTI (vedi hook sul <video>
  // qui sotto): la pagina invia un JPEG in data URL a ogni frame catturato,
  // qui lo teniamo solo in memoria (l'ultimo, sovrascritto) cosi' /call/video-frame
  // puo' servirlo on-demand senza tenere in RAM uno storico inutile.
  //
  // FIX "la UI video non compare per le chiamate avviate da JARVIS": il primo
  // frame video remoto arriva SOLO quando la chiamata e' davvero connessa
  // (WhatsApp assegna lo stream via srcObject solo a quel punto), ma per le
  // chiamate in USCITA (/call/start) nessuno faceva mai passare
  // state.activeCall.status da "dialing" ad "accepted": quella transizione
  // avveniva SOLO tramite /call/accept, che pero' e' chiamato solo per le
  // chiamate IN ARRIVO. actions/call_hud.py (CallPoller._run) avvia il poll
  // di /call/video-frame solo quando status === "accepted", quindi per una
  // videochiamata avviata da JARVIS quel poll non partiva MAI, anche se qui
  // in call_bridge.js il frame veniva gia' catturato correttamente: il video
  // restava chiuso in state.lastVideoFrame, mai richiesto da Python. Il primo
  // frame ricevuto e' il segnale piu' affidabile che la chiamata e' connessa
  // (anche per l'uscita, dove non esiste un evento nativo "l'altro ha
  // risposto"): lo usiamo per completare la transizione di stato mancante.
  await page.exposeFunction("__jarvisVideoFrame", (dataUrl) => {
    state.lastVideoFrame = dataUrl;
    state.lastVideoFrameAt = Date.now();
    if (state.activeCall && state.activeCall.status !== "accepted") {
      state.activeCall.status = "accepted";
      broadcast({ type: "call_accepted", call: state.activeCall });
    }
  });

  await page.evaluate(async () => {
    if (typeof WPP === "undefined" || !WPP.call) {
      window.__jarvisCallEvent({ type: "error", message: "WPP.call non disponibile: la beta chiamate su WhatsApp Web non e' attiva su questo account/numero." });
      return;
    }

    // --- Hook sul microfono reale, per un mute che funziona DAVVERO -------
    // Non esiste una WPP.call.mute/unmute (verificato: non e' tra le funzioni
    // esportate da @wppconnect/wa-js). L'unico modo affidabile e' intercettare
    // getUserMedia e disabilitare/riabilitare la vera MediaStreamTrack audio
    // catturata da WhatsApp per la chiamata, cosi' il muto agisce sul flusso
    // reale invece che su un flag che nessuno legge.
    if (!window.__jarvisMicHooked) {
      window.__jarvisMicHooked = true;
      window.__jarvisAudioTracks = new Set();
      // Stato desiderato del muto lato pagina: e' la fonte di verita' per
      // qualunque NUOVA MediaStreamTrack catturata durante la chiamata (vedi
      // sotto). FIX "si riattiva da solo": WPP puo' richiamare getUserMedia
      // piu' volte nella stessa chiamata (rinegoziazione audio, riconnessione,
      // passaggio a video...) creando un NUOVO track che nasce SEMPRE con
      // enabled=true, ignorando il muto in corso; prima di questo fix la
      // nuova track riattivava il microfono senza che __jarvisSetMuted
      // venisse mai richiamato e senza che lo stato lato Node cambiasse, cosi'
      // JARVIS restava convinto che il microfono fosse ancora muto mentre non
      // lo era piu'. Applichiamo qui lo stato desiderato a OGNI nuova track,
      // appena catturata, non solo a quelle gia' presenti al momento del
      // comando /call/mute.
      window.__jarvisMutedState = false;
      const origGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
      navigator.mediaDevices.getUserMedia = async function (constraints) {
        const stream = await origGetUserMedia(constraints);
        if (constraints && constraints.audio) {
          stream.getAudioTracks().forEach((track) => {
            track.enabled = !window.__jarvisMutedState;
            window.__jarvisAudioTracks.add(track);
            track.addEventListener("ended", () => window.__jarvisAudioTracks.delete(track));
          });
        }
        // Qualunque stream restituito da getUserMedia e' per definizione la
        // TUA webcam/microfono locale (mai quello dell'altra persona, che
        // arriva sempre via RTCPeerConnection, non da getUserMedia). Lo
        // marchiamo cosi' l'hook video qui sotto puo' escludere l'elemento
        // di auto-anteprima e agganciarsi solo allo stream remoto vero.
        try { stream.__jarvisLocalStream = true; } catch (_) { /* ignora */ }
        return stream;
      };
      window.__jarvisSetMuted = function (muted) {
        window.__jarvisMutedState = !!muted;
        let affected = 0;
        for (const track of Array.from(window.__jarvisAudioTracks)) {
          if (track.readyState !== "live") {
            window.__jarvisAudioTracks.delete(track);
            continue;
          }
          track.enabled = !muted;
          affected += 1;
        }
        return affected;
      };
      // Stato REALE del microfono, letto direttamente dalle MediaStreamTrack
      // vive invece che fidarsi solo del flag state.muted lato Node (che
      // riflette solo l'ultimo comando /call/mute o /call/unmute ricevuto e
      // puo' disallinearsi dalla realta', vedi sopra). Usato da /status
      // (vedi _tryGetRealMuted) cosi' che chi controlla lo stato prima di
      // agire (vedi actions/call_hud.py) veda sempre il microfono com'e'
      // DAVVERO, non quello che JARVIS pensa che sia.
      // true = tutte le track vive sono disabilitate (muto reale), false =
      // almeno una track viva e' attiva (NON muto), null = nessuna track viva
      // (nessuna chiamata in corso / mic non ancora catturato: stato ignoto).
      window.__jarvisGetMicMuted = function () {
        let sawLive = false;
        let allDisabled = true;
        for (const track of Array.from(window.__jarvisAudioTracks)) {
          if (track.readyState !== "live") {
            window.__jarvisAudioTracks.delete(track);
            continue;
          }
          sawLive = true;
          if (track.enabled) allDisabled = false;
        }
        if (!sawLive) return null;
        return allDisabled;
      };
    }

    // --- Hook sul VIDEO in ricezione, per mostrare l'altra persona nella HUD -
    // Stesso principio dell'hook audio qui sotto (WhatsApp assegna lo stream
    // remoto via srcObject a un elemento nascosto): quando lo stream ha anche
    // una traccia video, la disegniamo periodicamente su un <canvas> non
    // visibile e mandiamo il frame come JPEG a Node via __jarvisVideoFrame.
    // Nessuna finestra/overlay del browser viene mai mostrata: tutto resta
    // esattamente "in background" come per l'audio, cambia solo che ora
    // inviamo anche immagini oltre ai soli eventi di stato.
    // Frame rate e risoluzione tenuti bassi di proposito (non e' una vera
    // videochiamata fluida, solo un'anteprima nella HUD): meno CPU/banda.
    if (!window.__jarvisVideoHooked) {
      window.__jarvisVideoHooked = true;
      const CAPTURE_FPS = 5;
      const CAPTURE_MAX_W = 480;
      const _canvas = document.createElement("canvas");
      const _ctx = _canvas.getContext("2d", { alpha: false });

      window.__jarvisStartVideoCapture = function (videoEl) {
        if (window.__jarvisVideoCaptureEl === videoEl && window.__jarvisVideoCaptureTimer) return;
        if (window.__jarvisVideoCaptureTimer) clearInterval(window.__jarvisVideoCaptureTimer);
        window.__jarvisVideoCaptureEl = videoEl;
        window.__jarvisVideoCaptureTimer = setInterval(() => {
          try {
            if (!videoEl || videoEl.readyState < 2 || !videoEl.videoWidth) return; // frame non ancora pronto, salto
            const scale = Math.min(1, CAPTURE_MAX_W / videoEl.videoWidth);
            const w = Math.max(1, Math.round(videoEl.videoWidth * scale));
            const h = Math.max(1, Math.round(videoEl.videoHeight * scale));
            if (_canvas.width !== w) _canvas.width = w;
            if (_canvas.height !== h) _canvas.height = h;
            _ctx.drawImage(videoEl, 0, 0, w, h);
            window.__jarvisVideoFrame(_canvas.toDataURL("image/jpeg", 0.55));
          } catch (_) { /* frame saltato, si riprova al prossimo tick */ }
        }, Math.round(1000 / CAPTURE_FPS));
      };

      window.__jarvisStopVideoCapture = function () {
        if (window.__jarvisVideoCaptureTimer) clearInterval(window.__jarvisVideoCaptureTimer);
        window.__jarvisVideoCaptureTimer = null;
        window.__jarvisVideoCaptureEl = null;
      };
    }

    // --- Hook sull'audio IN RICEZIONE, per sentire davvero l'altra persona --
    // "enableCallInterface" (sotto) risolve l'inizializzazione dello stack
    // VoIP e il microfono in uscita, ma NON basta a garantire che l'audio
    // remoto sia udibile: WhatsApp Web assegna lo stream remoto via
    // `element.srcObject = stream` a un <audio>/<video> nascosto, e i
    // browser Chromium bloccano/mutano la riproduzione di media assegnati
    // via JS senza un gesto utente reale (mai presente qui, essendo
    // headless). Risultato: la chiamata si connette, il microfono funziona,
    // ma l'elemento che dovrebbe riprodurre la voce dell'altra persona resta
    // muto o in pausa. Intercettiamo l'assegnazione di ogni srcObject e
    // forziamo muted=false, volume massimo e play(), riprovando al primo
    // 'canplay' se play() viene rifiutato subito (stream non ancora pronto).
    if (!window.__jarvisRemoteAudioHooked) {
      window.__jarvisRemoteAudioHooked = true;
      const proto = HTMLMediaElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "srcObject");
      if (desc && desc.set) {
        Object.defineProperty(proto, "srcObject", {
          configurable: true,
          enumerable: desc.enumerable,
          get: desc.get,
          set: function (stream) {
            desc.set.call(this, stream);
            const hasAudio = stream && typeof stream.getAudioTracks === "function" && stream.getAudioTracks().length > 0;
            const hasVideo = stream && typeof stream.getVideoTracks === "function" && stream.getVideoTracks().length > 0;
            // FIX "non si apre la UI video dell'altra persona": senza questo
            // controllo, l'hook si agganciava anche all'elemento di
            // auto-anteprima (la TUA webcam, marcata piu' sopra da
            // getUserMedia), che spesso riceve/ri-riceve il suo srcObject
            // prima o insieme a quello dell'altra persona, rubando la
            // cattura frame. Ora catturiamo solo stream NON locali, cioe'
            // quello vero dell'altra persona (invariato per gli elementi
            // solo-audio: stesso identico comportamento di prima).
            if (hasVideo && !stream.__jarvisLocalStream && typeof window.__jarvisStartVideoCapture === "function") {
              window.__jarvisStartVideoCapture(this);
            }
            if (!hasAudio) return;
            this.muted = false;
            this.volume = 1.0;
            const forcePlay = () => { this.muted = false; this.play().catch(() => {}); };
            const p = this.play();
            if (p && typeof p.catch === "function") {
              p.catch(() => {
                this.addEventListener("canplay", forcePlay, { once: true });
              });
            }
          },
        });
      } else {
        window.__jarvisCallEvent({ type: "error", message: "Hook audio remoto non installato: HTMLMediaElement.srcObject non intercettabile in questo browser." });
      }
    }

    // --- Inizializza davvero lo stack VoIP nativo --------------------------
    // WPP.call.offer() chiama enableCallInterface() da sola per le chiamate
    // in uscita, ma per le chiamate IN ARRIVO nessuno lo fa mai: senza questa
    // chiamata i flag A/B che abilitano le chiamate web possono restare
    // spenti e lo stack VoIP nativo (WAWebVoipInit) puo' non inizializzarsi,
    // il che spiega sia il "non sento l'altra persona" sia le chiamate che
    // arrivano senza alcun avviso. La chiamiamo qui, all'avvio dell'hook,
    // cosi' e' gia' pronta per qualunque chiamata (in arrivo o in uscita).
    try {
      await WPP.call.enableCallInterface();
    } catch (e) {
      window.__jarvisCallEvent({ type: "error", message: "enableCallInterface fallita: " + (e && e.message ? e.message : String(e)) });
    }

    // --- Fix "non si toglie l'UI quando l'altra persona riattacca" --------
    // wa-js in questa versione registra internamente un SOLO evento di
    // chiamata reale ("call.incoming_call": vedi WA-JS:call:registerIncomingCallEvent
    // nel bundle, che intercetta CallStore.processIncomingCall/setActiveCall
    // solo per notificare le chiamate IN ARRIVO). Non esiste alcun
    // "call.call_ended" realmente emesso: per questo il listener piu' sotto
    // (tenuto per compatibilita' con eventuali build future) non scattava mai
    // e la HUD restava a schermo dopo che l'altra persona chiudeva la
    // chiamata. CallStore.activeCall pero' passa SEMPRE da
    // CallStore.setActiveCall(...) per essere impostato O azzerato (e' l'unico
    // punto d'ingresso, sia per una chiamata in arrivo che per la sua fine):
    // agganciandoci direttamente a quella funzione invece che a un evento
    // inesistente, intercettiamo anche la chiusura decisa dall'ALTRA parte,
    // non solo i nostri reject()/end().
    if (
      typeof WPP !== "undefined" &&
      WPP.whatsapp &&
      WPP.whatsapp.CallStore &&
      typeof WPP.whatsapp.CallStore.setActiveCall === "function" &&
      !window.__jarvisCallEndHooked
    ) {
      window.__jarvisCallEndHooked = true;
      const callStore = WPP.whatsapp.CallStore;
      const origSetActiveCall = callStore.setActiveCall.bind(callStore);
      callStore.setActiveCall = function (call, ...rest) {
        const result = origSetActiveCall(call, ...rest);
        if (!call) {
          window.__jarvisCallEvent({ type: "call_ended" });
        }
        return result;
      };
    }

    WPP.on("call.incoming_call", async (call) => {
      // Risolvi il nome del chiamante: l'evento nativo passa solo il Wid
      // (numero), mai un nome. WPP.contact.get() e' la stessa funzione usata
      // da resolveTarget() piu' sotto in questo file per l'operazione inversa.
      let callerName = null;
      try {
        const contactId = call.sender || call.peerJid;
        if (contactId && WPP.contact && typeof WPP.contact.get === "function") {
          const contact = await WPP.contact.get(contactId);
          callerName = (contact && (contact.name || contact.pushname || contact.formattedName)) || null;
        }
      } catch (_) { /* nome non risolvibile, andra' avanti solo col numero */ }

      window.__jarvisCallEvent({
        type: "incoming_call",
        call: {
          id: call.id,
          isGroup: !!call.isGroup,
          isVideo: !!call.isVideo,
          offerTime: call.offerTime,
          sender: call.sender ? call.sender.toString() : null,
          peerJid: call.peerJid ? call.peerJid.toString() : null,
          callerName,
        },
      });
    });
    // Alcuni build di wa-js emettono anche eventi di fine/rifiuto/accettazione
    // remota; ci mettiamo in ascolto in modo tollerante (try/catch, non
    // tutte le versioni li espongono con lo stesso nome).
    try {
      WPP.on("call.call_ended", (call) => {
        window.__jarvisCallEvent({ type: "call_ended", call });
      });
    } catch (_) {}
  });

  console.log("[call-bridge] Hook su WPP.call installato (enableCallInterface + hook microfono attivi).");
}

function handleCallEvent(evt) {
  if (evt.type === "error") {
    state.error = evt.message;
    console.error("[call-bridge]", evt.message);
    broadcast(evt);
    return;
  }
  if (evt.type === "incoming_call") {
    state.activeCall = {
      id: evt.call && evt.call.id,
      from: evt.call && (evt.call.sender || evt.call.peerJid),
      callerName: (evt.call && evt.call.callerName) || null,
      isGroup: !!(evt.call && evt.call.isGroup),
      isVideo: !!(evt.call && evt.call.isVideo),
      startedAt: Date.now(),
      status: "ringing",
    };
    console.log("[call-bridge] Chiamata in arrivo:", state.activeCall);
    broadcast({ type: "incoming_call", call: state.activeCall });
  } else if (evt.type === "call_ended") {
    console.log("[call-bridge] Chiamata terminata.");
    state.activeCall = null;
    state.muted = false;
    state.lastVideoFrame = null;
    try {
      const page = client && (client.page || client.waPage);
      if (page) {
        page.evaluate(() => {
          if (typeof window.__jarvisStopVideoCapture === "function") window.__jarvisStopVideoCapture();
        }).catch(() => {});
      }
    } catch (_) { /* non bloccante: la cattura si fermera' comunque al prossimo srcObject */ }
    broadcast({ type: "call_ended" });
  }
}

// Risolve un target (numero o nome contatto) in un jid WhatsApp valido.
// Se sembra un numero -> jid diretto. Altrimenti cerca tra le chat aperte
// via wa-js (fuzzy semplice, case-insensitive substring) e ritorna i
// candidati cosi' il chiamante puo' scegliere se ce n'e' piu' di uno.
async function resolveTarget(target) {
  const digitsOnly = target.replace(/[^0-9]/g, "");
  const looksLikeNumber = digitsOnly.length >= 8 && digitsOnly.length === target.replace(/[\s+]/g, "").length;
  if (looksLikeNumber) {
    // Nessun nome disponibile qui: il chiamante (Python) ha passato un
    // numero grezzo. Il nome, se il numero e' salvato in rubrica, viene
    // recuperato a parte da _resolveContactName() dopo l'aggancio della
    // chiamata (vedi /call/start).
    return { exact: `${digitsOnly}@c.us`, candidates: [], name: null };
  }

  const page = client.page || client.waPage;
  const needle = target.trim().toLowerCase();
  const matches = await page.evaluate(async (q) => {
    const out = [];
    const seen = new Set();

    async function safeList(fn) {
      if (typeof fn !== "function") return [];
      try {
        const r = await fn();          // await e' un no-op se r non e' una Promise
        return Array.isArray(r) ? r : [];
      } catch (e) {
        return [];
      }
    }

    if (typeof WPP !== "undefined" && WPP.chat) {
      const chats = await safeList(WPP.chat.list);
      for (const c of chats) {
        const name = (c.name || c.formattedTitle || "").toLowerCase();
        const id = c.id && c.id._serialized ? c.id._serialized : String(c.id);
        if (name.includes(q) && !seen.has(id)) {
          seen.add(id);
          out.push({ id, name: c.name || c.formattedTitle || "" });
        }
      }
    }

    // Fallback: chat.list() copre solo le chat gia' aperte/caricate. Se non
    // troviamo nulla li', proviamo anche la rubrica contatti completa.
    if (out.length === 0 && typeof WPP !== "undefined" && WPP.contact) {
      const contacts = await safeList(WPP.contact.list);
      for (const c of contacts) {
        const name = (c.name || c.pushname || c.formattedName || "").toLowerCase();
        const id = c.id && c.id._serialized ? c.id._serialized : String(c.id);
        if (name.includes(q) && !seen.has(id)) {
          seen.add(id);
          out.push({ id, name: c.name || c.pushname || c.formattedName || "" });
        }
      }
    }

    return out.slice(0, 5);
  }, needle);

  if (matches.length === 1) return { exact: matches[0].id, candidates: matches, name: matches[0].name || null };
  return { exact: null, candidates: matches, name: null };
}

// Nome del contatto a partire dal jid, per quando resolveTarget() non ne ha
// gia' uno (es. chiamata avviata con un numero grezzo). Stessa funzione
// (WPP.contact.get) gia' usata per risolvere il nome del chiamante nelle
// chiamate IN ARRIVO (vedi WPP.on("call.incoming_call", ...) qui sopra):
// usarla anche qui e' quello che fa comparire il nome nella HUD invece
// dell'id/numero grezzo anche per le chiamate avviate da JARVIS.
async function _resolveContactName(jid) {
  if (!jid) return null;
  try {
    const page = client.page || client.waPage;
    return await page.evaluate(async (id) => {
      try {
        if (typeof WPP === "undefined" || !WPP.contact || typeof WPP.contact.get !== "function") return null;
        const contact = await WPP.contact.get(id);
        return (contact && (contact.name || contact.pushname || contact.formattedName)) || null;
      } catch (_) {
        return null;
      }
    }, jid);
  } catch (_) {
    return null;
  }
}

// Prova diverse firme per WPP.call.offer, dato che non e' confermata al
// 100% quale sia esatta nella tua versione di wa-js. Verifica con
// Object.keys(WPP.call) + ispezione della funzione in DevTools se fallisce.
async function _tryOfferCall(jid, isVideo) {
  const page = client.page || client.waPage;
  return page.evaluate(async (id, video) => {
    if (typeof WPP === "undefined" || !WPP.call || typeof WPP.call.offer !== "function") {
      return { ok: false, error: "WPP.call.offer non disponibile in questa versione/sessione." };
    }
    const attempts = [
      () => WPP.call.offer(id, video),
      () => WPP.call.offer({ chatId: id, isVideo: video }),
      () => WPP.call.offer(id, { isVideo: video }),
    ];
    let lastErr = null;
    for (const attempt of attempts) {
      try {
        await attempt();  // await e' un no-op se il ritorno non e' una Promise
        return { ok: true };
      } catch (e) {
        lastErr = e && e.message ? e.message : String(e);
      }
    }
    return { ok: false, error: lastErr || "Nessuna firma nota di WPP.call.offer ha funzionato su questa versione." };
  }, jid, !!isVideo);
}


// Chiude la chiamata ATTUALMENTE ATTIVA (gia' accettata) usando l'unica API
// reale per farlo: WPP.call.end(). Non prende un callId - agisce sempre su
// CallStore.activeCall, esattamente come una chiamata "riattacca" deve fare.
async function _tryEndCall() {
  const page = client.page || client.waPage;
  return page.evaluate(async () => {
    if (typeof WPP === "undefined" || !WPP.call || typeof WPP.call.end !== "function") {
      return { ok: false, error: "WPP.call.end non disponibile in questa versione/sessione." };
    }
    try {
      await WPP.call.end();
      return { ok: true, used: "end" };
    } catch (e) {
      return { ok: false, error: e && e.message ? e.message : String(e) };
    }
  });
}

// cambiata tra versioni di wa-js. Ritorna il nome che ha funzionato o lancia.
async function _tryCallMethod(candidateNames, callId) {
  const page = client.page || client.waPage;
  const result = await page.evaluate(async (names, id) => {
    for (const name of names) {
      const fn = WPP.call && WPP.call[name];
      if (typeof fn === "function") {
        try {
          await fn(id);  // await e' un no-op se il ritorno non e' una Promise
          return { ok: true, used: name };
        } catch (e) {
          // prova il prossimo candidato
        }
      }
    }
    return { ok: false, tried: names, available: WPP.call ? Object.keys(WPP.call) : [] };
  }, candidateNames, callId);
  return result;
}

// --- HTTP API ----------------------------------------------------------------

const app = express();
app.use(express.json());

app.get("/status", async (_req, res) => {
  // Il muto REALE (vedi _tryGetRealMuted) ha sempre la precedenza sul flag
  // state.muted quando e' disponibile (chiamata attiva con track catturate):
  // e' quello che risolve "JARVIS non si accorge che il microfono e' stato
  // riattivato da solo". Se e' ignoto (null, es. nessuna chiamata in corso)
  // si ripiega sul vecchio flag, come prima.
  const realMuted = state.ready ? await _tryGetRealMuted() : null;
  if (realMuted !== null) state.muted = realMuted;
  res.json({
    ready: state.ready,
    qr: state.qr,
    error: state.error,
    activeCall: state.activeCall,
    muted: state.muted,
  });
});

app.get("/events", (req, res) => {
  res.set({
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    Connection: "keep-alive",
  });
  res.flushHeaders();
  sseClients.add(res);
  req.on("close", () => sseClients.delete(res));
});

// Avvia una chiamata in uscita. Body: { to: "nome o numero", isVideo?: bool }
app.post("/call/start", async (req, res) => {
  const to = (req.body && req.body.to || "").trim();
  const isVideo = !!(req.body && req.body.isVideo);
  if (!to) return res.status(400).json({ ok: false, error: "Parametro 'to' mancante." });

  console.log(`[call-bridge] /call/start richiesto: to="${to}" isVideo=${isVideo}`);

  try {
    const resolved = await resolveTarget(to);
    console.log(`[call-bridge] resolveTarget("${to}") ->`, JSON.stringify(resolved));
    if (!resolved.exact) {
      return res.status(409).json({
        ok: false,
        error: resolved.candidates.length
          ? "Piu' contatti corrispondono, specifica meglio."
          : "Nessun contatto trovato con quel nome.",
        candidates: resolved.candidates,
      });
    }

    const result = await _tryOfferCall(resolved.exact, isVideo);
    console.log(`[call-bridge] _tryOfferCall("${resolved.exact}") ->`, JSON.stringify(result));
    if (result.ok) {
      // Nome per la HUD: prima qui finiva sempre e solo il jid risolto
      // (es. "393331234567@c.us"), mai un nome, perche' la HUD leggeva "to"
      // come fallback. Ora risolviamo un nome vero, con la stessa priorita'
      // usata per le chiamate in arrivo, cosi' compare il contatto "come
      // quando chiamo io" invece del numero grezzo.
      let callerName = resolved.name || null;
      if (!callerName) callerName = await _resolveContactName(resolved.exact);
      if (!callerName) callerName = to; // ultima spiaggia: quello che era stato chiesto

      state.activeCall = {
        id: null, // WPP.call.offer non sempre ritorna un id chiamata utilizzabile subito
        to: resolved.exact,
        callerName,
        isVideo,
        direction: "outgoing",
        startedAt: Date.now(),
        status: "dialing",
      };
      broadcast({ type: "call_started", call: state.activeCall });
    }
    res.json(result);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.post("/call/accept", async (req, res) => {
  if (!state.activeCall) return res.status(409).json({ ok: false, error: "Nessuna chiamata in arrivo." });
  try {
    const result = await _tryCallMethod(["accept", "acceptCall"], state.activeCall.id);
    if (result.ok) {
      state.activeCall.status = "accepted";
      broadcast({ type: "call_accepted", call: state.activeCall });
    }
    res.json(result);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.post("/call/reject", async (req, res) => {
  if (!state.activeCall) return res.status(409).json({ ok: false, error: "Nessuna chiamata in arrivo." });
  try {
    const result = await _tryCallMethod(["reject", "rejectCall"], state.activeCall.id);
    if (result.ok) {
      broadcast({ type: "call_rejected", call: state.activeCall });
      state.activeCall = null;
    }
    res.json(result);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.post("/call/hangup", async (req, res) => {
  if (!state.activeCall) return res.status(409).json({ ok: false, error: "Nessuna chiamata attiva." });
  try {
    // WPP.call non espone hangup/hangUp/endCall (mai esistiti in questa API):
    // e' per questo che "riattacca" non ha mai funzionato. La chiamata giusta
    // dipende dallo stato: se sta ancora squillando (non accettata) va
    // rifiutata con reject(), altrimenti va chiusa con end() - reject() su
    // una chiamata gia' accettata lancia "call_is_not_incoming_ring".
    const result = state.activeCall.status === "ringing"
      ? await _tryCallMethod(["reject"], state.activeCall.id)
      : await _tryEndCall();
    if (result.ok) {
      broadcast({ type: "call_ended" });
      state.activeCall = null;
      state.muted = false;
    }
    res.json(result);
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// Mute/unmute: NON esiste una WPP.call.mute/unmute (verificato). Il tasto
// muto agisce direttamente sulla vera MediaStreamTrack del microfono
// catturata da getUserMedia (hook installato in hookNativeCallStack): e'
// l'unico modo per silenziare davvero l'audio che parte, non solo un flag
// che nessuno leggeva.
// Stato REALE del microfono (vedi window.__jarvisGetMicMuted in
// hookNativeCallStack): usato da /status per non affidarsi solo al flag
// state.muted, che riflette l'ultimo comando ricevuto e puo' disallinearsi
// se una nuova track audio viene catturata a meta' chiamata. Ritorna
// true/false quando c'e' una chiamata con audio attivo, null se ignoto
// (nessuna chiamata / mic non ancora catturato: in quel caso /status ripiega
// sul vecchio flag state.muted).
async function _tryGetRealMuted() {
  try {
    if (!client) return null;
    const page = client.page || client.waPage;
    if (!page) return null;
    return await page.evaluate(() => {
      if (typeof window.__jarvisGetMicMuted !== "function") return null;
      return window.__jarvisGetMicMuted();
    });
  } catch (e) {
    return null;
  }
}

async function _trySetMuted(muted) {
  const page = client.page || client.waPage;
  return page.evaluate((m) => {
    if (typeof window.__jarvisSetMuted !== "function") {
      return { ok: false, error: "Hook microfono non ancora installato (nessuna chiamata attiva?)." };
    }
    const affected = window.__jarvisSetMuted(m);
    return { ok: true, affected };
  }, muted);
}

// Ultimo frame video REMOTO come JPEG binario (non JSON: la UI Python lo usa
// direttamente come immagine). 204 se non c'e' nessuna videochiamata attiva o
// nessun frame ancora catturato: la UI lo interpreta come "nessuna anteprima".
app.get("/call/video-frame", (_req, res) => {
  if (!state.lastVideoFrame) return res.status(204).end();
  const match = /^data:image\/jpeg;base64,(.+)$/.exec(state.lastVideoFrame);
  if (!match) return res.status(204).end();
  res.set("Content-Type", "image/jpeg");
  res.set("Cache-Control", "no-store");
  res.send(Buffer.from(match[1], "base64"));
});

app.post("/call/mute", async (_req, res) => {
  try {
    const result = await _trySetMuted(true);
    state.muted = true;
    broadcast({ type: "muted" });
    res.json({ ok: true, muted: true, ...result });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.post("/call/unmute", async (_req, res) => {
  try {
    const result = await _trySetMuted(false);
    state.muted = false;
    broadcast({ type: "unmuted" });
    res.json({ ok: true, muted: false, ...result });
  } catch (e) {
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.listen(CALL_PORT, CALL_HOST, () => {
  console.log(`[call-bridge] HTTP API su http://${CALL_HOST}:${CALL_PORT}`);
});

start().catch((e) => {
  state.error = String(e && e.message ? e.message : e);
  console.error("[call-bridge] Errore avvio:", e);
});
