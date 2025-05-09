import subprocess
import sys
import argparse
import os # For path validation if needed

def download_video(url, video_format, audio_format, output_filename=None, cookie_file=None): # <-- ADDED cookie_file
    """
    Downloads a video from the given URL with specified video and audio formats.
    Optionally uses a cookie file.
    stderr from yt-dlp and ffmpeg will be printed directly to the console.

    Args:
        url (str): The URL of the video to download.
        video_format (str): The desired video format string for yt-dlp.
        audio_format (str): The desired audio format string for yt-dlp.
        output_filename (str, optional): The name of the output file.
                                         If None, output is streamed to stdout.
        cookie_file (str, optional): Path to the cookie file for yt-dlp.
    """
    video_process = None
    audio_process = None
    ffmpeg_process = None

    video_returncode = None
    audio_returncode = None
    ffmpeg_returncode = None

    try:
        # --- Construct yt-dlp commands ---
        base_yt_dlp_cmd = ["yt-dlp"]
        if cookie_file:
            # Basic validation: check if file exists, though yt-dlp will also error
            if os.path.isfile(cookie_file): # You might want more robust path validation
                base_yt_dlp_cmd.extend(["--cookies", cookie_file])
            else:
                print(f"Warning: Cookie file not found at {cookie_file}. Proceeding without cookies.", file=sys.stderr)
        
        video_dl_cmd = base_yt_dlp_cmd + ["--no-playlist", "-f", video_format, "-o", "-", "--", url]
        audio_dl_cmd = base_yt_dlp_cmd + ["--no-playlist", "-f", audio_format, "-o", "-", "--", url]
        
        print(f"yt-dlp video command: {' '.join(video_dl_cmd)}", file=sys.stderr) # For debugging
        print(f"yt-dlp audio command: {' '.join(audio_dl_cmd)}", file=sys.stderr) # For debugging

        video_process = subprocess.Popen(
            video_dl_cmd,
            stdout=subprocess.PIPE,
        )
        audio_process = subprocess.Popen(
            audio_dl_cmd,
            stdout=subprocess.PIPE,
        )
        
        if not video_process.stdout:
            raise IOError("yt-dlp video process stdout pipe not created.")
        if not audio_process.stdout:
            raise IOError("yt-dlp audio process stdout pipe not created.")

        ffmpeg_command = [
            "ffmpeg",
            "-hide_banner", "-y",
            "-i", "pipe:3",
            "-i", "pipe:4",
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "copy",
            "-f", "webm",
        ]

        ffmpeg_stdout_target = None
        if output_filename:
            ffmpeg_command.append(output_filename)
        else:
            ffmpeg_command.append("pipe:1")
            ffmpeg_stdout_target = subprocess.PIPE

        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            pass_fds=(video_process.stdout.fileno(), audio_process.stdout.fileno()),
            stdin=subprocess.PIPE,
            stdout=ffmpeg_stdout_target,
        )

        video_process.stdout.close()
        audio_process.stdout.close()

        if not output_filename and ffmpeg_process and ffmpeg_process.stdout:
            try:
                for chunk in iter(lambda: ffmpeg_process.stdout.read(4096), b""):
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            except Exception as e:
                print(f"Error streaming ffmpeg output: {e}", file=sys.stderr)
                if ffmpeg_process.poll() is None:
                    ffmpeg_process.terminate()
            finally:
                 if ffmpeg_process.stdout:
                    ffmpeg_process.stdout.close()
    except Exception as e:
        print(f"An unexpected error occurred during setup or initial execution: {e}", file=sys.stderr)
        if video_process and video_process.poll() is None: video_process.kill()
        if audio_process and audio_process.poll() is None: audio_process.kill()
        if ffmpeg_process and ffmpeg_process.poll() is None: ffmpeg_process.kill()
        sys.exit(1)
    finally:
        if ffmpeg_process:
            try:
                _ = ffmpeg_process.communicate(timeout=120) 
            except subprocess.TimeoutExpired:
                print("ffmpeg process timed out during cleanup. Killing...", file=sys.stderr)
                ffmpeg_process.kill()
                ffmpeg_process.communicate()
            except Exception as e:
                 print(f"Error during ffmpeg_process.communicate(): {e}", file=sys.stderr)
            ffmpeg_returncode = ffmpeg_process.returncode
            if ffmpeg_returncode != 0:
                print(f"ffmpeg exited with error (code {ffmpeg_returncode}). Check console for ffmpeg's error messages.", file=sys.stderr)

        if video_process:
            try:
                video_process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                print("yt-dlp (video) process timed out during cleanup. Killing...", file=sys.stderr)
                video_process.kill()
                video_process.communicate()
            except Exception as e:
                 print(f"Error during video_process.communicate(): {e}", file=sys.stderr)
            video_returncode = video_process.returncode
            if video_returncode != 0:
                print(f"yt-dlp (video) exited with error (code {video_returncode}). Check console for yt-dlp's error messages.", file=sys.stderr)

        if audio_process:
            try:
                audio_process.communicate(timeout=60)
            except subprocess.TimeoutExpired:
                print("yt-dlp (audio) process timed out during cleanup. Killing...", file=sys.stderr)
                audio_process.kill()
                audio_process.communicate()
            except Exception as e:
                 print(f"Error during audio_process.communicate(): {e}", file=sys.stderr)
            audio_returncode = audio_process.returncode
            if audio_returncode != 0:
                print(f"yt-dlp (audio) exited with error (code {audio_returncode}). Check console for yt-dlp's error messages.", file=sys.stderr)
        
        all_attempted_and_ok = (video_process is not None and video_returncode == 0 and
                                audio_process is not None and audio_returncode == 0 and
                                ffmpeg_process is not None and ffmpeg_returncode == 0)
        
        any_process_started = video_process or audio_process or ffmpeg_process

        if all_attempted_and_ok:
            if output_filename:
                print(f"Video successfully downloaded and saved to {output_filename}")
            else:
                print("Stream successfully completed to stdout.", file=sys.stderr)
        elif any_process_started:
            print("Download failed or one or more steps had errors. Check console for messages from yt-dlp/ffmpeg.", file=sys.stderr)
            if not sys.exc_info()[0]:
                sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download and merge video and audio streams from a given URL using yt-dlp and ffmpeg.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("url", help="The URL of the video to download.")
    parser.add_argument(
        "-vf", "--video_format",
        default="bestvideo[ext=webm]",
        help="The video format string for yt-dlp." # (details omitted for brevity)
    )
    parser.add_argument(
        "-af", "--audio_format",
        default="bestaudio[ext=webm]",
        help="The audio format string for yt-dlp." # (details omitted for brevity)
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output filename (e.g., my_video.webm)." # (details omitted for brevity)
    )
    # --- ADDED: Argument for cookie file ---
    parser.add_argument(
        "--cookie_file",
        default=None,
        help="Path to a Netscape format cookie file to use with yt-dlp."
    )
    # ---

    args = parser.parse_args()

    if not args.url.startswith("http://") and not args.url.startswith("https://"):
        print("Error: Invalid URL provided. It should start with http:// or https://", file=sys.stderr)
        sys.exit(1)

    # Pass the cookie_file argument to the download_video function
    download_video(args.url, args.video_format, args.audio_format, args.output, args.cookie_file)