"""
HD section downloader via byte-range pada fragmented-MP4 (DASH).

Latar: YouTube sudah menghapus format progresif HD (itag 22). Format HD kini
hanya DASH (video-only + audio terpisah). --download-sections + ffmpeg remote
seek pada DASH menggantung (bukan karena throttle server — server menghormati
HTTP Range dengan cepat, tapi ffmpeg `-ss` remote seek pada URL DASH bermasalah).

Solusi: fMP4 punya init (ftyp+moov) + `sidx` (indeks time→byte). Kita:
  1. ambil init+sidx (~beberapa ratus KB) dari awal file,
  2. parse sidx → tabel fragmen (byte offset + durasi per fragmen),
  3. hitung window byte untuk [start, end] yang diminta,
  4. range-fetch HANYA window itu (video + audio), gabung dengan init,
  5. merge A/V → potong presisi lokal.

Hasil: HD asli (720p/1080p) tanpa full-download, ~beberapa detik per section.
"""

import os
import struct
import hashlib
import subprocess
import urllib.request
from app.config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, yt_dlp_cmd

# Cascade format HD: (itag_video, label). Audio selalu itag 140 (m4a).
HD_VIDEO_FORMATS = [
    ("298", "720p60"),
    ("136", "720p30"),
    ("299", "1080p60"),
    ("137", "1080p30"),
]
AUDIO_FORMAT = "140"

# Client yang dipakai untuk extract URL. android_vr = satu-satunya yang kasih
# format HD untuk live VOD (web/tv/mweb kena DRM/images-only).
_PLAYER_CLIENT = "android_vr"

# Berapa detik ekstra fragmen diambil di ujung window (buffer aman).
_TAIL_PAD_SEC = 6.0


def _http_range(url: str, start: int, end: int, out_path: str, timeout: int = 60):
    """GET satu byte-range [start, end] inklusif → tulis ke out_path."""
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    with open(out_path, "wb") as f:
        f.write(data)
    return len(data)


def _get_format_url(url: str, itag: str) -> str:
    """Ambil direct googlevideo URL untuk satu itag via android_vr client."""
    out = subprocess.check_output(
        yt_dlp_cmd() + [
            "--extractor-args", f"youtube:player_client={_PLAYER_CLIENT}",
            "-f", itag, "-g", url,
        ], text=True, timeout=90
    )
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("http"):
            return line
    raise RuntimeError(f"URL kosong untuk itag {itag}")


def _parse_boxes(data: bytes):
    """Scan top-level mp4 box → list (offset, size, type). Berhenti jika box
    melewati buffer (belum ter-fetch)."""
    boxes = []
    off = 0
    n = len(data)
    while off + 8 <= n:
        size = struct.unpack(">I", data[off:off + 4])[0]
        typ = data[off + 4:off + 8]
        if size == 1:  # 64-bit largesize
            if off + 16 > n:
                break
            size = struct.unpack(">Q", data[off + 8:off + 16])[0]
        boxes.append((off, size, typ))
        if size == 0 or off + size > n:
            break
        off += size
    return boxes


def _parse_sidx(data: bytes):
    """
    Parse fMP4 header buffer → (init_len, fragments) di mana:
      init_len  = panjang ftyp+moov (byte sebelum sidx),
      fragments = list (byte_offset, byte_size, start_time_sec, dur_sec).
    Return None jika struktur tak dikenali.
    """
    boxes = _parse_boxes(data)
    sidx = next((b for b in boxes if b[2] == b"sidx"), None)
    if not sidx:
        return None
    sidx_off, sidx_size, _ = sidx
    init_len = sidx_off  # ftyp+moov berakhir tepat di awal sidx

    s = sidx_off + 8
    version = data[s]
    s += 4  # version(1) + flags(3)
    s += 4  # reference_ID
    timescale = struct.unpack(">I", data[s:s + 4])[0]
    s += 4
    if version == 0:
        s += 4  # earliest_presentation_time
        s += 4  # first_offset
    else:
        s += 8
        s += 8
    s += 2  # reserved
    count = struct.unpack(">H", data[s:s + 2])[0]
    s += 2

    # Fragmen pertama mulai tepat setelah box sidx.
    frag_byte = sidx_off + sidx_size
    frag_time = 0.0
    frags = []
    for _ in range(count):
        if s + 12 > len(data):
            return None  # sidx belum ter-fetch penuh
        ref = struct.unpack(">I", data[s:s + 4])[0]
        s += 4
        dur = struct.unpack(">I", data[s:s + 4])[0]
        s += 4
        s += 4  # SAP flags
        ref_size = ref & 0x7FFFFFFF
        dsec = dur / timescale
        frags.append((frag_byte, ref_size, frag_time, dsec))
        frag_byte += ref_size
        frag_time += dsec
    return init_len, frags


def _window_for_range(frags, start_sec: float, end_sec: float):
    """Cari (byte_start, byte_end, frag_start_time) yang mencakup [start,end]."""
    total_dur = frags[-1][2] + frags[-1][3]
    if start_sec >= total_dur:
        return None
    # fragmen pertama yang mencakup start
    i0 = 0
    for i, (b, sz, t, d) in enumerate(frags):
        if t <= start_sec < t + d:
            i0 = i
            break
    # fragmen terakhir yang mencakup end (+ padding)
    hard_end = min(end_sec + _TAIL_PAD_SEC, total_dur)
    i1 = i0
    for j in range(i0, len(frags)):
        i1 = j
        if frags[j][2] + frags[j][3] >= hard_end:
            break
    byte_start = frags[i0][0]
    byte_end = frags[i1][0] + frags[i1][1] - 1  # inklusif
    return byte_start, byte_end, frags[i0][2]


