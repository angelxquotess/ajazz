# Routing audio virtuale su Windows (per call-bridge)

Su Windows non esiste un equivalente scriptabile di PulseAudio `null-sink`:
il modo standard e affidabile è **VB-Audio Virtual Cable** (gratuito, o la
versione "VB-Cable A+B" se vuoi due cavi separati mic/speaker come su Linux).

## 1) Installa VB-Cable

Scarica ed installa da vb-audio.com (link ufficiale). Dopo l'installazione
avrai un device `CABLE Input` (altoparlante virtuale) e `CABLE Output`
(microfono virtuale) in Impostazioni Suono di Windows. Se vuoi due coppie
separate (una per il "mic" verso il browser, una per lo "speaker" dal
browser) installa anche VB-Cable A+B per avere `CABLE-A` e `CABLE-B`.

## 2) Imposta i device di default PRIMA di avviare call_bridge.js

Non c'è un comando nativo `pactl`-like da terminale; usa:
- Impostazioni Suono di Windows (imposta manualmente), oppure
- lo strumento a riga di comando `SoundVolumeView` (NirSoft) per farlo da
  script/batch, es.:
  ```
  SoundVolumeView.exe /SetDefault "CABLE Input" 0
  SoundVolumeView.exe /SetDefault "CABLE Output" 1
  ```

## 3) Hook TTS/STT

- **TTS -> mic virtuale**: la tua pipeline TTS deve riprodurre l'audio sul
  device `CABLE Input` (non sugli altoparlanti reali). Con `python`, ad
  esempio con `sounddevice`/`pyaudio`, seleziona esplicitamente quell'indice
  device in output invece del default di sistema.
- **STT <- speaker virtuale**: registra da `CABLE Output` come sorgente di
  input per la tua pipeline STT esistente.

## Nota

Il browser (Chromium via Puppeteer) userà i device di **default di sistema**
al momento dell'avvio — per questo l'ordine conta: imposta i cavi virtuali
come default *prima* di lanciare `npm start` nel call-bridge, altrimenti
Chromium aggancia il mic/speaker fisico reale.
