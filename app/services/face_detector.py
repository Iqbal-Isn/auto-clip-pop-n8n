"""
Face detection with OpenCV YuNet (DNN) — auto-detect streamer's face for facecam crop.

YuNet is a lightweight CNN-based face detector built into OpenCV.
Jauh lebih akurat dari Haar Cascade, khususnya untuk wajah kecil (<50px).
Model ONNX ~350KB, auto-download.
"""

import os
import subprocess
import threading
import cv2
import numpy as np
from app.config import (
    FFMPEG_EXE, FFPROBE_EXE, TMP_DIR, ASSETS_DIR,
    FACECAM_OUTPUT_SIZE, FACE_DETECTION_SAMPLES
)

# Thread lock — YuNet tidak thread-safe untuk concurrent detect()
_yunet_lock = threading.Lock()

# ─────────────────────────────────────────
# YUNET MODEL (auto-download jika belum ada)
# ─────────────────────────────────────────
_YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
_YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/"
    "models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
)
_YUNET_PATH = os.path.join(ASSETS_DIR, "models", _YUNET_FILENAME)

_yunet = None


def _ensure_yunet_model():
    """Download YuNet ONNX model jika belum ada."""
    if not os.path.exists(_YUNET_PATH):
        print(f"[face_detect] Downloading YuNet ONNX model "
              f"({_YUNET_FILENAME})...")
        try:
            import urllib.request
            os.makedirs(os.path.dirname(_YUNET_PATH), exist_ok=True)
            urllib.request.urlretrieve(_YUNET_URL, _YUNET_PATH)
            size_kb = os.path.getsize(_YUNET_PATH) / 1024
            print(f"[face_detect] Saved {_YUNET_FILENAME} ({size_kb:.0f} KB)")
        except Exception as e:
            print(f"[face_detect] GAGAL download YuNet: {e}")
            raise RuntimeError(
                f"YuNet model tidak ditemukan dan gagal didownload.\n"
                f"Download manual dari:\n  {_YUNET_URL}\n"
                f"Simpan ke:\n  {_YUNET_PATH}"
            )


def _get_yunet():
    """Init YuNet FaceDetectorYN sekali pakai (lazy load)."""
    global _yunet
    if _yunet is None:
        _ensure_yunet_model()
        _yunet = cv2.FaceDetectorYN.create(
            _YUNET_PATH, "",
            (320, 320),          # input size default (akan diset ulang per gambar)
            score_threshold=0.5,
            nms_threshold=0.3,
            top_k=5000
        )
        print("[face_detect] YuNet Face Detector siap!")
    return _yunet


# ─────────────────────────────────────────
# FRAME EXTRACTION
# ─────────────────────────────────────────

def _extract_sample_frames(video_path, timestamps, output_dir):
    """Extract frame PNG dari video di timestamp tertentu."""
    frame_paths = []
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(output_dir, f"sample_{i:02d}.png")
        subprocess.run([
            FFMPEG_EXE, "-ss", str(ts), "-i", video_path,
            "-frames:v", "1", "-q:v", "2",
            out_path, "-y"
        ], check=True, capture_output=True)
        if os.path.exists(out_path):
            frame_paths.append(out_path)
    return frame_paths


# ─────────────────────────────────────────
# FACECAM REGION DEFINITION
# ─────────────────────────────────────────

def _get_facecam_regions(frame_w, frame_h):
    """
    8 region facecam: 4 pojok + 4 tepi tengah.
    Masing-masing 22% lebar x 30% tinggi dari frame.
    """
    rw = int(frame_w * 0.22)
    rh = int(frame_h * 0.30)
    mid_x = (frame_w - rw) // 2
    mid_y = (frame_h - rh) // 2
    return {
        "topleft":   (0,               0,                 rw, rh),
        "topright":  (frame_w - rw,   0,                 rw, rh),
        "btmleft":   (0,              frame_h - rh,      rw, rh),
        "btmright":  (frame_w - rw,   frame_h - rh,      rw, rh),
        "leftmid":   (0,              mid_y,             rw, rh),
        "rightmid":  (frame_w - rw,   mid_y,             rw, rh),
        "topmid":    (mid_x,          0,                 rw, rh),
        "btmmid":    (mid_x,          frame_h - rh,      rw, rh),
    }


