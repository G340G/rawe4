# ARG Analogue Horror Video Generator

Generates a 40s+ analogue-horror / ARG-style MP4:
- Fictional missing-person bulletin + “unknown entity” safety protocol
- Unrelated weather + traffic interruptions
- VHS overlays (REC, timecode, signal %, tracking tears, scanlines)
- Random glitch bursts + “SIGNAL LOST” cards
- Eerie audio bed + text-to-speech narration
- Jumpscare near the end
- Pulls *fresh* images and text online each run from permissive sources:
  - Wikimedia Commons (licensed images)
  - Wikipedia random summaries (short text)
  - Open-Meteo (weather)

## Quick start (local)

### Requirements
- Python 3.10+
- ffmpeg installed
- Optional for better TTS: `espeak-ng` installed

### Install
```bash
pip install -r requirements.txt
