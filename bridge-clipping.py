from fastapi import FastAPI
from pydantic import BaseModel
from youtube_transcript_api import YouTubeTranscriptApi
import subprocess
import uvicorn
import os
import sys
import shutil
import tempfile
import platform

app = FastAPI()

IS_WINDOWS = platform.system() == "Windows"
TMP_DIR = tempfile.gettempdir()
OUTPUT_DIR = "/home/node/.n8n-files"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

COOKIES_PATH = "./youtube_cookies.txt"

class ClipRequest(BaseModel):
    url: str
    start: str
    end: str
    mode: str = "normal"  # "normal" atau "gaming"


# ─────────────────────────────────────────
# HELPER
# ─────────────────────────────────────────

def seconds_to_hhmmss(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def extract_video_id(url: str):
    if "v=" in url:
        return url.split("v=")[1].split("&")[0]
    elif "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    elif "/live/" in url:
        return url.split("/live/")[1].split("?")[0]
    return None

def _find_exe(name, common_dirs=None):
    """Cari executable — cek PATH dulu, lalu folder umum (Windows)."""
    # 1. Cek PATH
    found = shutil.which(name)
    if found:
        return found
    # 2. Cek folder umum
    for d in (common_dirs or []):
        exe = os.path.join(d, f"{name}.exe")
        if os.path.exists(exe):
            return exe
    # 3. Fallback — return name saja, biarkan subprocess coba PATH
    return name


# Cari ffmpeg/ffprobe sekali saja saat startup
_FFMPEG_BIN_DIRS = []
if IS_WINDOWS:
    import glob as _glob
    _winget_pkgs = os.path.join(os.path.expanduser("~"), "AppData", "Local", "Microsoft", "WinGet", "Packages")
    for _d in _glob.glob(os.path.join(_winget_pkgs, "Gyan.FFmpeg_*", "ffmpeg-*", "bin")):
        _FFMPEG_BIN_DIRS.append(_d)
    _FFMPEG_BIN_DIRS.extend([
        os.path.join(os.environ.get("ProgramFiles", "C:\\Program Files"), "FFmpeg", "bin"),
        "C:\\ffmpeg\\bin",
    ])

FFMPEG_EXE  = _find_exe("ffmpeg",  _FFMPEG_BIN_DIRS)
FFPROBE_EXE = _find_exe("ffprobe", _FFMPEG_BIN_DIRS)


def yt_dlp_cmd():
    """Return base yt-dlp command, dengan cookies jika file tersedia"""
    scripts_dir = os.path.join(os.path.dirname(sys.executable), "Scripts")
    yt_dlp_exe = _find_exe("yt-dlp", [scripts_dir])
    cmd = [yt_dlp_exe, "--ffmpeg-location", FFMPEG_EXE, "--progress", "--newline"]
    if os.path.exists(COOKIES_PATH):
        cmd += ["--cookies", COOKIES_PATH]
        print("🍪 Menggunakan cookies YouTube")
    return cmd


# ─────────────────────────────────────────
# FACECAM CROP (pojok kiri bawah)
# ─────────────────────────────────────────

def get_facecam_crop(video_path: str):
    result = subprocess.check_output([
        FFPROBE_EXE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path
    ]).decode().strip().split(",")

    vid_w = int(result[0])
    vid_h = int(result[1])
    print(f"📐 Video: {vid_w}x{vid_h}")

    cam_w = int(vid_w * 0.30)
    cam_h = int(vid_h * 0.35)
    cam_x = 0
    cam_y = vid_h - cam_h

    print(f"✅ Facecam → x={cam_x} y={cam_y} w={cam_w} h={cam_h}")
    return (cam_x, cam_y, cam_w, cam_h)


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
# VIDEO CUTTING
# ─────────────────────────────────────────

def cut_video_task(url: str, start: str, end: str, mode: str = "normal"):
    timestamp = start.replace(':', '')
    tmp_yt     = os.path.join(TMP_DIR, f"yt_clip_{timestamp}.mp4")
    tmp_h264   = os.path.join(TMP_DIR, f"yt_h264_{timestamp}.mp4")
    tmp_merged = os.path.join(TMP_DIR, f"merged_{timestamp}.mp4")
    output     = os.path.join(OUTPUT_DIR, f"final_tiktok_{timestamp}.mp4")
    video_game_pop = "./popskin.mp4"

    print(f"🎬 Mode: {mode.upper()}")

    try:
        # Step 1: Download clip YouTube
        print(f"⬇️ Downloading clip {start} - {end}...")
        subprocess.run(
            yt_dlp_cmd() + [
                "--download-sections", f"*{start}-{end}",
                "-f", "best[height<=1080]/best[height<=720]/best",
                "--merge-output-format", "mp4",
                "-o", tmp_yt,
                url
            ], check=True
        )

        # Step 2: Convert ke h264
        print("🔄 Convert ke h264...")
        subprocess.run([
            FFMPEG_EXE, "-i", tmp_yt,
            "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "128k",
            tmp_h264, "-y"
        ], check=True)
        os.remove(tmp_yt)

        # Step 3: Hitung durasi
        duration = float(subprocess.check_output([
            FFPROBE_EXE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            tmp_h264
        ]).decode().strip())
        print(f"⏱ Durasi clip: {duration:.1f} detik")

        # Step 4: Tentukan video filter berdasarkan mode
        if mode.lower() == "gaming":
            print("🎮 Mode GAMING — facecam atas, full screen bawah...")
            cx, cy, cw, ch = get_facecam_crop(tmp_h264)
            video_filter = (
                f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
                f"scale=1080:960:force_original_aspect_ratio=increase,"
                f"crop=1080:960,setsar=1[top];"
                f"[0:v]scale=1080:960:force_original_aspect_ratio=increase,"
                f"crop=1080:960,setsar=1[bottom];"
                f"[top][bottom]vstack=inputs=2[v]"
            )
            ffmpeg_cmd = [
                FFMPEG_EXE, "-i", tmp_h264,
                "-filter_complex", video_filter,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k",
                tmp_merged, "-y"
            ]
        else:
            print("📺 Mode NORMAL — full video atas, POB bawah...")
            video_filter = (
                "[0:v]scale=1080:960:force_original_aspect_ratio=increase,"
                "crop=1080:960,setsar=1[top];"
                "[1:v]scale=1080:960:force_original_aspect_ratio=increase," 
                "crop=1080:960,setsar=1[bottom];"
                "[top][bottom]vstack=inputs=2[v]"
            )
            ffmpeg_cmd = [
                FFMPEG_EXE, "-i", tmp_h264,
                "-stream_loop", "-1", "-t", str(duration), "-i", video_game_pop,
                "-filter_complex", video_filter,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k",
                tmp_merged, "-y"
            ]

        # Step 5: Jalankan ffmpeg
        print("🎬 Menggabungkan video...")
        subprocess.run(ffmpeg_cmd, check=True)

        # Step 6: Cek ukuran, compress jika perlu
        size_mb = os.path.getsize(tmp_merged) / (1024 * 1024)
        print(f"📊 Ukuran setelah merge: {size_mb:.1f}MB")

        if size_mb > 45:
            print(f"⚠️ Terlalu besar ({size_mb:.1f}MB), compressing ke <45MB...")
            video_bitrate = max(int((45 * 1024 * 8) / duration) - 96, 300)
            subprocess.run([
                FFMPEG_EXE, "-i", tmp_merged,
                "-b:v", f"{video_bitrate}k",
                "-maxrate", f"{video_bitrate}k",
                "-bufsize", f"{video_bitrate * 2}k",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "96k",
                output, "-y"
            ], check=True)
            os.remove(tmp_merged)
        else:
            os.rename(tmp_merged, output)
            print(f"✅ Ukuran aman, tidak perlu compress")

        final_size = os.path.getsize(output) / (1024 * 1024)
        print(f"✅ Selesai! File: {output} ({final_size:.1f}MB)")

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        for f in [tmp_yt, tmp_h264, tmp_merged]:
            if os.path.exists(f):
                os.remove(f)
                print(f"🗑️ Hapus temp: {f}")

    final_size = os.path.getsize(output) / (1024 * 1024)
    return {
        "status": "done",
        "file": os.path.basename(output),
        "size_mb": round(final_size, 1),
        "mode": mode
    }


@app.post("/cut")
async def cut_video(request: ClipRequest):
    result = cut_video_task(request.url, request.start, request.end, request.mode)
    return result


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)