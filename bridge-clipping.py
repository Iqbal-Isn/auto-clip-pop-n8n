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
import concurrent.futures

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

class BatchClipRequest(BaseModel):
    url: str
    clips: list[dict]  # [{"start": "HH:MM:SS", "end": "HH:MM:SS"}, ...]
    mode: str = "normal"


class GamingCompilationRequest(BaseModel):
    url: str
    clips: list[dict]  # [{"start": "HH:MM:SS", "end": "HH:MM:SS"}, ...]  (5 momen)
    facecam_position: str = "btmleft"  # "btmleft" | "btmright" | "topleft" | "topright"


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
        "Style: Default,Google Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
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

        words = e.text.strip().upper().split()
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

def create_ass_from_whisper(words, width=1080, height=1080, margin_v=30):
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
        "Style: Default,Google Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        f"1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # Grup kata → baris (jeda >0.7s atau >8 kata = baris baru)
    lines = []
    current_line = []
    for i, w in enumerate(words):
        w["word"] = w["word"].upper()
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

# Facecam position presets — (crop_anchor_x, crop_anchor_y)
FACECAM_POSITIONS = {
    "btmleft":  {"x": "left",   "y": "bottom"},
    "btmright": {"x": "right",  "y": "bottom"},
    "topleft":  {"x": "left",   "y": "top"},
    "topright": {"x": "right",  "y": "top"},
}


def get_facecam_crop(video_path: str, position: str = "btmleft"):
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

    cam_w = int(vid_w * 0.40)
    cam_h = int(vid_h * 0.45)

    pos = FACECAM_POSITIONS.get(position, FACECAM_POSITIONS["btmleft"])
    cam_x = vid_w - cam_w if pos["x"] == "right" else 0
    cam_y = vid_h - cam_h if pos["y"] == "bottom" else 0

    print(f"✅ Facecam ({position}) → x={cam_x} y={cam_y} w={cam_w} h={cam_h}")
    return (cam_x, cam_y, cam_w, cam_h)


# ─────────────────────────────────────────
# GAMING 50/50 FILTER (facecam atas, gameplay bawah)
# ─────────────────────────────────────────

