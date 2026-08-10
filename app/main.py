"""
bridge-clipping — FastAPI app untuk auto-clip YouTube + subtitle + filter TikTok/gaming.

Struktur modul:
  app/config.py                  → paths, Whisper lazy-load, ffmpeg/yt-dlp discovery
  app/models/requests.py         → Pydantic request models
  app/utils/helpers.py           → time conversion, video_id extraction
  app/services/subtitles.py      → SRT, ASS karaoke, Whisper transcribe
  app/services/filters.py        → facecam crop, TikTok layout, gaming filter
  app/services/transitions.py    → xfade concat, compress to target
  app/services/face_detector.py  → YuNet DNN face detection
  app/services/pipeline.py       → orkestrasi download, single-clip, gaming compilation
"""

from fastapi import FastAPI
from app.api.routes import router

app = FastAPI()
app.include_router(router)
