"""Pipeline orkestrasi: download, Whisper subtitle, clip processing, gaming compilation."""

import os
import re as _re
import time
import hashlib
import threading
import subprocess
import concurrent.futures
from datetime import datetime
from app.config import (FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, OUTPUT_DIR, TRANSISI_SOUND,
                        yt_dlp_cmd, FULL_CACHE_MAX_AGE_HOURS, FULL_CACHE_MAX_TOTAL_GB)
from app.utils.helpers import seconds_to_hhmmss, hhmmss_to_seconds
from app.services.subtitles import transcribe_audio, create_ass_from_whisper
from app.services.filters import apply_tiktok_filter, apply_gaming_filter
from app.services.transitions import concat_with_transitions, compress_to_target
from app.services.hd_downloader import throttle_pause


# ─────────────────────────────────────────
# THROTTLE (403) MITIGATION
# ─────────────────────────────────────────
# Saat YouTube CDN memberi 403 (rate-limit per IP, transien):
#   - HD byte-range di-retry beberapa kali di jalur yang SAMA dengan cooldown
#     lebih lama (metode fallback lain dari IP yang sama pasti 403 juga).
#   - Setelah STEP 1 paralel selesai, klip yang gagal di-retry lagi (retry pass)
#     secara serial — throttle sudah reda.
RETRY_PASS_ATTEMPTS = 2     # percobaan retry pass per klip
RETRY_PASS_PAUSE_SEC = 10.0 # jeda antar retry pass


# ─────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────

# ─────────────────────────────────────────
# FULL VIDEO CACHE — download sekali, potong lokal
# ─────────────────────────────────────────

# Kunci global: klip paralel dari video yang sama → hanya 1 thread yang download,
# sisanya menunggu & memakai cache.
_FULL_DL_LOCK = threading.Lock()


