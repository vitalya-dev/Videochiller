import asyncio
from asyncio.subprocess import DEVNULL
import json
import shlex  # For safe command splitting (optional but good practice)
import logging
from pathlib import Path
from urllib.parse import quote # Import the quote function for URL-encoding
import sys
import os
from typing import Dict


from fastapi import FastAPI, Request, Form, HTTPException, Path as FastAPIPath
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles # <--- Import StaticFiles

# --- Configuration ---
# Configure logging
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Path to yt-dlp executable (adjust if not in PATH)
YT_DLP_PATH = "yt-dlp"


COOKIE_FILE_INFO = os.getenv("YT_DLP_COOKIE_FILE_INFO", "cookies.firefox-private.txt")
COOKIE_FILE_STREAM = os.getenv("YT_DLP_COOKIE_FILE_STREAM", None)

# --- In-memory store for last actions ---
# This is a simple in-memory dictionary.
# For a production environment with multiple workers or needing persistence,
# consider using Redis, a database, or other shared memory solutions.
download_actions_log: Dict[str, str] = {}

# --- FastAPI App Setup ---
app = FastAPI()

# Setup Jinja2 templates
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# --- Helper Functions ---

def update_action_log(download_id: str | None, action: str):
    """Updates the in-memory action log for a given download_id."""
    if download_id:
        download_actions_log[download_id] = action
        logger.debug(f"ACTION_LOG: ID: {download_id} - Action: {action}")


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

async def get_video_info(url: str, cookie_file_path: str | None = None):
    """Gets video metadata using yt-dlp --dump-json."""
    args = ["--dump-json"]
    if cookie_file_path:
        if os.path.exists(cookie_file_path):
            args.extend(["--cookies", cookie_file_path])
        else:
            logger.warning(f"Cookie file for get_video_info specified but not found: {cookie_file_path}. Proceeding without cookies for this operation.")
    args.extend(["--no-playlist", "--", url])
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



async def delete_log_after_delay(download_id: str | None, delay_seconds: int):
    """
    Waits for a specified delay and then deletes the log entry
    for the given download_id.
    """
    if not download_id:
        return

    await asyncio.sleep(delay_seconds) # Wait for the specified number of seconds

    # Now, attempt to delete the log entry
    if download_id in download_actions_log:
        try:
            del download_actions_log[download_id]
            logger.info(f"ACTION_LOG: Successfully deleted ID: {download_id} from log after {delay_seconds}s delay.")
        except KeyError:
            # This might happen if it was somehow deleted by another concurrent process
            # or if the key was removed between the check and the del, though less likely here.
            logger.warning(f"ACTION_LOG: Attempted to delete ID: {download_id} after delay, but it was already gone.")
    else:
        # This case means the log was not found, perhaps it was cleared by another mechanism
        # or the initial check for download_id in download_actions_log was more than `delay_seconds` ago
        # and it got removed in that window (unlikely with this simple setup).
        logger.info(f"ACTION_LOG: ID: {download_id} was not found in logs for deletion after delay (perhaps already removed or never existed).")




