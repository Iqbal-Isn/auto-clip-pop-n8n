"""Pipeline orkestrasi: download, Whisper subtitle, clip processing, gaming compilation."""

import os
import subprocess
import concurrent.futures
from config import FFMPEG_EXE, TMP_DIR, OUTPUT_DIR, yt_dlp_cmd
from utils import seconds_to_hhmmss, hhmmss_to_seconds
from subtitles import transcribe_audio, create_ass_from_whisper
from filters import apply_tiktok_filter, apply_gaming_pip_filter
from transitions import concat_with_transitions, compress_to_target


# ─────────────────────────────────────────
# DOWNLOAD
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
                    apply_gaming_pip_filter,
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
