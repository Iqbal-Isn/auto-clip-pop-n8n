from fastapi import FastAPI, BackgroundTasks
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import subprocess
import uvicorn
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
    tmp_yt     = f"/tmp/yt_clip_{timestamp}.mp4"
    tmp_merged = f"/tmp/merged_{timestamp}.mp4"
    output     = f"/home/iqbal/Downloads/clip/final_tiktok_{timestamp}.mp4"
    video_game_pop = "./pob1.mp4"

    try:
        # Step 1: Download clip YouTube
        print(f"⬇️ Downloading clip {start} - {end}...")
        subprocess.run([
            "yt-dlp",
            "--download-sections", f"*{start}-{end}",
            "-f", "bestvideo[height<=1080]+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", tmp_yt,
            url
        ], check=True)

        # Step 2: Hitung durasi
        duration = float(subprocess.check_output([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            tmp_yt
        ]).decode().strip())
        print(f"⏱ Durasi clip: {duration:.1f} detik")

        # Step 3: Gabung video YT + video game ke file tmp
        print("🎬 Menggabungkan video...")
        subprocess.run([
            "ffmpeg", "-i", tmp_yt,
            "-stream_loop", "-1", "-t", str(duration), "-i", video_game_pop,
            "-filter_complex",
            "[0:v]scale=1080:960:force_original_aspect_ratio=increase,"
            "crop=1080:960,setsar=1[top];"
            "[1:v]scale=1080:960:force_original_aspect_ratio=increase,"
            "crop=1080:960,setsar=1[bottom];"
            "[top][bottom]vstack=inputs=2[v]",
            "-map", "[v]", "-map", "0:a?",
            "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k",
            tmp_merged, "-y"
        ], check=True)

        # Step 4: Cek ukuran hasil merge
        size_mb = os.path.getsize(tmp_merged) / (1024 * 1024)
        print(f"📊 Ukuran setelah merge: {size_mb:.1f}MB")

        if size_mb > 45:
            # Compress langsung ke output final, hapus tmp_merged sesudahnya
            print(f"⚠️ Terlalu besar ({size_mb:.1f}MB), compressing ke <45MB...")
            video_bitrate = max(int((45 * 1024 * 8) / duration) - 96, 300)
            subprocess.run([
                "ffmpeg", "-i", tmp_merged,
                "-b:v", f"{video_bitrate}k",
                "-maxrate", f"{video_bitrate}k",
                "-bufsize", f"{video_bitrate * 2}k",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "96k",
                output, "-y"
            ], check=True)
            os.remove(tmp_merged)  # Hapus tmp_merged setelah compress
        else:
            # Ukuran aman, rename langsung jadi output final
            os.rename(tmp_merged, output)
            print(f"✅ Ukuran aman, tidak perlu compress")

        final_size = os.path.getsize(output) / (1024 * 1024)
        print(f"✅ Selesai! File: {output} ({final_size:.1f}MB)")

    except Exception as e:
        print(f"❌ Gagal: {e}")
    finally:
        # Bersihkan semua file temporary
        for f in [tmp_yt, tmp_merged]:
            if os.path.exists(f):
                os.remove(f)
                print(f"🗑️ Hapus temp: {f}")

    print(f"DONE_PROCESSING:{output}")


@app.post("/cut")
async def cut_video(request: ClipRequest, background_tasks: BackgroundTasks):
    background_tasks.add_task(cut_video_task, request.url, request.start, request.end)
    return {"status": "Processing in background"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)