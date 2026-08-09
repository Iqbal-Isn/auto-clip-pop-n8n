"""
Config & discovery — paths, constants, Whisper lazy-load, ffmpeg/ffprobe/yt-dlp.
"""

import os
import sys
import shutil
import tempfile
import platform

IS_WINDOWS = platform.system() == "Windows"
TMP_DIR = tempfile.gettempdir()
OUTPUT_DIR = r"C:\Users\RWID\Downloads\clip"
if not os.path.exists(OUTPUT_DIR):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

COOKIES_PATH = "./youtube_cookies.txt"
TRANSISI_SOUND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transisi_sound.mpeg")
WATERMARK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wm_mk.png")

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


# ─────────────────────────────────────────
# FACECAM POSITION PRESETS
# ─────────────────────────────────────────

FACECAM_POSITIONS = {
    "btmleft":  {"x": "left",   "y": "bottom"},
    "btmright": {"x": "right",  "y": "bottom"},
    "topleft":  {"x": "left",   "y": "top"},
    "topright": {"x": "right",  "y": "top"},
}


# ─────────────────────────────────────────
# EXECUTABLE DISCOVERY (ffmpeg / ffprobe / yt-dlp)
# ─────────────────────────────────────────

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
    cmd = [yt_dlp_exe, "--ffmpeg-location", FFMPEG_EXE, "--progress", "--newline",
           "--js-runtimes", "node", "--remote-components", "ejs:github"]
    if os.path.exists(COOKIES_PATH):
        cmd += ["--cookies", COOKIES_PATH]
        print("🍪 Menggunakan cookies YouTube")
    return cmd
