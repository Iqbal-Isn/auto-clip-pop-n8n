import sys
import json
import subprocess

# Ambil data dari n8n
input_data = json.loads(sys.stdin.read())
url = input_data.get("url")
start_time = input_data.get("start", "00:00:10") # contoh mulai detik 10
end_time = input_data.get("end", "00:00:20")     # contoh berakhir detik 20

output_file = "/home/iqbal/video_clip.mp4"

# Perintah potong video tanpa download full (Sangat Hemat RAM!)
cmd = [
    "yt-dlp",
    "-g", url, # Ambil URL video mentah
    "-f", "best"
]
video_url = subprocess.check_output(cmd).decode('utf-8').strip()

# Gunakan FFmpeg untuk potong langsung dari stream (Cepat!)
ffmpeg_cmd = [
    "ffmpeg", "-ss", start_time, "-to", end_time,
    "-i", video_url, "-c", "copy", output_file, "-y"
]
subprocess.run(ffmpeg_cmd)

print(json.dumps({"status": "sukses", "file": output_file}))