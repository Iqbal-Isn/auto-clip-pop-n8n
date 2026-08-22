# Auto Clip Pop n8n 🎬

Sistem otomatisasi pemotongan video YouTube menjadi klip pendek TikTok/Shorts/Reels, dikendalikan melalui **Telegram Bot** dan diorkestrasi dengan **n8n workflow**.

## ✨ Fitur Utama

- **🎤 Transkrip Otomatis** — Ambil transkrip YouTube (Indonesia & Inggris) via `youtube-transcript-api`
- **🤖 AI Content Curation** — AI Agent (Claude/Gemini via n8n) menganalisis transkrip untuk menemukan momen paling viral
- **✂️ Auto Clip** — Potong video 30-60 detik dengan satu perintah
- **📝 Auto Subtitle** — Generate subtitle karaoke word-by-word (ASS format) menggunakan **Whisper** (faster-whisper)
- **🎮 Gaming Mode** — Layout khusus gaming: background blur, gameplay 4:3, facecam overlay, watermark
- **👤 Face Detection** — Auto-deteksi posisi facecam streamer dengan **YuNet DNN** (8 region: 4 pojok + 4 tepi)
- **🎬 Gaming Compilation** — Gabungkan 5 momen klimaks → 1 video kompilasi dengan transisi suara
- **📱 TikTok Layout** — Format vertikal 1080×1920 (blur background + konten di tengah)
- **📦 Batch Processing** — Download video sekali, potong banyak klip
- **🤖 Telegram Bot** — Kirim link YouTube via Telegram, terima klip jadi otomatis

## 🏗️ Arsitektur

```
Telegram Bot → n8n Workflow → FastAPI Backend → ffmpeg/ffprobe
                   │                │
                   ▼                ▼
              Claude/Gemini    yt-dlp (download)
              (AI Analysis)    Whisper (subtitle)
                               YuNet (face detect)
```

### Alur Kerja

```
1. User kirim link YouTube ke Telegram Bot
2. n8n validasi link → kirim notifikasi "sedang diproses"
3. Backend ambil transkrip YouTube
4. AI Agent analisis transkrip → pilih 3 momen terbaik (atau 5 untuk gaming)
5. Backend download section video → generate subtitle → apply filter → compress
6. Hasil klip dikirim balik ke user via Telegram
7. (Gaming) Klip digabung jadi 1 video kompilasi → upload Google Drive
```

## 📁 Struktur Proyek

```
auto-clip-pop-n8n/
├── run.py                      # 🚀 Entry point (python run.py)
├── requirements.txt            # 📦 Python dependencies
├── app/
│   ├── main.py                 # ⚡ FastAPI app + router mount
│   ├── config.py               # ⚙️ Konfigurasi, paths, discovery tools
│   ├── api/
│   │   └── routes.py           # 🔌 API endpoints (transcript, cut, batch, gaming)
│   ├── models/
│   │   └── requests.py         # 📋 Pydantic request models
│   ├── utils/
│   │   └── helpers.py          # 🔧 Time conversion, URL helpers
│   └── services/
│       ├── subtitles.py        # 📝 SRT/ASS subtitle + Whisper transcribe
│       ├── filters.py          # 🎨 TikTok & gaming video filters
│       ├── transitions.py      # 🔗 Video concat + compress
│       ├── face_detector.py    # 👤 YuNet DNN face detection
│       └── pipeline.py         # 🎯 Orkestrasi download → clip → output
├── assets/
│   ├── images/watermark.png    # 🖼️ Watermark overlay
│   ├── sounds/transisi_sound.mpeg  # 🔊 Sound effect transisi
│   └── models/                 # 🧠 YuNet ONNX (auto-download saat pertama pakai)
├── n8n/
│   └── workflow.json           # 🔄 n8n workflow definition
└── README.md
```

## 🔌 API Endpoints

Server berjalan di `http://localhost:8000`

