"""Video filters: facecam crop, TikTok layout, gaming 50/50 PiP."""

import os
import subprocess
from config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, FACECAM_POSITIONS


def get_facecam_crop(video_path: str, position: str = "btmleft"):
    """Deteksi posisi & ukuran facecam berdasarkan resolusi video."""
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

    cam_w = int(vid_w * 0.22)
    cam_h = int(vid_h * 0.30)

    pos = FACECAM_POSITIONS.get(position, FACECAM_POSITIONS["btmleft"])
    cam_x = vid_w - cam_w if pos["x"] == "right" else 0
    cam_y = vid_h - cam_h if pos["y"] == "bottom" else 0

    print(f"✅ Facecam ({position}) → x={cam_x} y={cam_y} w={cam_w} h={cam_h}")
    return (cam_x, cam_y, cam_w, cam_h)


def apply_gaming_pip_filter(video_path: str, srt_file: str | None,
                            facecam_position: str, prefix: str) -> str:
    """
    Gaming 50/50 split filter:
    - Top 50% (1080×960): Facecam crop → denoise → upscale → sharpen
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
        # Facecam (TOP 50%): crop → denoise → high-quality upscale → sharpen
        f"[cam_raw]crop={cw}:{ch}:{cx}:{cy},"
        f"hqdn3d=3:2:5:3,"
        f"scale=1080:960:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop=1080:960,"
        f"unsharp=5:5:1.0:5:5:0.5,"
        f"cas=0.4,"
        f"setsar=1[top];"
        # Gameplay (BOTTOM 50%): full frame → scale ke 1080×960
        f"[main]scale=1080:960:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop=1080:960,setsar=1[bottom];"
        # Stack: facecam atas, gameplay bawah
        f"[top][bottom]vstack=inputs=2[comp];"
        # Force CFR + normalize PTS → mencegah frame drop saat xfade concat
        f"[comp]fps=30,setpts=PTS-STARTPTS[comp_cfr];"
    )

    # Subtitle (jika ada)
    if srt_file:
        video_filter += f"[comp_cfr]{sub_chain}[v]"
    else:
        video_filter += f"[comp_cfr]null[v]"

    subprocess.run([
        FFMPEG_EXE, "-fflags", "+genpts", "-i", video_path,
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ], check=True)

    return tmp_merged


def apply_gaming_pip_overlay_filter(video_path: str, srt_file: str | None,
                                    facecam_position: str, prefix: str) -> str:
    """
    Gaming PiP overlay filter — facecam sebagai overlay kecil di pojok:
    - Gameplay fullscreen 1080×1920 (crop vertical from horizontal)
    - Facecam crop 22%×30% → overlay di pojok (500px lebar)
    - Subtitle dibakar di atas semuanya

    Crop lebih kecil + upscale lanczos = streamer lebih besar & tajam.
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    cx, cy, cw, ch = get_facecam_crop(video_path, facecam_position)
    cam_pos = FACECAM_POSITIONS.get(facecam_position, FACECAM_POSITIONS["btmleft"])

    # ⚙️ Ukuran display facecam (bisa diedit)
    FACECAM_SIZE = "500"

    # Posisi overlay facecam (20px margin dari tepi)
    overlay_x = "W-w-20" if cam_pos["x"] == "right" else "20"
    overlay_y = "H-h-20" if cam_pos["y"] == "bottom" else "20"

    # ASS subtitle chain
    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    # Build filter_complex
    video_filter = (
        # Split source: main gameplay + facecam
        f"[0:v]split[main][cam_raw];"
        # Gameplay: scale ke 1080×1920 (crop horizontal → vertical)
        f"[main]scale=1080:1920:flags=lanczos:force_original_aspect_ratio=increase,"
        f"crop=1080:1920,setsar=1[bg];"
        # Facecam: crop → denoise → upscale lanczos → sharpen
        f"[cam_raw]crop={cw}:{ch}:{cx}:{cy},"
        f"hqdn3d=3:2:5:3,"
        f"scale={FACECAM_SIZE}:-1:flags=lanczos,"
        f"unsharp=5:5:1.0:5:5:0.5,"
        f"cas=0.3,"
        f"setsar=1[cam];"
        # Overlay facecam di pojok gameplay
        f"[bg][cam]overlay={overlay_x}:{overlay_y}[comp];"
        # Force CFR + normalize PTS
        f"[comp]fps=30,setpts=PTS-STARTPTS[comp_cfr];"
    )

    # Subtitle (jika ada)
    if srt_file:
        video_filter += f"[comp_cfr]{sub_chain}[v]"
    else:
        video_filter += f"[comp_cfr]null[v]"

    subprocess.run([
        FFMPEG_EXE, "-fflags", "+genpts", "-i", video_path,
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ], check=True)

    return tmp_merged