def _apply_gaming_pip_filter(video_path: str, srt_file: str | None,
                             facecam_position: str, prefix: str) -> str:
    """
    Gaming 50/50 filter:
    - Top 50% (1080×960): Facecam crop → hqdn3d denoise → unsharp → scale
    - Bottom 50% (1080×960): Gameplay fullscreen → scale
    - vstack → 1080×1920, subtitle dibakar di atas komposit
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    cx, cy, cw, ch = get_facecam_crop(video_path, facecam_position)

    # ASS subtitle chain
    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    # Build filter_complex
    video_filter = (
        # Split source ke 2 stream
        f"[0:v]split[main][cam_raw];"
        # Facecam (TOP 50%): crop → scale 2x → sharpen → scale final
        f"[cam_raw]crop={cw}:{ch}:{cx}:{cy},"
        f"scale=iw*2:ih*2:flags=lanczos,"
        f"cas=0.5,"
        f"scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:960,setsar=1[top];"
        # Gameplay (BOTTOM 50%): full frame → scale ke 1080×960
        f"[main]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:960,setsar=1[bottom];"
        # Stack: facecam atas, gameplay bawah
        f"[top][bottom]vstack=inputs=2[comp];"
    )

    # Subtitle (jika ada)
    if srt_file:
        video_filter += f"[comp]{sub_chain}[v]"
    else:
        video_filter += f"[comp]null[v]"

    subprocess.run([
        FFMPEG_EXE, "-i", video_path,
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ], check=True)

    return tmp_merged


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
# VIDEO CUTTING — INTERNAL HELPERS
# ─────────────────────────────────────────

def _download_video_section(url: str, start_sec: float, end_sec: float, tag: str) -> str:
    """
    Download satu section YouTube.
    Return path ke file hasil download (codec asli YouTube).
    """
    start_str = seconds_to_hhmmss(start_sec)
    end_str = seconds_to_hhmmss(end_sec)

    tmp_yt = os.path.join(TMP_DIR, f"yt_dl_{tag}.mp4")

    print(f"⬇️ Downloading {start_str} - {end_str} ({tag})...")
    subprocess.run(
        yt_dlp_cmd() + [
            "--download-sections", f"*{start_str}-{end_str}",
            "-f", "37/22/18/best",  # non-DASH formats only (support section download)
            "--merge-output-format", "mp4",
            "-o", tmp_yt,
            url
        ], check=True
    )

    print(f"✅ Download selesai: {tmp_yt}")
    return tmp_yt


def _whisper_subtitle(video_path: str, prefix: str,
                      ass_width: int = 1080, ass_height: int = 1080,
                      ass_margin_v: int = 30) -> str | None:
    """
    Extract audio, transcribe dengan Whisper, generate ASS subtitle.
    Return path ke file ASS atau None jika gagal.
    """
    tmp_audio = os.path.join(TMP_DIR, f"audio_{prefix}.wav")
    try:
        print(f"🔉 Extract audio ({prefix})...")
        subprocess.run([
            FFMPEG_EXE, "-i", video_path, "-vn",
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            tmp_audio, "-y"
        ], check=True)

        print(f"🔊 Whisper transcribe ({prefix})...")
        words = transcribe_audio(tmp_audio)
        print(f"📝 Whisper: {len(words)} kata terdeteksi")

        ass_content = create_ass_from_whisper(words, width=ass_width, height=ass_height, margin_v=ass_margin_v)
        if ass_content:
            srt_file = os.path.join(TMP_DIR, f"sub_{prefix}.ass")
            with open(srt_file, 'w', encoding='utf-8') as f:
                f.write(ass_content)
            baris = sum(1 for l in ass_content.split('\n') if l.startswith('Dialogue:'))
            print(f"📝 Subtitle: {baris} entries")
            return srt_file
    except Exception as e:
        print(f"⚠️ Gagal Whisper subtitle: {e}")
    finally:
        if os.path.exists(tmp_audio):
            os.remove(tmp_audio)
    return None


def _apply_tiktok_filter(video_path: str, srt_file: str | None,
                         mode: str, prefix: str) -> str:
    """
    Apply TikTok filter (gaming/normal) + subtitle burn-in.
    Return path ke file hasil filter (tmp_merged).
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    if mode.lower() == "gaming":
        print(f"🎮 Mode GAMING ({prefix}) — facecam atas, full screen bawah...")
        cx, cy, cw, ch = get_facecam_crop(video_path)
        if srt_file:
            video_filter = (
                f"[0:v]{sub_chain}[subbed];"
                f"[subbed]crop={cw}:{ch}:{cx}:{cy},"
                f"scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:960,setsar=1[top];"
                f"[subbed]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:960,setsar=1[bottom];"
                f"[top][bottom]vstack=inputs=2[v]"
            )
        else:
            video_filter = (
                f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
                f"scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:960,setsar=1[top];"
                f"[0:v]scale=1080:960:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop=1080:960,setsar=1[bottom];"
                f"[top][bottom]vstack=inputs=2[v]"
            )
    else:
        print(f"📺 Mode NORMAL ({prefix}) — blur background + foreground + subtitle...")
        if srt_file:
            video_filter = (
                "[0:v]split[orig_raw][bg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,boxblur=15:15[blurred];"
                f"[orig_raw]scale=1080:1080:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1080[scaled];"
                f"[scaled]{sub_chain}[fg];"
                "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
            )
        else:
            video_filter = (
                "[0:v]split[orig][bg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,boxblur=15:15[blurred];"
                "[orig]scale=1080:1080:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1080[fg];"
                "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
            )

    subprocess.run([
        FFMPEG_EXE, "-i", video_path,
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ], check=True)

    return tmp_merged


# ─────────────────────────────────────────
# CONCAT + TRANSITIONS (xfade / acrossfade)
# ─────────────────────────────────────────

# Transisi per pasangan klip (1→2, 2→3, 3→4, 4→5)
_TRANSITIONS = [
    {"video": "fade",        "vid_dur": 0.4, "aud_dur": 0.3},
    {"video": "smoothleft",  "vid_dur": 0.5, "aud_dur": 0.4},
    {"video": "fade",        "vid_dur": 0.4, "aud_dur": 0.3},
    {"video": "fadeblack",   "vid_dur": 0.5, "aud_dur": 0.4},
]


def _get_duration(path: str) -> float:
    """Dapatkan durasi video (detik) via ffprobe."""
    return float(subprocess.check_output([
        FFPROBE_EXE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]).decode().strip())


