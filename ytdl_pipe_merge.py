# ytdl_pipe_merge.py
import subprocess
import sys
import argparse
import os
from typing import List, Optional, IO # Added IO for stream type

# Define a type alias for process return codes for clarity
ProcessReturnCode = Optional[int]

def download_video(
	url: str,
	video_format: str,
	audio_format: str,
	output_container: str = "mp4",
	output_filename: Optional[str] = None,
	cookie_file: Optional[str] = None
) -> None:
	video_process: Optional[subprocess.Popen[bytes]] = None # Specify Popen generic type
	audio_process: Optional[subprocess.Popen[bytes]] = None
	ffmpeg_process: Optional[subprocess.Popen[bytes]] = None

	video_returncode: ProcessReturnCode = None
	audio_returncode: ProcessReturnCode = None
	ffmpeg_returncode: ProcessReturnCode = None

	def _cleanup_process(
		process: Optional[subprocess.Popen[bytes]],
		process_name: str,
		timeout_duration: int = 60
	) -> ProcessReturnCode:
		if not process:
			return None
		exit_code: ProcessReturnCode = None
		try:
			process.communicate(timeout=float(timeout_duration))
			exit_code = process.returncode
		except subprocess.TimeoutExpired:
			print(f"ytdl_pipe_merge.py: {process_name} timed out. Killing...", file=sys.stderr)
			process.kill()
			try:
				process.communicate(timeout=10.0)
			except Exception as e_post_kill:
				print(f"ytdl_pipe_merge.py: Error post-kill communicate for {process_name}: {e_post_kill}", file=sys.stderr)
			exit_code = process.returncode
		except Exception as e_comm:
			print(f"ytdl_pipe_merge.py: Error communicate() for {process_name}: {e_comm}", file=sys.stderr)
			if process.poll() is not None:
				exit_code = process.returncode
			else:
				print(f"ytdl_pipe_merge.py: {process_name} state uncertain. Attempting kill.", file=sys.stderr)
				process.kill()
				try:
					process.wait(timeout=10.0)
				except subprocess.TimeoutExpired:
					print(f"ytdl_pipe_merge.py: {process_name} did not terminate post-kill.", file=sys.stderr)
				except Exception as e_wait_kill:
					print(f"ytdl_pipe_merge.py: Error wait after kill for {process_name}: {e_wait_kill}", file=sys.stderr)
				exit_code = process.returncode if process.poll() is not None else -1
		if exit_code is None:
			print(f"ytdl_pipe_merge.py: Warning: Could not determine exit code for {process_name}. Polling.", file=sys.stderr)
			exit_code = process.poll()
			if exit_code is None:
				print(f"ytdl_pipe_merge.py: Warning: {process_name} status unknown. Assuming error (-1).", file=sys.stderr)
				exit_code = -1
		if exit_code != 0:
			print(f"ytdl_pipe_merge.py: {process_name} exited with error (code {exit_code}).", file=sys.stderr)
		return exit_code

	try:
		base_yt_dlp_cmd: List[str] = ["yt-dlp"]
		if cookie_file:
			if os.path.isfile(cookie_file):
				base_yt_dlp_cmd.extend(["--cookies", cookie_file])
			else:
				print(f"Warning (ytdl_pipe_merge.py): Cookie file {cookie_file} not found.", file=sys.stderr)
		
		video_dl_cmd: List[str] = base_yt_dlp_cmd + ["--no-playlist", "-f", video_format, "-o", "-", "--", url]
		audio_dl_cmd: List[str] = base_yt_dlp_cmd + ["--no-playlist", "-f", audio_format, "-o", "-", "--", url]
		
		print(f"ytdl_pipe_merge.py: yt-dlp video: {' '.join(video_dl_cmd)}", file=sys.stderr)
		print(f"ytdl_pipe_merge.py: yt-dlp audio: {' '.join(audio_dl_cmd)}", file=sys.stderr)

		video_process = subprocess.Popen(video_dl_cmd, stdout=subprocess.PIPE, stderr=sys.stderr)
		audio_process = subprocess.Popen(audio_dl_cmd, stdout=subprocess.PIPE, stderr=sys.stderr)
		
		if video_process.stdout is None: # More explicit check
			raise IOError("yt-dlp video process stdout pipe not created.")
		if audio_process.stdout is None: # More explicit check
			raise IOError("yt-dlp audio process stdout pipe not created.")

		ffmpeg_command: List[str] = [
			"ffmpeg", "-hide_banner", "-y",
			"-i", "pipe:3", "-i", "pipe:4",
			"-map", "0:v", "-map", "1:a",
			"-c:v", "copy", "-c:a", "copy",
		]

		if output_container == "mp4":
			ffmpeg_command.extend(["-movflags", "frag_keyframe+empty_moov+faststart", "-f", "mp4"])
		elif output_container == "mkv":
			ffmpeg_command.extend(["-f", "matroska"])
		else:
			print(f"ytdl_pipe_merge.py: Error: Unsupported container '{output_container}'. Defaulting to mkv.", file=sys.stderr)
			ffmpeg_command.extend(["-f", "matroska"])

		ffmpeg_stdout_target: Optional[int] = None
		if output_filename:
			ffmpeg_command.append(output_filename)
		else:
			ffmpeg_command.append("pipe:1")
			ffmpeg_stdout_target = subprocess.PIPE 

		print(f"ytdl_pipe_merge.py: ffmpeg: {' '.join(ffmpeg_command)}", file=sys.stderr)

		video_fd: int = video_process.stdout.fileno()
		audio_fd: int = audio_process.stdout.fileno()

		ffmpeg_process = subprocess.Popen(
			ffmpeg_command,
			pass_fds=(video_fd, audio_fd),
			stdin=subprocess.PIPE, 
			stdout=ffmpeg_stdout_target,
			stderr=sys.stderr
		)

		video_process.stdout.close() # Close parent's end of the pipe
		audio_process.stdout.close() # Close parent's end of the pipe

		if not output_filename and ffmpeg_process:
			stdout_pipe: Optional[IO[bytes]] = ffmpeg_process.stdout # Capture for type narrowing
			
			if stdout_pipe is not None: 
				try:
					for chunk in iter(lambda: stdout_pipe.read(4096), b""):
						sys.stdout.buffer.write(chunk)
						sys.stdout.buffer.flush()
				except Exception as e:
					print(f"ytdl_pipe_merge.py: Error streaming ffmpeg output: {e}", file=sys.stderr)
					if ffmpeg_process.poll() is None:
						ffmpeg_process.terminate()
				finally:
					stdout_pipe.close() # stdout_pipe is known non-None here
			else:
				# This path should ideally not be hit if not output_filename is true
				print("ytdl_pipe_merge.py: Critical: ffmpeg stdout is None when streaming was expected.", file=sys.stderr)
				if ffmpeg_process.poll() is None: # Check if ffmpeg is running to terminate
					ffmpeg_process.terminate()


	except Exception as e:
		print(f"ytdl_pipe_merge.py: An unexpected error occurred: {e}", file=sys.stderr)
		if video_process and video_process.poll() is None: video_process.kill()
		if audio_process and audio_process.poll() is None: audio_process.kill()
		if ffmpeg_process and ffmpeg_process.poll() is None: ffmpeg_process.kill()
		sys.exit(1) 
	finally:
		if ffmpeg_process:
			ffmpeg_returncode = _cleanup_process(ffmpeg_process, "ffmpeg", timeout_duration=120)
		if audio_process: 
			audio_returncode = _cleanup_process(audio_process, "yt-dlp audio", timeout_duration=60)
		if video_process: 
			video_returncode = _cleanup_process(video_process, "yt-dlp video", timeout_duration=60)
		
		all_ok = (
			(video_process is not None and video_returncode == 0) and
			(audio_process is not None and audio_returncode == 0) and
			(ffmpeg_process is not None and ffmpeg_returncode == 0)
		)
		any_launched = video_process or audio_process or ffmpeg_process

		if all_ok:
			status_message = f"processed to {output_filename}" if output_filename else "streamed to stdout"
			print(f"ytdl_pipe_merge.py: Success: Video {status_message}.", file=sys.stderr)
		elif any_launched:
			print("ytdl_pipe_merge.py: Failure: Download/merge had errors. Review logs.", file=sys.stderr)
			if not sys.exc_info()[0]: sys.exit(1)

