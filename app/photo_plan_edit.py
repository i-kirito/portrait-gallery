"""Helpers for editing today's photo-plan schedule text."""
import re


def normalize_schedule_clock(value: str) -> str:
    match = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
    if not match:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def replace_schedule_activity(schedule_text: str, time_text: str, activity: str) -> tuple[str, bool]:
    """Replace one HH:mm activity line in a schedule-like text block."""
    target_time = normalize_schedule_clock(time_text)
    if not target_time:
        return schedule_text or "", False

    found = False
    lines = str(schedule_text or "").splitlines()
    updated_lines = []
    for line in lines:
        match = re.match(r"^(\s*)(\d{1,2}):(\d{2})\s*(.*)$", line)
        if not match:
            updated_lines.append(line)
            continue

        line_time = normalize_schedule_clock(f"{match.group(2)}:{match.group(3)}")
        if line_time != target_time:
            updated_lines.append(line)
            continue

        updated_lines.append(f"{match.group(1)}{target_time} {activity}".rstrip())
        found = True

    return "\n".join(updated_lines), found


def update_schedule_details_activity(details, time_text: str, activity: str) -> tuple[list, bool]:
    """Keep schedule_details from overriding a manually edited activity."""
    if not isinstance(details, list):
        return [], False
    target_time = normalize_schedule_clock(time_text)
    if not target_time:
        return list(details), False

    updated = []
    changed = False
    for item in details:
        if not isinstance(item, dict):
            updated.append(item)
            continue
        item_time = normalize_schedule_clock(str(item.get("time") or ""))
        if item_time != target_time:
            updated.append(item)
            continue

        new_item = dict(item)
        new_item["time"] = target_time
        new_item["activity_zh"] = activity
        new_item["activity_en"] = activity
        new_item["action_en"] = activity
        for stale_field in ("scene_en", "props_en", "lighting_en"):
            if stale_field in new_item:
                new_item[stale_field] = ""
        new_item["manual_activity_edit"] = True
        updated.append(new_item)
        changed = True

    return updated, changed
