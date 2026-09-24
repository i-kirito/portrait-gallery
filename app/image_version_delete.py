"""Delete one owned history version without touching the current image.

The caller must hold the image mutation lock shared by reroll/activation.
Archive bytes are staged until the atomic schedule-store update succeeds.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from pathlib import Path

from image_versions import image_version_path, normalize_image_versions
from store import ScheduleStore

logger = logging.getLogger(__name__)


class VersionDeleteError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def delete_history_version(data_dir: str, image_filename: str, version_id: str,
                           current_image_path: str) -> dict:
    if not re.fullmatch(r"[a-f0-9]{32}", version_id):
        raise VersionDeleteError("invalid_version_id", "历史版本编号无效。")
    if not current_image_path or not Path(current_image_path).is_file():
        raise VersionDeleteError("current_image_not_found", "当前图片不存在，已停止删除。", 404)
    current = Path(current_image_path).resolve()
    staged = None
    original = None
    result = {}

    def remove_record(all_data):
        nonlocal staged, original
        key = image_filename if isinstance(all_data.get(image_filename), dict) else next(
            (k for k, e in all_data.items() if isinstance(e, dict)
             and e.get("image_filename") == image_filename), None)
        if key is None:
            raise VersionDeleteError("not_found", "图片记录不存在。", 404)
        entry = dict(all_data[key])
        records = normalize_image_versions(entry.get("image_versions"))
        selected = next((r for r in records if r["id"] == version_id), None)
        if not selected:
            raise VersionDeleteError("version_not_found", "该历史版本已变化，请重新打开。", 404)
        if selected.get("original_image_filename", image_filename) != image_filename:
            raise VersionDeleteError("version_owner_mismatch", "此版本不属于当前图片。", 409)
        original = image_version_path(data_dir, selected)
        if not original or not original.is_file():
            raise VersionDeleteError("version_not_found", "历史版本文件不存在，请重新打开。", 404)
        if original.name != selected["archive_filename"]:
            raise VersionDeleteError("version_path_invalid", "历史版本路径异常，已停止删除。", 409)
        if original == current or original.samefile(current):
            raise VersionDeleteError("current_version_protected", "不能删除当前图片。", 409)
        for other_key, other in all_data.items():
            if other_key == key or not isinstance(other, dict):
                continue
            if any(r["archive_filename"] == selected["archive_filename"]
                   for r in normalize_image_versions(other.get("image_versions"))):
                raise VersionDeleteError("version_in_use", "该版本仍被其他图片使用，不能删除。", 409)
        remaining = [r for r in records if r["id"] != version_id]
        entry["image_versions"] = remaining
        entry["version_count"] = len(remaining)
        try:
            deleted_count = max(0, int(entry.get("deleted_version_count") or 0))
        except (TypeError, ValueError):
            deleted_count = 0
        entry["deleted_version_count"] = deleted_count + 1
        # Rename on the same filesystem so metadata failures can restore bytes.
        candidate = original.with_name(f".deleting-{uuid.uuid4().hex}-{original.name}")
        os.replace(original, candidate)
        staged = candidate
        all_data[key] = entry
        result.update(deleted_version_id=version_id, version_count=len(remaining))
        return all_data

    try:
        ScheduleStore(data_dir).update(remove_record)
    except BaseException:
        if staged is not None and staged.exists():
            try:
                os.replace(staged, original)
            except OSError:
                logger.exception("History deletion rollback failed; staged backup retained: %s", staged)
        raise

    cleanup_pending = False
    if staged is not None:
        try:
            staged.unlink()
        except OSError:
            # The record is gone; retain inaccessible staged bytes for cleanup.
            cleanup_pending = True
            logger.exception("History record removed; staged file cleanup pending: %s", staged)
    return {**result, "success": True, "cleanup_pending": cleanup_pending}
