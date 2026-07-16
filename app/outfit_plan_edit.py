"""Helpers for updating editable fields in today's outfit plan."""
import re


OUTFIT_PLAN_EDIT_FIELDS = {
    "发型": "hair_en",
    "穿搭": "outfit_en",
}
MAX_OUTFIT_PLAN_EDIT_LENGTH = 1200
_OUTFIT_FIELD_ORDER = ("风格", "发型", "穿搭", "动作", "场景")
_OUTFIT_LABEL_RE = re.compile(r"^\s*(风格|发型|穿搭|动作|场景)\s*[：:]")


def normalize_outfit_plan_field(value: str) -> str:
    field = str(value or "").strip()
    return field if field in OUTFIT_PLAN_EDIT_FIELDS else ""


def normalize_outfit_plan_value(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def replace_outfit_plan_field(outfit, field: str, value: str):
    """Replace one labeled field while preserving the rest of the outfit text."""
    field = normalize_outfit_plan_field(field)
    value = normalize_outfit_plan_value(value)
    if not field:
        raise ValueError("invalid_outfit_field")
    if not value:
        raise ValueError("empty_outfit_value")

    if isinstance(outfit, dict):
        updated = dict(outfit)
        updated[field] = value
        return "\n".join(
            f"{label}：{normalize_outfit_plan_value(updated.get(label))}"
            for label in _OUTFIT_FIELD_ORDER
            if normalize_outfit_plan_value(updated.get(label))
        )

    lines = str(outfit or "").splitlines()
    replacement = f"{field}：{value}"
    for index, line in enumerate(lines):
        match = _OUTFIT_LABEL_RE.match(line)
        if match and match.group(1) == field:
            lines[index] = replacement
            return "\n".join(lines)

    field_rank = _OUTFIT_FIELD_ORDER.index(field)
    insert_at = len(lines)
    for index, line in enumerate(lines):
        match = _OUTFIT_LABEL_RE.match(line)
        if match and _OUTFIT_FIELD_ORDER.index(match.group(1)) > field_rank:
            insert_at = index
            break
    lines.insert(insert_at, replacement)
    return "\n".join(lines).strip()


def update_schedule_details_outfit(details, field: str, value: str) -> list:
    """Apply an edited global outfit field to every structured schedule slot."""
    field = normalize_outfit_plan_field(field)
    value = normalize_outfit_plan_value(value)
    if not field:
        raise ValueError("invalid_outfit_field")
    if not value:
        raise ValueError("empty_outfit_value")

    detail_key = OUTFIT_PLAN_EDIT_FIELDS[field]
    updated = []
    for item in details if isinstance(details, list) else []:
        if not isinstance(item, dict):
            updated.append(item)
            continue
        copied = dict(item)
        copied[detail_key] = value
        updated.append(copied)
    return updated
