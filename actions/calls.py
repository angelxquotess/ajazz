# actions/calls.py
# Avvia una chiamata su WhatsApp / Telegram / Discord.
#
# Strategia:
#   1) Controlla se l'app desktop e' aperta (pygetwindow + nome processo).
#   2) Se NON e' aperta, la apre (deep-link wa.me/tg:// o eseguibile) e
#      attende fino a `app_open_wait_max` secondi che la finestra compaia.
#   3) Quando l'app e' aperta:
#       - WhatsApp Desktop: deep-link "whatsapp://call?phone=+39..." se e'
#         un numero, altrimenti pywinauto -> cerca contatto -> Ctrl+Shift+R
#       - Telegram Desktop: deep-link "tg://resolve?domain=username&voicechat=on"
#         oppure pywinauto sulla chat -> Ctrl+0 per chiamare
#       - Discord: comando voice via web/desktop solo se l'utente e' gia'
#         loggato; per i DM si apre "discord://" e si invia Ctrl+'
#
# Comando vocale: "Jarvis chiama Mario su WhatsApp"
#                 "Jarvis avvia una chiamata a +393331234567"

from __future__ import annotations
import os
import time
import subprocess
import shutil
from typing import Optional

try:
    from actions.call_bridge_client import call_start as _bridge_call_start, is_bridge_reachable as _bridge_up
except Exception:
    _bridge_call_start = None
    def _bridge_up() -> bool:  # type: ignore
        return False

try:
    from actions.whatsapp_settings import resolve_shortcut
except Exception:
    def resolve_shortcut(name: str) -> str:  # type: ignore
        return name


# ---- Finestra app -----------------------------------------------------

def _is_window_open(title_keyword: str) -> Optional[object]:
    try:
        import pygetwindow as gw  # type: ignore
        for w in gw.getAllWindows():
            t = (getattr(w, "title", "") or "").lower()
            if title_keyword in t and w.visible:
                return w
    except Exception:
        pass
    return None


def _bring_to_front(win) -> None:
    try:
        if getattr(win, "isMinimized", False):
            win.restore()
        win.activate()
    except Exception:
        try:
            win.minimize(); time.sleep(0.2); win.restore()
        except Exception:
            pass


def _wait_window(title_keyword: str, timeout: float = 12.0) -> Optional[object]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        w = _is_window_open(title_keyword)
        if w:
            return w
        time.sleep(0.5)
    return None


# ---- Helpers di apertura app ------------------------------------------

def _open_url(url: str) -> bool:
    try:
        if os.name == "nt":
            os.startfile(url)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", url])
        return True
    except Exception:
        return False


# ---- WhatsApp ---------------------------------------------------------

def _call_whatsapp(target: str) -> tuple[bool, str]:
    is_number = target.lstrip("+").replace(" ", "").isdigit()
    win = _is_window_open("whatsapp")
    if win is None:
        # apri WhatsApp Desktop
        if is_number:
            _open_url(f"whatsapp://send?phone={target.lstrip('+')}")
        else:
            _open_url("whatsapp://")
        win = _wait_window("whatsapp", timeout=15.0)
        if win is None:
            return False, "WhatsApp non si e' aperto in tempo, signore."
        time.sleep(2.0)  # margine ulteriore dopo l'apertura
    _bring_to_front(win)
    time.sleep(0.8)
    try:
        import pyautogui  # type: ignore
        if is_number:
            # apertura chat tramite deep-link gia' fatta; bastera' Ctrl+Shift+R
            pyautogui.hotkey("ctrl", "shift", "r")
            return True, f"Chiamata WhatsApp avviata verso {target}, signore."
        # ricerca contatto -> invio -> tasto chiamata
        pyautogui.hotkey("ctrl", "f"); time.sleep(0.4)
        pyautogui.typewrite(target, interval=0.03); time.sleep(0.8)
        pyautogui.press("enter"); time.sleep(0.8)
        pyautogui.hotkey("ctrl", "shift", "r")  # voice call
        return True, f"Chiamata WhatsApp avviata verso {target}, signore."
    except Exception as e:
        return False, f"Errore avvio chiamata WhatsApp: {e}"


# ---- Telegram ---------------------------------------------------------