def _concat_with_transitions(clip_paths: list[str], output: str) -> str:
    """
    Gabungkan N klip dengan xfade (video) + acrossfade (audio).
    Transisi bervariasi sesuai daftar _TRANSITIONS.
    """
    n = len(clip_paths)
    if n == 0:
        raise ValueError("Tidak ada klip untuk digabung")
    if n == 1:
        # Hanya 1 klip — rename langsung
        os.rename(clip_paths[0], output)
        return output

    print(f"\n🎬 CONCAT: {n} klip → {output}")

    # 1. Dapatkan durasi setiap klip
    durs = []
    for i, p in enumerate(clip_paths):
        d = _get_duration(p)
        durs.append(d)
        print(f"  Klip #{i+1}: {d:.1f}s")

    # 2. Bangun filter_complex untuk semua input
    inputs = []
    for p in clip_paths:
        inputs += ["-i", p]

    vid_streams = []
    aud_streams = []
    vid_filters = []
    aud_filters = []

    for i in range(n):
        vid_streams.append(f"[{i}:v]")
        aud_streams.append(f"[{i}:a]")

    # Chain xfade video
    # Format: [prev][curr]xfade=transition=T:dur=D:offset=O[next]
    curr_vid = vid_streams[0]
    cum_dur = durs[0]

    for i in range(1, n):
        tr = _TRANSITIONS[i - 1] if (i - 1) < len(_TRANSITIONS) else _TRANSITIONS[-1]
        offset = cum_dur - tr["vid_dur"]
        next_label = f"v{i}" if i < n - 1 else "v"
        vid_filters.append(
            f"{curr_vid}{vid_streams[i]}"
            f"xfade=transition={tr['video']}:duration={tr['vid_dur']}:offset={offset:.2f}"
            f"[{next_label}]"
        )
        curr_vid = f"[{next_label}]"
        cum_dur += durs[i]

    # Chain acrossfade audio
    curr_aud = aud_streams[0]
    cum_dur = durs[0]

    for i in range(1, n):
        tr = _TRANSITIONS[i - 1] if (i - 1) < len(_TRANSITIONS) else _TRANSITIONS[-1]
        offset = cum_dur - tr["aud_dur"]
        next_label = f"a{i}" if i < n - 1 else "a"
        aud_filters.append(
            f"{curr_aud}{aud_streams[i]}"
            f"acrossfade=d={tr['aud_dur']}:c1=tri:c2=tri"
            f"[{next_label}]"
        )
        curr_aud = f"[{next_label}]"
        cum_dur += durs[i]

    filter_complex = ";".join(vid_filters + aud_filters)

    # Total durasi estimasi
    total_dur = sum(durs) - sum(
        _TRANSITIONS[i]["vid_dur"] for i in range(min(n - 1, len(_TRANSITIONS)))
    )
    print(f"  Total durasi: ~{total_dur:.1f}s ({total_dur / 60:.1f} menit)")

    subprocess.run([
        FFMPEG_EXE,
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        output, "-y"
    ], check=True)

    final_size = os.path.getsize(output) / (1024 * 1024)
    print(f"✅ Concat selesai: {output} ({final_size:.1f}MB)")
    return output


def _compress_to_target(source: str, output: str, target_mb: float = 45):
    """
    Cek ukuran file. Jika > target_mb, compress ulang.
    Jika tidak, rename/move ke output.
    """
    size_mb = os.path.getsize(source) / (1024 * 1024)
    print(f"📊 Ukuran: {size_mb:.1f}MB")

    if size_mb > target_mb:
        duration = float(subprocess.check_output([
            FFPROBE_EXE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            source
        ]).decode().strip())

        print(f"⚠️ Terlalu besar ({size_mb:.1f}MB), compressing ke <{target_mb}MB...")
        video_bitrate = max(int((target_mb * 1024 * 8) / duration) - 128, 2000)
        subprocess.run([
            FFMPEG_EXE, "-i", source,
            "-b:v", f"{video_bitrate}k",
            "-maxrate", f"{video_bitrate}k",
            "-bufsize", f"{video_bitrate * 2}k",
            "-c:v", "libx264", "-preset", "fast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            output, "-y"
        ], check=True)
    else:
        if source != output:
            os.rename(source, output)
        print(f"✅ Ukuran aman, tidak perlu compress")

    final_size = os.path.getsize(output) / (1024 * 1024)
    print(f"✅ Selesai! File: {output} ({final_size:.1f}MB)")
    return final_size


