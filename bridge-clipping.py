from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import subprocess
import uvicorn
import re
import os


app = FastAPI()

class ClipRequest(BaseModel):
    url: str
    start: str
    end: str

@app.get("/transcript")
async def get_transcript(url: str):
    try:
        video_id = None
        if "v=" in url:
            video_id = url.split("v=")[1].split("&")[0]
        elif "youtu.be/" in url:
            video_id = url.split("youtu.be/")[1].split("?")[0]
        elif "/live/" in url:
            video_id = url.split("/live/")[1].split("?")[0]

        if not video_id:
            return {"error": "Video ID tidak ditemukan"}

        ytt_api = YouTubeTranscriptApi()
        fetched = ytt_api.fetch(video_id, languages=['id', 'en'])

        # Format dengan timestamp HH:MM:SS agar AI tahu posisi waktu
        def seconds_to_hhmmss(seconds):
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            s = int(seconds % 60)
            return f"{h:02d}:{m:02d}:{s:02d}"

        formatted_lines = []
        for snippet in fetched:
            timestamp = seconds_to_hhmmss(snippet.start)
            formatted_lines.append(f"[{timestamp}] {snippet.text}")

        full_text = "\n".join(formatted_lines)

        return {"transcript": full_text}

    except Exception as e:
        return {"error": f"Gagal total: {str(e)}"}

def cut_video_task(url: str, start: str, end: str):
    timestamp = start.replace(':', '')
    tmp_yt = f"/tmp/yt_clip_{timestamp}.mp4"
    output_final = f"/home/iqbal/Downloads/clip/final_tiktok_{timestamp}.mp4"
    video_game_pop = "./pob1.mp4"

    try:
        # Step 1: Download potongan kecil dulu
        subprocess.run([
            "yt-dlp",
            "--download-sections", f"*{start}-{end}",
            "-f", "bestvideo[height<=1080]+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", tmp_yt,
            url
        ], check=True)

        # Step 2: Hitung durasi YT clip secara otomatis
        probe = subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            tmp_yt
        ]).decode().strip()
        duration = float(probe)
        print(f"⏱ Durasi clip: {duration} detik")

        # Step 3: Gabung — video game dipotong tepat sesuai durasi YT
        ffmpeg_cmd = [
            "ffmpeg", "-i", tmp_yt,
            "-stream_loop", "-1", "-t", str(duration), "-i", video_game_pop,  # 👈 game dibatasi durasi YT
            "-filter_complex",
            "[0:v]scale=1080:960:force_original_aspect_ratio=increase,"
            "crop=1080:960,setsar=1[top];"
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase,"
            "crop=1080:960,setsar=1[bottom];"
            "[top][bottom]vstack=inputs=2[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k",
            output_final, "-y"
        ]
        subprocess.run(ffmpeg_cmd, check=True)
        print(f"✅ Selesai! Cek: {output_final}")

    except Exception as e:
        print(f"❌ Gagal: {e}")
    finally:
        if os.path.exists(tmp_yt):
            os.remove(tmp_yt)
    print(f"DONE_PROCESSING:{output_final}")
@app.post("/cut")
async def cut_video(request: ClipRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(cut_video_task, request.url, request.start, request.end)
    return {"status": "Processing in background"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)