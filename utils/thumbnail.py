"""
Thumbnail extraction utilities.
- For video files: extract first frame using ffmpeg (if available)
- Fallback: use Telegram-provided thumbnail
"""
import os
import subprocess
import tempfile
import logging

logger = logging.getLogger(__name__)

FFMPEG_AVAILABLE = False
try:
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
    FFMPEG_AVAILABLE = result.returncode == 0
except Exception:
    pass


def extract_video_thumbnail(video_path: str, output_path: str = None,
                             quality: int = 85) -> str | None:
    """
    Extract first frame from video as JPEG thumbnail.
    Returns path to thumbnail file or None on failure.
    """
    if not FFMPEG_AVAILABLE:
        logger.warning("ffmpeg not available – skipping thumbnail extraction")
        return None

    if output_path is None:
        fd, output_path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", "00:00:01",
            "-vframes", "1",
            "-q:v", str(max(1, 31 - quality // 4)),
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path
    except Exception as e:
        logger.error(f"ffmpeg error: {e}")

    return None


def get_thumbnail_bytes(thumb_path: str) -> bytes | None:
    """Read thumbnail file and return bytes."""
    if thumb_path and os.path.exists(thumb_path):
        with open(thumb_path, "rb") as f:
            return f.read()
    return None
