#!/usr/bin/env bash
# scripts/setup_virtual_audio_linux.sh
# =============================================================================
# Crea due device audio virtuali PulseAudio per il call-bridge:
#   - jarvis_call_mic     -> quello che il browser "sente" come microfono.
#                            La tua pipeline TTS deve scrivere qui (via paplay
#                            o simile) per far "parlare" JARVIS nella chiamata.
#   - jarvis_call_speaker -> quello che il browser usa come altoparlante.
#                            La tua pipeline STT deve leggere da qui (via
#                            parec sul .monitor) per "sentire" l'altra persona.
#
# USO:
#   chmod +x setup_virtual_audio_linux.sh
#   ./setup_virtual_audio_linux.sh
#
# Poi, PRIMA di avviare call_bridge.js:
#   pactl set-default-source jarvis_call_mic
#   pactl set-default-sink   jarvis_call_speaker
#
# Per la sessione headless serve anche un display virtuale (il vero WebRTC
# del browser non parte in modo affidabile in modalita' completamente
# headless per certe build Chromium/whatsapp-web):
#   Xvfb :99 -screen 0 1280x800x24 &
#   export DISPLAY=:99
#
# NOTE:
#   - Questo script NON persiste dopo reboot: mettilo in un systemd --user
#     unit o richiamalo dal tuo boot_sequence.py se vuoi che parta da solo.
#   - "jarvis_call_mic" e' un sink: per usarlo come "source" (microfono) il
#     browser deve leggere dal suo .monitor, che e' quanto configuriamo con
#     set-default-source sotto (vedi mapping).
# =============================================================================

set -euo pipefail

if ! command -v pactl >/dev/null 2>&1; then
  echo "pactl non trovato: installa pulseaudio-utils (o pipewire-pulse) prima di continuare." >&2
  exit 1
fi

echo "== Rimuovo eventuali sink residui da run precedenti =="
pactl unload-module module-null-sink 2>/dev/null || true

echo "== Creo sink virtuale: microfono JARVIS -> browser (TTS scrive qui) =="
pactl load-module module-null-sink \
  sink_name=jarvis_call_mic \
  sink_properties=device.description=JARVIS_Call_Mic

echo "== Creo sink virtuale: altoparlante browser -> JARVIS (STT legge da qui) =="
pactl load-module module-null-sink \
  sink_name=jarvis_call_speaker \
  sink_properties=device.description=JARVIS_Call_Speaker

echo
echo "Fatto. Device disponibili:"
pactl list short sinks | grep jarvis_call || true

cat <<'EOF'

PROSSIMI PASSI:
  1) Imposta i device come default PRIMA di avviare Chromium/call_bridge.js:
       pactl set-default-source jarvis_call_mic.monitor   # NON esiste come source diretta:
       # in realta' un null-sink NON crea una source con lo stesso nome;
       # per farlo apparire come "microfono" selezionabile dal browser usa
       # il suo .monitor come source:
       pactl set-default-source jarvis_call_mic.monitor

  2) Verifica che il browser lo veda:
       pactl list short sources | grep jarvis_call_mic.monitor

  3) TTS -> scrivi l'audio sintetizzato sul sink "jarvis_call_mic":
       paplay --device=jarvis_call_mic risposta.wav

  4) STT -> registra cio' che arriva dall'altra persona leggendo il monitor
     dello speaker virtuale:
       parec --device=jarvis_call_speaker.monitor --format=s16le --rate=16000 --channels=1 \
         | <pipe verso la tua pipeline STT esistente (voice_io.py)>

Questi due comandi (paplay in scrittura, parec in lettura) sono i due hook
point da collegare alla tua pipeline vocale gia' esistente in JARVIS.
EOF
