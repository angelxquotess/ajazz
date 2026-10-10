# actions/send_message.py
# =============================================================================
# REWORKED (2026-01) - JARVIS fix v2:
#   Flusso "conferma vocale fuzzy":
#     1. Provo il send diretto sul bridge.
#     2. Se il bridge non trova il contatto ma ritorna suggestions:
#        - se c'e' un match >= 92 -> invio direttamente (silent auto-pick).
#        - altrimenti chiedo vocalmente e memorizzo lo stato in
#          message_state (pending_fuzzy) per la conferma successiva.
#     3. L'utente conferma con "il primo" / "Noemi Rossi" / "annulla":
#        gestito da confirm_fuzzy_send().
# =============================================================================

from __future__ import annotations
import re
import time
import threading

from actions.whatsapp_bridge import (
    send_via_bridge,
    reply_to_last_via_bridge,
    resolve_contact_via_bridge,
    status_bridge,
    WA_BASE,
)
from actions.message_state import (
    get_last_record,
    set_pending_fuzzy,
    get_pending_fuzzy,
    clear_pending_fuzzy,
)
from actions.whatsapp_settings import resolve_shortcut

_FUZZY_STRONG   = 92
_FUZZY_TIMEOUT  = 5.0  # secondi per la risposta vocale


def _log(player, line: str) -> None:
    if player and hasattr(player, "write_log"):
        try:
            player.write_log(line)
        except Exception:
            pass


def _speak(response, text: str) -> None:
    """Wrapper tollerante: usa response.speak se disponibile."""
    if not response or not text:
        return
    try:
        if hasattr(response, "speak"):
            response.speak(text)
        elif callable(response):
            response(text)
    except Exception:
        pass


def _human_options(names: list[str]) -> str:
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} o {names[1]}"
    return ", ".join(names[:-1]) + f", oppure {names[-1]}"