if __name__ == "__main__":
	parser = argparse.ArgumentParser(
		description="Download & merge video/audio using yt-dlp & ffmpeg to fMP4 or MKV.",
		formatter_class=argparse.RawTextHelpFormatter
	)
	parser.add_argument("url", help="Video URL.")
	parser.add_argument("-vf", "--video_format", default="bestvideo[ext=webm]/bestvideo[ext=mp4]/bestvideo", help="yt-dlp video format.")
	parser.add_argument("-af", "--audio_format", default="bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio", help="yt-dlp audio format.")
	parser.add_argument("-o", "--output", default=None, help="Output filename (e.g., video.mp4). Streams to stdout if not set.")
	parser.add_argument("--cookie_file", default=None, help="Path to Netscape cookie file for yt-dlp.")
	parser.add_argument("-c", "--container", default="mkv", choices=["mp4", "mkv"], help="Output container: 'mp4' or 'mkv'. Default: 'mkv'.")

	args = parser.parse_args()

	if not args.url.startswith(("http://", "https://")): # Simpler check for multiple prefixes
		print("ytdl_pipe_merge.py: Error: URL must start with http:// or https://", file=sys.stderr)
		sys.exit(1)

	download_video(
		args.url, args.video_format, args.audio_format,
		output_container=args.container,
		output_filename=args.output,
		cookie_file=args.cookie_file
	)