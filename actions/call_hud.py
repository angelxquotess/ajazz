# actions/call_hud.py
# ---------------------------------------------------------------------------
# Comandi vocali per il call-bridge ("Jarvis rispondi", "Jarvis rifiuta",
# "Jarvis metti in muto", "Jarvis riattacca") + un poller in background che
# osserva /status del call-bridge e apre la tendina HUD chiamate quando
# arriva una chiamata (delegando il rendering a player.show_call_panel,
# vedi ui/call_hud_overlay.py).
#
# Pattern identico a actions/message_hud.py: questo modulo NON disegna
# nulla, delega tutto alla UI passata come `player`.
# ---------------------------------------------------------------------------

from __future__ import annotations
import threading
import time
from typing import Any, Callable

from actions.call_bridge_client import (
    call_status, call_accept, call_reject, call_hangup, call_mute, call_unmute,
    call_video_frame,
)

try:
    # Stesso meccanismo di debounce/coda usato per le notifiche di nuovi
    # messaggi (actions/check_messages.py), cosi' l'annuncio vocale della
    # chiamata rispetta il muto TTS e non si accavalla se JARVIS sta gia'
    # parlando.
    from actions.message_state import is_tts_muted, enqueue_muted_speech
except Exception:
    def is_tts_muted() -> bool:  # type: ignore
        return False
    def enqueue_muted_speech(_line: str) -> None:  # type: ignore
        pass

_POLL_INTERVAL_S = 1.5
# Poll separato e piu' rapido, SOLO durante una videochiamata gia' accettata
# (vedi CallPoller._ensure_video_poll_running): a 1.5s l'anteprima sarebbe
# uno slideshow, ma non ha senso interrogare /call/video-frame quando non
# c'e' nessun video da mostrare, da qui i due intervalli separati.
_VIDEO_POLL_INTERVAL_S = 0.2


def _speak_or_queue(speak: Callable[[str], None], line: str) -> None:
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


def answer_call(parameters: dict | None = None, response=None,
                 player=None, session_memory=None) -> str:
    result = call_accept()
    if not result.get("ok"):
        return f"Non sono riuscito a rispondere: {result.get('error', 'errore sconosciuto')}, signore."
    if player is not None and hasattr(player, "update_call_panel"):
        try:
            player.update_call_panel(status="accepted")
        except Exception:
            pass
    return "Chiamata accettata, signore."


def reject_call(parameters: dict | None = None, response=None,
                 player=None, session_memory=None) -> str:
    result = call_reject()
    if not result.get("ok"):
        return f"Non sono riuscito a rifiutare la chiamata: {result.get('error', 'errore sconosciuto')}, signore."
    if player is not None and hasattr(player, "hide_call_panel"):
        try:
            player.hide_call_panel()
        except Exception:
            pass
    return "Chiamata rifiutata, signore."


def hangup_call(parameters: dict | None = None, response=None,
                 player=None, session_memory=None) -> str:
    result = call_hangup()
    if not result.get("ok"):
        return f"Non sono riuscito a chiudere la chiamata: {result.get('error', 'errore sconosciuto')}, signore."
    if player is not None and hasattr(player, "hide_call_panel"):
        try:
            player.hide_call_panel()
        except Exception:
            pass
    return "Chiamata terminata, signore."


def mute_call(parameters: dict | None = None, response=None,
              player=None, session_memory=None) -> str:
    result = call_mute()
    if player is not None and hasattr(player, "update_call_panel"):
        try:
            player.update_call_panel(muted=True)
        except Exception:
            pass
    return "Microfono disattivato, signore." if result.get("ok") else "Non sono riuscito a mettere in muto, signore."


def unmute_call(parameters: dict | None = None, response=None,
                 player=None, session_memory=None) -> str:
    result = call_unmute()
    if player is not None and hasattr(player, "update_call_panel"):
        try:
            player.update_call_panel(muted=False)
        except Exception:
            pass
    return "Microfono riattivato, signore." if result.get("ok") else "Non sono riuscito a riattivare il microfono, signore."


