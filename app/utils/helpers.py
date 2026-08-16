"""Time conversion & URL parsing helpers."""


def seconds_to_hhmmss(seconds):
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def extract_video_id(url: str):
    if "v=" in url:
        return url.split("v=")[1].split("&")[0]
    elif "youtu.be/" in url:
        return url.split("youtu.be/")[1].split("?")[0]
    elif "/live/" in url:
        return url.split("/live/")[1].split("?")[0]
    return None


def hhmmss_to_seconds(t: str):
    """Convert HH:MM:SS atau MM:SS ke detik"""
    parts = t.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return int(parts[0])


def parse_time_range(range_str: str):
    """
    Parse range 'HH:MM:SS-HH:MM:SS' → (start_sec, end_sec).
    Return (None, None) jika range kosong atau format tidak valid.
    """
    if not range_str or "-" not in range_str:
        return None, None
    try:
        start_s, end_s = range_str.split("-", 1)
        start = hhmmss_to_seconds(start_s.strip())
        end = hhmmss_to_seconds(end_s.strip())
        if end <= start:
            return None, None
        return start, end
    except (ValueError, IndexError):
        return None, None
