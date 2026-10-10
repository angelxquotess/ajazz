"""
actions/messaging_panel.py
==========================

REWORKED (2026-01) — TENDINA WhatsApp SOLA.
"Apri WhatsApp" apre esclusivamente una piccola **tendina** (floating
window) che mostra WhatsApp Web dentro un QWebEngineView. Le vecchie
tendine Instagram e le pipeline desktop sono state rimosse: WhatsApp
e' l'unico canale supportato ed e' guidato dal wa-bridge / dalla tendina,
senza mai aprire l'app desktop nativa.

* La tendina e' un QWidget frameless, always-on-top, ridimensionabile.
* Include un QWebEngineView che carica web.whatsapp.com
* Sopra al webview mostra un badge con il numero di messaggi non letti.
* Un piccolo poller JS legge il DOM ogni ~4 secondi ed emette un segnale
  quando arriva un nuovo messaggio -> il MessagingPanelManager mostra un
  popup toast "Nuovo messaggio da X - vuoi rispondere?" con bottone Si/No;
  se l'utente clicca Si lancia lo speak "Cosa vuoi rispondere?" e chiama
  la callback di reply (che poi passa da actions/send_message).

Il manager e' un singleton — la stessa istanza viene riutilizzata per
tutte le chiamate.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from PyQt6.QtCore import (
    Qt, QTimer, QUrl, pyqtSignal, QPoint, QObject, QMetaObject, Q_ARG,
)
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
    QSizeGrip,
)

try:
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWebEngineCore import (
        QWebEngineScript, QWebEngineProfile, QWebEnginePage,
        QWebEngineSettings, QWebEngineUrlRequestInterceptor,
    )
    _WEB_OK = True
except Exception:  # pragma: no cover
    QWebEngineView = None  # type: ignore
    _WEB_OK = False


# ----------------------------------------------------------------------
# Telemetry blocker: WhatsApp Web fa chiamate ripetute a
# dit.whatsapp.net/deidentified_telemetry che spesso finiscono in loop
# CORS -> ~30% CPU inutile. Le blocchiamo a livello di URL interceptor.
# ----------------------------------------------------------------------
if _WEB_OK:
    class _WhatsAppTelemetryBlocker(QWebEngineUrlRequestInterceptor):
        _BLOCK_SUBSTR = (
            "dit.whatsapp.net",
            "deidentified_telemetry",
            "/telemetry",
        )

        def interceptRequest(self, info):  # noqa: N802
            try:
                url = info.requestUrl().toString()
                if any(s in url for s in self._BLOCK_SUBSTR):
                    info.block(True)
            except Exception:
                pass

    _GLOBAL_INTERCEPTOR = _WhatsAppTelemetryBlocker()
else:
    _GLOBAL_INTERCEPTOR = None

# Forza il tema chiaro dentro il webview: WhatsApp Web sceglie dark/light
# in base a `prefers-color-scheme`, che QtWebEngine eredita dal tema
# scuro dell'app JARVIS. In dark mode il QR ha meno contrasto e alcune
# fotocamere non lo leggono bene -> sovrascriviamo matchMedia cosi'
# WhatsApp Web crede sempre di essere in light mode.
_FORCE_LIGHT_JS = """
(() => {
  try {
    const realMatchMedia = window.matchMedia.bind(window);
    window.matchMedia = function(query) {
      if (typeof query === 'string' && query.includes('prefers-color-scheme: dark')) {
        return {
          matches: false, media: query,
          onchange: null,
          addListener() {}, removeListener() {},
          addEventListener() {}, removeEventListener() {},
          dispatchEvent() { return false; },
        };
      }
      return realMatchMedia(query);
    };
  } catch (e) {}
})();
"""


# Profili QtWebEngine dedicati e NON persistenti, uno per piattaforma.
#
# Il bug: usando il default profile (persistente su disco), WhatsApp Web
# scrive la sua preferenza tema in localStorage/IndexedDB dentro quel
# profilo. Anche forzando prefers-color-scheme via JS, una volta che
# WhatsApp ha imparato "dark" la riapplica ad ogni avvio leggendo lo
# storage salvato, ignorando il flag. Creando un QWebEngineProfile senza
# nome (off-the-record) non si scrive nulla su disco: ogni riavvio di
# JARVIS riparte da un profilo pulito, e _force_light_theme torna a fare
# effetto in modo affidabile.
#
# Contropartita: essendo non persistente, il login WhatsApp/Instagram non
# sopravvive al riavvio dell'app (serve ri-scansionare il QR ogni volta
# che JARVIS riparte). Durante la stessa sessione il profilo resta in
# memoria e viene riusato (vedi _panels in _Manager), quindi riaprire la
# tendina non richiede un nuovo login finche' l'app resta aperta.
_WEB_PROFILES: dict[str, "QWebEngineProfile"] = {}


def _get_clean_profile(platform: str) -> "QWebEngineProfile":
    """Ritorna il profilo off-the-record dedicato alla piattaforma, creandolo
    se necessario. Non persiste nulla su disco tra un riavvio e l'altro."""
    profile = _WEB_PROFILES.get(platform)
    if profile is None:
        # Nessun 'name' passato al costruttore -> profilo off-the-record,
        # non persistente (Qt non gli assegna una storagePath su disco).
        profile = QWebEngineProfile()
        _WEB_PROFILES[platform] = profile
    return profile