# ---------------------------------------------------------------------------
# Poller in background: se non vuoi rispondere a voce mentre suona, questo
# e' quello che fa comparire/scomparire la tendina HUD da solo.
# ---------------------------------------------------------------------------

class CallPoller:
    def __init__(self, player: Any = None, speak: Callable[[str], None] | None = None):
        self._player = player
        self._speak = speak
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_call_id = None
        self._video_thread: threading.Thread | None = None
        self._video_stop = threading.Event()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._stop_video_poll()

    def _run(self):
        while not self._stop.is_set():
            try:
                status = call_status()
                active = status.get("activeCall")
                # NOTA: per le chiamate in uscita call_bridge.js imposta
                # id=None (WPP.call.offer non ritorna sempre un call id
                # utilizzabile subito). Confrontare solo "id" significa che
                # None != None e' sempre False -> la HUD non compariva mai
                # per le chiamate avviate da JARVIS. Usiamo un identificatore
                # che esiste sempre (startedAt) con fallback su id/to.
                active_key = None
                if active:
                    active_key = active.get("id") or active.get("startedAt") or active.get("to")
                if active and active_key != self._last_call_id:
                    self._last_call_id = active_key
                    if self._player is not None and hasattr(self._player, "show_call_panel"):
                        try:
                            self._player.show_call_panel(active)
                        except Exception:
                            pass
                    # Annuncio vocale: prima non veniva mai avvisato a voce di
                    # una chiamata in arrivo, solo la tendina HUD compariva
                    # (facile da non notare se non si guarda lo schermo). Solo
                    # per le chiamate IN ARRIVO (non per quelle avviate da
                    # JARVIS stesso, che hanno direction == "outgoing").
                    if self._speak is not None and active.get("direction") != "outgoing" \
                            and active.get("status") == "ringing":
                        name = active.get("callerName") or active.get("from") or "numero sconosciuto"
                        kind = "videochiamata" if active.get("isVideo") else "chiamata"
                        _speak_or_queue(self._speak, f"Signore, {kind} in arrivo da {name}.")
                elif not active and self._last_call_id is not None:
                    self._last_call_id = None
                    if self._player is not None and hasattr(self._player, "hide_call_panel"):
                        try:
                            self._player.hide_call_panel()
                        except Exception:
                            pass

                # Anteprima video: solo a chiamata gia' accettata e con
                # isVideo=True. In tutti gli altri casi (audio, squillo,
                # nessuna chiamata) ci si assicura che il poll veloce sia
                # spento, cosi' non si interroga /call/video-frame a vuoto.
                if active and active.get("isVideo") and active.get("status") == "accepted":
                    self._ensure_video_poll_running()
                else:
                    self._stop_video_poll()
            except Exception:
                pass
            time.sleep(_POLL_INTERVAL_S)
        self._stop_video_poll()

    def _ensure_video_poll_running(self):
        if self._video_thread and self._video_thread.is_alive():
            return
        self._video_stop.clear()
        self._video_thread = threading.Thread(target=self._video_run, daemon=True)
        self._video_thread.start()

    def _stop_video_poll(self):
        was_running = bool(self._video_thread and self._video_thread.is_alive())
        self._video_stop.set()
        if was_running and self._player is not None and hasattr(self._player, "update_call_video"):
            try:
                self._player.update_call_video(None)
            except Exception:
                pass

    def _video_run(self):
        while not self._video_stop.is_set() and not self._stop.is_set():
            try:
                frame = call_video_frame()
                if frame and self._player is not None and hasattr(self._player, "update_call_video"):
                    try:
                        self._player.update_call_video(frame)
                    except Exception:
                        pass
            except Exception:
                pass
            time.sleep(_VIDEO_POLL_INTERVAL_S)
