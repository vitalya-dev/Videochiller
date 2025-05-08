import asyncio
import sys

async def run_downloader(url: str):
    # Launch the downloader script as a subprocess
    #   - 'python3' (or 'python') must be on your PATH
    #   - 'downloader.py' is your existing script
    #   - you capture stdout so you can process the video bytes
    proc = await asyncio.create_subprocess_exec(
        sys.executable,  # ensures same Python interpreter
        "test.py",
        url,
        stdout=asyncio.subprocess.PIPE,
    )

    # Read from its stdout in chunks and do whatever you like with the bytes
    # (e.g. write to file, feed to ffmpeg, stream to client, etc.)
    with open("video.webm", "wb") as f:
        while True:
            chunk = await proc.stdout.read(64 * 1024)
            if not chunk:
                break
            f.write(chunk)


    # Wait for the subprocess to finish and check exit code
    return_code = await proc.wait()
    if return_code != 0:
        raise RuntimeError(f"Downloader exited with code {return_code}")

async def main():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    try:
        await run_downloader(url)
        print("Download complete!")
    except Exception as e:
        print("Error:", e, file=sys.stderr)

if __name__ == "__main__":
    asyncio.run(main())