import logging
from pathlib import Path
from typing import Literal

from app.platform.config.snapshot import get_config

from .media_paths import image_files_dir, video_files_dir

MediaType = Literal["image", "video"]

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_VIDEO_EXTS = {".mp4"}


def _limit_bytes(media_type: MediaType) -> int:
    raw = get_config(f"cache.local.{media_type}_max_mb", 0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    if value <= 0:
        return 0
    return int(value * 1024 * 1024)


def _media_dir(media_type: MediaType) -> Path:
    return image_files_dir() if media_type == "image" else video_files_dir()


def _allowed_exts(media_type: MediaType) -> set[str]:
    return _IMAGE_EXTS if media_type == "image" else _VIDEO_EXTS


def _trim_local_cache(media_type: MediaType, protected_name: str) -> None:
    max_bytes = _limit_bytes(media_type)
    if max_bytes <= 0:
        return

    directory = _media_dir(media_type)
    files: list[tuple[Path, int, int]] = []
    for path in directory.glob("*"):
        if not path.is_file() or path.suffix.lower() not in _allowed_exts(media_type):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((path, int(stat.st_size), int(stat.st_mtime_ns)))

    total = sum(size for _path, size, _mtime in files)
    if total <= max_bytes:
        return

    removed = 0
    for path, size, _mtime in sorted(files, key=lambda item: (item[2], item[0].name)):
        if total <= max_bytes:
            break
        if path.name == protected_name:
            continue
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "local media cache delete failed: media_type=%s name=%s error=%s",
                media_type,
                path.name,
                exc,
            )
            continue
        total = max(0, total - size)
        removed += 1

    if removed:
        logger.info(
            "local media cache trimmed: media_type=%s removed=%s usage_bytes=%s limit_bytes=%s",
            media_type,
            removed,
            total,
            max_bytes,
        )


def save_local_image(raw: bytes, mime: str, file_id: str) -> str:
    directory = image_files_dir()
    ext = ".png" if "png" in mime else ".jpg"
    path = directory / f"{file_id}{ext}"
    if not path.exists():
        path.write_bytes(raw)
    else:
        path.touch()
    _trim_local_cache("image", path.name)
    return file_id


def save_local_video(raw: bytes, file_id: str) -> Path:
    directory = video_files_dir()
    path = directory / f"{file_id}.mp4"
    if not path.exists():
        path.write_bytes(raw)
    else:
        path.touch()
    _trim_local_cache("video", path.name)
    return path


__all__ = ["save_local_image", "save_local_video"]
