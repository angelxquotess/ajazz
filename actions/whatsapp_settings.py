# actions/whatsapp_settings.py
# =============================================================================
# WhatsApp settings management for JARVIS/DAIDAI.
#
# Persist:
#   * read_archived : bool  -> allow the assistant to look up ARCHIVED chats
#   * shortcuts     : dict  -> mapping alias -> real contact name
#                              es. {"babbo": "Mario Rossi", "capo": "Luca Bianchi"}
#
# Stored as JSON under: config/whatsapp_settings.json
# =============================================================================

from __future__ import annotations
import json
import unicodedata
from pathlib import Path
from typing import Any

_BASE_DIR = Path(__file__).resolve().parent.parent
_CFG_DIR = _BASE_DIR / "config"
_CFG_DIR.mkdir(parents=True, exist_ok=True)
_CFG_FILE = _CFG_DIR / "whatsapp_settings.json"

_DEFAULTS: dict[str, Any] = {
    "read_archived": False,
    "autoplay_voice": True,  # riproduci in background (via wa-bridge) i vocali in arrivo
    "shortcuts": {},   # alias(lowercase) -> contact name
}


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = "".join(
        c for c in unicodedata.normalize("NFD", s)
        if unicodedata.category(c) != "Mn"
    )
    return s


def load_settings() -> dict:
    if not _CFG_FILE.is_file():
        return dict(_DEFAULTS)
    try:
        data = json.loads(_CFG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return dict(_DEFAULTS)
    out = dict(_DEFAULTS)
    out["read_archived"] = bool(data.get("read_archived", False))
    out["autoplay_voice"] = bool(data.get("autoplay_voice", True))
    sc = data.get("shortcuts") or {}
    if isinstance(sc, dict):
        out["shortcuts"] = {
            _norm(str(k)): str(v).strip()
            for k, v in sc.items()
            if str(k).strip() and str(v).strip()
        }
    return out


def save_settings(data: dict) -> None:
    payload = {
        "read_archived": bool(data.get("read_archived", False)),
        "autoplay_voice": bool(data.get("autoplay_voice", True)),
        "shortcuts": {
            _norm(str(k)): str(v).strip()
            for k, v in (data.get("shortcuts") or {}).items()
            if str(k).strip() and str(v).strip()
        },
    }
    _CFG_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                         encoding="utf-8")


def get_read_archived() -> bool:
    return bool(load_settings().get("read_archived", False))


def get_autoplay_voice() -> bool:
    return bool(load_settings().get("autoplay_voice", True))


def set_autoplay_voice(enabled: bool) -> None:
    d = load_settings()
    d["autoplay_voice"] = bool(enabled)
    save_settings(d)


def resolve_shortcut(name: str) -> str:
    """If ``name`` matches a known alias (case/accent-insensitive), return the
    mapped real contact name. Otherwise return ``name`` unchanged."""
    if not name:
        return name
    sc = load_settings().get("shortcuts") or {}
    key = _norm(name)
    return sc.get(key, name)


def list_shortcuts() -> list[tuple[str, str]]:
    sc = load_settings().get("shortcuts") or {}
    return sorted(sc.items(), key=lambda kv: kv[0])


def set_shortcut(alias: str, contact: str) -> None:
    d = load_settings()
    sc = dict(d.get("shortcuts") or {})
    sc[_norm(alias)] = contact.strip()
    d["shortcuts"] = sc
    save_settings(d)


def delete_shortcut(alias: str) -> None:
    d = load_settings()
    sc = dict(d.get("shortcuts") or {})
    sc.pop(_norm(alias), None)
    d["shortcuts"] = sc
    save_settings(d)


def set_read_archived(enabled: bool) -> None:
    d = load_settings()
    d["read_archived"] = bool(enabled)
    save_settings(d)