def apply_gaming_normal_overlay_filter(video_path: str, srt_file: str | None,
                                        facecam_position: str, prefix: str) -> str:
    """
    Gaming "Normal" layout:
    - Background blur 1080×1920 (dari gameplay yang sama)
    - Gameplay 4:3 (1080×810) centered, tidak blur
    - Facecam overlay 480px di pojok gameplay box (nempel tepi)
    - Subtitle dibakar di atas komposit

    MINIMAL PROCESSING: light denoise → high-quality scale → light sharpen.
    Prinsip: source jelek + proses berat = makin blur. Lebih baik simpel.
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    cx, cy, cw, ch = get_facecam_crop(video_path, facecam_position)
    cam_pos = FACECAM_POSITIONS.get(facecam_position, FACECAM_POSITIONS["btmright"])

    # ⚙️ Ukuran display facecam (bisa diedit)
    FACECAM_SIZE = "480"

    # Posisi overlay facecam di dalam gameplay box (1080×810) — nempel tepi
    overlay_x = "W-w" if cam_pos["x"] == "right" else "0"
    overlay_y = "H-h" if cam_pos["y"] == "bottom" else "0"

    # ASS subtitle chain
    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    # Build filter_complex
    video_filter = (
        # Split source ke 3 stream: gameplay, facecam raw, background
        f"[0:v]split=3[main][cam_raw][bg_raw];"
        # Background: scale → blur penuh 1080×1920
        f"[bg_raw]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:1920,boxblur=15:15,setsar=1[bg];"
        # Gameplay 4:3: scale ke 1080×810, centered (tidak blur)
        f"[main]scale=1080:810:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop=1080:810,setsar=1[fg];"
        # Facecam: MINIMAL — light denoise → scale → light sharpen
        f"[cam_raw]crop={cw}:{ch}:{cx}:{cy},"
        f"scale={FACECAM_SIZE}:-1:flags=lanczos+accurate_rnd,"
        f"unsharp=3:3:0.6:3:3:0.4,"
        f"cas=0.2,"
        f"setsar=1[cam];"
        # Overlay facecam di pojok gameplay box (bukan di canvas)
        f"[fg][cam]overlay={overlay_x}:{overlay_y}[fg_with_cam];"
        # Overlay gameplay+facecam di tengah background blur
        f"[bg][fg_with_cam]overlay=(W-w)/2:(H-h)/2[comp];"
        # Force CFR + normalize PTS
        f"[comp]fps=30,setpts=PTS-STARTPTS[comp_cfr];"
    )

    # Subtitle (jika ada)
    if srt_file:
        video_filter += f"[comp_cfr]{sub_chain}[v]"
    else:
        video_filter += f"[comp_cfr]null[v]"

    subprocess.run([
        FFMPEG_EXE, "-fflags", "+genpts", "-i", video_path,
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "17", "-preset", "slower", "-pix_fmt", "yuv420p",
        "-tune", "film",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ], check=True)

    return tmp_merged


def apply_tiktok_filter(video_path: str, srt_file: str | None,
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
                f"hqdn3d=3:2:5:3,"
                f"scale=1080:960:flags=lanczos:force_original_aspect_ratio=increase,"
                f"crop=1080:960,"
                f"unsharp=5:5:1.0:5:5:0.5,"
                f"cas=0.3,"
                f"setsar=1[top];"
                f"[subbed]scale=1080:960:flags=lanczos:force_original_aspect_ratio=increase,"
                f"crop=1080:960,setsar=1[bottom];"
                f"[top][bottom]vstack=inputs=2[v]"
            )
        else:
            video_filter = (
                f"[0:v]crop={cw}:{ch}:{cx}:{cy},"
                f"hqdn3d=3:2:5:3,"
                f"scale=1080:960:flags=lanczos:force_original_aspect_ratio=increase,"
                f"crop=1080:960,"
                f"unsharp=5:5:1.0:5:5:0.5,"
                f"cas=0.3,"
                f"setsar=1[top];"
                f"[0:v]scale=1080:960:flags=lanczos:force_original_aspect_ratio=increase,"
                f"crop=1080:960,setsar=1[bottom];"
                f"[top][bottom]vstack=inputs=2[v]"
            )
    else:
        print(f"📺 Mode NORMAL ({prefix}) — blur background + foreground + subtitle...")
        if srt_file:
            video_filter = (
                "[0:v]split[orig_raw][bg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=spline,crop=1080:1920,boxblur=15:15[blurred];"
                f"[orig_raw]scale=1080:1080:force_original_aspect_ratio=increase:flags=spline,crop=1080:1080[scaled];"
                f"[scaled]{sub_chain}[fg];"
                "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
            )
        else:
            video_filter = (
                "[0:v]split[orig][bg];"
                "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=spline,crop=1080:1920,boxblur=15:15[blurred];"
                "[orig]scale=1080:1080:force_original_aspect_ratio=increase:flags=spline,crop=1080:1080[fg];"
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
