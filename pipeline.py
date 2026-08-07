"""Pipeline orkestrasi: download, Whisper subtitle, clip processing, gaming compilation."""

import os
import subprocess
import concurrent.futures
from datetime import datetime
from config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, OUTPUT_DIR, yt_dlp_cmd
from utils import seconds_to_hhmmss, hhmmss_to_seconds
from subtitles import transcribe_audio, create_ass_from_whisper
from filters import apply_tiktok_filter, apply_gaming_filter
from transitions import concat_with_transitions, compress_to_target


# ─────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────

# ─────────────────────────────────────────
# FULL VIDEO CACHE — download sekali, potong lokal
# ─────────────────────────────────────────

import re as _re
import hashlib

def _extract_video_id(url: str) -> str:
    """Extract YouTube video ID dari URL."""
    for pat in [r'(?:youtube\.com/live/|youtube\.com/watch\?v=|youtu\.be/)([a-zA-Z0-9_-]{11})']:
        m = _re.search(pat, url)
        if m:
            return m.group(1)
    return hashlib.md5(url.encode()).hexdigest()[:12]


def _download_full_video(url: str) -> str:
    """
    Download FULL video SEKALI dengan format DASH HD → cache di TMP_DIR.
    Return path ke file full video yang sudah di-download.
    Full download (tanpa --download-sections) → standard yt-dlp code path,
    lebih reliable daripada --download-sections untuk DASH.
    """
    video_id = _extract_video_id(url)
    cache_path = os.path.join(TMP_DIR, f"full_{video_id}.mp4")

    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 100000:
        print(f"📦 Cache hit: {cache_path} ({os.path.getsize(cache_path)/1024/1024:.0f}MB)")
        return cache_path

    print(f"📥 Download FULL video (sekali untuk semua klip)...")
    # Cascade: 720p60 DASH → 480p DASH → best merged
    FORMAT_CASCADE = [
        ("298+140", "720p60 DASH"),
        ("135+140", "480p30 DASH"),
        ("best",     "merged"),
    ]

    for fmt, label in FORMAT_CASCADE:
        try:
            print(f"   🎯 Mencoba: {label} ({fmt})...")
            subprocess.run(
                yt_dlp_cmd() + [
                    "-f", fmt,
                    "--merge-output-format", "mp4",
                    "-o", cache_path,
                    url
                ], check=True, timeout=7200  # 2 jam untuk full video
            )

            # Cek resolusi
            probe = subprocess.check_output([
                FFPROBE_EXE, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
                cache_path
            ]).decode().strip()
            w, h = probe.split(",")
            size_gb = os.path.getsize(cache_path) / (1024**3)
            print(f"   ✅ Full video: {w}x{h}, {size_gb:.1f}GB [{label}]")
            if int(w) < 1280:
                print(f"   ⚠️ {w}x{h} — facecam akan kurang tajam!")
            return cache_path
        except Exception as e:
            print(f"   ❌ Gagal ({label}): {e}")
            if os.path.exists(cache_path):
                os.remove(cache_path)
            continue

    raise RuntimeError("Gagal download full video — semua format gagal")


