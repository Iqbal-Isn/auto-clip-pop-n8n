"""Concat with transitions (xfade/acrossfade) & compress to target size."""

import os
import subprocess
from config import FFMPEG_EXE, FFPROBE_EXE

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


def concat_with_transitions(clip_paths: list[str], output: str) -> str:
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
