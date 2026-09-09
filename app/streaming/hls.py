"""HLS rendition ladder — adaptive bitrate presets. Kept as plain config
(not hardcoded into ffmpeg command strings scattered across the module)
so the ladder can be tuned per deployment without touching the transcode
logic itself."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Rendition:
    name: str          # "1080p", "720p", "480p", "360p" — also the output subfolder name
    width: int
    height: int
    video_bitrate_kbps: int
    audio_bitrate_kbps: int = 128


DEFAULT_VOD_RENDITION_LADDER: list[Rendition] = [
    Rendition("1080p", 1920, 1080, 5000),
    Rendition("720p", 1280, 720, 2800),
    Rendition("480p", 854, 480, 1400),
    Rendition("360p", 640, 360, 800),
]

# Live/CCTV defaults to a single rendition (matching source resolution is
# usually good enough for a security-camera feed, and multi-rendition
# live transcoding multiplies CPU cost per concurrent stream) — a
# deployment with real bandwidth-variance requirements can override this.
DEFAULT_LIVE_RENDITION_LADDER: list[Rendition] = [
    Rendition("720p", 1280, 720, 2000),
]

HLS_SEGMENT_DURATION_SECONDS = 6
HLS_LIVE_PLAYLIST_SIZE = 10       # rolling window of segments kept in a live playlist
HLS_VOD_SEGMENT_FILENAME_PATTERN = "segment_%05d.ts"