# ─────────────────────────────────────────
# FACE DETECTION (YuNet DNN)
# ─────────────────────────────────────────

def _detect_faces_in_image(image_bgr):
    """
    Deteksi wajah dalam image array (BGR numpy) dengan YuNet.
    Return list of {bbox: (x,y,w,h), score: float} — koordinat relatif thd image.

    YuNet confidence score native (0-1), bukan proxy.
    """
    detector = _get_yunet()
    h, w = image_bgr.shape[:2]

    # Upscale jika gambar terlalu kecil (YuNet optimal di input >= 150px)
    scale = 1.0
    if w < 150 or h < 150:
        scale = max(200.0 / w, 200.0 / h)
        new_w, new_h = int(w * scale), int(h * scale)
        image_bgr = cv2.resize(image_bgr, (new_w, new_h),
                               interpolation=cv2.INTER_LANCZOS4)

    # Set input size dan detect (THREAD-SAFE: lock untuk hindari crash)
    detector.setInputSize((image_bgr.shape[1], image_bgr.shape[0]))
    with _yunet_lock:
        status, faces = detector.detect(image_bgr)

    faces_list = []
    if status and faces is not None and len(faces) > 0:
        for face in faces:
            # YuNet format: [x, y, w, h, ...10 landmarks..., confidence]
            fx, fy, fw, fh = face[0], face[1], face[2], face[3]
            confidence = float(face[14]) if len(face) > 14 else 0.5

            # Scale back ke koordinat original
            fx, fy = int(fx / scale), int(fy / scale)
            fw, fh = int(fw / scale), int(fh / scale)

            faces_list.append({
                "bbox": (fx, fy, fw, fh),
                "score": round(confidence, 3)
            })

    return faces_list


# ─────────────────────────────────────────
# ORCHESTRATOR UTAMA
# ─────────────────────────────────────────

