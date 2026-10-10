# actions/whatsapp_bridge.py
# =============================================================================
# REWORKED (2026-01) — JARVIS fix v2:
#   * Aggiunto resolve_contact_via_bridge(name, top_n=3, threshold=65) che
#     chiama /resolveContact e ritorna la lista dei candidati con score.
#   * send_via_bridge ora ritorna anche "suggestions" quando il bridge Node
#     non ha trovato il contatto: cosi' send_message.py puo' chiedere
#     conferma vocale con i top-3 senza una seconda chiamata.
#   * Fuzzy matching lato client come safety-net (rapidfuzz opzionale).
# =============================================================================

from __future__ import annotations
import os
import threading
import time
import unicodedata
from pathlib import Path
from typing import Callable, Any

import requests

try:
    from dotenv import load_dotenv  # type: ignore
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(_ENV_PATH, override=False)
except Exception:
    pass

try:
    # Import lazy per evitare cicli
    from actions.whatsapp_settings import get_read_archived  # type: ignore
except Exception:  # fallback in caso di import problem
    def get_read_archived() -> bool:  # type: ignore
        return False


WA_BASE = os.environ.get("WHATSAPP_BRIDGE_URL", "http://127.0.0.1:8765")

# rapidfuzz e' opzionale: se non installato usiamo fallback client-side
# semplice (il grosso del fuzzy comunque lo fa il bridge Node).
try:
    from rapidfuzz import fuzz as _rf_fuzz  # type: ignore
    _HAS_RF = True
except Exception:
    _HAS_RF = False


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    return s


def _score(a: str, b: str) -> int:
    if _HAS_RF:
        try:
            return int(_rf_fuzz.token_set_ratio(_norm(a), _norm(b)))
        except Exception:
            pass
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0
    if na == nb:
        return 100
    if na in nb or nb in na:
        return 88
    # Levenshtein-normalizzato inline (evita dipendenza)
    la, lb = len(na), len(nb)
    prev = list(range(lb + 1))
    for i in range(la):
        cur = [i + 1] + [0] * lb
        for j in range(lb):
            cost = 0 if na[i] == nb[j] else 1
            cur[j + 1] = min(cur[j] + 1, prev[j + 1] + 1, prev[j] + cost)
        prev = cur
    dist = prev[lb]
    return int(round((1 - dist / max(la, lb)) * 100))


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------
def status_bridge() -> dict:
    try:
        r = requests.get(f"{WA_BASE}/status", timeout=4)
        if r.ok:
            return r.json() or {}
    except Exception as e:
        return {"ready": False, "online": False, "error": str(e)}
    return {"ready": False, "online": False, "error": "bridge unreachable"}


# ---------------------------------------------------------------------------
# Contatti / fuzzy resolve
# ---------------------------------------------------------------------------
def list_chats_via_bridge() -> list[dict]:
    """Ritorna la lista chat (id, name, isGroup). Se l'utente ha abilitato
    ``read_archived`` nelle impostazioni WhatsApp, chiede al bridge di
    includere anche le chat archiviate."""
    try:
        params = {}
        if get_read_archived():
            params["includeArchived"] = "1"
        r = requests.get(f"{WA_BASE}/chats", params=params, timeout=8)
        if not r.ok:
            return []
        return list(r.json().get("chats") or [])
    except Exception:
        return []


