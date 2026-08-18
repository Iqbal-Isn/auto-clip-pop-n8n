"""Subtitle generation: SRT, ASS word-by-word karaoke, Whisper transcribe."""

import os
import re
import glob
import subprocess
from app.config import get_whisper_model, yt_dlp_cmd, TMP_DIR
from app.utils.helpers import seconds_to_hhmmss


def create_srt(entries, clip_start_sec, clip_end_sec):
    """Buat konten SRT dari transcript entries dalam rentang clip (per-baris)"""
    srt = []
    idx = 1
    for e in entries:
        es = e.start
        ee = es + e.duration
        if ee < clip_start_sec or es > clip_end_sec:
            continue
        rel_start = max(0, es - clip_start_sec)
        rel_end = min(clip_end_sec, ee) - clip_start_sec

        def fmt(sec):
            h = int(sec // 3600)
            m = int((sec % 3600) // 60)
            s = int(sec % 60)
            ms = int((sec % 1) * 1000)
            return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

        srt.append(f"{idx}\n{fmt(rel_start)} --> {fmt(rel_end)}\n{e.text}\n")
        idx += 1
    return "\n".join(srt)


def create_ass_word_by_word(entries, clip_start_sec, clip_end_sec, width=1080, height=1080):
    """Buat ASS subtitle karaoke — teks terakumulasi, kata aktif di-highlight"""
    def fmt_ass(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int((sec % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    header = (
        "[Script Info]\n"
        "Title: Auto Subtitles\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Google Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        "1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,30,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogues = []
    for e in entries:
        es = e.start
        ee = es + e.duration
        if ee < clip_start_sec or es > clip_end_sec:
            continue
        rel_start = max(0, es - clip_start_sec)
        rel_end = min(clip_end_sec, ee) - clip_start_sec

        words = e.text.strip().upper().split()
        if not words:
            continue

        total_dur = rel_end - rel_start
        word_count = len(words)
        dur_per_word = total_dur / word_count

        # Teks terakumulasi: tiap entry = semua kata sblmnya + kata baru dgn \K highlight
        accumulated = []
        t = rel_start
        for i, w in enumerate(words):
            accumulated.append(w)
            is_last = (i == word_count - 1)
            entry_end = rel_end if is_last else t + dur_per_word

            # \K = durasi highlight kata baru (centidetik)
            k_val = int((entry_end - t) * 100)
            full_text = " ".join(accumulated)

            if len(accumulated) == 1:
                text = f"{{\\K{k_val}}}{full_text}"
            else:
                before = " ".join(accumulated[:-1])
                text = f"{before} {{\\K{k_val}}}{w}"

            dialogues.append(
                f"Dialogue: 0,{fmt_ass(t)},{fmt_ass(entry_end)},Default,,0,0,0,,{text}"
            )
            t = entry_end

    return header + "\n".join(dialogues)


def transcribe_audio(audio_path):
    """Transcribe audio dengan faster-whisper → word-level timestamps"""
    model = get_whisper_model()
    segments, info = model.transcribe(
        audio_path,
        word_timestamps=True,
        language="id",
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
    )
    words = []
    for segment in segments:
        if segment.words:
            for w in segment.words:
                word_text = w.word.strip()
                if word_text:
                    words.append({
                        "word": word_text,
                        "start": w.start,
                        "end": w.end
                    })
    return words


def create_ass_from_whisper(words, width=1080, height=1080, margin_v=30):
    """Buat ASS karaoke dari whisper word timestamps"""
    if not words:
        return ""

    def fmt_ass(sec):
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = int(sec % 60)
        cs = int((sec % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    header = (
        "[Script Info]\n"
        "Title: Auto Subtitles (Whisper)\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 2\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour,"
        " OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut,"
        " ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow,"
        " Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Google Sans,56,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,"
        f"1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    # Grup kata → baris (jeda >0.7s atau >8 kata = baris baru)
    lines = []
    current_line = []
    for i, w in enumerate(words):
        w["word"] = w["word"].upper()
        current_line.append(w)
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 999
        if gap > 0.7 or len(current_line) >= 2:
            lines.append(current_line)
            current_line = []
    if current_line:
        lines.append(current_line)

    # Generate ASS dialogues dengan akumulasi karaoke
    dialogues = []
    for line in lines:
        accumulated = []
        for i, w in enumerate(line):
            accumulated.append(w["word"])
            start = w["start"]
            is_last = (i == len(line) - 1)
            end = line[-1]["end"] if is_last else line[i + 1]["start"]

            k_val = max(int((w["end"] - w["start"]) * 100), 5)

            if len(accumulated) == 1:
                text = f"{{\\K{k_val}}}{w['word']}"
            else:
                before = " ".join(accumulated[:-1])
                text = f"{before} {{\\K{k_val}}}{w['word']}"

            dialogues.append(
                f"Dialogue: 0,{fmt_ass(start)},{fmt_ass(end)},Default,,0,0,0,,{text}"
            )

    return header + "\n".join(dialogues)


# ─────────────────────────────────────────
# TRANSCRIPT FALLBACK — yt-dlp auto-subs (members-only support)
# ─────────────────────────────────────────

def _clean_caption_text(text: str) -> str:
    """Bersihkan tag & entity dari teks caption (auto-generated VTT/SRT)."""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _parse_caption_file(path: str) -> list[tuple[float, str]]:
    """Parse file .vtt/.srt → list (start_sec, text) terurut."""
    entries: list[tuple[float, str]] = []
    cur_start = None
    cur_text: list[str] = []
    is_vtt = path.lower().endswith(".vtt")
    ts_re = (re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->")
             if is_vtt else
             re.compile(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->"))

    def flush():
        nonlocal cur_start, cur_text
        if cur_start is not None and cur_text:
            entries.append((cur_start, " ".join(cur_text)))
        cur_start = None
        cur_text = []

    with open(path, encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                flush()
                continue
            m = ts_re.match(line)
            if m:
                flush()
                h, mi, s, ms = m.groups()
                cur_start = (int(h) * 3600 + int(mi) * 60
                             + int(s) + int(ms) / 1000)
            elif ("-->" not in line and not line.isdigit()
                  and not line.startswith("WEBVTT")
                  and not line.startswith("Kind:")
                  and not line.startswith("Language:")):
                clean = _clean_caption_text(line)
                if clean:
                    cur_text.append(clean)
    flush()
    entries.sort(key=lambda e: e[0])
    return entries


def fetch_transcript_via_ytdlp(video_id: str,
                               start_sec=None, end_sec=None) -> str | None:
    """
    Fallback transkrip via yt-dlp --write-auto-subs.
    Mendukung konten members-only (dengan cookies di youtube_cookies.txt).
    Return teks '[HH:MM:SS] ...' ter-filter range, atau None jika gagal.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    subdir = os.path.join(TMP_DIR, f"yt_subs_{video_id}")
    os.makedirs(subdir, exist_ok=True)

    try:
        subprocess.run(
            yt_dlp_cmd() + [
                "--skip-download",
                # Client web_embedded: subs TIDAK butuh PO Token (web client
                # membuang subtitle tanpa PO token → tidak ada file caption).
                # Didukung cookies → akses konten members-only.
                "--extractor-args", "youtube:player_client=web_embedded",
                # Paksa format tersedia (storyboard) agar yt-dlp tidak gagal
                # "Requested format is not available" saat --skip-download.
                "-f", "sb0",
                "--write-auto-subs", "--write-subs",
                "--sub-langs", "id.*,en.*,original",
                "--sub-format", "vtt/srt",
                "--no-playlist",
                "-o", os.path.join(subdir, "sub.%(ext)s"),
                url,
            ], check=True, timeout=180, capture_output=True
        )
    except Exception as e:
        print(f"  ⚠️ yt-dlp subs gagal: {str(e)[:80]}")
        return None

    entries: list[tuple[float, str]] = []
    # Preferensi bahasa: Indonesian > original > English. Glob diurutkan alfabet
    # ('sub.en.vtt' menang atas 'sub.id.vtt'), jadi kita urutkan ulang manual.
    def _lang_rank(p: str) -> int:
        base = os.path.basename(p)
        if f"sub.id." in base:
            return 0
        if "original" in base:
            return 1
        if f"sub.en." in base:
            return 2
        return 99

    files = sorted(glob.glob(os.path.join(subdir, "sub.*")),
                   key=lambda p: (_lang_rank(p), p))
    for p in files:
        if p.lower().endswith((".vtt", ".srt")):
            entries = _parse_caption_file(p)
            if entries:
                print(f"  📝 Transkrip via yt-dlp: {os.path.basename(p)} "
                      f"({len(entries)} baris)")
                break

    if not entries:
        print("  ⚠️ yt-dlp subs: tidak ada file caption ditemukan")
        return None

    lines = []
    for ts, text in entries:
        if start_sec is not None and (ts < start_sec or ts > end_sec):
            continue
        lines.append(f"[{seconds_to_hhmmss(ts)}] {text}")

    return "\n".join(lines) if lines else None