def find_largest_face_in_corners(video_path,
                                 frame_count=None):
    """
    Auto-detect wajah terbesar di 8 region facecam (4 pojok + 4 tepi).

    Returns {"face_bbox": (x,y,w,h), "corner": "btmright", "score": 0.95}
    atau None kalau tidak ada wajah terdeteksi.
    """
    if frame_count is None:
        frame_count = FACE_DETECTION_SAMPLES

    # -- ffprobe: durasi --
    try:
        duration_str = subprocess.check_output([
            FFPROBE_EXE, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            video_path
        ]).decode().strip()
        duration = float(duration_str)
    except Exception as e:
        print(f"  ffprobe durasi gagal: {e}")
        return None

    # -- ffprobe: resolusi --
    try:
        res_str = subprocess.check_output([
            FFPROBE_EXE, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "csv=p=0",
            video_path
        ]).decode().strip().split(",")
        vid_w = int(res_str[0])
        vid_h = int(res_str[1])
        print(f"  Face detect: {vid_w}x{vid_h}, durasi={duration:.1f}s")
    except Exception as e:
        print(f"  ffprobe resolusi gagal: {e}")
        return None

    # -- Generate timestamp sample --
    if duration <= 1:
        timestamps = [0.0]
    else:
        margin = min(0.5, duration * 0.05)
        if frame_count == 1:
            timestamps = [duration / 2]
        else:
            step = (duration - 2 * margin) / (frame_count - 1)
            timestamps = [margin + i * step for i in range(frame_count)]

    # -- Extract full frame --
    frames_dir = os.path.join(
        TMP_DIR, f"face_detect_{os.path.basename(video_path).rsplit('.', 1)[0]}"
    )
    os.makedirs(frames_dir, exist_ok=True)

    try:
        frame_paths = _extract_sample_frames(video_path, timestamps, frames_dir)
    except Exception as e:
        print(f"  Extract frame gagal: {e}")
        return None

    # -- Deteksi di tiap region facecam per frame --
    regions = _get_facecam_regions(vid_w, vid_h)
    corner_faces = {c: [] for c in regions}

    for frame_path in frame_paths:
        full_img = cv2.imread(frame_path, cv2.IMREAD_COLOR)
        if full_img is None:
            continue
        fh, fw = full_img.shape[:2]

        for corner_name, (rx, ry, rw, rh) in regions.items():
            if rx < 0 or ry < 0 or rx + rw > fw or ry + rh > fh:
                continue

            # Crop region facecam
            region_img = full_img[ry:ry + rh, rx:rx + rw]

            # Deteksi wajah di dalam region
            faces = _detect_faces_in_image(region_img)

            if faces:
                best = max(faces, key=lambda f: f["bbox"][2] * f["bbox"][3])
                fx, fy, fw_face, fh_face = best["bbox"]
                corner_faces[corner_name].append({
                    "bbox": (rx + fx, ry + fy, fw_face, fh_face),
                    "score": best["score"],
                    "area": fw_face * fh_face,
                })

    # -- Cleanup --
    for f in frame_paths:
        try:
            os.remove(f)
        except OSError:
            pass
    try:
        os.rmdir(frames_dir)
    except OSError:
        pass

    # -- Majority voting --
    MIN_FRAMES = 2
    valid_corners = {}
    for corner_name, faces in corner_faces.items():
        if len(faces) >= MIN_FRAMES:
            faces_sorted = sorted(faces, key=lambda f: f["area"])
            median_face = faces_sorted[len(faces_sorted) // 2]
            valid_corners[corner_name] = {
                **median_face,
                "corner": corner_name,
                "frame_count": len(faces),
            }
            print(f"  Pojok {corner_name}: {len(faces)}/{len(frame_paths)} frame, "
                  f"median area={median_face['area']}px, "
                  f"score={median_face['score']:.2f}")

    skipped = [c for c in corner_faces if c not in valid_corners
               and len(corner_faces[c]) > 0]
    if skipped:
        print(f"  Diabaikan (<{MIN_FRAMES} frame): {skipped}")

    if not valid_corners:
        print("  Tidak ada wajah lolos majority voting - fallback ke posisi manual")
        return None

    # -- Pilih pojok terbaik: frame_count > area --
    best = max(valid_corners.values(),
               key=lambda f: (f["frame_count"], f["area"]))
    print(f"  Wajah terpilih: pojok {best['corner']}, "
          f"bbox={best['bbox']}, "
          f"score={best['score']:.2f}, "
          f"muncul di {best['frame_count']} frame")

    return {
        "face_bbox": best["bbox"],
        "corner": best["corner"],
        "score": best["score"],
    }


# ─────────────────────────────────────────
# CROP CALCULATION
# ─────────────────────────────────────────

def calculate_face_crop(face_bbox, frame_w, frame_h,
                        output_size=None):
    """Crop persegi centered pada wajah, clamp ke batas frame."""
    if output_size is None:
        output_size = FACECAM_OUTPUT_SIZE

    fx, fy, fw, fh = face_bbox
    center_x = fx + fw // 2
    center_y = fy + fh // 2
    half = output_size // 2

    crop_x = center_x - half
    crop_y = center_y - half

    if crop_x < 0:
        crop_x = 0
    if crop_y < 0:
        crop_y = 0
    if crop_x + output_size > frame_w:
        crop_x = frame_w - output_size
    if crop_y + output_size > frame_h:
        crop_y = frame_h - output_size

    return (crop_x, crop_y, output_size, output_size)
