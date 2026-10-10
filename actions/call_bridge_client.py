# actions/call_bridge_client.py
# =============================================================================
# Client HTTP per wa-bridge/call-bridge (sessione WPPConnect dedicata alle
# chiamate native WhatsApp Web). Stesso stile di actions/whatsapp_bridge.py:
# base URL da env, richieste brevi con timeout, nessuna eccezione che risale
# fino al chiamante vocale (ritorna sempre dict con "ok").
#
# Requisiti: il processo wa-bridge/call-bridge/call_bridge.js deve essere
# avviato separatamente (npm start), coi device audio virtuali gia'
# impostati come default di sistema (vedi scripts/).
# =============================================================================

from __future__ import annotations
import os
from pathlib import Path
from typing import Any

import requests

try:
    from dotenv import load_dotenv  # type: ignore
    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.is_file():
        load_dotenv(_ENV_PATH, override=False)
except Exception:
    pass

CALL_BASE = os.environ.get("CALL_BRIDGE_URL", "http://127.0.0.1:8767")
_TIMEOUT = 5.0


def _get(path: str) -> dict[str, Any]:
    try:
        r = requests.get(f"{CALL_BASE}{path}", timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _post(path: str) -> dict[str, Any]:
    try:
        r = requests.post(f"{CALL_BASE}{path}", timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def call_status() -> dict[str, Any]:
    """{ready, qr, error, activeCall, muted}"""
    return _get("/status")


def call_start(to: str, is_video: bool = False) -> dict[str, Any]:
    """Avvia una chiamata in uscita nativa WhatsApp verso 'to' (nome o numero)."""
    try:
        r = requests.post(f"{CALL_BASE}/call/start", json={"to": to, "isVideo": is_video}, timeout=_TIMEOUT)
        return r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def call_accept() -> dict[str, Any]:
    return _post("/call/accept")


def call_reject() -> dict[str, Any]:
    return _post("/call/reject")


def call_hangup() -> dict[str, Any]:
    return _post("/call/hangup")


def call_mute() -> dict[str, Any]:
    return _post("/call/mute")


def call_unmute() -> dict[str, Any]:
    return _post("/call/unmute")


def call_video_frame() -> bytes | None:
    """Ultimo frame JPEG del video REMOTO (None se non c'e' una videochiamata
    attiva o nessun frame e' ancora stato catturato). Nessuna eccezione risale
    al chiamante, stesso stile del resto del modulo."""
    try:
        r = requests.get(f"{CALL_BASE}/call/video-frame", timeout=_TIMEOUT)
        if r.status_code == 204 or not r.content:
            return None
        r.raise_for_status()
        return r.content
    except Exception:
        return None


def is_bridge_reachable() -> bool:
    status = call_status()
    if not status.get("ready"):
        return False
    return not status.get("error")
