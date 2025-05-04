import asyncio
import json
import shlex  # For safe command splitting (optional but good practice)
import logging
from pathlib import Path
from urllib.parse import quote # Import the quote function for URL-encoding


from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles # <--- Import StaticFiles

# --- Configuration ---
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
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
    """Async generator to stream video content from yt-dlp, with client-cancel support."""
    # 1. Select format: prefer mp4, else best available.
    format_selection = (
        format_code
        if format_code
        else "best"
    )

    # 2. Build yt-dlp args to pipe output to stdout.
    args = [
        "-f", format_selection,
        "-o", "-",        # write to stdout
        "--external-downloader", "aria2c",
        "--external-downloader-args", "-x 16 -s 16 -k 1M",
        "--", url         # positional URL
    ]

    # 3. Launch the subprocess.
    process = await run_yt_dlp_command(args)

    try:
        # 4. Give yt-dlp a moment to emit any immediate errors.
        try:
            await asyncio.wait_for(process.stderr.read(1), timeout=1.0)
        except asyncio.TimeoutError:
            pass  # likely no immediate errors

        # 5. Stream stdout in chunks, watching for disconnect.
        chunk_size = 8 * 1024 # 8 KB
        while True:
            # 5a. If client has gone away, kill yt-dlp and stop.
            # if await request.is_disconnected():
            #     logger.info("Client disconnected—terminating yt-dlp process")
            #     process.kill()
            #     break

            if process.stdout is None:
                logger.warning("yt-dlp stdout is None; stopping stream")
                break

            chunk = await process.stdout.read(chunk_size)
            if not chunk:
                break
            yield chunk

        # 6. Wait for process to exit cleanly (or with error).
        await process.wait()

        # 7. If yt-dlp errored after streaming, log the stderr.
        if process.returncode != 0:
            stderr_output = b""
            if process.stderr:
                stderr_output = await process.stderr.read()
            err = stderr_output.decode(errors="ignore").strip()
            logger.error(f"yt-dlp exited {process.returncode}: {err}")

    finally:
        # 8. Ensure no rogue yt-dlp is left running.
        if process.returncode is None:  # still running?
            logger.info("Finally block: yt-dlp process still running, attempting to kill")
            try:
                process.kill()
                logger.info("Finally block: yt-dlp process killed successfully")
            except Exception as e:
                logger.error(f"Finally block: error killing yt-dlp process: {e}")




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
                f"best[height<={quality}]"
            )

        # Return Streaming Response
        return StreamingResponse(
            stream_video_content(request, url, format_code), # Pass the original URL here
            media_type=media_type,
            headers=headers
        )

    except HTTPException as e:
        logger.warning(f"HTTP Exception during download request: {e.detail}")
        raise e
    except Exception as e:
        logger.exception(f"Unexpected error processing download for {url}: {e}")
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")


# --- Optional: Run directly with Uvicorn ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Uvicorn server...")
    # Use reload=True for development, remove for production
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)