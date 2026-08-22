"""Video filters: facecam crop, TikTok layout, gaming filter."""

import os
import subprocess
from app.config import FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, FACECAM_POSITIONS, WATERMARK_PATH, FACECAM_OUTPUT_SIZE
from app.services.face_detector import find_largest_face_in_corners, calculate_face_crop

# Cap thread per encode agar 3 worker paralel tidak saling cekik (oversubscription).
# 3 worker × ENCODE_THREADS ≈ jumlah core logis → utilisasi penuh tanpa thrashing.
ENCODE_THREADS = str(max(2, (os.cpu_count() or 6) // 3))


def get_facecam_crop(video_path: str, position: str = "btmleft",
                     auto_detect: bool = True,
                     target_aspect: float | None = None,
                     region_scale: float = 1.0):
    """
    Deteksi posisi & ukuran facecam.

    Auto-detect (default):
      - Cari wajah terbesar di 8 region facecam (YuNet)
      - Default: crop persegi centered pada wajah (FACECAM_OUTPUT_SIZE)
      - Jika target_aspect diisi (w/h): crop rectangle berasio target,
        centered pada wajah, clamp dalam region facecam
      - region_scale > 1 memperluas region deteksi (dipakai GAMING5 agar
        upscale facecam tidak terlalu besar)
      - Kalau gagal -> fallback ke posisi manual

    Manual (fallback):
      - Default: crop region 22%x30% dari frame sesuai FACECAM_POSITIONS
      - Jika target_aspect diisi: crop rectangle berasio target di region itu

    Returns (crop_x, crop_y, crop_w, crop_h, corner, is_auto)
      - corner: "btmright" | "btmleft" | "topleft" | "topright" | edge-mid
      - is_auto: True jika hasil auto-deteksi, False jika fallback
    """
    # -- ffprobe: resolusi video --
    result = subprocess.check_output([
        FFPROBE_EXE, "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        video_path
    ]).decode().strip().split(",")

    vid_w = int(result[0])
    vid_h = int(result[1])
    print(f"[facecam] Video: {vid_w}x{vid_h}")

    def _region_from_pos(pos_name: str, scale: float = 1.0):
        region_w = min(vid_w, int(vid_w * 0.22 * scale))
        region_h = min(vid_h, int(vid_h * 0.30 * scale))
        pos = FACECAM_POSITIONS.get(pos_name, FACECAM_POSITIONS["btmleft"])
        if pos["x"] == "right":
            region_x = vid_w - region_w
        elif pos["x"] == "middle":
            region_x = (vid_w - region_w) // 2
        else:
            region_x = 0
        if pos["y"] == "bottom":
            region_y = vid_h - region_h
        elif pos["y"] == "middle":
            region_y = (vid_h - region_h) // 2
        else:
            region_y = 0
        return region_x, region_y, region_w, region_h

    def _fit_crop(region_x, region_y, region_w, region_h,
                  center_x=None, center_y=None):
        if target_aspect is None:
            crop_w = min(FACECAM_OUTPUT_SIZE, region_w, region_h)
            crop_h = crop_w
        else:
            crop_w = region_w
            crop_h = int(round(crop_w / target_aspect))
            if crop_h > region_h:
                crop_h = region_h
                crop_w = int(round(crop_h * target_aspect))

        if center_x is None:
            center_x = region_x + region_w // 2
        if center_y is None:
            center_y = region_y + region_h // 2

        cx = int(round(center_x - crop_w / 2))
        cy = int(round(center_y - crop_h / 2))
        cx = max(cx, region_x)
        cy = max(cy, region_y)
        if cx + crop_w > region_x + region_w:
            cx = region_x + region_w - crop_w
        if cy + crop_h > region_y + region_h:
            cy = region_y + region_h - crop_h
        return cx, cy, crop_w, crop_h

    # === 1. Auto face detection ===
    if auto_detect:
        face_result = find_largest_face_in_corners(video_path)
        if face_result:
            fx, fy, fw, fh = face_result["face_bbox"]
            corner = face_result["corner"]
            region_x, region_y, region_w, region_h = _region_from_pos(corner, region_scale)

            face_cx = fx + fw // 2
            face_cy = fy + fh // 2
            crop = _fit_crop(region_x, region_y, region_w, region_h,
                             center_x=face_cx, center_y=face_cy)

            print(f"[facecam] Auto facecam -> pojok {corner}, "
                  f"region=({region_x},{region_y},{region_w},{region_h}), "
                  f"crop={crop}, target_aspect={target_aspect}")
            return (*crop, corner, True)

    # === 2. Fallback: posisi manual ===
    cam_x, cam_y, cam_w, cam_h = _region_from_pos(position, region_scale)
    if target_aspect is not None:
        cam_x, cam_y, cam_w, cam_h = _fit_crop(cam_x, cam_y, cam_w, cam_h)

    print(f"[facecam] Fallback ({position}) -> x={cam_x} y={cam_y} "
          f"w={cam_w} h={cam_h} target_aspect={target_aspect}")
    return (cam_x, cam_y, cam_w, cam_h, position, False)


def apply_gaming_filter(video_path: str, srt_file: str | None,
                        facecam_position: str, prefix: str) -> str:
    """
    Gaming compilation layout (GAMING5):
    - Output portrait 1080×1920
    - Facecam 40% bagian atas: 1080×768, crop mengikuti auto deteksi wajah
    - Gameplay 60% bagian bawah: 1080×1152
    - Watermark 150px di tengah hasil akhir (jika watermark.png tersedia)
    - Subtitle dibakar di atas komposit (jika ada)

    MINIMAL PROCESSING: light denoise → high-quality scale → light sharpen.
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    # Area facecam baru: 1080×768 = 40% dari 1920. Rasio ini dipakai agar
    # crop sumber tidak distretch dan wajah tidak terpotong berlebihan.
    facecam_aspect = 1080 / 768
    # Perluas region deteksi 1.6× per dimensi (area ±2.6×) agar crop wajah
    # untuk area 1080×768 tidak terlalu kecil lalu di-upscale berlebihan.
    # Makin besar region_scale = makin kecil rasio upscale = facecam makin tajam.
    facecam_region_scale = 1.6
    cx, cy, cw, ch, detected_corner, is_auto = get_facecam_crop(
        video_path, facecam_position,
        target_aspect=facecam_aspect,
        region_scale=facecam_region_scale
    )
    cam_source = f"auto:{detected_corner}" if is_auto else f"fallback:{facecam_position}"
    print(f"[gaming5] Facecam source={cam_source}, crop=({cx},{cy},{cw},{ch})")

    # ASS subtitle chain
    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    # Cek apakah watermark tersedia
    wm_available = os.path.exists(WATERMARK_PATH)
    if wm_available:
        print(f"[watermark] {WATERMARK_PATH} (150px, tengah hasil akhir)")

    # Build filter_complex: facecam 40% atas + gameplay 60% bawah.
    video_filter = (
        f"[0:v]split=2[cam_src][game_src];"
        # Facecam: crop di sekitar wajah → denoise ringan → scale ke target
        # → sharpen SETELAH scale (kunci anti-buram). Tidak ada upscale→downscale
        # karena sharpening di resolusi intermediate akan hilang saat downsample.
        f"[cam_src]crop={cw}:{ch}:{cx}:{cy},"
        f"hqdn3d=1:1:2:1,"
        f"scale=1080:768:flags=lanczos+accurate_rnd,"
        f"unsharp=7:7:1.2:7:7:0.5,"
        f"cas=0.7,"
        f"setsar=1[top];"
        # Gameplay: isi penuh area bawah 1080×1152.
        f"[game_src]scale=1080:1152:force_original_aspect_ratio=increase:flags=lanczos+accurate_rnd,"
        f"crop=1080:1152,setsar=1[bottom];"
        f"[top][bottom]vstack=inputs=2[stacked];"
    )

    # Watermark: scale 150px → overlay tepat di tengah hasil akhir 1080×1920.
    if wm_available:
        video_filter += (
            f"[1:v]scale=150:-1:flags=lanczos,setsar=1[wm];"
            f"[stacked][wm]overlay=(W-w)/2:(H-h)/2[with_wm];"
            f"[with_wm]fps=30,setpts=PTS-STARTPTS[comp_cfr];"
        )
    else:
        video_filter += (
            f"[stacked]fps=30,setpts=PTS-STARTPTS[comp_cfr];"
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
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-threads", ENCODE_THREADS,
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ]

    subprocess.run(cmd, check=True)

    return tmp_merged


def apply_tiktok_filter(video_path: str, srt_file: str | None,
                        mode: str, prefix: str) -> str:
    """
    Apply TikTok filter normal + subtitle burn-in.
    `mode` dipertahankan untuk kompatibilitas lama; layout gaming lama sudah dihapus.
    Return path ke file hasil filter (tmp_merged).
    """
    tmp_merged = os.path.join(TMP_DIR, f"merged_{prefix}.mp4")

    sub_chain = ""
    if srt_file:
        srt_escaped = srt_file.replace('\\', '/').replace(':', '\\:')
        sub_chain = f"ass='{srt_escaped}'"

    if mode.lower() != "normal":
        print(f"[tiktok] Mode {mode} sudah tidak didukung; pakai layout NORMAL ({prefix})")

    print(f"[tiktok] Mode NORMAL ({prefix}) - blur background + foreground + subtitle...")
    # Foreground anti-buram: denoise ringan → scale lanczos → unsharp + cas.
    # Kompensasi upscale sumber (720p/360p) ke 1080×1080.
    if srt_file:
        video_filter = (
            "[0:v]split[orig_raw][bg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=spline,crop=1080:1920,boxblur=15:15[blurred];"
            "[orig_raw]hqdn3d=2:1:3:2,"
            "scale=1080:1080:force_original_aspect_ratio=increase:flags=lanczos+accurate_rnd,"
            "crop=1080:1080,"
            "unsharp=5:5:1.0:5:5:0.5,"
            "cas=0.3,"
            "setsar=1[scaled];"
            f"[scaled]{sub_chain}[fg];"
            "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )
    else:
        video_filter = (
            "[0:v]split[orig][bg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase:flags=spline,crop=1080:1920,boxblur=15:15[blurred];"
            "[orig]hqdn3d=2:1:3:2,"
            "scale=1080:1080:force_original_aspect_ratio=increase:flags=lanczos+accurate_rnd,"
            "crop=1080:1080,"
            "unsharp=5:5:1.0:5:5:0.5,"
            "cas=0.3,"
            "setsar=1[fg];"
            "[blurred][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )

    # TikTok filter jalan SEQUENTIAL (satu klip per waktu) → biarkan pakai semua core.
    subprocess.run([
        FFMPEG_EXE, "-i", video_path,
        "-filter_complex", video_filter,
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-crf", "20", "-preset", "fast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        tmp_merged, "-y"
    ], check=True)

    return tmp_merged