def _fetch_header_and_parse(url: str, token: str):
    """Ambil header (init+sidx) secara progresif lalu parse. Return
    (init_len, frags) atau None."""
    for probe_size in (512 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024):
        tmp = os.path.join(TMP_DIR, f"hdr_probe_{token}.bin")
        try:
            _http_range(url, 0, probe_size - 1, tmp)
            with open(tmp, "rb") as f:
                data = f.read()
            parsed = _parse_sidx(data)
            if parsed:
                return parsed
        except Exception:
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return None


def _download_stream_slice(url: str, init_len: int, frags,
                           start_sec: float, end_sec: float,
                           tag: str, token: str) -> tuple[str, float]:
    """
    Unduh init + window byte untuk satu stream (video/audio) → file .mp4 valid.
    Return (path, frag_start_time). Raise jika di luar jangkauan.
    """
    win = _window_for_range(frags, start_sec, end_sec)
    if not win:
        raise RuntimeError(f"[{tag}] range {start_sec}-{end_sec} di luar durasi")
    byte_start, byte_end, frag_t = win

    init_path = os.path.join(TMP_DIR, f"hd_{tag}_init_{token}.bin")
    win_path = os.path.join(TMP_DIR, f"hd_{tag}_win_{token}.bin")
    slice_path = os.path.join(TMP_DIR, f"hd_{tag}_slice_{token}.mp4")

    _http_range(url, 0, init_len - 1, init_path)          # ftyp+moov
    _http_range(url, byte_start, byte_end, win_path)      # moof+mdat window

    with open(slice_path, "wb") as out:
        for p in (init_path, win_path):
            with open(p, "rb") as f:
                out.write(f.read())
    for p in (init_path, win_path):
        if os.path.exists(p):
            os.remove(p)
    return slice_path, frag_t


def download_section_hd(url: str, start_sec: float, end_sec: float,
                        output_path: str) -> str:
    """
    Unduh section HD [start_sec, end_sec] via byte-range fMP4, potong presisi.
    Raise Exception jika gagal (caller wajib fallback ke jalur 360p).
    """
    duration = end_sec - start_sec
    if duration <= 0:
        raise ValueError("durasi <= 0")

    # token unik per-panggilan → temp tidak tabrakan saat 3 worker paralel
    token = hashlib.md5(f"{output_path}:{start_sec}".encode()).hexdigest()[:10]

    # ── STEP 1: pilih format video HD dari cascade ──
    audio_url = _get_format_url(url, AUDIO_FORMAT)
    audio_hdr = _fetch_header_and_parse(audio_url, token)
    if not audio_hdr:
        raise RuntimeError("audio: sidx tak terbaca")

    video_slice = video_frag_t = None
    chosen = None
    for itag, label in HD_VIDEO_FORMATS:
        try:
            vurl = _get_format_url(url, itag)
            vhdr = _fetch_header_and_parse(vurl, token)
            if not vhdr:
                continue
            v_init, v_frags = vhdr
            video_slice, video_frag_t = _download_stream_slice(
                vurl, v_init, v_frags, start_sec, end_sec, "v", token
            )
            chosen = label
            print(f"   🎯 HD byte-range: {label} (itag {itag})")
            break
        except Exception as e:
            print(f"   ⚠️ itag {itag} gagal: {str(e)[:60]}")
            continue

    if not video_slice:
        raise RuntimeError("semua format HD gagal")

    # ── STEP 2: unduh slice audio ──
    a_init, a_frags = audio_hdr
    audio_slice, audio_frag_t = _download_stream_slice(
        audio_url, a_init, a_frags, start_sec, end_sec, "a", token
    )

    # ── STEP 3: potong TIAP stream terpisah dengan offset-nya sendiri ──
    # PENTING: window video & audio mulai di batas fragmen yang BERBEDA
    # (mis. video @3599s, audio @3594s untuk target yang sama). Jadi offset
    # potong tiap stream berbeda. Kalau di-merge dulu lalu dipotong 1 offset,
    # audio akan geser (desync). Solusi: potong masing-masing ke titik real
    # yang sama [start,end] → keduanya mulai di 0 → merge pasti sinkron.
    # -ss SETELAH -i = accurate seek (slice pendek, murah).
    v_trim = os.path.join(TMP_DIR, f"hd_vtrim_{token}.mp4")
    a_trim = os.path.join(TMP_DIR, f"hd_atrim_{token}.m4a")
    v_off = max(0.0, start_sec - video_frag_t)
    a_off = max(0.0, start_sec - audio_frag_t)

    subprocess.run([
        FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
        "-i", video_slice, "-ss", f"{v_off:.3f}", "-t", f"{duration:.3f}",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-video_track_timescale", "90000",
        v_trim, "-y",
    ], check=True, timeout=300)

    subprocess.run([
        FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
        "-i", audio_slice, "-ss", f"{a_off:.3f}", "-t", f"{duration:.3f}",
        "-vn", "-c:a", "aac", "-b:a", "160k",
        a_trim, "-y",
    ], check=True, timeout=300)

    # ── STEP 4: gabung (keduanya sudah mulai di 0 → sinkron, copy saja) ──
    subprocess.run([
        FFMPEG_EXE, "-hide_banner", "-loglevel", "error",
        "-i", v_trim, "-i", a_trim,
        "-c", "copy", "-shortest",
        "-movflags", "+faststart",
        output_path, "-y",
    ], check=True, timeout=120)

    for p in (video_slice, audio_slice, v_trim, a_trim):
        if os.path.exists(p):
            os.remove(p)

    h = int(subprocess.check_output([
        FFPROBE_EXE, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=height", "-of", "csv=p=0", output_path
    ]).decode().strip())
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"✅ HD section: {h}p [{chosen}], {size_mb:.1f}MB")
    return output_path
