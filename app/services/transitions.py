"""Concat with transitions (black screen + sound) & compress to target size."""

import os
import subprocess
from app.config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR


def _get_duration(path: str) -> float:
    """Dapatkan durasi video (detik) via ffprobe."""
    return float(subprocess.check_output([
        FFPROBE_EXE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path
    ]).decode().strip())


def _get_resolution(path: str) -> tuple[int, int]:
    """Dapatkan resolusi video (width, height)."""
    w, h = subprocess.check_output([
        FFPROBE_EXE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        path
    ]).decode().strip().split(",")
    return int(w), int(h)


def _generate_black_transition(sound_path: str, output_path: str,
                                width: int, height: int) -> str:
    """
    Generate layar hitam + sound effect.
    Durasi otomatis mengikuti durasi file sound.
    """
    # Deteksi durasi sound
    sound_dur = _get_duration(sound_path)
    # Bulatkan ke 1 desimal (0.7, bukan 0.696599...)
    dur = round(sound_dur, 1)

    subprocess.run([
        FFMPEG_EXE,
        "-f", "lavfi", "-i",
        f"color=c=black:s={width}x{height}:d={dur}:r=30",
        "-i", sound_path,
        "-filter_complex",
        f"[1:a]atrim=0:{dur},asetpts=PTS-STARTPTS[a]",
        "-map", "0:v", "-map", "[a]",
        "-c:v", "libx264", "-crf", "18", "-preset", "ultrafast",
        "-c:a", "aac", "-b:a", "128k",
        "-t", str(dur),
        output_path, "-y"
    ], check=True)

    return output_path


def concat_with_transitions(clip_paths: list[str], output: str,
                             transition_sound: str | None = None) -> str:
    """
    Gabungkan N klip:
    - Jika ada transition_sound: sisipkan layar hitam 1 detik + sound antar klip
    - Jika tidak: concat langsung tanpa transisi
    """
    n = len(clip_paths)
    if n == 0:
        raise ValueError("Tidak ada klip untuk digabung")
    if n == 1:
        os.rename(clip_paths[0], output)
        return output

    print(f"\n🎬 CONCAT: {n} klip → {output}")

    # Dapatkan resolusi dari klip pertama
    width, height = _get_resolution(clip_paths[0])

    # Hitung durasi
    total_dur = 0.0
    for i, p in enumerate(clip_paths):
        d = _get_duration(p)
        total_dur += d
        print(f"  Klip #{i+1}: {d:.1f}s")

    # Generate transition segment (reusable)
    transition_path = None
    if transition_sound and os.path.exists(transition_sound):
        transition_path = os.path.join(TMP_DIR, "transition_black.mp4")
        print(f"🔊 Generate transisi hitam + sound ({transition_sound})...")
        _generate_black_transition(transition_sound, transition_path,
                                   width, height)
        transition_dur = _get_duration(transition_path)
        total_dur += transition_dur * (n - 1)
        print(f"  Transisi: {transition_dur:.1f}s × {n - 1} = {transition_dur * (n - 1):.1f}s")

    print(f"  Total durasi: ~{total_dur:.1f}s ({total_dur / 60:.1f} menit)")

    # Build segment list: clip1, transition, clip2, transition, clip3, ...
    segments = []
    for i, path in enumerate(clip_paths):
        segments.append(path)
        if i < n - 1 and transition_path:
            segments.append(transition_path)

    # Concat filter — gabungkan semua segment
    seg_count = len(segments)
    concat_parts = ""
    for i in range(seg_count):
        concat_parts += f"[{i}:v][{i}:a]"
    concat_filter = f"{concat_parts}concat=n={seg_count}:v=1:a=1[v][a]"

    inputs = []
    for p in segments:
        inputs += ["-i", p]

    subprocess.run([
        FFMPEG_EXE,
        *inputs,
        "-filter_complex", concat_filter,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        output, "-y"
    ], check=True)

    # Cleanup
    if transition_path and os.path.exists(transition_path):
        os.remove(transition_path)

    final_size = os.path.getsize(output) / (1024 * 1024)
    print(f"✅ Concat selesai: {output} ({final_size:.1f}MB)")
    return output


def compress_to_target(source: str, output: str, target_mb: float = 45):
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
