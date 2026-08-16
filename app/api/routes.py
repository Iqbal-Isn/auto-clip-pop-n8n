"""API route handlers — transcript, single cut, batch cut, gaming compilation."""

from fastapi import APIRouter
from youtube_transcript_api import YouTubeTranscriptApi

from app.models.requests import ClipRequest, BatchClipRequest, GamingCompilationRequest
from app.utils.helpers import seconds_to_hhmmss, extract_video_id, parse_time_range
from app.services.pipeline import cut_video_task, _cut_video_impl, process_gaming_compilation

router = APIRouter()


# ─────────────────────────────────────────
# TRANSCRIPT
# ─────────────────────────────────────────

@router.get("/transcript")
async def get_transcript(url: str, range: str = ""):
    """
    Ambil transkrip YouTube.
    Opsional filter range: 'HH:MM:SS-HH:MM:SS' — hanya snippet yang overlap
    rentang yang dikembalikan. Timestamp output tetap ABSOLUTE (bukan relatif)
    agar nilai start/end langsung bisa dipakai endpoint /cut.
    """
    try:
        video_id = extract_video_id(url)
        if not video_id:
            return {"error": "Video ID tidak ditemukan"}

        start_sec, end_sec = parse_time_range(range)

        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=['id', 'en'])

        formatted_lines = []
        for snippet in fetched:
            es = snippet.start
            ee = snippet.start + snippet.duration
            if start_sec is not None and (ee < start_sec or es > end_sec):
                continue
            timestamp = seconds_to_hhmmss(es)
            formatted_lines.append(f"[{timestamp}] {snippet.text}")

        return {"transcript": "\n".join(formatted_lines)}

    except Exception as e:
        return {"error": f"Gagal total: {str(e)}"}


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

@router.post("/cut")
async def cut_video(request: ClipRequest):
    result = cut_video_task(request.url, request.start, request.end, request.mode)
    return result


@router.post("/cut-batch")
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


@router.post("/cut-gaming-compilation")
async def cut_gaming_compilation(request: GamingCompilationRequest):
    """
    Endpoint kompilasi gaming: N klip → 1 video dengan transisi.
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
      "file": "gaming_compilation_20260805_143025.mp4",
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
