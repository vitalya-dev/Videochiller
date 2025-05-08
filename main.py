import asyncio
import json
import shlex  # For safe command splitting (optional but good practice)
import logging
from pathlib import Path
from urllib.parse import quote # Import the quote function for URL-encoding
import sys


from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles # <--- Import StaticFiles

# --- Configuration ---
# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Path to yt-dlp executable (adjust if not in PATH)
YT_DLP_PATH = "yt-dlp"

# --- FastAPI App Setup ---
app = FastAPI()

# Setup Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# --- Helper Functions ---

async def run_yt_dlp_command(args):
    """Runs a yt-dlp command asynchronously and returns stdout, stderr, returncode."""
    command = [YT_DLP_PATH] + args
    logger.info(f"Running command: {' '.join(shlex.quote(str(arg)) for arg in command)}")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    return process

async def get_video_info(url: str):
    """Gets video metadata using yt-dlp --dump-json."""
    args = ["--dump-json", "--", url] # '--' ensures URL is treated as positional arg
    process = await run_yt_dlp_command(args)
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        error_message = stderr.decode(errors='ignore').strip() # Use errors='ignore' for stderr
        logger.error(f"yt-dlp error (get_info): {error_message}")
        # Try to decode stderr as UTF-8, fallback to ignore errors
        try:
            decoded_stderr = stderr.decode('utf-8')
        except UnicodeDecodeError:
            decoded_stderr = stderr.decode('latin-1', errors='ignore')
        error_message = decoded_stderr.strip()
        raise HTTPException(status_code=400, detail=f"Failed to get video info: {error_message}")

    try:
        # yt-dlp JSON output should be UTF-8
        return json.loads(stdout.decode('utf-8'))
    except json.JSONDecodeError:
        logger.error("Failed to parse yt-dlp JSON output.")
        raise HTTPException(status_code=500, detail="Error parsing video information.")
    except UnicodeDecodeError:
        logger.error("Failed to decode yt-dlp JSON output as UTF-8.")
        raise HTTPException(status_code=500, detail="Error decoding video information (non-UTF8).")


async def stream_video_content(
    request: Request,
    url: str,
    format_code: str | None = None
):
    logger.debug("stream_video_content start: url=%s, format_code=%s", url, format_code)
    """Async generator to stream video content from yt-dlp, with client-cancel support."""
    # 1. Select format: prefer mp4, else best available.
    # 1) Decide video-only / audio-only format strings
    video_fmt = (
        format_code
        if False #debug
        else "bestvideo[ext=webm]"
    )
    audio_fmt = (
        format_code
        if False #debug
        else "bestaudio[ext=webm]"
    )
    logger.debug("Using formats: video=%s, audio=%s", video_fmt, audio_fmt)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,  # ensures same Python interpreter
        "test.py",
        url,
        stdout=asyncio.subprocess.PIPE,
    )

    try:
        # 4) Stream ffmpeg stdout in chunks, watching for client disconnect
        chunk_size = 8 * 1024
        while True:
            if await request.is_disconnected():
                proc.kill()
                break

            chunk = await proc.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk

        # 5) Wait for ffmpeg → then for yt-dlp procs
        rc = await proc.wait()

        if rc != 0:
            raise RuntimeError(f"ffmpeg exited {rc}")

    finally:
        if proc.returncode is None:
            proc.kill()






# --- FastAPI Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serves the main HTML page with the form."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/download")
async def download_video(request: Request, url: str = Form(...), quality: str | None = Form(None)):
    """Handles the download request, gets info, and streams the video."""
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is missing.")

    logger.info(f"Received download request for URL: {url}")

    try:
        logger.info(f"Fetching video info for: {url}")
        info = await get_video_info(url)

        # Extract title and extension
        title = info.get('title', 'video')
        ext = info.get('ext', 'mp4')

        # Create the desired filename (potentially with Unicode characters)
        # Basic sanitization: remove characters problematic for filenames, replace quote types
        unsafe_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        safe_title = title
        for char in unsafe_chars:
            safe_title = safe_title.replace(char, '_')
        safe_title = safe_title.replace("'", "_").replace('"', '_').strip() # Replace quotes too

        original_filename = f"{safe_title}.{ext}" if safe_title else f"video.{ext}"


        # --- Correctly encode filename for Content-Disposition ---
        # Percent-encode the filename using UTF-8. Safe='' ensures chars like '/' are encoded if present.
        encoded_filename = quote(original_filename, safe='')

        # Create a simple ASCII-only fallback filename for the 'filename=' parameter
        # Replace non-ASCII characters with underscores (_)
        ascii_fallback_filename = "".join(c if ord(c) < 128 else '_' for c in original_filename)
        # Ensure fallback doesn't have quotes which break the header value itself
        ascii_fallback_filename = ascii_fallback_filename.replace('"', '_')

        # Construct the Content-Disposition header value according to RFC 6266
        # Provides both the standard 'filename*' for modern browsers
        # and a simple ASCII 'filename=' as a fallback.
        content_disposition = (
            f'attachment; filename="{ascii_fallback_filename}"; '
            f"filename*=UTF-8''{encoded_filename}"
        )
        # --- End filename encoding ---

        # Determine media type
        media_type_map = {
            "mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska",
            "mp3": "audio/mpeg", "m4a": "audio/mp4", "ogg": "audio/ogg",
            "opus": "audio/opus", "flv": "video/x-flv", "avi": "video/x-msvideo",
        }
        media_type = media_type_map.get(ext, "application/octet-stream")

        logger.info(f"Determined filename: '{original_filename}', Fallback: '{ascii_fallback_filename}', Media type: '{media_type}'")
        logger.info(f"Content-Disposition header: {content_disposition}")

        # Prepare Headers for Streaming Response
        headers = {
            'Content-Disposition': content_disposition
            # 'Content-Type' is handled by StreamingResponse's media_type parameter
        }

        logger.info(f"Starting video stream for '{original_filename}'...")

        format_code = None
        if quality:
            format_code = (
                f"bestvideo[height<={quality},ext=webm]+bestaudio/best[height<={quality},ext=webm]"

            )

        # Return Streaming Response
        return StreamingResponse(
            stream_video_content(request, url, format_code), # Pass the original URL here
            media_type=media_type,
            headers=headers
        )

    except Exception as e:
        logger.exception(f"Unexpected error processing download for {url}: {e}")
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")


# --- Optional: Run directly with Uvicorn ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Uvicorn server...")
    # Use reload=True for development, remove for production
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)