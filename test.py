import subprocess
import sys

url = "https://www.youtube.com/watch?v=me8P3OUTTJk"

# 1) Launch yt-dlp downloaders for video & audio
video = subprocess.Popen(
    ["yt-dlp", "-f", "bestvideo[ext=webm]", "-o", "-", "--", url],
    stdout=subprocess.PIPE
)
audio = subprocess.Popen(
    ["yt-dlp", "-f", "bestaudio[ext=webm]", "-o", "-", "--", url],
    stdout=subprocess.PIPE
)

# 2) Launch ffmpeg, reading from those pipes, writing merged .webm to its stdout
ffmpeg = subprocess.Popen(
    [
      "ffmpeg",
      "-hide_banner", "-y",
      "-i", "pipe:3",
      "-i", "pipe:4",
      "-map", "0:v", "-map", "1:a",
      "-c:v", "copy", "-c:a", "copy",
      "-f", "webm", "pipe:1"
    ],
    pass_fds=(video.stdout.fileno(), audio.stdout.fileno()),
    stdout=subprocess.PIPE,
)

# 3) Stream ffmpeg’s stdout directly to our stdout
try:
    for chunk in iter(lambda: ffmpeg.stdout.read(4096), b""):
        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
finally:
    # 4) Clean up subprocesses
    ffmpeg_rc = ffmpeg.wait()
    video.wait()
    audio.wait()