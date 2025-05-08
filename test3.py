import asyncio
import sys

async def download_and_merge(url):
    # 1) Launch yt-dlp downloaders for video & audio asynchronously
    video_proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "-f", "bestvideo[ext=webm]", "-o", "-", "--", url,
        stdout=asyncio.subprocess.PIPE
    )
    
    audio_proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "-f", "bestaudio[ext=webm]", "-o", "-", "--", url,
        stdout=asyncio.subprocess.PIPE
    )
    
    # 2) Launch ffmpeg, reading from those pipes, writing merged .webm to its stdout
    ffmpeg_proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner", "-y",
        "-i", "pipe:0", 
        "-i", "pipe:1",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy", "-c:a", "copy",
        "-f", "webm", "pipe:1",
        stdin=None,  # We'll handle the input redirection manually
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    
    # 3) Stream data between processes asynchronously
    try:
        # Create tasks to pipe video and audio data to ffmpeg and read output
        video_task = asyncio.create_task(pipe_stream(video_proc.stdout, ffmpeg_proc.stdin))
        audio_task = asyncio.create_task(pipe_stream(audio_proc.stdout, ffmpeg_proc.stdin))
        
        # Stream ffmpeg's stdout directly to our stdout
        async for chunk in read_stream(ffmpeg_proc.stdout, 4096):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            
        # 4) Clean up subprocesses
        await asyncio.gather(video_task, audio_task)
        ffmpeg_rc = await ffmpeg_proc.wait()
        await video_proc.wait()
        await audio_proc.wait()
        
        if ffmpeg_rc != 0:
            err = await ffmpeg_proc.stderr.read()
            err_text = err.decode(errors="ignore")
            raise RuntimeError(f"ffmpeg exited {ffmpeg_rc}\nstderr:\n{err_text}")
    
    except Exception as e:
        # Ensure processes are terminated on error
        for proc in [video_proc, audio_proc, ffmpeg_proc]:
            if proc.returncode is None:
                proc.terminate()
        raise e

async def pipe_stream(input_stream, output_stream):
    """Pipe data from input stream to output stream."""
    try:
        while True:
            chunk = await input_stream.read(4096)
            if not chunk:
                break
            output_stream.write(chunk)
            await output_stream.drain()
    finally:
        output_stream.close()

async def read_stream(stream, chunk_size):
    """Read stream in chunks and yield each chunk."""
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        yield chunk

async def main():
    url = "https://www.youtube.com/watch?v=me8P3OUTTJk"
    await download_and_merge(url)

if __name__ == "__main__":
    asyncio.run(main())