async def stream_video_content(
    request: Request,
    url: str,
    quality_pref: str | None = None,  # e.g., "720" for 720p, or None for default
    cookie_file_path: str | None = None,
    download_id: str | None = None
):
    """
    Async generator to stream video content by calling ytdl_pipe_merge.py.
    Supports client disconnect handling and custom quality preferences.
    """
    logger.debug("stream_video_content start: url=%s, quality_pref=%s", url, quality_pref)

    command = [
        sys.executable,  # Use the same Python interpreter that runs FastAPI
        str(Path(__file__).parent / "ytdl_pipe_merge.py"), # Path to the script
        url,  # First argument to ytdl_pipe_merge.py is the URL
    ]

    if quality_pref:
        # Construct format strings for yt-dlp that ytdl_pipe_merge.py will use.
        # Prioritize WebM and MP4 source formats; ytdl_pipe_merge.py now outputs Matroska (MKV) via ffmpeg.
        # Fallbacks ensure that a format is found if the specific quality/ext is not available.
        # (protocol!=m3u8) avoids HLS streams which might be slower for yt-dlp to pipe.
        video_format_arg = (
            f"(bestvideo[height<={quality_pref}][ext=webm][protocol!=m3u8]/"
            f"bestvideo[height<={quality_pref}][ext=mp4][protocol!=m3u8]/"
            f"bestvideo[height<={quality_pref}][protocol!=m3u8]/"
            f"bestvideo[ext=webm][protocol!=m3u8]/bestvideo[protocol!=m3u8])"
        )
        audio_format_arg = (
            "(bestaudio[ext=webm][protocol!=m3u8]/"
            "bestaudio[ext=m4a][protocol!=m3u8]/bestaudio[protocol!=m3u8])"
        )
        command.extend(["--video_format", video_format_arg])
        command.extend(["--audio_format", audio_format_arg])
    else:
        # If no quality preference, aim for the best available formats,
        # still prioritizing webm/mp4 and avoiding m3u8.
        video_format_arg = (
            "(bestvideo[ext=webm][protocol!=m3u8]/"
            "bestvideo[ext=mp4][protocol!=m3u8]/"
            "bestvideo[protocol!=m3u8])"
        )
        audio_format_arg = (
            "(bestaudio[ext=webm][protocol!=m3u8]/"
            "bestaudio[ext=m4a][protocol!=m3u8]/bestaudio[protocol!=m3u8])"
        )
        command.extend(["--video_format", video_format_arg])
        command.extend(["--audio_format", audio_format_arg])

    if cookie_file_path:
        # We will need to add an argument like "--cookie_file" to ytdl_pipe_merge.py
        command.extend(["--cookies", cookie_file_path])
    # If quality_pref is None, ytdl_pipe_merge.py will use its own default formats.

    logger.info(f"Executing ytdl_pipe_merge.py with command: {' '.join(shlex.quote(c) for c in command)}")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=DEVNULL
    )

    if download_id:
        asyncio.create_task(delete_log_after_delay(download_id, 2)) # Using 10 seconds as requested
        logger.info(f"ACTION_LOG: Scheduled deletion of log for ID: {download_id} in 2 seconds.")

    try:
        chunk_size = 8 * 1024  # 8KB
        while True:
            if await request.is_disconnected():
                logger.warning(f"Client disconnected for URL {url}. Terminating ytdl_pipe_merge.py process.")
                if process.returncode is None:  # If process is still running
                    try:
                        process.terminate()  # Politely ask to terminate (SIGTERM)
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                        logger.info("ytdl_pipe_merge.py terminated gracefully after client disconnect.")
                    except asyncio.TimeoutError:
                        logger.warning("ytdl_pipe_merge.py did not terminate in 5s. Killing (SIGKILL).")
                        process.kill()
                        await process.wait() # Ensure it's reaped
                        logger.info("ytdl_pipe_merge.py killed.")
                    except Exception as e:
                        logger.error(f"Error during process termination on disconnect: {e}")
                        if process.returncode is None: process.kill(); await process.wait()
                break

            if process.stdout:
                chunk = await process.stdout.read(chunk_size)
                if not chunk:
                    logger.debug(f"ytdl_pipe_merge.py stdout EOF for URL {url}.")
                    break
                yield chunk
            else: # Should not happen if Popen succeeded with stdout=PIPE
                logger.error("ytdl_pipe_merge.py stdout is None, breaking stream.")
                break


        return_code = await process.wait()
        if return_code != 0:
            # stderr should have already been logged by the log_stderr task
            logger.error(f"ytdl_pipe_merge.py exited with error code {return_code} for URL {url}.")
            # Raising an error here might be too late if data has been streamed.
            # The client will receive a truncated stream.
            # For now, we rely on the logged error.

    except Exception as e:
        logger.exception(f"Error during streaming from ytdl_pipe_merge.py for URL {url}: {e}")
        if process.returncode is None:
            logger.info(f"Killing ytdl_pipe_merge.py due to exception: {e}")
            process.kill() # Kill immediately on unexpected error
            await process.wait()
        raise # Re-raise the exception to be handled by FastAPI
    finally:
        # Final cleanup: ensure process is terminated and stderr logger is done.
        if process.returncode is None:
            logger.warning(f"ytdl_pipe_merge.py process (pid {process.pid}) still running in finally block for URL {url}. Terminating.")
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning(f"Final kill for ytdl_pipe_merge.py (pid {process.pid}) for URL {url}.")
                process.kill()
                await process.wait() # Ensure kill is processed
            except Exception as e:
                logger.error(f"Error during final termination of process {process.pid}: {e}")
                if process.returncode is None: process.kill(); await process.wait()
        logger.debug("stream_video_content finished for URL: %s", url)






