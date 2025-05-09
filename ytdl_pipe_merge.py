import subprocess
import sys
import argparse

def download_video(url, video_format, audio_format, output_filename=None):
    """
    Downloads a video from the given URL with specified video and audio formats.
    stderr from yt-dlp and ffmpeg will be printed directly to the console.

    Args:
        url (str): The URL of the video to download.
        video_format (str): The desired video format string for yt-dlp.
        audio_format (str): The desired audio format string for yt-dlp.
        output_filename (str, optional): The name of the output file.
                                         If None, output is streamed to stdout.
    """
    video_process = None
    audio_process = None
    ffmpeg_process = None

    video_returncode = None
    audio_returncode = None
    ffmpeg_returncode = None

    try:
        # 1) Launch yt-dlp downloaders for video & audio
        #    -o - pipes output to stdout for ffmpeg. stderr goes to console.
        video_process = subprocess.Popen(
            ["yt-dlp", "--cookies", "cookies.Gemini.txt", "--no-playlist", "-f", video_format, "-o", "-", "--", url],
            stdout=subprocess.PIPE,
            # stderr is not piped, will go to console
        )
        audio_process = subprocess.Popen(
            ["yt-dlp", "--cookies", "cookies.Gemini.txt", "--no-playlist", "-f", audio_format, "-o", "-", "--", url],
            stdout=subprocess.PIPE,
            # stderr is not piped, will go to console
        )
        
        if not video_process.stdout: # Should not happen if Popen succeeds with stdout=PIPE
            raise IOError("yt-dlp video process stdout pipe not created.")
        if not audio_process.stdout: # Should not happen
            raise IOError("yt-dlp audio process stdout pipe not created.")

        # Prepare ffmpeg command
        ffmpeg_command = [
            "ffmpeg",
            "-hide_banner", "-y",
            "-i", "pipe:3",        # Input from video_process stdout
            "-i", "pipe:4",        # Input from audio_process stdout
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
            ffmpeg_command.append("pipe:1") # Output to ffmpeg's stdout
            ffmpeg_stdout_target = subprocess.PIPE # Pipe ffmpeg's stdout to this script

        # 2) Launch ffmpeg, reading from yt-dlp pipes
        ffmpeg_process = subprocess.Popen(
            ffmpeg_command,
            pass_fds=(video_process.stdout.fileno(), audio_process.stdout.fileno()),
            stdin=subprocess.PIPE, # Not strictly needed but good practice
            stdout=ffmpeg_stdout_target,
            # stderr is not piped, will go to console
        )

        # Crucial: Close the parent's file descriptors for yt-dlp's stdout pipes.
        # ffmpeg has its own copies via pass_fds.
        video_process.stdout.close()
        audio_process.stdout.close()

        # 3) Stream ffmpeg’s stdout directly to our stdout if no output file is specified
        if not output_filename and ffmpeg_process and ffmpeg_process.stdout:
            # print("Streaming ffmpeg output to stdout...", file=sys.stderr) # Optional debug
            try:
                for chunk in iter(lambda: ffmpeg_process.stdout.read(4096), b""):
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
            except Exception as e:
                print(f"Error streaming ffmpeg output: {e}", file=sys.stderr)
                if ffmpeg_process.poll() is None: # Check if process is still running
                    ffmpeg_process.terminate() # Ask it to terminate
            finally:
                 # Ensure ffmpeg's stdout pipe is closed after streaming or error
                 if ffmpeg_process.stdout:
                    ffmpeg_process.stdout.close()
    except Exception as e:
        print(f"An unexpected error occurred during setup or initial execution: {e}", file=sys.stderr)
        # Attempt to clean up any processes that might have started
        if video_process and video_process.poll() is None: video_process.kill()
        if audio_process and audio_process.poll() is None: audio_process.kill()
        if ffmpeg_process and ffmpeg_process.poll() is None: ffmpeg_process.kill()
        sys.exit(1)
    finally:
        # 4) Clean up subprocesses and check results
        # stderr from these processes will have already gone to the console.
        # communicate() is called to wait for processes and get return codes.
        if ffmpeg_process:
            try:
                # If ffmpeg's stdout was piped (ffmpeg_stdout_target=PIPE) but not fully read
                # (e.g., due to an error before or during the streaming loop),
                # communicate() might try to read remaining stdout. We discard it here.
                # If stdout was a file, or piped and fully streamed (and closed),
                # communicate() just waits.
                _ = ffmpeg_process.communicate(timeout=120) # Timeout for waiting
            except subprocess.TimeoutExpired:
                print("ffmpeg process timed out during cleanup. Killing...", file=sys.stderr)
                ffmpeg_process.kill()
                ffmpeg_process.communicate() # Final attempt to reap
            except Exception as e:
                 print(f"Error during ffmpeg_process.communicate(): {e}", file=sys.stderr)
            ffmpeg_returncode = ffmpeg_process.returncode
            if ffmpeg_returncode != 0:
                print(f"ffmpeg exited with error (code {ffmpeg_returncode}). Check console for ffmpeg's error messages.", file=sys.stderr)

        if video_process:
            try:
                # stdout was piped to ffmpeg and closed in parent. communicate() mainly waits.
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
                # stdout was piped to ffmpeg and closed in parent. communicate() mainly waits.
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
        
        # Determine overall success
        # All processes must have been attempted and completed successfully.
        all_attempted_and_ok = (video_process is not None and video_returncode == 0 and
                                audio_process is not None and audio_returncode == 0 and
                                ffmpeg_process is not None and ffmpeg_returncode == 0)
        
        # Check if any process was started, even if it failed.
        any_process_started = video_process or audio_process or ffmpeg_process

        if all_attempted_and_ok:
            if output_filename:
                print(f"Video successfully downloaded and saved to {output_filename}")
            else:
                # For stdout streaming, success message goes to stderr to not mix with video data
                print("Stream successfully completed to stdout.", file=sys.stderr)
        elif any_process_started: # If some process was started but the overall result is not success
            print("Download failed or one or more steps had errors. Check console for messages from yt-dlp/ffmpeg.", file=sys.stderr)
            # Ensure script exits with error status if not already exiting due to an exception in the try block
            if not sys.exc_info()[0]: # If no active exception being handled that would cause exit
                sys.exit(1)
        # If no process was started, it means an error occurred very early (e.g. Popen for first process failed),
        # and the main try-except block would have called sys.exit(1).


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download and merge video and audio streams from a given URL using yt-dlp and ffmpeg.",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("url", help="The URL of the video to download.")
    parser.add_argument(
        "-vf", "--video_format",
        default="bestvideo[ext=webm]",
        help="The video format string for yt-dlp.\n"
             "Examples:\n"
             "  'bestvideo[ext=webm]' (default)\n"
             "  'bv'\n"
             "  'bestvideo[height<=1080]'\n"
             "  'mp4'"
    )
    parser.add_argument(
        "-af", "--audio_format",
        default="bestaudio[ext=webm]",
        help="The audio format string for yt-dlp.\n"
             "Examples:\n"
             "  'bestaudio[ext=webm]' (default)\n"
             "  'ba'\n"
             "  'bestaudio[abr<=128]'\n"
             "  'm4a'"
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="Output filename (e.g., my_video.webm).\n"
             "If not specified, the merged stream is sent to standard output."
    )

    args = parser.parse_args()

    if not args.url.startswith("http://") and not args.url.startswith("https://"):
        print("Error: Invalid URL provided. It should start with http:// or https://", file=sys.stderr)
        sys.exit(1)

    download_video(args.url, args.video_format, args.audio_format, args.output)