def _force_light_theme(view: "QWebEngineView") -> None:
    """Inietta lo script anti-dark-mode nel profilo del webview."""
    try:
        script = QWebEngineScript()
        script.setName("jarvis_force_light_theme")
        script.setSourceCode(_FORCE_LIGHT_JS)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        view.page().profile().scripts().insert(script)
    except Exception as e:
        print(f"[messaging_panel] impossibile forzare tema chiaro: {e}")


_PLATFORMS = {
    "whatsapp": {
        "url": "https://web.whatsapp.com/",
        "title": "WhatsApp",
        "color": "#25D366",
        # DOM heuristic: WA renders unread badges as spans with aria-label
        # like "3 messaggi non letti"
        "js_unread": (
            "(() => {"
            "  try {"
            "    const badges = document.querySelectorAll('[aria-label*=\"non lett\"], [aria-label*=\"unread\"]');"
            "    let n = 0; let last = '';"
            "    badges.forEach(b => {"
            "      const m = (b.getAttribute('aria-label')||'').match(/\\d+/);"
            "      if (m) n += parseInt(m[0], 10);"
            "      const row = b.closest('[role=\"listitem\"]');"
            "      if (row && !last) {"
            "        const t = row.querySelector('span[dir=\"auto\"][title]');"
            "        if (t) last = t.getAttribute('title')||'';"
            "      }"
            "    });"
            "    return {count: n, sender: last};"
            "  } catch(e) { return {count: 0, sender: '', err: String(e)}; }"
            "})()"
        ),
    },
}


