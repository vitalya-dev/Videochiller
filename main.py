import asyncio
import json
import shlex  # For safe command splitting (optional but good practice)
import logging
from pathlib import Path
from urllib.parse import quote # Import the quote function for URL-encoding


from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

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


async def stream_video_content(url: str, format_code: str | None = None):
    """Async generator to stream video content from yt-dlp."""
    
    # Select format: Prefer mp4, fall back to best available. Adjust as needed.
    # You could pass a specific format_code obtained from get_video_info if desired.
    format_selection = format_code if format_code else "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
    
    args = [
        "-f", format_selection,
        "-o", "-",  # Output to standard output
        "--", url    # Treat URL as positional argument
    ]
    
    process = await run_yt_dlp_command(args)

    # Check if the process started successfully before streaming
    # A small initial delay allows checking stderr for immediate errors
    try:
        await asyncio.wait_for(process.stderr.read(1), timeout=1.0) 
        # If we read something or timeout happens without error, stderr might contain info/warnings later
    except asyncio.TimeoutError:
        # No immediate error, likely starting download.
        pass 
        
    # Stream stdout
    chunk_size = 8192 # Read in 8KB chunks
    while True:
        if process.stdout is None:
             logger.warning("yt-dlp stdout stream is None. Process might have exited early.")
             break # Safety check
             
        chunk = await process.stdout.read(chunk_size)
        if not chunk:
            break # End of stream
        yield chunk

    # Wait for the process to finish and check for errors
    await process.wait() # Same as await process.communicate() if stdout/stderr already consumed

    if process.returncode != 0:
        # Try reading any remaining stderr after streaming finished
        stderr_output = b""
        if process.stderr:
           stderr_output = await process.stderr.read() 
        error_message = stderr_output.decode().strip()
        logger.error(f"yt-dlp streaming error (return code {process.returncode}): {error_message}")
        # Note: We can't raise HTTPException here as headers are already sent.
        # The client will likely see a prematurely terminated download.
        print(f"yt-dlp streaming error (return code {process.returncode}): {error_message}") # Log to console
        # Consider implementing a mechanism to signal the error to the client if possible,
        # e.g., by appending an error message to the stream (if the client expects it)
        # or logging it prominently for server-side debugging.


# --- FastAPI Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serves the main HTML page with the form."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/download")
async def download_video(request: Request, url: str = Form(...)):
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

        # Return Streaming Response
        return StreamingResponse(
            stream_video_content(url), # Pass the original URL here
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