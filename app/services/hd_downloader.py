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
import re
import json
import time
import struct
import hashlib
import threading
import subprocess
import urllib.request
from app.config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, COOKIES_PATH, yt_dlp_cmd

# Cascade format HD: (itag_video, label). Audio selalu itag 140 (m4a).
HD_VIDEO_FORMATS = [
    ("298", "720p60"),
    ("136", "720p30"),
    ("299", "1080p60"),
    ("137", "1080p30"),
]
AUDIO_FORMAT = "140"

# Selector audio: video multi-audio-track tidak punya itag plain "140" —
# ID formatnya disuffiks bahasa (140-0, 140-1, 140-drc, dst). Fallback
# "ba[ext=m4a]" tetap me-resolve SATU format m4a apa pun pelabelannya.
_AUDIO_SELECTOR = "140/ba[ext=m4a]"

# Client untuk extract URL direct googlevideo. web_embedded = client yang
# PROVEN bisa mengunduh media (client yang sama dengan jalur full-download)
# → URL-nya menerima HTTP Range dari IP yang sama. android_vr/web/tv
# sering menolak akses media langsung (HTTP 403, butuh PO token) → hanya
# dipakai sebagai fallback. web_embedded dijalankan DENGAN cookies; android_vr
# tanpa cookies (tidak mendukung cookies).
_PLAYER_CLIENT = "web_embedded"
_PLAYER_CLIENT_FALLBACKS = ("android_vr", "web", "tv")

# Fallback User-Agent bila http_headers tidak tersedia dari yt-dlp.
_UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Berapa detik ekstra fragmen diambil di ujung window (buffer aman).
_TAIL_PAD_SEC = 6.0

# ─────────────────────────────────────────
# URL CACHE (per-worker) — googlevideo URL self-signed valid ~2 jam.
# Cache disimpan PER WORKER THREAD: tiap worker ekstrak & pakai URL-nya sendiri.
# Ini menghindari byte-range request bersamaan ke URL yang SAMA — YouTube CDN
# membalas 403 → penyebab gagal saat 3 worker paralel pakai 1 URL global.
# ─────────────────────────────────────────
_url_cache_local = threading.local()
_URL_CACHE_TTL_FALLBACK = 5400  # 90 menit jika param expire tak ter-parse

# ─────────────────────────────────────────
# DISK CACHE untuk URL yang TERBUKTI bisa jump (ungated).
# Gate jump-range GVS pada live VOD bersifat roulette per-mint: URL yang
# kebetulan di-mint bebas gate itu langka & berharga — URL tsb valid ~6 jam
# dan bisa melayani SEMUA klip + run berikutnya. Simpan ke disk agar tidak
# hilang saat proses restart.
# ─────────────────────────────────────────
_DISK_CACHE_PATH = os.path.join(TMP_DIR, "hd_url_cache.json")
_DISK_CACHE_LOCK = threading.Lock()


