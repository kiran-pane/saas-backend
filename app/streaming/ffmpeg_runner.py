"""Thin wrapper around the ffmpeg binary — builds command lines for VOD
(one-shot, full file) and live (continuous, RTSP/RTMP input) transcodes,
and runs them as async subprocesses. No ffmpeg Python binding is used
(ffmpeg-python/similar libraries are thin wrappers around the same CLI
anyway) — shelling out directly keeps this one dependency-free layer
that's easy to reason about and debug (you can copy the exact printed
command and run it by hand)."""
import asyncio
import os
import shlex

from app.config import settings
from app.streaming.hls import HLS_SEGMENT_DURATION_SECONDS, HLS_VOD_SEGMENT_FILENAME_PATTERN, Rendition


class FFmpegError(Exception):
    def __init__(self, message: str, stderr: str = ""):
        super().__init__(message)
        self.stderr = stderr


def _rendition_filter(rendition: Rendition) -> str:
    return f"scale=w={rendition.width}:h={rendition.height}:force_original_aspect_ratio=decrease"


def build_vod_transcode_command(input_path: str, output_dir: str, renditions: list[Rendition]) -> list[str]:
    """One ffmpeg invocation producing all renditions + a master playlist
    in a single pass (using -filter_complex split + separate output
    streams) — more efficient than one ffmpeg process per rendition,
    since the input is only decoded once."""
    os.makedirs(output_dir, exist_ok=True)

    filter_parts = []
    map_args = []
    var_stream_map = []

    split_outputs = "".join(f"[v{i}]" for i in range(len(renditions)))
    filter_parts.append(f"[0:v]split={len(renditions)}{split_outputs}")

    for i, rendition in enumerate(renditions):
        filter_parts.append(f"[v{i}]{_rendition_filter(rendition)}[v{i}out]")
        map_args += ["-map", f"[v{i}out]", "-map", "0:a?"]
        var_stream_map.append(f"v:{i},a:{i},name:{rendition.name}")

    cmd = [
        settings.FFMPEG_BINARY_PATH, "-y", "-i", input_path,
        "-filter_complex", ";".join(filter_parts),
        *map_args,
    ]
    for i, rendition in enumerate(renditions):
        cmd += [
            f"-c:v:{i}", "libx264", f"-b:v:{i}", f"{rendition.video_bitrate_kbps}k",
            f"-c:a:{i}", "aac", f"-b:a:{i}", f"{rendition.audio_bitrate_kbps}k",
        ]
    cmd += [
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_DURATION_SECONDS),
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_filename", os.path.join(output_dir, "%v", HLS_VOD_SEGMENT_FILENAME_PATTERN),
        "-master_pl_name", "master.m3u8",
        "-var_stream_map", " ".join(var_stream_map),
        os.path.join(output_dir, "%v", "playlist.m3u8"),
    ]
    return cmd


def build_live_transcode_command(source_url: str, output_dir: str, renditions: list[Rendition],
                                  record_to_path: str | None = None) -> list[str]:
    """Continuous transcode from an RTSP/RTMP source into a rolling
    live HLS playlist. `-hls_flags delete_segments` keeps disk usage
    bounded (only the last HLS_LIVE_PLAYLIST_SIZE segments are kept on
    disk) — critical for a long-running camera feed that would otherwise
    fill the disk within days. Optionally also writes a continuous MP4
    recording (record_to_path) for DVR-style archival, separate from the
    live-viewing HLS output."""
    from app.streaming.hls import HLS_LIVE_PLAYLIST_SIZE

    os.makedirs(output_dir, exist_ok=True)
    rendition = renditions[0]  # live defaults to a single rendition — see hls.py

    cmd = [
        settings.FFMPEG_BINARY_PATH, "-y",
        "-rtsp_transport", "tcp",   # more firewall/NAT-friendly than UDP for RTSP sources
        "-i", source_url,
        "-vf", _rendition_filter(rendition),
        "-c:v", "libx264", "-b:v", f"{rendition.video_bitrate_kbps}k",
        "-c:a", "aac", "-b:a", f"{rendition.audio_bitrate_kbps}k",
        "-f", "hls",
        "-hls_time", str(HLS_SEGMENT_DURATION_SECONDS),
        "-hls_list_size", str(HLS_LIVE_PLAYLIST_SIZE),
        "-hls_flags", "delete_segments+independent_segments",
        "-hls_segment_filename", os.path.join(output_dir, HLS_VOD_SEGMENT_FILENAME_PATTERN),
        os.path.join(output_dir, "playlist.m3u8"),
    ]
    if record_to_path:
        os.makedirs(os.path.dirname(record_to_path), exist_ok=True)
        # A second output (recording) tee'd from the same decoded input —
        # cheap to add since decoding only happens once.
        cmd = cmd[:-1] + ["-c", "copy", "-f", "segment", "-segment_time", "3600",
                           "-strftime", "1", record_to_path] + [cmd[-1]]
    return cmd


async def run_ffmpeg_once(cmd: list[str], timeout_seconds: int = 3600) -> None:
    """For VOD (one-shot) transcodes — waits for completion."""
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise FFmpegError(f"ffmpeg timed out after {timeout_seconds}s: {shlex.join(cmd)}")

    if process.returncode != 0:
        raise FFmpegError(
            f"ffmpeg exited with code {process.returncode}",
            stderr=stderr.decode(errors="replace")[-4000:],  # tail only — ffmpeg stderr can be very long
        )


async def start_ffmpeg_process(cmd: list[str]) -> asyncio.subprocess.Process:
    """For live (continuous) transcodes — returns the running process
    handle for the caller (live_manager.py) to supervise, without
    waiting for it to exit."""
    return await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
