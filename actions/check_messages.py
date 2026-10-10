# actions/check_messages.py
# =============================================================================
# REWORKED (2026-01):
# Ora l'unico canale supportato e' WhatsApp, esclusivamente tramite wa-bridge
# (whatsapp-web.js headless in background). Nessuna app desktop viene mai
# aperta: tutto avviene mentre l'utente lavora ad altro.
#
# Comandi pubblici:
#   * check_messages(...)              -> "ho messaggi non letti?"
#   * read_last_from(...)              -> "dimmi cosa mi ha detto <nome>"
#   * read_last_notifications(...)     -> "leggimi le ultime N notifiche"
#   * start_notification_pollers(...)  -> avvia SOLO il poller WhatsApp
#
# I vecchi poller Telegram/Discord/Instagram sono stati rimossi: gli helper
# _unread_telegram / _unread_discord / _unread_instagram restano come stub
# vuoti per compatibilita' con vecchie chiamate residue nel codice.
# =============================================================================

from __future__ import annotations
import re
import time
import threading
from typing import Callable

from actions.message_state import (
    set_last_incoming,
    get_recent_notifications,
    get_last_record,
    is_tts_muted,
    enqueue_muted_speech,
)
from actions.whatsapp_bridge import (
    WA_BASE,  # noqa: F401 (usato altrove per compatibilita')
    unread_via_bridge,
    last_from_via_bridge,
    status_bridge,
)
from actions.whatsapp_settings import resolve_shortcut


URGENT_RE = re.compile(r"\b(urgente|emergenza|chiamami|aiuto|help|urgent)\b", re.I)

# Debounce annunci vocali (2s) per stessa combinazione mittente+testo.
_NOTIF_DEBOUNCE_S = 2.0
_last_ann: dict[str, float] = {}


def _debounce_ok(key: str) -> bool:
    now = time.time()
    prev = _last_ann.get(key, 0.0)
    if now - prev < _NOTIF_DEBOUNCE_S:
        return False
    _last_ann[key] = now
    # gc leggero
    if len(_last_ann) > 200:
        cutoff = now - 60
        for k in list(_last_ann.keys()):
            if _last_ann[k] < cutoff:
                _last_ann.pop(k, None)
    return True


def _speak_or_queue(speak: Callable[[str], None], line: str) -> None:
    """Se JARVIS sta parlando all'utente, accoda; altrimenti pronuncia subito.

    L'utente ha scelto "accoda" nella domanda aperta (vedi problem statement).
    """
    if is_tts_muted():
        try:
            enqueue_muted_speech(line)
        except Exception:
            pass
        return
    try:
        speak(line)
    except Exception:
        pass