def _disk_cache_load() -> dict:
    try:
        with open(_DISK_CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _disk_cache_save(data: dict):
    try:
        with open(_DISK_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _disk_cache_get(video_id: str, itag: str):
    """Return (url, headers) jika ada URL ungated tersimpan & belum expired."""
    with _DISK_CACHE_LOCK:
        data = _disk_cache_load()
    entry = data.get(f"{video_id}:{itag}")
    if not entry:
        return None
    if entry.get("expire", 0) < time.time() + 300:  # sisa < 5 menit → anggap basi
        return None
    return entry["url"], dict(entry.get("headers") or {})


def _disk_cache_put(video_id: str, itag: str, url: str, headers: dict):
    """Simpan URL yang TERBUKTI lolos jump-range (206) ke disk."""
    with _DISK_CACHE_LOCK:
        data = _disk_cache_load()
        data[f"{video_id}:{itag}"] = {
            "url": url,
            "headers": dict(headers or {}),
            "expire": _url_expire_ts(url),
        }
        _disk_cache_save(data)
        print(f"   💾 URL ungated disimpan ke disk cache (itag {itag}, valid ~6 jam)")

# Ekstraksi URL di-SERIALIZE global: banyak proses yt-dlp paralel dalam burst
# pendek memicu bot-detection YouTube → URL yang dihasilkan ditolak GVS dengan
# 403 walau header benar. Tiap worker tetap dapat URL sendiri, tapi proses
# ekstraksinya antre satu per satu.
_EXTRACT_LOCK = threading.Lock()

# Jeda antar percobaan download/ekstraksi (detik). YouTube CDN merespons 403
# (rate-limit per IP) jika terlalu banyak request dalam burst pendek — terutama
# dari cascade fallback yang agresif. Jeda ini menyebar request agar throttle
# tidak terpicu / segera pulih.
_THROTTLE_PAUSE_SEC = 2.0


def throttle_pause(reason: str = "", seconds: float = _THROTTLE_PAUSE_SEC):
    """Jeda singkat antar percobaan — kurangi burst request ke CDN YouTube."""
    if reason:
        print(f"   ⏳ Cooldown ({reason})...")
    time.sleep(seconds)


class _HDThrottleError(RuntimeError):
    """HD byte-range gagal karena throttle HTTP 403 — tanda untuk retry/cooldown
    pada jalur yang sama, BUKAN fallback ke metode lain (metode lain dari IP
    yang sama pasti kena 403 juga)."""


def _retry_on_403(fn, tries: int = 3, pause: float = 3.0, label: str = ""):
    """Retry callable pada URL yang SAMA saat HTTP 403.

    Temuan empiris: gate jump-range GVS sering hanya menolak percobaan
    PERTAMA untuk satu URL — retry URL yang sama setelah cooldown ~3 detik
    dijawab 206, dan begitu lolos sekali, URL tsb tetap terbuka untuk semua
    jump-range berikutnya (inilah "URL jackpot" di disk cache). Rotasi
    client/evict URL (jalur lama) justru membuang URL yang sebenarnya masih
    bisa hidup dan mendarat di client tanpa PO token yang pasti 403.
    """
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if "403" not in str(e):
                raise
            last = e
            if i < tries - 1:
                print(f"   ⚠️ 403 ({label}, try {i + 1}/{tries}) — "
                      f"retry URL sama + cooldown...")
                time.sleep(pause)
    raise last


def _worker_url_cache() -> dict:
    """Cache URL milik worker thread ini (tidak dishare antar worker)."""
    cache = getattr(_url_cache_local, "cache", None)
    if cache is None:
        cache = _url_cache_local.cache = {}
    return cache


def _worker_media_cache() -> dict:
    """Cache (sidx frags, init bytes) per googlevideo URL — milik worker ini.

    Header sidx dan init segment (ftyp+moov) IDENTIK untuk satu URL, jadi cukup
    diambil SEKALI lalu dipakai ulang untuk semua klip yang diproses worker ini.
    Ini memangkas jumlah byte-range request per klip (mis. 6-10 → 2) — burst
    request adalah pemicu utama throttle 403 GVS.
    """
    cache = getattr(_url_cache_local, "media", None)
    if cache is None:
        cache = _url_cache_local.media = {}
    return cache


def _url_expire_ts(url: str) -> float:
    """Ambil waktu kedaluwarsa googlevideo URL dari param `expire`."""
    m = re.search(r"[?&]expire=(\d+)", url)
    if m:
        return float(m.group(1))
    return time.time() + _URL_CACHE_TTL_FALLBACK


def _http_range(url: str, start: int, end: int, out_path: str,
                headers: dict | None = None, timeout: int = 60):
    """GET satu byte-range [start, end] inklusif → tulis ke out_path.

    `headers` = http_headers format dari yt-dlp (User-Agent, Accept,
    Accept-Language, Sec-Fetch-Mode, dst). WAJIB sama dengan header yang
    dipakai saat ekstraksi URL — googlevideo menolak request dengan
    header/client lain (HTTP 403).
    """
    hdrs = dict(headers or {})
    hdrs.setdefault("User-Agent", _UA_BROWSER)
    # WAJIB: urllib tidak mengirim Accept-Encoding — GVS (googlevideo) menolak
    # request tanpa header ini (403) karena tak menyisipkan bot. `identity`
    # = tanpa kompresi → byte-range mentah.
    hdrs.setdefault("Accept-Encoding", "identity")
    hdrs["Range"] = f"bytes={start}-{end}"
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    with open(out_path, "wb") as f:
        f.write(data)
    return len(data)


def _evict_url_cache(url: str, itag: str):
    """Buang URL ter-cache (mis. kena 403) agar percobaan berikutnya
    men-extract URL baru (tanda tangan / client segar)."""
    _worker_url_cache().pop((url, itag), None)


def _sigfuncs_cache_dir() -> str:
    """Lokasi cache youtube-sigfuncs yt-dlp (Windows memakai ~/.cache/yt-dlp)."""
    return os.path.join(os.path.expanduser("~"), ".cache", "yt-dlp", "youtube-sigfuncs")


def _clear_sigfuncs_cache():
    """Hapus cache sigfuncs yt-dlp.

    Cache sigfuncs yang BASI membuat yt-dlp solve challenge `n` dengan hasil
    SALAH → URL googlevideo ditolak GVS dengan HTTP 403 walau header request
    sudah benar. Menghapus cache memaksa yt-dlp mengunduh player JS baru dan
    solve ulang challenge → URL fresh langsung diterima (206).
    """
    import shutil
    try:
        d = _sigfuncs_cache_dir()
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            print("   🧹 Cache sigfuncs yt-dlp dibersihkan (solve ulang challenge n)")
    except Exception:
        pass


def _refresh_format_url(page_url: str, itag: str, attempt: int = 1) -> tuple[str, dict]:
    """Evict URL ter-cache lalu extract URL baru dari client BERBEDA.

    Gate jump-range GVS bersifat PER-SERVER dan bergiliran: client berbeda
    (android_vr / tv_embedded / web_embedded) dapat server googlevideo
    berbeda. Saat 403, refresh dari client yang sama hanya mendapat server
    yang sama (tetap 403) — rotasi cascade per attempt lah yang menemukan
    server yang bebas gate. attempt 2 juga membersihkan cache sigfuncs
    (cache basi membuat solve challenge `n` salah → semua URL 403).
    """
    _evict_url_cache(page_url, itag)
    if attempt >= 2:
        _clear_sigfuncs_cache()
    return _get_format_url(page_url, itag, rotate=attempt)


def _get_format_url(url: str, itag: str, rotate: int = 0) -> tuple[str, dict]:
    """Ambil (direct googlevideo URL, http_headers) untuk satu itag.

    Ekstraksi memakai `-J` (dump JSON) — BUKAN `-g` — karena kita butuh
    `http_headers` format (User-Agent Chrome versi spesifik, Accept,
    Accept-Language, Sec-Fetch-Mode). Request byte-range ke googlevideo
    hanya diterima bila header-nya persis sama dengan konteks client yang
    menandatangani URL; header berbeda → HTTP 403.

    `rotate` menggeser urutan cascade client — dipakai saat retry 403 untuk
    mendapat URL dari server googlevideo yang BERBEDA (gate jump-range GVS
    bersifat per-server, bergiliran antar client).

    URL + headers di-cache per (url, itag) DI DALAM worker yang sama → tiap
    worker pakai URL sendiri, dipakai ulang untuk klip-klip worker itu.
    """
    key = (url, itag)
    now = time.time()
    cache = _worker_url_cache()
    hit = cache.get(key)
    if hit and hit[0] > now:
        return hit[1], hit[2]

    # Cascade client untuk extract URL direct googlevideo:
    # - web_embedded + PO token (bgutil provider di Docker :4416): menembus
    #   gate jump-range GVS (403) pada live VOD → prioritas PERTAMA.
    # - android_vr / tv: fallback tanpa PO token.
    # - web_embedded + cookies: members-only.
    # Non-member selalu TANPA cookies — konten publik tidak butuh sesi login.
    clients = [
        (_PLAYER_CLIENT, False),   # web_embedded + PO token ✅ (anti-gate)
        ("android_vr", False),     # live VOD HD — range-friendly (tanpa PO)
        ("tv", False),             # live VOD — range-friendly (tanpa PO)
        (_PLAYER_CLIENT, True),    # web_embedded + cookies — members-only
        (None, True),              # default — dengan cookies
        ("web", True),
        ("tv", True),
    ]
    # Rotasi attempt: mulai cascade dari posisi ke-`rotate` (wrap-around).
    if rotate % len(clients):
        cut = rotate % len(clients)
        clients = clients[cut:] + clients[:cut]
    last_err = None
    with _EXTRACT_LOCK:
        for client, use_cookies in clients:
            label = client if client else "default"
            try:
                cmd = yt_dlp_cmd(use_cookies=use_cookies)
                if client:
                    # Client web-family + fetch_pot=always: URL googlevideo
                    # membawa PO token (dari provider bgutil :4416) →
                    # range-request lompat jauh diterima (206), menembus
                    # gate jump-range GVS yang membalas 403.
                    if client in ("web_embedded", "web", "tv"):
                        cmd += ["--extractor-args",
                                f"youtube:player_client={client};fetch_pot=always"]
                    else:
                        cmd += ["--extractor-args", f"youtube:player_client={client}"]
                cmd += ["-f", _AUDIO_SELECTOR if itag == AUDIO_FORMAT else itag,
                        "-J", url]
                # Output JSON bisa berisi unicode → pastikan pipe UTF-8.
                env = dict(os.environ, PYTHONIOENCODING="utf-8")
                out = subprocess.check_output(cmd, timeout=180, env=env)
                info = json.loads(out)
                direct = info.get("url") or ""
                headers = dict(info.get("http_headers") or {})
                if direct.startswith("http"):
                    if client != _PLAYER_CLIENT:
                        print(f"   🎮 itag {itag} via client '{label}' (fallback)")
                    cache[key] = (_url_expire_ts(direct), direct, headers)
                    return direct, headers
            except Exception as e:
                last_err = e
                print(f"   [client] itag {itag} via '{label}' gagal: {str(e)[:60]}")
                throttle_pause(f"client {label}")
                continue
    raise RuntimeError(f"URL kosong untuk itag {itag} ({str(last_err)[:60]})")


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


def _fetch_header_and_parse(url: str, headers: dict, token: str):
    """Ambil header (init+sidx) secara progresif lalu parse. Return
    (init_len, frags) atau None. HTTP 403 (throttle) di-propagate agar caller
    bisa membedakan throttle dari kegagalan biasa. Hasil di-cache per URL
    (sidx tidak berubah untuk satu URL)."""
    mcache = _worker_media_cache()
    hit = mcache.get(url)
    if hit:
        # hit = (parsed, init_bytes); parsed = (init_len, frags).
        return hit[0]
    for probe_size in (512 * 1024, 2 * 1024 * 1024, 8 * 1024 * 1024):
        tmp = os.path.join(TMP_DIR, f"hdr_probe_{token}.bin")
        try:
            _http_range(url, 0, probe_size - 1, tmp, headers=headers)
            with open(tmp, "rb") as f:
                data = f.read()
            parsed = _parse_sidx(data)
            if parsed:
                # init segment (ftyp+moov) sudah termasuk dalam buffer probe.
                mcache[url] = (parsed, data[:parsed[0]])
                return parsed
        except Exception as e:
            if "403" in str(e):
                print("   ⚠️ 403 saat ambil header init/sidx (range 0..512KB)...")
                raise
            pass
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    return None


def _download_stream_slice(url: str, headers: dict, init_len: int, frags,
                           start_sec: float, end_sec: float,
                           tag: str, token: str) -> tuple[str, float]:
    """
    Unduh init + window byte untuk satu stream (video/audio) → file .mp4 valid.
    Init segment diambil dari cache worker (sekali per URL) — hanya window
    moof+mdat yang di-fetch per klip → 1 request per stream per klip.
    Return (path, frag_start_time). Raise jika di luar jangkauan.
    """
    win = _window_for_range(frags, start_sec, end_sec)
    if not win:
        raise RuntimeError(f"[{tag}] range {start_sec}-{end_sec} di luar durasi")
    byte_start, byte_end, frag_t = win

    mcache = _worker_media_cache()
    entry = mcache.get(url)
    if entry and entry[1]:
        init_bytes = entry[1]
    else:
        init_path = os.path.join(TMP_DIR, f"hd_{tag}_init_{token}.bin")
        _http_range(url, 0, init_len - 1, init_path, headers=headers)
        with open(init_path, "rb") as f:
            init_bytes = f.read()
        if os.path.exists(init_path):
            os.remove(init_path)
        mcache[url] = ((init_len, frags), init_bytes)

    win_path = os.path.join(TMP_DIR, f"hd_{tag}_win_{token}.bin")
    slice_path = os.path.join(TMP_DIR, f"hd_{tag}_slice_{token}.mp4")

    try:
        _http_range(url, byte_start, byte_end, win_path, headers=headers)  # moof+mdat window
    except Exception as e:
        if "403" in str(e):
            print(f"   ⚠️ 403 saat ambil window (jump ke byte {byte_start/1024/1024:.0f}MB) — "
                  f"gate jump-range GVS")
        raise

    with open(slice_path, "wb") as out:
        out.write(init_bytes)
        with open(win_path, "rb") as f:
            out.write(f.read())
    if os.path.exists(win_path):
        os.remove(win_path)
    return slice_path, frag_t


def _page_video_id(page_url: str) -> str:
    """Ambil video ID (11 char) dari URL halaman YouTube apa pun."""
    m = re.search(r"(?:v=|/live/|/shorts/|youtu\.be/)([A-Za-z0-9_-]{11})", page_url)
    return m.group(1) if m else page_url


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
    video_id = _page_video_id(url)

    video_slice = video_frag_t = None
    chosen = None

    # ── STEP 0: URL ungated dari disk cache (hasil mint "jackpot" sebelumnya) ──
    # Gate jump-range GVS roulette per-mint: URL yang pernah terbukti bisa jump
    # (206) dipakai ulang sampai expired (~6 jam) — tidak perlu mint ulang.
    cached_v = _disk_cache_get(video_id, HD_VIDEO_FORMATS[0][0])
    if cached_v:
        cv_url, cv_hdrs = cached_v
        try:
            cv_hdr = _retry_on_403(
                lambda: _fetch_header_and_parse(cv_url, cv_hdrs, token),
                label="disk cache header",
            )
            if cv_hdr:
                cv_init, cv_frags = cv_hdr
                video_slice, video_frag_t = _download_stream_slice(
                    cv_url, cv_hdrs, cv_init, cv_frags, start_sec, end_sec, "v", token
                )
                chosen = f"{HD_VIDEO_FORMATS[0][1]} (disk cache)"
                print(f"   🎯 HD byte-range: {chosen}")
        except Exception as e:
            print(f"   ⚠️ disk cache URL basi/gagal ({str(e)[:50]}) — mint baru...")

    # ── STEP 1: pilih format video HD dari cascade ──
    # Gate jump-range GVS untuk live VOD bersifat roulette per-mint & waktu:
    # kadang longgar (jump 206 OK), sering ketat (403 untuk SEMUA client).
    # Strategi: beberapa rotasi client per itag; jika itag pertama kena gate
    # (403), jangan habiskan waktu dengan itag lain (gate-nya per-KONTEN,
    # bukan per-format) → langsung gagal agar pipeline segera fallback ke
    # full download 720p yang andal.
    # ── STEP 1: siapkan URL AUDIO (disk cache dulu, lalu extract baru) ──
    # NOTE: audio WAJIB di-resolve walau video sudah dari disk cache —
    # slice audio tetap dibutuhkan tiap klip.
    cached_a = _disk_cache_get(video_id, AUDIO_FORMAT)
    if cached_a:
        audio_url, audio_hdrs = cached_a
        print("   💾 Audio URL dari disk cache (ungated)")
    else:
        audio_url, audio_hdrs = _get_format_url(url, AUDIO_FORMAT)
    audio_hdr = None
    for attempt in (1, 2):
        try:
            audio_hdr = _retry_on_403(
                lambda: _fetch_header_and_parse(audio_url, audio_hdrs, token),
                label="audio header",
            )
            break
        except Exception as e:
            if "403" not in str(e) or attempt == 2:
                raise
            print("   ⚠️ audio 403 (URL sama sudah 3x) — rotasi client...")
            throttle_pause("audio 403", seconds=3.0)
            audio_url, audio_hdrs = _refresh_format_url(url, AUDIO_FORMAT, attempt)
    if not audio_hdr:
        # sidx tak terbaca (mis. URL cache basi) → mint URL baru sekali
        print("   ⚠️ audio sidx tak terbaca — mint URL baru...")
        audio_url, audio_hdrs = _refresh_format_url(url, AUDIO_FORMAT, 1)
        audio_hdr = _fetch_header_and_parse(audio_url, audio_hdrs, token)
    if not audio_hdr:
        raise RuntimeError("audio: sidx tak terbaca")

    seen_403 = False
    if not video_slice:
        for itag, label in HD_VIDEO_FORMATS:
            # Itag prioritas dapat rotasi client lebih banyak — gate jump-range
            # GVS bersifat roulette PER-MINT: client berbeda dapat server
            # googlevideo berbeda, salah satunya bisa bebas gate.
            max_attempts = 4 if itag == HD_VIDEO_FORMATS[0][0] else 2
            for attempt in range(1, max_attempts + 1):
                try:
                    vurl, vhdrs = _get_format_url(url, itag, rotate=attempt - 1)
                    vhdr = _retry_on_403(
                        lambda: _fetch_header_and_parse(vurl, vhdrs, token),
                        label=f"itag {itag} header",
                    )
                    if not vhdr:
                        break  # sidx tak terbaca → lanjut itag berikutnya
                    v_init, v_frags = vhdr
                    video_slice, video_frag_t = _retry_on_403(
                        lambda: _download_stream_slice(
                            vurl, vhdrs, v_init, v_frags,
                            start_sec, end_sec, "v", token
                        ),
                        label=f"itag {itag} window",
                    )
                    chosen = label
                    print(f"   🎯 HD byte-range: {label} (itag {itag})")
                    # URL ini TERBUKTI bisa jump → simpan untuk klip/run berikutnya
                    _disk_cache_put(video_id, itag, vurl, vhdrs)
                    break
                except Exception as e:
                    if "403" in str(e) and attempt < max_attempts:
                        print(f"   ⚠️ itag {itag} kena 403 (attempt {attempt}) — rotasi client...")
                        _evict_url_cache(url, itag)
                        if attempt >= 2:
                            # cache sigfuncs basi membuat URL hasil rotasi tetap
                            # salah tanda tangan → 403 lagi walau client benar.
                            _clear_sigfuncs_cache()
                        throttle_pause(f"itag {itag} 403", seconds=3.0)
                        continue
                    if "403" in str(e):
                        if itag == HD_VIDEO_FORMATS[0][0]:
                            # semua rotasi client untuk itag prioritas kena gate →
                            # konten ini sedang di-gate menyeluruh, itag lain pun
                            # pasti sama → fail-fast ke fallback.
                            print("   ⚠️ gate jump-range aktif untuk konten ini — fail-fast ke fallback")
                            raise _HDThrottleError("jump-range gated (403)")
                        seen_403 = True
                        break
                    print(f"   ⚠️ itag {itag} gagal: {str(e)[:60]}")
                    throttle_pause(f"itag {itag}")
                    break
                if video_slice:
                    break
            if video_slice:
                break

    if not video_slice:
        if seen_403:
            raise _HDThrottleError("semua format HD gagal (403 throttle)")
        raise RuntimeError("semua format HD gagal")

    # ── STEP 2: unduh slice audio ──
    # Jika 403: satu rotasi client, parse ulang header, retry — lalu gagal.
    a_init, a_frags = audio_hdr
    audio_slice = None
    audio_frag_t = None
    for attempt in (1, 2):
        try:
            audio_slice, audio_frag_t = _retry_on_403(
                lambda: _download_stream_slice(
                    audio_url, audio_hdrs, a_init, a_frags,
                    start_sec, end_sec, "a", token
                ),
                label="audio window",
            )
            # Audio URL terbukti bisa jump → simpan ke disk cache juga
            _disk_cache_put(video_id, AUDIO_FORMAT, audio_url, audio_hdrs)
            break
        except Exception as e:
            if "403" not in str(e) or attempt == 2:
                if "403" in str(e):
                    raise _HDThrottleError(str(e)) from e
                raise
            print("   ⚠️ audio slice 403 (URL sama sudah 3x) — rotasi client...")
            throttle_pause("audio slice 403", seconds=3.0)
            audio_url, audio_hdrs = _refresh_format_url(url, AUDIO_FORMAT, attempt)
            audio_hdr = _retry_on_403(
                lambda: _fetch_header_and_parse(audio_url, audio_hdrs, token),
                label="audio header (retry)",
            )
            if not audio_hdr:
                raise RuntimeError("audio: sidx tak terbaca (retry)")
            a_init, a_frags = audio_hdr

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


# ─────────────────────────────────────────
# COOKIE HEALTH CHECK — fail-fast sebelum kerja berat
# ─────────────────────────────────────────

def check_session_ok(url: str) -> None:
    """Pastikan sesi cookies masih valid sebelum download konten member.

    Raise RuntimeError dengan pesan jelas jika cookies invalid/roted, atau
    akun tidak punya akses membership. Video publik tidak terblokir.
    """
    cmd = yt_dlp_cmd() + ["--print", "%(id)s", url]
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=120)
    except Exception as e:
        print(f"⚠️ Cek cookies gagal (biarkan pipeline lanjut): {str(e)[:60]}")
        return

    if proc.returncode == 0:
        return

    err = proc.stderr + proc.stdout
    if "Join this channel" not in err:
        print("⚠️ Ekstraksi video gagal (bukan masalah cookies) — lanjut pipeline.")
        return

    if not os.path.exists(COOKIES_PATH):
        raise RuntimeError(
            "🍪 youtube_cookies.txt tidak ditemukan — video members-only butuh "
            "cookies. Export via 'Get cookies.txt LOCALLY' dari Chrome (login "
            "akun member), timpa file-nya, lalu jalankan ulang."
        )
    if "cookies are no longer valid" in err:
        raise RuntimeError(
            "🍪 Cookies YouTube invalid/dirotasi. Export ulang via 'Get cookies.txt "
            "LOCALLY' dari Chrome (login akun member), timpa youtube_cookies.txt, "
            "lalu jalankan ulang."
        )
    raise RuntimeError(
        "🔒 Video members-only, tapi akun tidak terdeteksi punya akses. Pastikan "
        "Chrome login ke akun yang punya membership channel ini, lalu export ulang."
    )
