"""
bridge-clipping — FastAPI app untuk auto-clip YouTube + subtitle + filter TikTok/gaming.

Struktur modul:
  config.py      → paths, Whisper lazy-load, ffmpeg/yt-dlp discovery
  models.py      → Pydantic request models
  utils.py       → time conversion, video_id extraction
  subtitles.py   → SRT, ASS karaoke, Whisper transcribe
  filters.py     → facecam crop, TikTok layout, gaming 50/50 PiP
  transitions.py → xfade concat, compress to target
  pipeline.py    → orkestrasi download, single-clip, gaming compilation
"""

from fastapi import FastAPI
from youtube_transcript_api import YouTubeTranscriptApi
import uvicorn

from models import ClipRequest, BatchClipRequest, GamingCompilationRequest
from utils import seconds_to_hhmmss, extract_video_id
from pipeline import cut_video_task, _cut_video_impl, process_gaming_compilation

app = FastAPI()


# ─────────────────────────────────────────
# TRANSCRIPT
# ─────────────────────────────────────────

@app.get("/transcript")
async def get_transcript(url: str):
    try:
        video_id = extract_video_id(url)
        if not video_id:
            return {"error": "Video ID tidak ditemukan"}

        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=['id', 'en'])

        formatted_lines = []
        for snippet in fetched:
            timestamp = seconds_to_hhmmss(snippet.start)
            formatted_lines.append(f"[{timestamp}] {snippet.text}")

        return {"transcript": "\n".join(formatted_lines)}

    except Exception as e:
        return {"error": f"Gagal total: {str(e)}"}


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

@app.post("/cut")
async def cut_video(request: ClipRequest):
    result = cut_video_task(request.url, request.start, request.end, request.mode)
    return result


@app.post("/cut-batch")
async def cut_video_batch(request: BatchClipRequest):
    """
    Endpoint batch: download video SEKALI, potong semua klip.
    Request body:
    {
      "url": "https://youtube.com/watch?v=xxx",
      "clips": [
        {"start": "00:01:30", "end": "00:02:00"},
        {"start": "00:05:10", "end": "00:05:50"}
      ],
      "mode": "normal"
    }
    Response:
    {
      "status": "done",
      "clips": [
        {"file": "final_tiktok_000130_0.mp4", "size_mb": 8.5, "status": "success"},
        {"file": null, "size_mb": 0, "status": "error", "error": "..."}
      ]
    }
    """
    results = _cut_video_impl(request.url, request.clips, request.mode)

    success_count = sum(1 for r in results if r.get("status") == "success")
    fail_count = sum(1 for r in results if r.get("status") == "error")

    return {
        "status": "done",
        "total": len(results),
        "success": success_count,
        "failed": fail_count,
        "clips": results
    }


@app.post("/cut-gaming-compilation")
async def cut_gaming_compilation(request: GamingCompilationRequest):
    """
    Endpoint kompilasi gaming: 5 klip → 1 video dengan transisi.
    Request body:
    {
      "url": "https://youtube.com/watch?v=xxx",
      "clips": [
        {"start": "00:01:30", "end": "00:02:00"},
        ... (5 momen)
      ],
      "facecam_position": "btmleft"
    }
    Response:
    {
      "status": "success",
      "file": "gaming_compilation.mp4",
      "size_mb": 45.2,
      "clips_processed": 5,
      "total_clips": 5,
      "facecam_position": "btmleft"
    }
    """
    try:
        result = process_gaming_compilation(
            request.url, request.clips, request.facecam_position
        )
        return result
    except Exception as e:
        print(f"❌ Gaming compilation GAGAL: {e}")
        return {
            "status": "error",
            "file": None,
            "size_mb": 0,
            "clips_processed": 0,
            "total_clips": len(request.clips) if request.clips else 0,
            "error": str(e)
        }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