def resolve_contact_via_bridge(name: str, top_n: int = 3,
                               threshold: int = 65) -> dict:
    """Chiede al bridge Node di risolvere ``name`` in top-N candidati.

    Ritorna: ``{"exact": {name,id,score}|None, "matches": [{name,id,score}]}``
    Se il bridge non risponde, esegue un fallback client-side su /chats.

    NB: la ricerca del contatto include SEMPRE anche le chat archiviate,
    indipendentemente dal toggle ``read_archived``. Quel toggle riguarda
    solo la LETTURA/notifica dei messaggi in arrivo dagli archiviati, non
    la capacita' di trovare un contatto (che potrebbe avere la chat
    archiviata) a cui mandare o rispondere un messaggio.
    """
    empty = {"exact": None, "matches": []}
    if not name:
        return empty
    try:
        params = {"name": name, "topN": top_n, "threshold": threshold,
                  "includeArchived": "1"}
        r = requests.get(
            f"{WA_BASE}/resolveContact",
            params=params,
            timeout=8,
        )
        if r.ok:
            data = r.json() or {}
            return {
                "exact":   data.get("exact"),
                "matches": data.get("matches") or [],
            }
    except Exception:
        pass

    # Fallback client-side su /chats (qui invece rispetta il toggle,
    # perche' list_chats_via_bridge() e' usata anche per la lettura)
    try:
        chats = list_chats_via_bridge()
        scored = sorted(
            (
                {"name": c.get("name", ""), "id": c.get("id", ""),
                 "score": _score(name, c.get("name", ""))}
                for c in chats
            ),
            key=lambda x: x["score"],
            reverse=True,
        )
        good = [s for s in scored if s["score"] >= threshold]
        exact = next(
            (s for s in scored if _norm(s["name"]) == _norm(name)),
            None,
        )
        return {"exact": exact, "matches": good[:top_n]}
    except Exception:
        return empty


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
def send_via_bridge(recipient: str, message: str) -> tuple[bool, str, list]:
    """Invia ``message`` a ``recipient``.

    Ritorna ``(ok, info, suggestions)``. Quando ``ok=False`` per chat
    non trovata, ``suggestions`` contiene i top-3 candidati fuzzy da
    proporre all'utente per conferma vocale.

    NB: la risoluzione del destinatario include SEMPRE anche le chat
    archiviate, indipendentemente dal toggle ``read_archived``: quel
    toggle controlla solo la lettura/notifica dei messaggi in arrivo dagli
    archiviati, non deve impedire di trovare/inviare a un contatto la cui
    chat e' semplicemente archiviata.
    """
    try:
        payload = {"to": recipient, "name": recipient, "text": message,
                   "includeArchived": True}
        r = requests.post(
            f"{WA_BASE}/send",
            json=payload,
            timeout=20,
        )
        try:
            data = r.json()
        except Exception:
            data = {}
        ok = bool(r.ok and (data.get("ok") is True))
        suggestions = list(data.get("suggestions") or [])
        return ok, (r.text if not ok else "ok"), suggestions
    except Exception as e:
        return False, f"ERR: {e}", []


# ---------------------------------------------------------------------------
# Unread queue
# ---------------------------------------------------------------------------
def unread_via_bridge() -> list[dict]:
    try:
        r = requests.get(f"{WA_BASE}/unread", timeout=6)
        if not r.ok:
            return []
        return list(r.json().get("messages") or [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Cronologia
# ---------------------------------------------------------------------------
def last_from_via_bridge(name: str, limit: int = 1) -> list[dict]:
    if not name:
        return []
    try:
        params = {"limit": max(1, int(limit))}
        if get_read_archived():
            params["includeArchived"] = "1"
        r = requests.get(
            f"{WA_BASE}/lastFrom/{requests.utils.quote(name)}",
            params=params,
            timeout=15,
        )
        if not r.ok:
            return []
        return list(r.json().get("messages") or [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Reply
# ---------------------------------------------------------------------------
def reply_to_last_via_bridge(name: str, text: str,
                             msg_id: str = "") -> tuple[bool, str]:
    """Risponde a ``name``. Come send_via_bridge, la ricerca del contatto
    include sempre gli archiviati, a prescindere dal toggle read_archived."""
    try:
        r = requests.post(
            f"{WA_BASE}/reply",
            json={"name": name, "text": text, "quoteMsgId": msg_id or "",
                  "includeArchived": True},
            timeout=20,
        )
        try:
            data = r.json()
        except Exception:
            data = {}
        ok = bool(r.ok and (data.get("ok") is True))
        return ok, r.text
    except Exception as e:
        return False, f"ERR: {e}"


# ---------------------------------------------------------------------------
# Background poller (compat)
# ---------------------------------------------------------------------------
def start_incoming_poller(on_message: Callable[[str, str], Any]) -> None:
    def _loop():
        seen: set[str] = set()
        while True:
            try:
                for m in unread_via_bridge():
                    mid = (m.get("id")
                           or (m.get("from", "") + "|" + (m.get("body", "")[:40])))
                    if mid in seen:
                        continue
                    seen.add(mid)
                    if len(seen) > 500:
                        seen = set(list(seen)[-250:])
                    try:
                        on_message(m.get("from", ""), m.get("body", ""))
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(8)

    threading.Thread(target=_loop, daemon=True).start()