def _process_single_clip_full(url: str, start_str: str, end_str: str,
                              mode: str, clip_idx: int) -> dict:
    """
    Download section spesifik + proses satu klip.
    Setiap klip download section-nya sendiri (bukan full range)
    agar tidak membuang waktu download bagian yang tidak diperlukan.
    """
    start_sec = hhmmss_to_seconds(start_str)
    end_sec = hhmmss_to_seconds(end_str)
    prefix = f"{start_str.replace(':', '')}_{clip_idx}"
    output = os.path.join(OUTPUT_DIR, f"final_tiktok_{prefix}.mp4")

    source_h264 = None
    tmp_merged = None
    srt_file = None

    try:
        # Step 1: Download section spesifik klip ini saja (30-60 detik)
        source_h264 = _download_video_section(url, start_sec, end_sec, prefix)

        # Step 2: Whisper subtitle
        srt_file = _whisper_subtitle(source_h264, prefix)

        # Step 3: TikTok filter
        tmp_merged = _apply_tiktok_filter(source_h264, srt_file, mode, prefix)

        # Step 4: Compress if needed
        final_size = _compress_to_target(tmp_merged, output)

        return {
            "file": os.path.basename(output),
            "size_mb": round(final_size, 1),
            "mode": mode
        }

    finally:
        # Cleanup temp files
        for f in [source_h264, tmp_merged, srt_file]:
            if f and os.path.exists(f):
                os.remove(f)
                print(f"🗑️ Hapus temp: {f}")


def _cut_video_impl(url: str, segments: list[dict], mode: str = "normal") -> list[dict]:
    """
    Core implementation:
    Proses setiap klip secara sequential — download section spesifik per klip.
    Ini menghindari download range besar yang tidak perlu.
    Keuntungan batch endpoint: satu HTTP call, error isolation, clean response.
    """
    if not segments:
        return []

    print(f"\n{'='*50}")
    print(f"🎬 BATCH PROCESSING: {len(segments)} klip")
    print(f"📐 Mode: {mode.upper()}")
    for i, seg in enumerate(segments):
        print(f"  Klip #{i+1}: {seg['start']} → {seg['end']}")
    print(f"{'='*50}\n")

    results = []
    for i, seg in enumerate(segments):
        print(f"\n{'─'*40}")
        print(f"🎯 Klip #{i+1}: {seg['start']} → {seg['end']}")
        print(f"{'─'*40}")

        try:
            result = _process_single_clip_full(
                url, seg["start"], seg["end"], mode, i
            )
            result["status"] = "success"
            results.append(result)
        except Exception as e:
            print(f"❌ Klip #{i+1} GAGAL: {e}")
            results.append({
                "status": "error",
                "file": None,
                "size_mb": 0,
                "mode": mode,
                "error": str(e)
            })

    return results


# ─────────────────────────────────────────
# GAMING COMPILATION — 5 klip → 1 video dengan transisi
# ─────────────────────────────────────────

