import os
import asyncio

async def download_mux_no_shell(url: str) -> bytes:
    # 1) Create two OS pipes:
    video_r, video_w = os.pipe()
    audio_r, audio_w = os.pipe()

    # 2) Start the video download (writing to video_w):
    video_proc = await asyncio.create_subprocess_exec(
        'yt-dlp',
        '-f', 'bestvideo[ext=webm]',
        '-o', '-',
        '--', url,
        stdout=video_w,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(video_w,)
    )

    # 3) Start the audio download (writing to audio_w):
    audio_proc = await asyncio.create_subprocess_exec(
        'yt-dlp',
        '-f', 'bestaudio[ext=webm]',
        '-o', '-',
        '--', url,
        stdout=audio_w,
        stderr=asyncio.subprocess.PIPE,
        pass_fds=(audio_w,)
    )

    # Close the write ends in the parent, so EOF propagates correctly:
    #os.close(video_w)
    #os.close(audio_w)

    # 4) Now run ffmpeg, reading from those two fds:
    #    note: "pipe:3" -> fd=3, "pipe:4" -> fd=4
    ffmpeg_proc = await asyncio.create_subprocess_exec(
        'ffmpeg',
        '-i', 'pipe:3',
        '-i', 'pipe:4',
        '-map', '0:v',
        '-map', '1:a',
        '-c:v', 'copy',
        '-c:a', 'copy',
        '-f', 'webm',
        'pipe:1',
        stdin=None,
        stdout=asyncio.subprocess.PIPE,
        pass_fds=(video_r, audio_r)
    )

    # Close the read-ends in the parent once handed off:
    #os.close(video_r)
    #os.close(audio_r)

    # 5) Read the output and wait for everyone to finish:
    output, ff_err = await ffmpeg_proc.communicate()
    await video_proc.wait()
    await audio_proc.wait()

    if ffmpeg_proc.returncode:
        raise RuntimeError(f"ffmpeg failed ({ffmpeg_proc.returncode}):\n{ff_err.decode()}")
    return output

async def main():
    data = await download_mux_no_shell("https://www.youtube.com/watch?v=me8P3OUTTJk")
    with open("output.webm", "wb") as f:
        f.write(data)

if __name__ == '__main__':
    asyncio.run(main())