def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID dari URL."""
    for pat in [r'(?:youtube\.com/live/|youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})']:
        m = _re.search(pat, url)
        if m:
            return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:12]


# ─────────────────────────────────────────
# CACHE CLEANUP — cegah disk penuh oleh full_{id}.mp4 (1-3GB per video)
# ─────────────────────────────────────────

def cleanup_full_video_cache(keep_url: str | None = None):
    """Bersihkan cache full video di TMP_DIR:
    1. Hapus yang berumur > FULL_CACHE_MAX_AGE_HOURS.
    2. Jika total masih > FULL_CACHE_MAX_TOTAL_GB, hapus tertua dulu.
       File yang sedang dipakai run ini (keep_url) tidak pernah dihapus.
    Juga sapu file staging .dl.mp4 yang basi (proses mati di tengah download).
    """
    keep_id = _extract_video_id(keep_url) if keep_url else None
    now = time.time()
    entries = []  # (mtime, size, path)
    try:
        for fn in os.listdir(TMP_DIR):
            if not (fn.startswith("full_") and fn.endswith(".mp4")):
                continue
            if keep_id and fn == f"full_{keep_id}.mp4":
                continue
            p = os.path.join(TMP_DIR, fn)
            try:
                st = os.stat(p)
                entries.append((st.st_mtime, st.st_size, p))
            except OSError:
                pass
    except OSError:
        return

    removed_bytes = 0
    kept = []
    for mtime, size, p in sorted(entries):  # tertua dulu
        age_h = (now - mtime) / 3600
        if age_h > FULL_CACHE_MAX_AGE_HOURS:
            try:
                os.remove(p)
                removed_bytes += size
            except OSError:
                pass
        else:
            kept.append((mtime, size, p))

    # Enforce batas total: hapus tertua dulu sampai masuk kuota
    total = sum(s for _, s, _ in kept)
    limit = FULL_CACHE_MAX_TOTAL_GB * 1024**3
    while total > limit and kept:
        mtime, size, p = kept.pop(0)  # tertua
        try:
            os.remove(p)
            total -= size
            removed_bytes += size
        except OSError:
            pass

    # Sapu staging basi (unduhan mati di tengah jalan) — aman: file .dl aktif
    # sedang ditulis & akan di-replace, hapus yang berumur > 6 jam saja.
    for fn in os.listdir(TMP_DIR):
        if fn.startswith("full_") and ".dl." in fn:
            p = os.path.join(TMP_DIR, fn)
            try:
                if now - os.stat(p).st_mtime > 6 * 3600:
                    os.remove(p)
            except OSError:
                pass

    if removed_bytes:
        print(f"🧹 Cache cleanup: {removed_bytes/1024**3:.1f}GB dibebaskan "
              f"(sisanya {total/1024**3:.1f}GB)")


def _download_full_video(url: str) -> str:
    """
    Download FULL video SEKALI via client web_embedded → cache di TMP_DIR.
    Return path ke file full video yang valid.

    Prioritas 720p AVC merged (tajam untuk facecam). Cache low-res (<720p)
    dari run sebelumnya TIDAK dipakai permanen — tiap run dicoba upgrade
    720p dulu; cache low-res hanya dipakai jika upgrade gagal (403 throttle),
    agar klip tetap jadi walau kurang tajam.
    """
    video_id = _extract_video_id(url)
    cache_path = os.path.join(TMP_DIR, f"full_{video_id}.mp4")
    # staging: download ke file sementara agar cache lama tak rusak jika gagal
    tmp_path = cache_path + ".dl.mp4"

    def _cached_res():
        """Return (w,h) jika cache valid; None jika tidak ada (korup → hapus)."""
        if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100000:
            try:
                return _probe_resolution(cache_path)
            except Exception:
                print("⚠️ Cache korup, download ulang...")
                os.remove(cache_path)
        return None

    with _FULL_DL_LOCK:
        # Double-check setelah dapat lock (thread lain mungkin sudah download)
        res = _cached_res()
        if res and res[1] >= 720:
            print(f"📦 Cache hit: {cache_path} "
                  f"({os.path.getsize(cache_path)/1024/1024:.0f}MB, {res[0]}x{res[1]})")
            return cache_path
        if res:
            print(f"📦 Cache low-res {res[0]}x{res[1]} — coba upgrade 720p...")

        # ── 720p AVC merged: 2 percobaan + cooldown (403 throttle transien) ──
        fmt_720 = ("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]"
                   "/bv*[height<=720]+ba/b[height<=720]")
        for attempt in (1, 2):
            try:
                print(f"   🎯 Mencoba: 720p AVC merged (percobaan {attempt})...")
                subprocess.run(
                    yt_dlp_cmd() + [
                        "--extractor-args", "youtube:player_client=web_embedded",
                        "-f", fmt_720,
                        "--merge-output-format", "mp4",
                        "-o", tmp_path,
                        url
                    ], check=True, timeout=7200  # 2 jam untuk full video
                )
                w, h = _probe_resolution(tmp_path)
                os.replace(tmp_path, cache_path)
                size_gb = os.path.getsize(cache_path) / (1024**3)
                print(f"   ✅ Full video: {w}x{h}, {size_gb:.1f}GB [720p merged]")
                return cache_path
            except Exception as e:
                print(f"   ❌ Gagal (720p, percobaan {attempt}): {str(e)[:80]}")
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
                throttle_pause("full 720p", seconds=10.0)

        # ── 720p gagal: pakai cache low-res lama jika ada (klip tetap jadi) ──
        if res:
            print(f"   ⚠️ 720p gagal — pakai cache low-res {res[0]}x{res[1]} "
                  f"(klip akan kurang tajam)")
            return cache_path

        # ── 360p progressive — jalan terakhir agar klip tetap diproduksi ──
        try:
            print("   🎯 Mencoba: 360p progressive (fallback terakhir)...")
            subprocess.run(
                yt_dlp_cmd() + [
                    "--extractor-args", "youtube:player_client=web_embedded",
                    "-f", "18/best[height<=480]/best",
                    "--merge-output-format", "mp4",
                    "-o", tmp_path,
                    url
                ], check=True, timeout=7200
            )
            os.replace(tmp_path, cache_path)
            w, h = _probe_resolution(cache_path)
            print(f"   ⚠️ Full video: {w}x{h} [360p] — kualitas rendah!")
            return cache_path
        except Exception as e:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise RuntimeError(f"Gagal download full video: {str(e)[:80]}")


def _probe_resolution(path: str) -> tuple[int, int]:
    """Probe resolusi video (width, height) via ffprobe."""
    out = subprocess.check_output([
        FFPROBE_EXE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        path
    ]).decode().strip()
    w, h = out.split(",")
    return int(w), int(h)


def _download_section(url: str, start_sec: float, end_sec: float, output_path: str):
    """
    Download section:
    0. HD byte-range fMP4 (720p/1080p) — jalur cepat OPTIONAL. Sejak 2026
       YouTube CDN menolak byte-range lompat jauh (HTTP 403) untuk banyak
       video & IP → jika gagal (termasuk 403), LANGSUNG fallback, tidak retry.
    1. Full download via web_embedded (sekali per video, cache) + potong lokal —
       jalur andal utama. --download-sections DASH digantung YouTube, tidak dipakai.
    """
    start_str = seconds_to_hhmmss(start_sec)
    end_str = seconds_to_hhmmss(end_sec)
    duration = end_sec - start_sec

    print(f"⚡ Download section {start_str} → {end_str}...")

    # ── STEP 0: HD byte-range fMP4 (opsional, cepat) ──
    from app.services.hd_downloader import download_section_hd
    try:
        download_section_hd(url, start_sec, end_sec, output_path)
        w, h = _probe_resolution(output_path)
        if h < 720:
            raise RuntimeError(f"HD <720p ({w}x{h})")
        return output_path
    except Exception as e:
        if os.path.exists(output_path):
            os.remove(output_path)
        print(f"⚠️ HD byte-range gagal ({str(e)[:70]}) — fallback full download + cut lokal...")

    # ── STEP 1: Full download (sekali per video, cache) + potong lokal ──
    # Download penuh via web_embedded andal; --download-sections DASH digantung
    # YouTube. Potongan lokal stream-copy = instan.
    full_video = _download_full_video(url)
    _cut_section_locally(full_video, start_sec, end_sec, output_path)
    return output_path


def _cut_section_locally(full_path: str, start_sec: float, end_sec: float,
                          output_path: str):
    """Potong section dari full video lokal pakai ffmpeg stream copy (instan)."""
    start_str = seconds_to_hhmmss(start_sec)
    duration = end_sec - start_sec
    subprocess.run([
        FFMPEG_EXE, "-ss", start_str, "-i", full_path,
        "-t", str(duration),
        "-c:v", "copy", "-c:a", "copy",
        "-avoid_negative_ts", "make_zero",
        "-y", output_path
    ], check=True)


# ─────────────────────────────────────────
# WHISPER SUBTITLE HELPER
# ─────────────────────────────────────────

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


# ─────────────────────────────────────────
# SINGLE CLIP PROCESSING
# ─────────────────────────────────────────

def _process_single_clip_full(url: str, start_str: str, end_str: str,
                              mode: str, clip_idx: int) -> dict:
    """
    Download section LANGSUNG via --download-sections (CEPAT) + Whisper + Filter + Compress.
    Fallback ke full download + local cut jika --download-sections gagal.
    """
    start_sec = hhmmss_to_seconds(start_str)
    end_sec = hhmmss_to_seconds(end_str)
    prefix = f"{start_str.replace(':', '')}_{clip_idx}"
    output = os.path.join(OUTPUT_DIR, f"final_tiktok_{prefix}.mp4")

    source_h264 = None
    tmp_merged = None
    srt_file = None

    try:
        # Step 1: Download section LANGSUNG (cepat — hanya 30-60 detik dari video)
        tmp_yt = os.path.join(TMP_DIR, f"yt_dl_{prefix}.mp4")
        _download_section(url, start_sec, end_sec, tmp_yt)
        source_h264 = tmp_yt

        # Step 2: Whisper subtitle (skip untuk mode gaming)
        if mode.lower() != "gaming":
            srt_file = _whisper_subtitle(source_h264, prefix)
        else:
            print(f"🎮 Mode GAMING — skip Whisper subtitle")

        # Step 3: TikTok filter
        tmp_merged = apply_tiktok_filter(source_h264, srt_file, mode, prefix)

        # Step 4: Compress if needed
        final_size = compress_to_target(tmp_merged, output)

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

    # Fail-fast: pastikan sesi cookies member masih valid sebelum kerja berat.
    try:
        from app.services.hd_downloader import check_session_ok
        check_session_ok(url)
    except RuntimeError as e:
        print(f"🍪 {e}")
        return [{
            "status": "error", "file": None, "size_mb": 0,
            "mode": mode, "error": str(e)
        }]

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


def cut_video_task(url: str, start: str, end: str, mode: str = "normal"):
    """Single clip — kompatibilitas backward"""
    segments = [{"start": start, "end": end}]
    results = _cut_video_impl(url, segments, mode)
    if results and results[0].get("status") == "error":
        return {"status": "error", "message": results[0].get("error", "Unknown error")}
    return results[0] if results else {"status": "error", "message": "No results"}


# ─────────────────────────────────────────
# GAMING COMPILATION — 5 klip → 1 video
# ─────────────────────────────────────────

def process_gaming_compilation(url: str, clips: list[dict],
                                facecam_position: str = "btmleft") -> dict:
    """
    Proses N klip gaming:
    1. Parallel --download-sections (2 workers, CEPAT — kurangi burst agar tak kena 403)
    2. Whisper SKIP — mode gaming tanpa auto subtitle
    3. Parallel gaming filter + encode (3 workers)
    4. Concat dengan xfade transitions → 1 video final
    """
    n = len(clips)
    print(f"\n{'='*50}")

    # Fail-fast: pastikan sesi cookies member masih valid sebelum kerja berat.
    try:
        from app.services.hd_downloader import check_session_ok
        check_session_ok(url)
    except RuntimeError as e:
        print(f"🍪 {e}")
        raise
    print(f"🎮 GAMING COMPILATION: {n} klip → 1 video")
    print(f"📐 Facecam position: {facecam_position.upper()}")
    for i, seg in enumerate(clips):
        print(f"  Klip #{i+1}: {seg['start']} → {seg['end']}")
    print(f"{'='*50}\n")

    # Bebaskan disk dari cache run sebelumnya (video INI dilindungi).
    cleanup_full_video_cache(keep_url=url)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = os.path.join(OUTPUT_DIR, f"gaming_compilation_{timestamp}.mp4")

    downloaded = {}   # idx → path
    filtered = {}     # idx → path
    temp_files = []

    try:
        # ═══ STEP 1: Download section SEQUENTIAL (anti-burst) ═══
        # BURST request paralel = pemicu utama throttle 403 GVS. HD byte-range
        # kini mem-cache URL + header sidx + init segment per worker, jadi klip
        # pertama warm-up dan klip berikutnya hanya butuh 2 range request —
        # serial tetap cepat TANPA memicu 403.
        print("⚡ STEP 1: Download section (sequential, anti-burst)...")
        for i, seg in enumerate(clips):
            start_sec = hhmmss_to_seconds(seg["start"])
            end_sec = hhmmss_to_seconds(seg["end"])
            prefix = f"g5_{seg['start'].replace(':', '')}_{i}"
            tmp_yt = os.path.join(TMP_DIR, f"yt_dl_{prefix}.mp4")
            try:
                _download_section(url, start_sec, end_sec, tmp_yt)
                downloaded[i] = tmp_yt
                temp_files.append(tmp_yt)
                print(f"  ✅ Klip #{i+1}: {seg['start']} → {seg['end']} ({os.path.getsize(tmp_yt)/1024/1024:.1f}MB)")
            except Exception as e:
                print(f"  ❌ Klip #{i+1} download GAGAL: {e}")
                if os.path.exists(tmp_yt):
                    os.remove(tmp_yt)

        if len(downloaded) < 1:
            raise RuntimeError("Semua download gagal — tidak bisa melanjutkan")
        print(f"  📊 {len(downloaded)}/{n} berhasil didownload\n")

        # ═══ STEP 1b: RETRY PASS — klip yang gagal (throttle sudah reda) ═══
        # Dilakukan serial (bukan paralel) agar tidak memicu burst request baru.
        failed_idx = [i for i in range(n) if i not in downloaded]
        if failed_idx:
            print(f"🔄 STEP 1b: Retry pass untuk {len(failed_idx)} klip yang gagal...")
            for i in failed_idx:
                seg = clips[i]
                start_sec = hhmmss_to_seconds(seg["start"])
                end_sec = hhmmss_to_seconds(seg["end"])
                prefix = f"g5_{seg['start'].replace(':', '')}_{i}"
                tmp_yt = os.path.join(TMP_DIR, f"yt_dl_{prefix}.mp4")
                for attempt in range(RETRY_PASS_ATTEMPTS):
                    try:
                        _download_section(url, start_sec, end_sec, tmp_yt)
                        downloaded[i] = tmp_yt
                        temp_files.append(tmp_yt)
                        print(f"  ✅ Klip #{i+1} RETRY sukses: {seg['start']} → {seg['end']}"
                              f" ({os.path.getsize(tmp_yt)/1024/1024:.1f}MB)")
                        break
                    except Exception as e:
                        if os.path.exists(tmp_yt):
                            os.remove(tmp_yt)
                        if attempt + 1 < RETRY_PASS_ATTEMPTS:
                            print(f"  ❌ Klip #{i+1} retry {attempt+1}/{RETRY_PASS_ATTEMPTS} gagal: "
                                  f"{str(e)[:60]} — cooldown lalu coba lagi...")
                            throttle_pause("retry pass", seconds=RETRY_PASS_PAUSE_SEC)
                        else:
                            print(f"  ❌ Klip #{i+1} retry pass GAGAL: {e}")
            print(f"  📊 {len(downloaded)}/{n} berhasil didownload (setelah retry pass)\n")

        # ═══ STEP 2: Whisper SKIP — mode gaming tidak pakai auto subtitle ═══
        print("🔇 STEP 2: Whisper SKIP — gaming mode tanpa auto subtitle\n")
        srt_files = {}  # idx → path — selalu kosong untuk gaming

        # ═══ STEP 3: Gaming filter + encode (parallel, 3 workers — CPU bound) ═══
        print("🎨 STEP 3: Gaming filter + encode (parallel)...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for i in sorted(downloaded.keys()):
                prefix = f"g5_{clips[i]['start'].replace(':', '')}_{i}"
                srt = srt_files.get(i)
                fut = pool.submit(
                    apply_gaming_filter,
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
        concat_with_transitions(ordered_paths, output, TRANSISI_SOUND)

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