# ----------------------------------------------------------------------
# Reply-confirmation toast — small popup on top of the panel
# ----------------------------------------------------------------------
class _ReplyToast(QWidget):
    reply_yes = pyqtSignal()
    reply_no  = pyqtSignal()

    def __init__(self, sender: str, body: str, color: str,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.Tool
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(320)

        card = QWidget(self)
        card.setStyleSheet(f"""
            background: #0b0f12;
            border: 1px solid {color};
            border-radius: 8px;
        """)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        title = QLabel(f"Nuovo messaggio — {sender or '—'}")
        title.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {color}; background: transparent;")
        lay.addWidget(title)

        if body:
            b = QLabel(body[:180])
            b.setWordWrap(True)
            b.setFont(QFont("Courier New", 8))
            b.setStyleSheet("color: #cfe; background: transparent;")
            lay.addWidget(b)

        q = QLabel("Vuoi rispondere?")
        q.setFont(QFont("Courier New", 9))
        q.setStyleSheet("color: #7fd; background: transparent;")
        lay.addWidget(q)

        btns = QHBoxLayout(); btns.setSpacing(6)
        for text, sig, bg in (("Sì", self.reply_yes, color), ("No", self.reply_no, "#333")):
            b = QPushButton(text)
            b.setFixedHeight(26)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{
                    background: {bg}; color: #001;
                    border: none; border-radius: 4px;
                    font-family: 'Courier New'; font-weight: bold;
                    padding: 0 12px;
                }}
                QPushButton:hover {{ opacity: 0.85; }}
            """)
            b.clicked.connect(sig.emit)
            b.clicked.connect(self.close)
            btns.addWidget(b)
        lay.addLayout(btns)

        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)


# ----------------------------------------------------------------------
# The floating messaging panel itself
# ----------------------------------------------------------------------
class MessagingPanel(QWidget):
    """One floating tendina per platform (whatsapp/instagram)."""

    new_message = pyqtSignal(str, str, str)  # platform, sender, body

    def __init__(self, platform: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self._platform = platform
        cfg = _PLATFORMS[platform]
        self._color = cfg["color"]
        self._js = cfg["js_unread"]
        self._last_unread = 0
        self._last_sender = ""
        self.setWindowTitle(f"JARVIS — {cfg['title']}")
        self.resize(430, 620)
        self.setStyleSheet(f"background: #05070a; border: 1px solid {self._color};")

        # ---- header bar ----
        hdr = QWidget(self); hdr.setFixedHeight(34)
        hdr.setStyleSheet(f"background: #0a1014; border-bottom: 1px solid {self._color};")
        hlay = QHBoxLayout(hdr); hlay.setContentsMargins(10, 0, 6, 0); hlay.setSpacing(8)

        title_lbl = QLabel(f"◈  {cfg['title'].upper()}")
        title_lbl.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {self._color}; background: transparent; border: none;")
        hlay.addWidget(title_lbl)

        self._badge = QLabel("0")
        self._badge.setFixedHeight(20)
        self._badge.setStyleSheet(f"""
            color: #001; background: {self._color};
            border-radius: 10px; padding: 0 8px;
            font-family: 'Courier New'; font-weight: bold; font-size: 10px;
        """)
        self._badge.hide()
        hlay.addWidget(self._badge)
        hlay.addStretch()

        close_btn = QPushButton("×")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton { color: #9df; background: transparent; border: none;
                          font-family: 'Courier New'; font-weight: bold; font-size: 16px; }
            QPushButton:hover { color: #fff; }
        """)
        close_btn.clicked.connect(self.hide)
        hlay.addWidget(close_btn)

        # dragging state
        self._drag_pos: Optional[QPoint] = None

        # ---- web view / fallback ----
        if _WEB_OK:
            self._web = QWebEngineView(self)
            # Profilo dedicato e non persistente (niente storage su disco):
            # vedi commento su _get_clean_profile per il perche'.
            profile = _get_clean_profile(platform)
            # Installa l'interceptor di telemetria (idempotente).
            try:
                if _GLOBAL_INTERCEPTOR is not None:
                    profile.setUrlRequestInterceptor(_GLOBAL_INTERCEPTOR)
            except Exception:
                pass
            page = QWebEnginePage(profile, self._web)
            self._web.setPage(page)

            # ---- Fix CPU: disabilita WebGL / canvas hw ----
            # WhatsApp Web non ha bisogno di WebGL. Lasciarlo abilitato causa
            # "Too many active WebGL contexts" quando il pannello viene
            # nascosto/mostrato e satura la CPU.
            try:
                s = self._web.settings()
                s.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, False)
                s.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, False)
                s.setAttribute(QWebEngineSettings.WebAttribute.PluginsEnabled, False)
                s.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadIconsForPage, False)
            except Exception:
                pass

            _DESKTOP_UA = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            self._web.page().profile().setHttpUserAgent(_DESKTOP_UA)
            _force_light_theme(self._web)
            self._web.load(QUrl(cfg["url"]))
        else:
            self._web = QLabel(
                "PyQt6-WebEngine non installato.\n"
                "Esegui: pip install PyQt6-WebEngine",
                self,
            )
            self._web.setStyleSheet("color: #f88; padding: 20px;")
            self._web.setAlignment(Qt.AlignmentFlag.AlignCenter)

        grip = QSizeGrip(self)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        root.addWidget(hdr)
        root.addWidget(self._web, stretch=1)
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0); row.addStretch(); row.addWidget(grip)
        root.addLayout(row)

        # ---- poller ----
        # Fix CPU: era 4s (troppo aggressivo). 15s e' sufficiente perche'
        # in parallelo il bridge Node ci push-a i /unread via HTTP.
        self._poll_tmr = QTimer(self)
        self._poll_tmr.timeout.connect(self._poll_unread)
        self._poll_tmr.start(15000)

    # ---- lifecycle: sospendi rendering quando nascosto (fix CPU) --------
    def hideEvent(self, ev):  # noqa: N802
        try:
            self._poll_tmr.stop()
        except Exception:
            pass
        try:
            if _WEB_OK and hasattr(self._web, "page"):
                page = self._web.page()
                if hasattr(page, "setLifecycleState") and hasattr(page, "LifecycleState"):
                    page.setLifecycleState(page.LifecycleState.Frozen)
        except Exception:
            pass
        super().hideEvent(ev)

    def showEvent(self, ev):  # noqa: N802
        try:
            if _WEB_OK and hasattr(self._web, "page"):
                page = self._web.page()
                if hasattr(page, "setLifecycleState") and hasattr(page, "LifecycleState"):
                    page.setLifecycleState(page.LifecycleState.Active)
        except Exception:
            pass
        try:
            self._poll_tmr.start(15000)
        except Exception:
            pass
        super().showEvent(ev)

    # ---- drag from header ----
    def mousePressEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton and ev.position().y() < 34:
            self._drag_pos = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
            ev.accept()

    def mouseMoveEvent(self, ev):  # noqa: N802
        if self._drag_pos is not None:
            self.move(ev.globalPosition().toPoint() - self._drag_pos)
            ev.accept()

    def mouseReleaseEvent(self, ev):  # noqa: N802
        self._drag_pos = None
        ev.accept()

    # ---- unread poll ----
    def _poll_unread(self) -> None:
        if not _WEB_OK:
            return
        try:
            self._web.page().runJavaScript(self._js, self._on_unread_result)
        except Exception:
            pass

    def _on_unread_result(self, r) -> None:
        if not isinstance(r, dict):
            return
        n = int(r.get("count", 0) or 0)
        sender = (r.get("sender") or "").strip()
        if n > 0:
            self._badge.setText(str(n))
            self._badge.show()
        else:
            self._badge.hide()
        if n > self._last_unread and sender:
            # ↑ new unread appeared → emit signal (managed by manager)
            self.new_message.emit(self._platform, sender, "")
        self._last_unread = n
        self._last_sender = sender

    # ---- helpers ----
    def announce_unread(self, speak: Optional[Callable[[str], None]] = None) -> str:
        """Return a spoken-friendly sentence describing current unread state."""
        n = self._last_unread
        plat = _PLATFORMS[self._platform]["title"]
        if n <= 0:
            msg = f"Nessun messaggio non letto su {plat}, signore."
        elif n == 1:
            who = f" da {self._last_sender}" if self._last_sender else ""
            msg = f"Signore, hai un messaggio non letto su {plat}{who}."
        else:
            msg = f"Signore, hai {n} messaggi non letti su {plat}."
        if speak:
            try:
                speak(msg)
            except Exception:
                pass
        return msg