### `GET /transcript`
Ambil transkrip YouTube.

| Param | Tipe | Deskripsi |
|-------|------|-----------|
| `url` | string | URL YouTube (watch / youtu.be / live) |

**Response:**
```json
{
  "transcript": "[00:00:01] Teks transkrip\n[00:00:05] ..."
}
```

### `POST /cut`
Potong 1 klip tunggal.

```json
{
  "url": "https://youtube.com/watch?v=xxx",
  "start": "00:01:30",
  "end": "00:02:00",
  "mode": "normal"
}
```

| Field | Deskripsi |
|-------|-----------|
| `mode` | `"normal"` (TikTok split) atau `"gaming"` (facecam top + full bottom) |

### `POST /cut-batch`
Potong banyak klip dari 1 video (download hanya sekali).

```json
{
  "url": "https://youtube.com/watch?v=xxx",
  "clips": [
    {"start": "00:01:30", "end": "00:02:00"},
    {"start": "00:05:10", "end": "00:05:50"}
  ],
  "mode": "normal"
}
```

**Response:**
```json
{
  "status": "done",
  "total": 2,
  "success": 2,
  "failed": 0,
  "clips": [
    {"file": "final_tiktok_000130_0.mp4", "size_mb": 8.5, "status": "success"},
    {"file": "final_tiktok_000510_1.mp4", "size_mb": 7.2, "status": "success"}
  ]
}
```

### `POST /cut-gaming-compilation`
N klip gaming → 1 video kompilasi dengan transisi.

```json
{
  "url": "https://youtube.com/watch?v=xxx",
  "clips": [
    {"start": "00:01:30", "end": "00:02:00"},
    {"start": "00:05:10", "end": "00:05:40"},
    {"start": "00:08:20", "end": "00:08:50"},
    {"start": "00:12:00", "end": "00:12:30"},
    {"start": "00:15:45", "end": "00:16:15"}
  ],
  "facecam_position": "btmleft"
}
```

`facecam_position` — 8 opsi:
| Value | Posisi |
|-------|--------|
| `btmleft` | Kiri bawah |
| `btmright` | Kanan bawah |
| `topleft` | Kiri atas |
| `topright` | Kanan atas |
| `leftmid` | Kiri tengah |
| `rightmid` | Kanan tengah |
| `topmid` | Atas tengah |
| `btmmid` | Bawah tengah |

## 🎬 Mode Video

### Mode Normal (`mode: "normal"`)
- Background blur dari video asli (1080×1920)
- Konten foreground di tengah (1080×1080)
- Subtitle karaoke auto-generated (Whisper)
- Cocok untuk: podcast, interview, vlog

### Mode Gaming (`mode: "gaming"`)
- **Single clip**: Facecam di atas, full gameplay di bawah (split 50:50)
- **Compilation**: Background blur, gameplay 4:3 (1080×810) centered, facecam overlay 360px, watermark
- Subtitle di-skip (gaming tidak perlu auto subtitle)
- Cocok untuk: highlight MLBB, Valorant, PUBG, dll.

## 🚀 Setup

### Prerequisites
- **Python 3.11+**
- **ffmpeg & ffprobe** (tersedia di PATH atau auto-discovery)
- **yt-dlp** (`pip install yt-dlp`)
- **Node.js** (untuk yt-dlp JS runtime)
- **n8n** (untuk workflow Telegram Bot)

### Install Dependencies

```bash
pip install -r requirements.txt
```

### Menjalankan Server

```bash
python run.py
# Server berjalan di http://0.0.0.0:8000
# Dokumentasi API: http://localhost:8000/docs
```

### n8n Workflow Setup

1. Import `n8n/workflow.json` ke n8n
2. Konfigurasi credentials:
   - **Telegram API** — untuk bot trigger & send
   - **Anthropic API** — untuk AI Agent (Claude)
   - **Google Drive** — untuk upload hasil kompilasi