def _shorten(s: str, n: int = 300) -> str:
    s = (s or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[:n].rstrip() + "..."


# ---------------------------------------------------------------------------
# Notify wrapper
# ---------------------------------------------------------------------------
def _notify(speak: Callable[[str], None],
            on_new_message: Callable | None,
            platform: str, sender: str,
            body: str = "", kind: str = "text",
            audio_url: str = "", audio_path: str = "",
            msg_id: str = "") -> None:
    if not sender:
        return
    try:
        set_last_incoming(platform, sender, body=body, kind=kind,
                          audio_url=audio_url, audio_path=audio_path,
                          msg_id=msg_id)
    except Exception:
        pass

    if on_new_message:
        try:
            on_new_message(platform, sender,
                           body=body, kind=kind,
                           audio_url=audio_url, audio_path=audio_path)
            return
        except TypeError:
            try:
                on_new_message(platform, sender)
                return
            except Exception:
                pass
        except Exception:
            pass

    plat_label = platform.capitalize()
    urgent = bool(body and URGENT_RE.search(body))
    prefix = "ATTENZIONE, " if urgent else ""

    dkey = f"{platform}|{sender}|{(body or '')[:60]}"
    if not _debounce_ok(dkey):
        return

    try:
        if kind == "voice":
            line = (f"{prefix}Signore, {sender} le ha inviato un messaggio "
                    f"vocale su {plat_label}. Vuole che lo riproduca?")
        elif body:
            line = (f"{prefix}Signore, nuovo messaggio da {sender}: {body}")
        else:
            line = f"{prefix}Signore, nuovo messaggio {plat_label} da {sender}"
        _speak_or_queue(speak, line)
        if urgent:
            time.sleep(0.6)
            _speak_or_queue(speak, line)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WhatsApp poller (unico rimasto)
# ---------------------------------------------------------------------------
def _wa_extract(m: dict) -> tuple[str, str, str, str, str]:
    if m.get("fromMe") or m.get("from_me"):
        return ("", "", "", "", "")
    sender = (m.get("notifyName") or m.get("pushname")
              or m.get("from", "") or "qualcuno")
    msg_id = str(m.get("id") or "")
    mtype = (m.get("type") or "").lower()
    body  = m.get("body") or m.get("text") or ""
    audio = ""
    kind  = "text"
    if mtype in ("ptt", "audio") or m.get("isVoice"):
        kind = "voice"
        audio = m.get("mediaUrl") or (f"{WA_BASE}/media/{msg_id}" if msg_id else "")
    elif mtype == "image":
        kind = "image"
    elif mtype == "video":
        kind = "video"
    elif mtype == "sticker":
        kind = "sticker"
    return (sender, body, kind, audio, msg_id)


def _poll_whatsapp(speak, on_new_message) -> None:
    last_ids: set[str] = set()
    first_pass = True
    while True:
        try:
            for m in unread_via_bridge():
                mid = m.get("id") or (m.get("from", "") + "|" + (m.get("body", "")[:40]))
                if mid in last_ids:
                    continue
                last_ids.add(mid)
                if len(last_ids) > 300:
                    last_ids = set(list(last_ids)[-150:])
                if first_pass:
                    continue
                sender, body, kind, audio, msg_id = _wa_extract(m)
                if not sender:
                    continue
                _notify(speak, on_new_message, "whatsapp",
                        sender, body=body, kind=kind,
                        audio_url=audio, msg_id=msg_id)
            first_pass = False
        except Exception:
            pass
        # Il bridge Node fa gia' debounce + push in coda. Un intervallo piu'
        # corto (5s) rende l'annuncio dei nuovi messaggi molto piu' reattivo
        # senza pesare sulla CPU.
        time.sleep(5)


# ---------------------------------------------------------------------------
# Snapshot non-letti (unico canale: WhatsApp via bridge)
# ---------------------------------------------------------------------------
def _unread_whatsapp() -> list[dict]:
    return [
        {"from": m.get("from", ""), "body": m.get("body", "")}
        for m in unread_via_bridge()
    ]


# Stub vuoti per compatibilita' con qualunque vecchia chiamata rimasta
def _unread_telegram()  -> list[dict]: return []
def _unread_discord()   -> list[dict]: return []
def _unread_instagram() -> list[dict]: return []


def gather_unread() -> dict:
    return {"whatsapp": _unread_whatsapp()}


def summarize_unread(data: dict | None = None) -> str:
    data = data or gather_unread()
    items = data.get("whatsapp") or []
    if not items:
        # Distinguo "nessun messaggio" da "bridge non connesso": altrimenti
        # JARVIS dice "nessun messaggio" anche quando in realta' non e'
        # collegato a WhatsApp.
        st = status_bridge()
        if not st.get("ready"):
            return ("Signore, non riesco a leggere i messaggi: WhatsApp non e' "
                    "collegato al bridge. Apri " + WA_BASE + " e scansiona il QR.")
        return "Nessun messaggio non letto, signore."
    n = len(items)
    names = ", ".join((it.get("from", "") or "?") for it in items[:5])
    more = "" if n <= 5 else f", e altri {n - 5}"
    if n == 1:
        return f"Signore, ha un messaggio non letto su WhatsApp, da {names}."
    return f"Signore, ha {n} messaggi non letti su WhatsApp: {names}{more}."


def check_messages(parameters: dict | None = None, response=None,
                   player=None, session_memory=None) -> str:
    """Comando: 'Jarvis ho messaggi non letti?'"""
    text = summarize_unread(gather_unread())
    if player and hasattr(player, "write_log"):
        try:
            player.write_log("[messages] " + text)
        except Exception:
            pass
    return text


# ---------------------------------------------------------------------------
# NUOVO: "Dimmi cosa mi ha detto <nome>"
# ---------------------------------------------------------------------------
def read_last_from(parameters: dict | None = None, response=None,
                   player=None, session_memory=None) -> str:
    """Legge l'ultimo messaggio ricevuto da un contatto specifico su WhatsApp.

    Espone la funzione al motore comandi di JARVIS. Non apre alcuna app
    desktop: recupera il testo direttamente dal bridge in background.

    Parametri accettati (tutti opzionali, primo trovato vince):
        ``contact`` / ``name`` / ``from`` / ``sender`` / ``receiver``
    """
    params = parameters or {}
    name = (
        params.get("contact")
        or params.get("name")
        or params.get("from")
        or params.get("sender")
        or params.get("receiver")
        or ""
    ).strip()
    if not name:
        return "Signore, di chi devo leggere l'ultimo messaggio?"

    # Espandi le scorciatoie utente (es. "babbo" -> "Mario Rossi")
    resolved_name = resolve_shortcut(name)
    if resolved_name and resolved_name.strip().lower() != name.strip().lower():
        name = resolved_name

    msgs = last_from_via_bridge(name, limit=1)
    if not msgs:
        return (f"Non trovo messaggi da {name} su WhatsApp, signore. "
                f"Controlli che il bridge sia connesso.")

    m = msgs[0]
    body = (m.get("body") or "").strip()
    kind = (m.get("type") or "text").lower()

    # Aggiorna l'ultimo record cosi' 'rispondi al messaggio' funziona subito.
    try:
        set_last_incoming(
            "whatsapp", name, body=body,
            kind=("voice" if kind in ("ptt", "audio") else "text"),
            msg_id=str(m.get("id") or ""),
        )
    except Exception:
        pass

    if kind in ("ptt", "audio"):
        text = f"{name} le ha inviato un messaggio vocale, signore."
    elif not body:
        text = f"{name} le ha inviato un allegato di tipo {kind}, signore."
    else:
        text = f'{name} le ha detto, cito: "{_shorten(body, 400)}".'

    if player and hasattr(player, "write_log"):
        try:
            player.write_log("[last-from] " + text)
        except Exception:
            pass
    return text


# ---------------------------------------------------------------------------
# Ultime N notifiche (invariato)
# ---------------------------------------------------------------------------
def read_last_notifications(parameters: dict | None = None, response=None,
                            player=None, session_memory=None) -> str:
    params = parameters or {}
    try:
        n = int(params.get("count") or params.get("n") or 5)
    except Exception:
        n = 5
    items = get_recent_notifications(n)
    if not items:
        return "Nessuna notifica recente, signore."
    lines = []
    for it in items:
        plat = (it.get("platform") or "").capitalize()
        snd  = it.get("sender") or "?"
        knd  = it.get("kind") or "text"
        if knd == "voice":
            lines.append(f"{plat}, {snd}: messaggio vocale.")
        else:
            body = _shorten(it.get("body") or "", 200)
            lines.append(f"{plat}, {snd}: {body}" if body else f"{plat}, {snd}.")
    text = ("Ecco le ultime " + str(len(lines))
            + " notifiche, signore. " + " ".join(lines))
    if player and hasattr(player, "write_log"):
        try:
            player.write_log("[notifiche] " + text)
        except Exception:
            pass
    return text


# ---------------------------------------------------------------------------
# Poller bootstrap (solo WhatsApp)
# ---------------------------------------------------------------------------
_started = False
_lock = threading.Lock()


def start_notification_pollers(
    speak: Callable[[str], None],
    on_new_message: Callable[[str, str], None] | None = None,
) -> None:
    """Avvia il poller WhatsApp in un thread daemon. Idempotente."""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(
        target=_poll_whatsapp,
        args=(speak, on_new_message),
        daemon=True,
    ).start()
