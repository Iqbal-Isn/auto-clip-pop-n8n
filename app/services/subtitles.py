"""Subtitle generation: SRT, ASS word-by-word karaoke, Whisper transcribe."""

import os
from app.config import get_whisper_model


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