def _call_telegram(target: str) -> tuple[bool, str]:
    win = _is_window_open("telegram")
    if win is None:
        # deep link: se ha @ all'inizio assumiamo username
        uname = target.lstrip("@")
        _open_url(f"tg://resolve?domain={uname}")
        win = _wait_window("telegram", timeout=15.0)
        if win is None:
            return False, "Telegram non si e' aperto in tempo, signore."
        time.sleep(2.0)
    _bring_to_front(win)
    time.sleep(0.8)
    try:
        import pyautogui  # type: ignore
        # cerca contatto
        pyautogui.hotkey("ctrl", "k"); time.sleep(0.4)
        pyautogui.typewrite(target.lstrip("@"), interval=0.03); time.sleep(0.8)
        pyautogui.press("enter"); time.sleep(0.8)
        pyautogui.hotkey("ctrl", "0")  # shortcut: voice call su Telegram Desktop
        return True, f"Chiamata Telegram avviata verso {target}, signore."
    except Exception as e:
        return False, f"Errore avvio chiamata Telegram: {e}"


# ---- Discord ----------------------------------------------------------

def _call_discord(target: str) -> tuple[bool, str]:
    win = _is_window_open("discord")
    if win is None:
        _open_url("discord://")
        win = _wait_window("discord", timeout=15.0)
        if win is None:
            return False, "Discord non si e' aperto in tempo, signore."
        time.sleep(2.0)
    _bring_to_front(win)
    time.sleep(0.8)
    try:
        import pyautogui  # type: ignore
        pyautogui.hotkey("ctrl", "k"); time.sleep(0.4)
        pyautogui.typewrite(target, interval=0.03); time.sleep(0.8)
        pyautogui.press("enter"); time.sleep(0.8)
        # Ctrl+' avvia chiamata DM su Discord Desktop
        pyautogui.hotkey("ctrl", "'")
        return True, f"Chiamata Discord avviata verso {target}, signore."
    except Exception as e:
        return False, f"Errore avvio chiamata Discord: {e}"


# ---- Dispatcher pubblico ---------------------------------------------

def start_call(parameters: dict | None = None, response=None,
               player=None, session_memory=None) -> str:
    """Tool 'start_call'. Param: platform (whatsapp/telegram/discord),
    receiver (nome contatto, @username o numero +39...)."""
    params = parameters or {}
    plat = (params.get("platform") or "").strip().lower()
    target = (params.get("receiver") or params.get("to") or "").strip()
    if not target:
        return "Chi devo chiamare, signore?"
    if not plat:
        # default a WhatsApp se sembra un numero
        plat = "whatsapp" if target.lstrip("+").replace(" ", "").isdigit() else "whatsapp"
    if plat == "whatsapp":
        # Espandi le scorciatoie utente (stesse di actions/send_message.py,
        # config/whatsapp_settings.json), es. "babbo" -> "Enzo". Prima si
        # applicavano solo ai messaggi: qui coprono anche le chiamate, sia
        # via call-bridge nativo che via fallback pyautogui.
        resolved_name = resolve_shortcut(target)
        if resolved_name and resolved_name.strip().lower() != target.strip().lower():
            print(f"[Calls] shortcut '{target}' -> '{resolved_name}'")
            if player and hasattr(player, "write_log"):
                try: player.write_log(f"[shortcut] {target} -> {resolved_name}")
                except Exception: pass
            target = resolved_name

        ok, msg = False, ""
        bridge_attempted = False
        if _bridge_call_start is not None and _bridge_up():
            bridge_attempted = True
            is_video = bool(params.get("is_video") or params.get("video"))
            bridge_result = _bridge_call_start(target, is_video=is_video)
            if bridge_result.get("ok"):
                ok, msg = True, f"Chiamata WhatsApp avviata verso {target}, signore."
            elif bridge_result.get("candidates"):
                names = ", ".join(c.get("name", "") for c in bridge_result["candidates"])
                ok, msg = False, f"Piu' contatti corrispondono a '{target}': {names}. Quale, signore?"
            else:
                # Bridge raggiungibile ma la chiamata nativa e' fallita
                # (contatto non trovato, API WPP.call.offer non disponibile,
                # ecc.): riporta il motivo invece di mascherarlo col
                # fallback pyautogui, altrimenti diventa impossibile
                # capire cosa non funziona.
                ok, msg = False, f"Chiamata nativa fallita: {bridge_result.get('error', 'errore sconosciuto')}, signore."
        if not bridge_attempted:
            # Il bridge non e' nemmeno su: qui il fallback ha senso, non sta
            # mascherando nulla.
            ok, msg = _call_whatsapp(target)
    elif plat == "telegram":
        ok, msg = _call_telegram(target)
    elif plat == "discord":
        ok, msg = _call_discord(target)
    else:
        return f"Piattaforma '{plat}' non supportata per le chiamate, signore."
    if player and hasattr(player, "write_log"):
        try: player.write_log("[call] " + msg)
        except Exception: pass
    return msg