# --- FastAPI Endpoints ---

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Serves the main HTML page with the form."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/download")
async def download_video(
    request: Request,
    url: str = Form(...),
    quality: str | None = Form(None),
    download_id: str = Form(...) # Added download_id form field
):
    """Handles the download request, gets info, and streams the video (always as WebM)."""
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is missing.")
    if not download_id: # Should be guaranteed by Form(...)
        # This is more of a safeguard if Form(...) was not `...`
        logger.error(f"Critical: Download ID missing in POST request despite being required.")
        raise HTTPException(status_code=400, detail="Download ID parameter is missing.")

    logger.info(f"Received download request for URL: {url}, Quality: {quality}")

    try:
        logger.info(f"Fetching video info for: {url}")
        update_action_log(download_id, f"Fetching video info for: {url}")
        info = await get_video_info(url, cookie_file_path=COOKIE_FILE_INFO) # Get original video info for title, etc.

        # --- Output is always WebM when using ytdl_pipe_merge.py ---
        output_ext = "mkv"
        media_type = "video/x-matroska"
        # ---

        title = info.get('title', 'video')
        # Basic sanitization for filename
        unsafe_chars = ['/', '\\', ':', '*', '?', '"', '<', '>', '|']
        safe_title = title
        for char in unsafe_chars:
            safe_title = safe_title.replace(char, '_')
        safe_title = safe_title.replace("'", "_").replace('"', '_').strip()

        # Ensure filename has the correct extension (now .mkv)
        original_filename = f"{safe_title}.{output_ext}" if safe_title else f"video.{output_ext}"

        # Percent-encode the filename for Content-Disposition header
        encoded_filename = quote(original_filename, safe='')
        ascii_fallback_filename = "".join(c if ord(c) < 128 else '_' for c in original_filename).replace('"', '_')
        content_disposition = (
            f'attachment; filename="{ascii_fallback_filename}"; '
            f"filename*=UTF-8''{encoded_filename}"
        )


        logger.info(f"Determined filename: '{original_filename}', Fallback: '{ascii_fallback_filename}', Media type: '{media_type}'")
        logger.info(f"Content-Disposition header: {content_disposition}")

        headers = {
            'Content-Disposition': content_disposition
        }

        logger.info(f"Starting video stream for '{original_filename}' (as MKV)...")
        update_action_log(download_id, f"Starting video stream for '{original_filename}' (as MKV)...")

        # Pass the quality preference (e.g., "720" or None) to stream_video_content
        return StreamingResponse(
            stream_video_content(request, url, quality, COOKIE_FILE_STREAM, download_id),
            media_type=media_type, 
            headers=headers,
        )

    except HTTPException: # Re-raise HTTPExceptions directly
        raise
    except Exception as e:
        logger.exception(f"Unexpected error processing download for {url}: {e}")
        raise HTTPException(status_code=500, detail=f"An internal server error occurred: {str(e)}")

@app.get("/log/{download_id}", response_class=JSONResponse)
async def get_log_entry(
    download_id: str = FastAPIPath(..., title="The ID of the download to get logs for")
):
    """
    Retrieves the last logged action for a specific download ID.
    """
    logger.info(f"Log query received for ID: {download_id}")
    action = download_actions_log.get(download_id)
    if action is None:
        logger.warning(f"No log found for ID: {download_id}")
        raise HTTPException(status_code=404, detail="Log not found for this ID.")
    
    return {"download_id": download_id, "last_action": action}



# --- Optional: Run directly with Uvicorn ---
if __name__ == "__main__":
    import uvicorn
    logger.info("Starting Uvicorn server...")
    # Use reload=True for development, remove for production
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)