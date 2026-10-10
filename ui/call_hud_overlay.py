# ui/call_hud_overlay.py
# ---------------------------------------------------------------------------
# CallHudOverlay — tendina per gestire chiamate WhatsApp native (accept /
# reject / mute / hangup) dal call-bridge, con anteprima video dell'altra
# persona per le videochiamate (riquadro nero, nascosto per le chiamate
# solo-audio). Stile calcato su ClipboardPanel in ui.py: stesso schema
# colori (usa il tuo modulo colori C esistente), stesso font Courier New,
# stesso pattern QWidget flottante.
#
# QUESTO FILE E' STANDALONE: importalo in ui.py e istanzialo, vedi
# INTEGRATION.md in questa cartella per il diff esatto da applicare.
# ---------------------------------------------------------------------------

from __future__ import annotations
from typing import Any

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QPixmap
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

# Riusa la tua palette colori esistente (stesso import usato da ClipboardPanel
# in ui.py). Se il modulo si chiama diversamente nel tuo progetto, aggiusta
# questo import.
from ui_colors import C  # noqa: F401  -- adegua al nome reale del tuo modulo colori


class CallHudOverlay(QWidget):
    """Tendina flottante per controllare una chiamata WhatsApp in corso/in arrivo."""

    accept_requested = pyqtSignal()
    reject_requested = pyqtSignal()
    hangup_requested = pyqtSignal()
    mute_toggled = pyqtSignal(bool)  # True = muto

    _W, _H = 300, 140

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CallHudOverlay {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._muted = False
        self._ringing = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 9)
        lay.setSpacing(6)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        self._icon_lbl = QLabel("☎  CHIAMATA IN ARRIVO")
        self._icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(self._icon_lbl); hdr.addStretch()
        lay.addLayout(hdr)

        self._from_lbl = QLabel("")
        self._from_lbl.setFont(QFont("Courier New", 9))
        self._from_lbl.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        lay.addWidget(self._from_lbl)

        # Anteprima video dell'altra persona (solo videochiamate attive).
        # Nascosta di default: resta invisibile per le chiamate solo-audio e
        # finche' non arriva il primo frame da actions/call_hud.py.
        self._video_lbl = QLabel()
        self._video_lbl.setFixedSize(self._W - 20, 160)
        self._video_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._video_lbl.setStyleSheet(
            f"background: #000; border: 1px solid {C.BORDER}; border-radius: 3px;"
        )
        self._video_lbl.hide()
        lay.addWidget(self._video_lbl)

        # Riga 1: rispondi / rifiuta (mostrata solo mentre squilla)
        self._ring_row = QHBoxLayout(); self._ring_row.setSpacing(4)
        accept_btn = QPushButton("RISPONDI")
        accept_btn.setFixedHeight(26)
        accept_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        accept_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        accept_btn.setStyleSheet(
            f"QPushButton {{ background: #0d2e14; color: #4ade80; "
            f"border: 1px solid #1f5c2e; border-radius: 2px; }}"
            f"QPushButton:hover {{ border-color: #4ade80; }}"
        )
        accept_btn.clicked.connect(self.accept_requested.emit)
        self._ring_row.addWidget(accept_btn)

        reject_btn = QPushButton("RIFIUTA")
        reject_btn.setFixedHeight(26)
        reject_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        reject_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        reject_btn.setStyleSheet(
            f"QPushButton {{ background: #2e0d0d; color: #f87171; "
            f"border: 1px solid #5c1f1f; border-radius: 2px; }}"
            f"QPushButton:hover {{ border-color: #f87171; }}"
        )
        reject_btn.clicked.connect(self.reject_requested.emit)
        self._ring_row.addWidget(reject_btn)
        lay.addLayout(self._ring_row)

        # Riga 2: muto / riattacca (mostrata solo a chiamata attiva)
        self._active_row = QHBoxLayout(); self._active_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        self._mute_btn = QPushButton("MUTO")
        self._mute_btn.setFixedHeight(24)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.setStyleSheet(_bs)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._active_row.addWidget(self._mute_btn)

        hangup_btn = QPushButton("RIATTACCA")
        hangup_btn.setFixedHeight(24)
        hangup_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        hangup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        hangup_btn.setStyleSheet(
            f"QPushButton {{ background: #2e0d0d; color: #f87171; "
            f"border: 1px solid #5c1f1f; border-radius: 2px; }}"
            f"QPushButton:hover {{ border-color: #f87171; }}"
        )
        hangup_btn.clicked.connect(self.hangup_requested.emit)
        self._active_row.addWidget(hangup_btn)
        lay.addLayout(self._active_row)

        self.hide()

    def _toggle_mute(self):
        self._muted = not self._muted
        self._mute_btn.setText("RIATTIVA MIC" if self._muted else "MUTO")
        self.mute_toggled.emit(self._muted)

    # --- API chiamata da actions/call_hud.py tramite player.show_call_panel/... ---

    def show_incoming(self, call: dict):
        self._ringing = True
        self._icon_lbl.setText("☎  CHIAMATA IN ARRIVO")
        self._from_lbl.setText(call.get("from") or "Numero sconosciuto")
        self._video_lbl.hide()
        self._video_lbl.clear()
        self._ring_row_widgets_visible(True)
        self.show(); self.raise_()

    def show_active(self, call: dict | None = None):
        self._ringing = False
        self._icon_lbl.setText("☎  CHIAMATA IN CORSO")
        if call:
            self._from_lbl.setText(call.get("from") or "")
        self._ring_row_widgets_visible(False)
        self.show(); self.raise_()

    def set_muted(self, muted: bool):
        self._muted = muted
        self._mute_btn.setText("RIATTIVA MIC" if muted else "MUTO")

    def set_video_frame(self, jpeg_bytes: "bytes | None"):
        """Chiamato da actions/call_hud.py con l'ultimo frame JPEG del video
        remoto (None per nascondere il riquadro, es. a fine chiamata)."""
        if not jpeg_bytes:
            self._video_lbl.hide()
            self._video_lbl.clear()
            return
        pix = QPixmap()
        if not pix.loadFromData(jpeg_bytes, "JPG"):
            return
        pix = pix.scaled(
            self._video_lbl.width(), self._video_lbl.height(),
            Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation,
        )
        self._video_lbl.setPixmap(pix)
        self._video_lbl.show()

    def hide_panel(self):
        self._video_lbl.hide()
        self._video_lbl.clear()
        self.hide()

    def _ring_row_widgets_visible(self, ringing: bool):
        for i in range(self._ring_row.count()):
            w = self._ring_row.itemAt(i).widget()
            if w:
                w.setVisible(ringing)
        for i in range(self._active_row.count()):
            w = self._active_row.itemAt(i).widget()
            if w:
                w.setVisible(not ringing)