3. Pastikan n8n bisa akses `http://host.docker.internal:8000` (jika n8n di Docker)
4. Output file dibaca dari `/home/node/.n8n-files/` (mount ke `OUTPUT_DIR`)

### Model AI

- **YuNet ONNX** — Auto-download dari OpenCV Zoo saat pertama kali digunakan (~350KB)
- **Whisper Small** — Auto-download oleh faster-whisper saat pertama transcribe (~500MB)

## 📝 Format Telegram Command

```
<URL YouTube> [opsional: HH:MM:SS-HH:MM:SS] [opsional: GAMING5] [opsional: posisi facecam]

Contoh:
https://youtube.com/watch?v=xxx
https://youtube.com/watch?v=xxx 00:05:00-00:10:00
https://youtube.com/watch?v=xxx GAMING5
https://youtube.com/watch?v=xxx GAMING5 btmright
```

| Keyword | Efek |
|---------|------|
| (tanpa keyword) | Mode normal, AI pilih 3 momen terbaik |
| `GAMING5` | Mode gaming kompilasi, AI pilih 5 klimaks → 1 video. Layout: facecam 40% atas + gameplay 60% bawah |
| Range waktu | Filter transkrip dalam rentang tertentu |

## 🔧 Konfigurasi

Edit `app/config.py` untuk menyesuaikan:

| Konstanta | Default | Deskripsi |
|-----------|---------|-----------|
| `OUTPUT_DIR` | `C:\Users\RWID\Downloads\clip` | Folder output video |
| `COOKIES_PATH` | `./youtube_cookies.txt` | Cookies YouTube (opsional) |
| `FACECAM_OUTPUT_SIZE` | `200` | Ukuran crop facecam (px) |
| `FACE_DETECTION_CONFIDENCE` | `0.5` | Threshold YuNet |
| `FACE_DETECTION_SAMPLES` | `5` | Jumlah frame sample untuk deteksi |

## 🎯 Fitur Detil

### Face Detection (YuNet DNN)
- Auto-deteksi wajah terbesar di 8 region facecam
- Majority voting dari 5 sample frame (min. 2 frame)
- Fallback ke posisi manual jika tidak terdeteksi
- Crop centered pada wajah, clamp dalam region; untuk GAMING5 crop mengikuti rasio area facecam 1080×768

### Whisper Subtitle
- faster-whisper `small` di CPU (int8)
- Word-level timestamps untuk karaoke akurat
- Grup kata per baris (max 2 kata, jeda >0.7s = baris baru)
- Subtitle ASS dengan highlight kata aktif (`\K` tag)

### Layout Kompilasi GAMING5
- Output portrait 1080×1920 per klip
- Facecam 40% bagian atas: 1080×768, memakai auto deteksi wajah YuNet dengan region crop diperluas 1.35× per dimensi
- Gameplay 60% bagian bawah: 1080×1152
- Watermark 150px di tengah hasil akhir jika `assets/images/watermark.png` tersedia
- Whisper/auto subtitle dilewati untuk mode ini

### Transisi Kompilasi Gaming
- Layar hitam + sound effect antar klip
- Durasi transisi otomatis mengikuti durasi file suara
- Concat semua segmen dengan xfade filter

### Kompresi
- Target ukuran: 45MB (sesuai batas WhatsApp)
- Auto-compress dengan bitrate kalkulasi jika melebihi target

## 📦 Dependencies

```
fastapi
uvicorn
youtube-transcript-api
faster-whisper
pydantic
opencv-python
numpy
yt-dlp (system)
ffmpeg (system)
```

## 🤝 Credits

Dibangun dengan:
- **FastAPI** — Backend framework
- **n8n** — Workflow automation
- **Claude (Anthropic)** — AI content analysis
- **Whisper (OpenAI)** — Speech-to-text
- **YuNet (OpenCV)** — Face detection
- **ffmpeg** — Video processing