# ----------------------------------------------------------------------
# Singleton manager
# ----------------------------------------------------------------------
class _Manager:
    def __init__(self) -> None:
        self._panels: dict[str, MessagingPanel] = {}
        self._toasts: list[_ReplyToast] = []
        self._speak: Optional[Callable[[str], None]] = None
        self._reply_cb: Optional[Callable[[str, str], None]] = None  # (platform, sender)

    def configure(self, speak=None, reply_cb=None) -> None:
        if speak is not None:
            self._speak = speak
        if reply_cb is not None:
            self._reply_cb = reply_cb

    def open(self, platform: str) -> str:
        platform = platform.lower().strip()
        if platform not in _PLATFORMS:
            return f"Piattaforma non supportata: {platform}"
        if QApplication.instance() is None:
            return "GUI non ancora avviata."
        panel = self._panels.get(platform)
        if panel is None:
            panel = MessagingPanel(platform)
            panel.new_message.connect(self._on_new)
            self._panels[platform] = panel
            # position: right of primary screen
            scr = QApplication.primaryScreen().availableGeometry()
            panel.move(scr.right() - panel.width() - 40,
                       scr.top() + 60)
        panel.show(); panel.raise_(); panel.activateWindow()

        # announce unread after webview has (probably) loaded
        QTimer.singleShot(6500, lambda: panel.announce_unread(self._speak))
        return f"Tendina {platform} aperta."

    def close(self, platform: str) -> None:
        p = self._panels.get(platform)
        if p is not None:
            p.hide()

    def notify_external(self, platform: str, sender: str, body: str = "") -> None:
        """Called by check_messages when a new message arrives via poller."""
        self._on_new(platform, sender, body)

    def _on_new(self, platform: str, sender: str, body: str = "") -> None:
        if platform not in _PLATFORMS:
            return
        color = _PLATFORMS[platform]["color"]
        toast = _ReplyToast(sender or "?", body or "", color)
        # position at bottom-right
        scr = QApplication.primaryScreen().availableGeometry()
        toast.adjustSize()
        toast.move(scr.right() - toast.width() - 30,
                   scr.bottom() - toast.height() - 60)

        def _yes():
            if self._speak:
                try:
                    self._speak(f"Va bene signore, cosa vuoi rispondere a {sender}?")
                except Exception:
                    pass
            if self._reply_cb:
                try:
                    self._reply_cb(platform, sender)
                except Exception:
                    pass

        toast.reply_yes.connect(_yes)
        toast.show()
        self._toasts.append(toast)
        # auto-dismiss after 20 s
        QTimer.singleShot(20000, toast.close)