# ----------------------------------------------------------------------------
# send_message
# ----------------------------------------------------------------------------
def send_message(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params       = parameters or {}
    receiver     = (params.get("receiver") or params.get("to") or "").strip()
    message_text = (params.get("message_text") or params.get("text") or "").strip()
    platform     = (params.get("platform") or "whatsapp").strip().lower()

    if not receiver:
        return "Signore, mi indichi il destinatario, per favore."
    if not message_text:
        return "Signore, quale messaggio devo inviare?"

    if platform not in ("whatsapp", "wp", "wapp", "wa"):
        return ("Signore, in questa versione posso inviare messaggi solo "
                "tramite WhatsApp, via il bridge in background.")

    # Espandi le scorciatoie utente (es. "babbo" -> "Mario Rossi")
    resolved_name = resolve_shortcut(receiver)
    if resolved_name and resolved_name.strip().lower() != receiver.strip().lower():
        print(f"[SendMessage] shortcut '{receiver}' -> '{resolved_name}'")
        _log(player, f"[shortcut] {receiver} -> {resolved_name}")
        receiver = resolved_name

    preview = message_text[:60] + ("..." if len(message_text) > 60 else "")
    print(f"[SendMessage] whatsapp(bridge) -> {receiver}: {preview}")
    _log(player, f"[msg] whatsapp -> {receiver}")

    ok, info, suggestions = send_via_bridge(receiver, message_text)
    if ok:
        result = f"Messaggio inviato a {receiver} su WhatsApp, signore."
        _log(player, f"[msg] {result}")
        return result

    # Fallback: prova fuzzy resolve
    if not suggestions:
        resolved = resolve_contact_via_bridge(receiver, top_n=3, threshold=65)
        exact = resolved.get("exact")
        suggestions = resolved.get("matches") or []
        if exact:
            ok2, info2, _s = send_via_bridge(exact["name"], message_text)
            if ok2:
                result = f"Messaggio inviato a {exact['name']} su WhatsApp, signore."
                _log(player, f"[msg] {result}")
                return result

    if suggestions:
        # Se il top score e' molto alto, invio senza chiedere
        top = suggestions[0]
        if int(top.get("score", 0)) >= _FUZZY_STRONG:
            ok2, info2, _s = send_via_bridge(top["name"], message_text)
            if ok2:
                result = (f"Non ho trovato il nome esatto, ho inviato a "
                          f"{top['name']}, signore.")
                _log(player, f"[msg] {result}")
                return result

        # Chiedi conferma vocale (top-3)
        names = [s["name"] for s in suggestions[:3]]
        question = (f"Non ho trovato {receiver} esatto, signore. "
                    f"Intendeva {_human_options(names)}?")
        set_pending_fuzzy({
            "kind":     "send",
            "created":  time.time(),
            "receiver": receiver,
            "text":     message_text,
            "options":  names,
        })
        _speak(response, question)
        _log(player, f"[fuzzy] proposti: {names}")
        return question

    result = (f"Nessun contatto simile a {receiver}, signore. "
              f"Controlli che il bridge sia attivo e la chat esista.")
    _log(player, f"[msg-err] {info}")

    # Diagnostica: se il bridge non e' connesso a WhatsApp, il vero problema
    # non e' il nome del contatto ma la sessione mancante. Diamo un messaggio
    # chiaro con il passo da fare (scansionare il QR del bridge).
    st = status_bridge()
    if not st.get("ready"):
        if not st.get("online", True) or st.get("error"):
            result = (
                "Signore, il bridge WhatsApp non risponde. Assicurati che sia "
                "avviato, poi apri " + WA_BASE + " nel browser."
            )
        else:
            result = (
                "Signore, WhatsApp non e' collegato al bridge. Apri " + WA_BASE +
                " nel browser e scansiona il QR da WhatsApp, Dispositivi "
                "collegati. La tendina che vedi e' una sessione diversa."
            )
        _log(player, "[msg-err] bridge non pronto")
    return result


# ----------------------------------------------------------------------------
# confirm_fuzzy_send: chiamato quando l'utente conferma vocalmente
# ----------------------------------------------------------------------------
_ORD_WORDS = {
    "primo": 0, "prima": 0, "uno": 0, "1": 0, "una": 0,
    "secondo": 1, "seconda": 1, "due": 1, "2": 1,
    "terzo": 2, "terza": 2, "tre": 2, "3": 2,
}


def _pick_from_reply(reply: str, options: list[str]) -> int | None:
    if not reply or not options:
        return None
    r = reply.strip().lower()
    if any(w in r for w in ("annulla", "lascia perdere", "no", "cancel")):
        return -1
    # ordinal
    for w, idx in _ORD_WORDS.items():
        if re.search(rf"\b{re.escape(w)}\b", r):
            if idx < len(options):
                return idx
    # nome esplicito (match parziale)
    for i, name in enumerate(options):
        parts = name.lower().split()
        if any(p and p in r for p in parts):
            return i
    return None


def confirm_fuzzy_send(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Comando: risposta vocale dell'utente al prompt "Intendeva X, Y o Z?"."""
    params = parameters or {}
    reply  = (params.get("reply") or params.get("text")
              or params.get("message_text") or "").strip()

    pending = get_pending_fuzzy()
    if not pending or pending.get("kind") != "send":
        return "Nessuna conferma in sospeso, signore."

    if time.time() - float(pending.get("created", 0)) > _FUZZY_TIMEOUT + 30:
        clear_pending_fuzzy()
        return "Ho lasciato scadere la conferma, signore. Ripeta pure il comando."

    options = pending.get("options") or []
    idx = _pick_from_reply(reply, options)
    if idx == -1:
        clear_pending_fuzzy()
        _speak(response, "Va bene, annullato.")
        return "Invio annullato, signore."
    if idx is None:
        _speak(response, "Non ho capito, signore: mi dica il primo, il secondo o il nome.")
        return "In attesa di conferma."

    picked = options[idx]
    text   = pending.get("text") or ""
    clear_pending_fuzzy()

    ok, info, _s = send_via_bridge(picked, text)
    if ok:
        result = f"Messaggio inviato a {picked} su WhatsApp, signore."
    else:
        result = f"Non sono riuscito a inviare a {picked}, signore."
    _speak(response, result)
    _log(player, f"[fuzzy-confirm] {result}")
    return result


# ----------------------------------------------------------------------------
# reply_message (invariato salvo unpack)
# ----------------------------------------------------------------------------
def reply_message(
    parameters: dict | None = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    text = (params.get("message_text") or params.get("text") or "").strip()

    rec = get_last_record()
    if not rec:
        return "Non ho un messaggio recente a cui rispondere, signore."

    sender = (rec.get("sender") or "").strip()
    plat   = (rec.get("platform") or "").strip().lower()
    if not sender:
        return "Non so a chi rispondere, signore: nessun mittente in memoria."
    if plat and plat != "whatsapp":
        return ("Signore, l'ultimo messaggio non e' arrivato da WhatsApp e "
                "in questa versione posso rispondere solo su WhatsApp.")

    if not text:
        return (f"A cosa devo rispondere a {sender}, signore? Mi detti il "
                f"testo e provvedo tramite il bridge.")

    ok, info = reply_to_last_via_bridge(sender, text, rec.get("msg_id") or "")
    if ok:
        result = f"Risposta inviata a {sender} su WhatsApp, signore."
    else:
        result = (f"Non sono riuscito a rispondere a {sender} su WhatsApp. "
                  f"Bridge non pronto o chat non trovata.")
        _log(player, f"[reply-err] {info}")

    _log(player, f"[reply] {result}")
    return result