def _download_section(url: str, start_sec: float, end_sec: float, output_path: str):
    """
    Download section 720p HD (~5-15 detik).
    1. Format 22 (720p merged) via --download-sections — tercepat
    2. DASH 720p via direct URL + ffmpeg remote seek
    3. Fallback full download
    """
    start_str = seconds_to_hhmmss(start_sec)
    end_str = seconds_to_hhmmss(end_sec)
    duration = end_sec - start_sec

    print(f"⚡ Download section {start_str} → {end_str}...")

    # ── STEP 1: Format 22 (720p merged) via --download-sections ──
    try:
        subprocess.run(
            yt_dlp_cmd() + [
                "--download-sections", f"*{start_str}-{end_str}",
                "-f", "22/best[height<=720]/best",
                "--merge-output-format", "mp4",
                "-o", output_path,
                url
            ], check=True, timeout=120
        )
        h = int(subprocess.check_output([
            FFPROBE_EXE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=height",
            "-of", "csv=p=0", output_path
        ]).decode().strip())
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"✅ 720p merged: {h}p, {size_mb:.1f}MB")
        return output_path
    except Exception as e:
        print(f"⚠️ Format 22 tidak tersedia ({str(e)[:60]}), coba DASH...")
        if os.path.exists(output_path):
            os.remove(output_path)

    # ── STEP 2: DASH 720p via direct URL + ffmpeg remote seek ──
    try:
        print("🔗 Direct DASH 720p URL...")
        raw = subprocess.check_output(
            yt_dlp_cmd() + ["-f", "bestvideo[height<=720]+bestaudio", "-g", url],
            text=True, timeout=60
        ).strip()
        urls = [u for u in raw.split('\n') if u.startswith('http')]

        print(f"🎬 ffmpeg remote seek...")
        subprocess.run([
            FFMPEG_EXE,
            "-ss", start_str, "-i", urls[0],
            "-ss", start_str, "-i", urls[1],
            "-t", str(duration),
            "-c:v", "copy", "-c:a", "copy",
            "-avoid_negative_ts", "make_zero",
            output_path, "-y"
        ], check=True, timeout=120)

        h = int(subprocess.check_output([
            FFPROBE_EXE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=height",
            "-of", "csv=p=0", output_path
        ]).decode().strip())
        size_mb = os.path.getsize(output_path) / (1024 * 1024)
        print(f"✅ DASH 720p: {h}p, {size_mb:.1f}MB")
        return output_path

    except Exception as e:
        print(f"⚠️ DASH gagal ({str(e)[:60]}), fallback full download...")
        if os.path.exists(output_path):
            os.remove(output_path)

    # ── STEP 3: Full download + local cut ──
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
    1. Parallel --download-sections (3 workers, CEPAT)
    2. Whisper SKIP — mode gaming tanpa auto subtitle
    3. Parallel gaming filter + encode (3 workers)
    4. Concat dengan xfade transitions → 1 video final
    """
    n = len(clips)
    print(f"\n{'='*50}")
    print(f"🎮 GAMING COMPILATION: {n} klip → 1 video")
    print(f"📐 Facecam position: {facecam_position.upper()}")
    for i, seg in enumerate(clips):
        print(f"  Klip #{i+1}: {seg['start']} → {seg['end']}")
    print(f"{'='*50}\n")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = os.path.join(OUTPUT_DIR, f"gaming_compilation_{timestamp}.mp4")

    downloaded = {}   # idx → path
    filtered = {}     # idx → path
    temp_files = []

    try:
        # ═══ STEP 1: Parallel download section LANGSUNG (CEPAT) ═══
        print("⚡ STEP 1: Parallel download section (3 workers)...")
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=3) as pool:
            futures = {}
            for i, seg in enumerate(clips):
                start_sec = hhmmss_to_seconds(seg["start"])
                end_sec = hhmmss_to_seconds(seg["end"])
                prefix = f"g5_{seg['start'].replace(':', '')}_{i}"
                tmp_yt = os.path.join(TMP_DIR, f"yt_dl_{prefix}.mp4")

                fut = pool.submit(_download_section, url, start_sec, end_sec, tmp_yt)
                futures[fut] = (i, seg, tmp_yt)

            for fut in cf.as_completed(futures):
                i, seg, tmp_yt = futures[fut]
                try:
                    fut.result()
                    downloaded[i] = tmp_yt
                    temp_files.append(tmp_yt)
                    print(f"  ✅ Klip #{i+1}: {seg['start']} → {seg['end']} ({os.path.getsize(tmp_yt)/1024/1024:.1f}MB)")
                except Exception as e:
                    print(f"  ❌ Klip #{i+1} download GAGAL: {e}")

        if len(downloaded) < 1:
            raise RuntimeError("Semua download gagal — tidak bisa melanjutkan")
        print(f"  📊 {len(downloaded)}/{n} berhasil didownload\n")

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
        concat_with_transitions(ordered_paths, output)

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