_MANAGER = _Manager()


# ----------------------------------------------------------------------
# Main-thread bridge (Qt).
# `open_messaging_panel` may be called from any thread (Gemini tool
# executor lives in an asyncio worker). PyQt widgets must be created on
# the QApplication thread, so we route the call via a QObject that
# lives on the main thread and connects with QueuedConnection.
# ----------------------------------------------------------------------
class _MainThreadBridge(QObject):
    _instance: "Optional[_MainThreadBridge]" = None
    open_requested = pyqtSignal(str)
    close_requested = pyqtSignal(str)
    notify_requested = pyqtSignal(str, str, str)  # platform, sender, body

    def __init__(self) -> None:
        super().__init__()
        app = QApplication.instance()
        if app is not None:
            self.moveToThread(app.thread())
        self.open_requested.connect(self._do_open, Qt.ConnectionType.QueuedConnection)
        self.close_requested.connect(self._do_close, Qt.ConnectionType.QueuedConnection)
        self.notify_requested.connect(self._do_notify, Qt.ConnectionType.QueuedConnection)

    @classmethod
    def instance(cls) -> "_MainThreadBridge":
        if cls._instance is None:
            cls._instance = _MainThreadBridge()
        return cls._instance

    def _do_open(self, platform: str) -> None:
        try:
            _MANAGER.open(platform)
        except Exception as e:
            print(f"[messaging_panel] open err: {e}")

    def _do_close(self, platform: str) -> None:
        try:
            _MANAGER.close(platform)
        except Exception:
            pass

    def _do_notify(self, platform: str, sender: str, body: str) -> None:
        try:
            _MANAGER.notify_external(platform, sender, body)
        except Exception:
            pass


def get_manager() -> "_Manager":
    return _MANAGER


# ---------- Threading helpers ----------
def open_messaging_panel(platform: str) -> str:
    """Thread-safe: schedule the panel opening on the Qt main thread."""
    app = QApplication.instance()
    if app is None:
        return "GUI non ancora avviata."
    try:
        bridge = _MainThreadBridge.instance()
        bridge.open_requested.emit(platform)
    except Exception as e:
        print(f"[messaging_panel] bridge err: {e}")
        return f"Errore apertura tendina {platform}: {e}"
    return f"Tendina {platform} in apertura, signore."


def close_messaging_panel(platform: str) -> None:
    if QApplication.instance() is None:
        return
    try:
        _MainThreadBridge.instance().close_requested.emit(platform)
    except Exception:
        pass


def notify_new_message(platform: str, sender: str, body: str = "") -> None:
    """Thread-safe: schedule the reply-toast on the Qt main thread."""
    if QApplication.instance() is None:
        return
    try:
        _MainThreadBridge.instance().notify_requested.emit(platform, sender, body or "")
    except Exception:
        pass
