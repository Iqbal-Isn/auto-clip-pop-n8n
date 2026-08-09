"""Video filters: facecam crop, TikTok layout, gaming filter."""

import os
import subprocess
from config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, FACECAM_POSITIONS, WATERMARK_PATH


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


def apply_gaming_filter(video_path: str, srt_file: str | None,
                        facecam_position: str, prefix: str) -> str:
    """
    Gaming layout:
    - Background blur 1080×1920 (dari gameplay yang sama)
    - Gameplay 4:3 (1080×810) centered, tidak blur
    - Watermark 150px di tengah gameplay box (jika wm_mk.png tersedia)
    - Facecam overlay 360px di pojok gameplay box (nempel tepi, di atas watermark)
    - Subtitle dibakar di atas komposit (jika ada)

    MINIMAL PROCESSING: light denoise → high-quality scale → light sharpen.
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    cx, cy, cw, ch = get_facecam_crop(video_path, facecam_position)
    cam_pos = FACECAM_POSITIONS.get(facecam_position, FACECAM_POSITIONS["btmright"])

    # ⚙️ Ukuran display facecam (bisa diedit)
    FACECAM_SIZE = "360"

    # Posisi overlay facecam di dalam gameplay box (1080×810) — nempel tepi
    overlay_x = "W-w" if cam_pos["x"] == "right" else "0"
    overlay_y = "H-h" if cam_pos["y"] == "bottom" else "0"

    # ASS subtitle chain
    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    # Cek apakah watermark tersedia
    wm_available = os.path.exists(WATERMARK_PATH)
    if wm_available:
        print(f"🔒 Watermark: {WATERMARK_PATH} (150px, tengah gameplay 4:3)")

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
    )

    # Watermark: scale 150px → overlay di tengah gameplay 4:3 (sebelum facecam)
    if wm_available:
        video_filter += (
            f"[1:v]scale=150:-1:flags=lanczos,setsar=1[wm];"
            f"[fg][wm]overlay=(W-w)/2:(H-h)/2[fg_with_wm];"
            f"[fg_with_wm][cam]overlay={overlay_x}:{overlay_y}[fg_with_cam];"
        )
    else:
        video_filter += (
            f"[fg][cam]overlay={overlay_x}:{overlay_y}[fg_with_cam];"
        )

    video_filter += (
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

    # Build ffmpeg command
    cmd = [
        FFMPEG_EXE, "-fflags", "+genpts", "-i", video_path,
    ]
    if wm_available:
        cmd += ["-i", WATERMARK_PATH]
    cmd += [
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "17", "-preset", "slower", "-pix_fmt", "yuv420p",
        "-tune", "film",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ]

    subprocess.run(cmd, check=True)

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
