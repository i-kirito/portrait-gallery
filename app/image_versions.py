"""Archive and resolve image versions replaced by precision edits."""

import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

from PIL import Image


IMAGE_VERSION_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
IMAGE_VERSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")
IMAGE_VERSION_FILE_RE = re.compile(
    r"^(?P<id>[a-f0-9]{32})(?P<extension>\.(?:jpg|jpeg|png|webp|gif))$",
    re.IGNORECASE,
)


def image_versions_dir(data_dir: str) -> Path:
    data_root = Path(data_dir).expanduser().resolve()
    archive_dir = (data_root / "image_versions").resolve()
    try:
        archive_dir.relative_to(data_root)
    except ValueError as exc:
        raise ValueError("unsafe_image_versions_dir") from exc
    return archive_dir


def normalize_image_versions(value) -> list[dict]:
    """Return safe, deduplicated version records from stored JSON data."""
    if not isinstance(value, list):
        return []

    normalized = []
    seen_ids = set()
    for raw in value:
        if not isinstance(raw, dict):
            continue
        version_id = str(raw.get("id") or "").strip().lower()
        archive_filename = str(raw.get("archive_filename") or "").strip()
        file_match = IMAGE_VERSION_FILE_RE.fullmatch(archive_filename)
        if (
            not IMAGE_VERSION_ID_RE.fullmatch(version_id)
            or not file_match
            or file_match.group("id").lower() != version_id
            or version_id in seen_ids
        ):
            continue

        record = {
            "id": version_id,
            "archive_filename": archive_filename,
        }
        for field, limit in (
            ("original_image_filename", 255),
            ("archived_at", 64),
            ("target", 40),
            ("target_label", 40),
            ("instruction", 800),
            ("date", 20),
            ("time", 20),
        ):
            text = str(raw.get(field) or "").strip()
            if text:
                record[field] = text[:limit]
        for field in ("width", "height", "file_size_bytes"):
            try:
                number = int(raw.get(field) or 0)
            except (TypeError, ValueError):
                number = 0
            if number > 0:
                record[field] = number

        normalized.append(record)
        seen_ids.add(version_id)
    return normalized


def archive_image_version(
    data_dir: str,
    source_path: str,
    *,
    original_image_filename: str,
    archived_at: str = "",
    target: str = "",
    target_label: str = "",
    instruction: str = "",
    date: str = "",
    time: str = "",
) -> dict:
    """Copy a gallery image into the private version archive atomically."""
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    extension = source.suffix.lower()
    if extension not in IMAGE_VERSION_EXTENSIONS:
        raise ValueError("unsupported_image_extension")

    archive_dir = image_versions_dir(data_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    version_id = uuid.uuid4().hex
    archive_filename = f"{version_id}{extension}"
    destination = archive_dir / archive_filename
    temp_fd, temp_path = tempfile.mkstemp(
        dir=archive_dir,
        prefix=f".{version_id}.",
        suffix=extension,
    )
    os.close(temp_fd)
    try:
        shutil.copy2(source, temp_path)
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise

    record = {
        "id": version_id,
        "archive_filename": archive_filename,
        "original_image_filename": os.path.basename(
            str(original_image_filename or source.name)
        ),
        "archived_at": str(archived_at or datetime.now().isoformat(timespec="seconds")),
        "target": str(target or "").strip(),
        "target_label": str(target_label or "").strip(),
        "instruction": str(instruction or "").strip(),
        "date": str(date or "").strip(),
        "time": str(time or "").strip(),
        "file_size_bytes": destination.stat().st_size,
    }
    try:
        with Image.open(destination) as image:
            record["width"], record["height"] = image.size
    except Exception:
        pass
    return normalize_image_versions([record])[0]


def image_version_path(data_dir: str, record: dict) -> Path | None:
    """Resolve one validated archive record inside the private version dir."""
    normalized = normalize_image_versions([record])
    if not normalized:
        return None
    try:
        archive_dir = image_versions_dir(data_dir)
    except ValueError:
        return None
    candidate = (archive_dir / normalized[0]["archive_filename"]).resolve()
    try:
        candidate.relative_to(archive_dir)
    except ValueError:
        return None
    return candidate


def find_image_version(data_dir: str, records, version_id: str) -> tuple[dict, Path] | None:
    normalized_id = str(version_id or "").strip().lower()
    if not IMAGE_VERSION_ID_RE.fullmatch(normalized_id):
        return None
    for record in normalize_image_versions(records):
        if record["id"] != normalized_id:
            continue
        path = image_version_path(data_dir, record)
        if path and path.is_file():
            return record, path
        return None
    return None


def delete_image_versions(data_dir: str, records) -> tuple[int, list[str]]:
    """Delete validated archive files, returning a count and any I/O errors."""
    deleted = 0
    errors = []
    for record in normalize_image_versions(records):
        path = image_version_path(data_dir, record)
        if not path or not path.exists():
            continue
        try:
            if path.is_file():
                path.unlink()
                deleted += 1
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return deleted, errors