def _process_gaming_compilation(url: str, clips: list[dict],
                                facecam_position: str = "btmleft") -> dict:
    """
    Proses 5 klip gaming:
    1. Parallel download (2 workers)
    2. Sequential Whisper (thread-safety)
    3. Parallel PiP filter + encode (3 workers)
    4. Concat dengan xfade transitions → 1 video final
    """
    n = len(clips)
    print(f"\n{'='*50}")
    print(f"🎮 GAMING COMPILATION: {n} klip → 1 video")
    print(f"📐 Facecam position: {facecam_position.upper()}")
    for i, seg in enumerate(clips):
        print(f"  Klip #{i+1}: {seg['start']} → {seg['end']}")
    print(f"{'='*50}\n")

    output = os.path.join(OUTPUT_DIR, "gaming_compilation.mp4")

    downloaded = {}   # idx → path
    filtered = {}     # idx → path
    temp_files = []

    try:
        # ═══ STEP 1: Parallel download (max 2) + retry gagal ═══
        print("📥 STEP 1: Parallel download...")
        MAX_RETRIES = 2  # retry per klip yang gagal (403 transient)

        def _download_with_retry(clip_index: int, seg: dict) -> str | None:
            """Download satu klip, retry up to MAX_RETRIES kali jika gagal."""
            start_sec = hhmmss_to_seconds(seg["start"])
            end_sec = hhmmss_to_seconds(seg["end"])
            prefix = f"g5_{seg['start'].replace(':', '')}_{clip_index}"
            last_error = None
            for attempt in range(1 + MAX_RETRIES):
                try:
                    path = _download_video_section(url, start_sec, end_sec, prefix)
                    if attempt > 0:
                        print(f"  ✅ Klip #{clip_index+1} retry #{attempt} BERHASIL")
                    return path
                except Exception as e:
                    last_error = e
                    if attempt < MAX_RETRIES:
                        print(f"  🔄 Klip #{clip_index+1} gagal (attempt {attempt+1}), retry...")
            print(f"  ❌ Klip #{clip_index+1} download GAGAL (setelah {1+MAX_RETRIES}x): {last_error}")
            return None

        # Parallel batch pertama
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = {}
            for i, seg in enumerate(clips):
                fut = pool.submit(_download_with_retry, i, seg)
                futures[fut] = i

            for fut in concurrent.futures.as_completed(futures):
                i = futures[fut]
                try:
                    path = fut.result()
                    if path:
                        downloaded[i] = path
                        temp_files.append(path)
                        print(f"  ✅ Klip #{i+1} downloaded")
                except Exception as e:
                    print(f"  ❌ Klip #{i+1} download GAGAL: {e}")

        if len(downloaded) < 1:
            raise RuntimeError("Semua download gagal — tidak bisa melanjutkan")
        print(f"  📊 {len(downloaded)}/{n} berhasil didownload\n")

        # ═══ STEP 2: Sequential Whisper (thread-safety model) ═══
        print("🔊 STEP 2: Whisper transcribe (sequential)...")
        srt_files = {}  # idx → path
        for i in sorted(downloaded.keys()):
            prefix = f"g5_{clips[i]['start'].replace(':', '')}_{i}"
            try:
                srt = _whisper_subtitle(
                    downloaded[i], prefix,
                    ass_width=1080, ass_height=1920,  # canvas 9:16
                    ass_margin_v=120  # margin tinggi — hindari tumpang tindih dgn facecam
                )
                if srt:
                    srt_files[i] = srt
                    temp_files.append(srt)
                    print(f"  ✅ Klip #{i+1}: {srt}")
                else:
                    print(f"  ⚠️ Klip #{i+1}: tanpa subtitle")
            except Exception as e:
                print(f"  ⚠️ Klip #{i+1} Whisper GAGAL: {e}")
        print(f"  📊 {len(srt_files)}/{len(downloaded)} subtitle siap\n")

        # ═══ STEP 3: Parallel PiP filter + encode (max 3 — CPU bound) ═══
        print("🎨 STEP 3: PiP filter + encode (parallel)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for i in sorted(downloaded.keys()):
                prefix = f"g5_{clips[i]['start'].replace(':', '')}_{i}"
                srt = srt_files.get(i)
                fut = pool.submit(
                    _apply_gaming_pip_filter,
                    downloaded[i], srt, facecam_position, prefix
                )
                futures[fut] = i

            for fut in concurrent.futures.as_completed(futures):
                i = futures[fut]
                try:
                    path = fut.result()
                    filtered[i] = path
                    temp_files.append(path)
                    print(f"  ✅ Klip #{i+1} filtered")
                except Exception as e:
                    print(f"  ❌ Klip #{i+1} filter GAGAL: {e}")

        if len(filtered) < 1:
            raise RuntimeError("Semua filter gagal — tidak bisa melanjutkan")
        print(f"  📊 {len(filtered)}/{len(downloaded)} berhasil difilter\n")

        # ═══ STEP 4: Concat dengan transisi ═══
        print("🔗 STEP 4: Concat dengan transisi...")
        ordered_paths = [filtered[i] for i in sorted(filtered.keys())]
        _concat_with_transitions(ordered_paths, output)

        final_size = os.path.getsize(output) / (1024 * 1024)
        print(f"\n✅ KOMPILASI SELESAI: {output} ({final_size:.1f}MB)")

        return {
            "status": "success",
            "file": os.path.basename(output),
            "size_mb": round(final_size, 1),
            "clips_processed": len(filtered),
            "total_clips": n,
            "facecam_position": facecam_position
        }

    finally:
        # Cleanup temp files (kecuali output final)
        for f in temp_files:
            if f and os.path.exists(f) and f != output:
                os.remove(f)
                print(f"🗑️ Hapus temp: {f}")


# ─────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────

def cut_video_task(url: str, start: str, end: str, mode: str = "normal"):
    """Single clip — kompatibilitas backward"""
    segments = [{"start": start, "end": end}]
    results = _cut_video_impl(url, segments, mode)
    if results and results[0].get("status") == "error":
        return {"status": "error", "message": results[0].get("error", "Unknown error")}
    return results[0] if results else {"status": "error", "message": "No results"}


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
        result = _process_gaming_compilation(
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
