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
OUTPUT_DIR = r"C:\Users\RWID\Downloads\clip"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

COOKIES_PATH = "./youtube_cookies.txt"

# ─────────────────────────────────────────
# WHISPER (lazy load — hanya init sekali)
# ─────────────────────────────────────────
_whisper_model = None

def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        print("🔊 Loading Whisper model (small)...")
        _whisper_model = WhisperModel("small", device="cpu", compute_type="int8")
        print("✅ Whisper siap!")
    return _whisper_model

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

def hhmmss_to_seconds(t: str):
    """Convert HH:MM:SS atau MM:SS ke detik"""
    parts = t.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return int(parts[0])

def create_srt(entries, clip_start_sec, clip_end_sec):
    """Buat konten SRT dari transcript entries dalam rentang clip (per-baris)"""
    srt = []
    idx = 1
    for e in entries:
        es = e.start
        ee = es + e.duration
        if ee < clip_start_sec or es > clip_end_sec:
            continue
        rel_start = max(0, es - clip_start_sec)
        rel_end = min(clip_end_sec, ee) - clip_start_sec

        def fmt(sec):
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            ms = int((sec % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        srt.append(f"{idx}\n{fmt(rel_start)} --> {fmt(rel_end)}\n{e.text}\n")
        idx += 1
    return "\n".join(srt)

def create_ass_word_by_word(entries, clip_start_sec, clip_end_sec, width=1080, height=1080):
    """Buat ASS subtitle karaoke — teks terakumulasi, kata aktif di-highlight"""
    def fmt_ass(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int((sec % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    header = (
        "[Script Info]\n"
        "Title: Auto Subtitles\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,30,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogues = []
    for e in entries:
        es = e.start
        ee = es + e.duration
        if ee < clip_start_sec or es > clip_end_sec:
            continue
        rel_start = max(0, es - clip_start_sec)
        rel_end = min(clip_end_sec, ee) - clip_start_sec

        words = e.text.strip().split()
        if not words:
            continue

        total_dur = rel_end - rel_start
        word_count = len(words)
        dur_per_word = total_dur / word_count

        # Teks terakumulasi: tiap entry = semua kata sblmnya + kata baru dgn \K highlight
        accumulated = []
        t = rel_start
        for i, w in enumerate(words):
            accumulated.append(w)
            is_last = (i == word_count - 1)
            entry_end = rel_end if is_last else t + dur_per_word

            # \K = durasi highlight kata baru (centidetik)
            k_val = int((entry_end - t) * 100)
            full_text = " ".join(accumulated)

            if len(accumulated) == 1:
                # Hanya 1 kata — full text dengan highlight
                text = f"{{\\K{k_val}}}{full_text}"
            else:
                # Pisahkan kata2 sblmnya (sudah putih) dan kata baru (highlight)
                before = " ".join(accumulated[:-1])
                text = f"{before} {{\\K{k_val}}}{w}"

            dialogues.append(
                f"Dialogue: 0,{fmt_ass(t)},{fmt_ass(entry_end)},Default,,0,0,0,,{text}"
            )
            t = entry_end

    return header + "\n".join(dialogues)

def transcribe_audio(audio_path):
    """Transcribe audio dengan faster-whisper → word-level timestamps"""
    model = get_whisper_model()
    segments, info = model.transcribe(
        audio_path,
        word_timestamps=True,
        language="id",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    words = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                word_text = w.word.strip()
                if word_text:
                    words.append({
                        "word": word_text,
                        "start": w.start,
                        "end": w.end
                    })
    return words

def create_ass_from_whisper(words, width=1080, height=1080):
    """Buat ASS karaoke dari whisper word timestamps"""
    if not words:
        return ""

    def fmt_ass(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int((sec % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    header = (
        "[Script Info]\n"
        "Title: Auto Subtitles (Whisper)\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,30,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # Grup kata → baris (jeda >0.7s atau >8 kata = baris baru)
    lines = []
    current_line = []
    for i, w in enumerate(words):
        current_line.append(w)
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 999
        if gap > 0.7 or len(current_line) >= 2:
            lines.append(current_line)
            current_line = []
    if current_line:
        lines.append(current_line)

    # Generate ASS dialogues dengan akumulasi karaoke
    dialogues = []
    for line in lines:
        accumulated = []
        for i, w in enumerate(line):
            accumulated.append(w["word"])
            start = w["start"]
            is_last = (i == len(line) - 1)
            end = line[-1]["end"] if is_last else line[i + 1]["start"]

            k_val = max(int((w["end"] - w["start"]) * 100), 5)

            if len(accumulated) == 1:
                text = f"{{\\K{k_val}}}{w['word']}"
            else:
                before = " ".join(accumulated[:-1])
                text = f"{before} {{\\K{k_val}}}{w['word']}"

            dialogues.append(
                f"Dialogue: 0,{fmt_ass(start)},{fmt_ass(end)},Default,,0,0,0,,{text}"
            )

    return header + "\n".join(dialogues)

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
    tmp_audio  = os.path.join(TMP_DIR, f"audio_{timestamp}.wav")
    output     = os.path.join(OUTPUT_DIR, f"final_tiktok_{timestamp}.mp4")

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

        # Step 3.5: Whisper — transcribe audio clip ke word-level subtitle
        srt_file = None
        try:
            # Extract audio dari clip
            print("🔉 Extract audio...")
            subprocess.run([
                FFMPEG_EXE, "-i", tmp_h264, "-vn",
                "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                tmp_audio, "-y"
            ], check=True)

            # Transcribe pakai Whisper
            print("🔊 Whisper transcribe...")
            words = transcribe_audio(tmp_audio)
            print(f"📝 Whisper: {len(words)} kata terdeteksi")

            # Generate ASS subtitle
            ass_content = create_ass_from_whisper(words)
            if ass_content:
                srt_file = os.path.join(TMP_DIR, f"sub_{timestamp}.ass")
                with open(srt_file, 'w', encoding='utf-8') as f:
                    f.write(ass_content)
                baris = sum(1 for l in ass_content.split('\n') if l.startswith('Dialogue:'))
                print(f"📝 Subtitle: {baris} entries")
        except Exception as e:
            print(f"⚠️ Gagal Whisper subtitle: {e}")

        # Step 4: Proses berdasarkan mode
        sub_chain = ""
        if srt_file:
            srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
            sub_chain = f"ass='{srt_escaped}'"

        if mode.lower() == "gaming":
            print("🎮 Mode GAMING — facecam atas, full screen bawah...")
            cx, cy, cw, ch = get_facecam_crop(tmp_h264)
            if srt_file:
                video_filter = (
                    f"[0:v]{sub_chain}[subbed];"
                    f"[subbed]crop={cw}:{ch}:{cx}:{cy},"
                    f"scale=1080:960:force_original_aspect_ratio=increase,"
                    f"crop=1080:960,setsar=1[top];"
                    f"[subbed]scale=1080:960:force_original_aspect_ratio=increase,"
                    f"crop=1080:960,setsar=1[bottom];"
                    f"[top][bottom]vstack=inputs=2[v]"
                )
            else:
                video_filter = (
                    f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
                    f"scale=1080:960:force_original_aspect_ratio=increase,"
                    f"crop=1080:960,setsar=1[top];"
                    f"[0:v]scale=1080:960:force_original_aspect_ratio=increase,"
                    f"crop=1080:960,setsar=1[bottom];"
                    f"[top][bottom]vstack=inputs=2[v]"
                )
            subprocess.run([
                FFMPEG_EXE, "-i", tmp_h264,
                "-filter_complex", video_filter,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k",
                tmp_merged, "-y"
            ], check=True)
            final_source = tmp_merged
        else:
            print("📺 Mode NORMAL — blur background + foreground + subtitle...")
            # Canvas 1080x1920 (9:16 TikTok/Reels)
            # Background: video diperbesar + blur penuhi layar
            # Foreground: video 1:1 di tengah, jernih + subtitle
            if srt_file:
                video_filter = (
                    "[0:v]split[orig_raw][bg];"
                    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=15:15[blurred];"
                    f"[orig_raw]scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080[scaled];"
                    f"[scaled]{sub_chain}[fg];"
                    "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
                )
            else:
                video_filter = (
                    "[0:v]split[orig][bg];"
                    "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=15:15[blurred];"
                    "[orig]scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080[fg];"
                    "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
                )
            subprocess.run([
                FFMPEG_EXE, "-i", tmp_h264,
                "-filter_complex", video_filter,
                "-map", "[v]", "-map", "0:a?",
                "-c:v", "libx264", "-crf", "28", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "128k",
                tmp_merged, "-y"
            ], check=True)
            final_source = tmp_merged

        # Step 5: Cek ukuran, compress jika perlu
        size_mb = os.path.getsize(final_source) / (1024 * 1024)
        print(f"📊 Ukuran: {size_mb:.1f}MB")

        if size_mb > 45:
            print(f"⚠️ Terlalu besar ({size_mb:.1f}MB), compressing ke <45MB...")
            video_bitrate = max(int((45 * 1024 * 8) / duration) - 96, 300)
            subprocess.run([
                FFMPEG_EXE, "-i", final_source,
                "-b:v", f"{video_bitrate}k",
                "-maxrate", f"{video_bitrate}k",
                "-bufsize", f"{video_bitrate * 2}k",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "96k",
                output, "-y"
            ], check=True)
        else:
            os.rename(final_source, output)
            print(f"✅ Ukuran aman, tidak perlu compress")

        final_size = os.path.getsize(output) / (1024 * 1024)
        print(f"✅ Selesai! File: {output} ({final_size:.1f}MB)")

    except Exception as e:
        return {"status": "error", "message": str(e)}
    finally:
        for f in [tmp_yt, tmp_h264, tmp_merged, tmp_audio, srt_file]:
            if f and os.path.exists(f):
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